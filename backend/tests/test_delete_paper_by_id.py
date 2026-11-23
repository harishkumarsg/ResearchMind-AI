"""
Deleting a paper names exactly one paper: by its immutable id.

The defect: a paper's title is its uploaded filename, and only
(owner_id, content_hash) is unique, so one owner can hold two different
papers with the same title. DELETE /paper/{paper_name} resolved the title
with `.first()` — an arbitrary row — and deleted that row's vectors and
PDF. Reproduced offline: the user meant paper B, and paper A's row,
vectors and Storage object were removed.

Now:
  - DELETE /papers/{paper_id} resolves (paper_id, verified owner) and
    deletes that paper only;
  - the legacy title route still works for a title that names one paper,
    and answers 409 — touching nothing — when it names more than one.

Everything below the endpoint that leaves the process is replaced by an
in-memory model that applies the same scoping the real services do:
Qdrant evaluates the scroll filter it is given, and Storage holds objects
at build_storage_path(owner, paper). No network, no real data.

TestStorageStepIsConfirmed goes one layer deeper for the Storage step: it
runs the REAL delete_pdf and supabase-py client and replaces only httpx's
final send, so the failure modes are the ones the client really produces.
"""
import json
import os
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.delete_paper import AMBIGUOUS_TITLE_DETAIL  # noqa: E402
from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id  # noqa: E402
from app.db.models import Base, Paper  # noqa: E402
from app.db.session import get_db_session  # noqa: E402
from app.services import storage as storage_module  # noqa: E402
from app.services.storage import build_storage_path  # noqa: E402

OWNER_A = "aaaaaaaa-3333-4333-8333-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-3333-4333-8333-bbbbbbbbbbbb"
SHARED_TITLE = "paper.pdf"
INDEXING_DETAIL = "This paper is still being indexed — try again shortly"
VECTORS_PER_PAPER = 3


class FakeQdrant:
    """Points held in memory. scroll() applies the filter it is given, so
    a missing owner or paper condition would select the wrong points here
    exactly as it would in Qdrant."""

    def __init__(self):
        self.points = {}  # point id -> payload
        self.scroll_filters = []
        self.fail_next = None  # "scroll" or "delete": raise once

    def add(self, owner_id, paper_id, count=VECTORS_PER_PAPER):
        for i in range(count):
            self.points[f"{paper_id}-{i}"] = {"owner_id": owner_id, "paper_id": paper_id}

    def ids_for(self, owner_id, paper_id):
        return {
            k for k, v in self.points.items()
            if v["owner_id"] == owner_id and v["paper_id"] == paper_id
        }

    def _maybe_fail(self, operation):
        if self.fail_next == operation:
            self.fail_next = None
            raise RuntimeError("qdrant unreachable")

    def scroll(self, collection_name, scroll_filter, **kwargs):
        self._maybe_fail("scroll")
        assert isinstance(scroll_filter, Filter), scroll_filter
        self.scroll_filters.append(scroll_filter)
        wanted = [(c.key, c.match.value) for c in scroll_filter.must]
        hits = [
            SimpleNamespace(id=point_id)
            for point_id, payload in self.points.items()
            if all(payload.get(key) == value for key, value in wanted)
        ]
        return hits, None

    def delete(self, collection_name, points_selector, **kwargs):
        self._maybe_fail("delete")
        assert isinstance(points_selector, PointIdsList), points_selector
        for point_id in points_selector.points:
            self.points.pop(point_id, None)


class FakeStorage:
    """Objects keyed by their conventional path. Stands in for
    app.services.storage.delete_pdf, which rebuilds the path from the
    owner and paper id it is given."""

    def __init__(self):
        self.objects = set()
        self.calls = []

    def add(self, owner_id, paper_id):
        self.objects.add(build_storage_path(owner_id, paper_id))

    def delete_pdf(self, owner_id, paper_id, user_jwt):
        self.calls.append({"owner_id": owner_id, "paper_id": paper_id, "user_jwt": user_jwt})
        self.objects.discard(build_storage_path(owner_id, paper_id))


class DeletePaperTestCase(unittest.TestCase):

    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        self.addCleanup(engine.dispose)

        self.qdrant = FakeQdrant()
        self.storage = FakeStorage()
        for target, replacement in (
            ("app.api.delete_paper.client", self.qdrant),
            ("app.api.delete_paper.delete_pdf", self.storage.delete_pdf),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.main import app

        self.current_owner = OWNER_A

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_current_identity] = lambda: AuthenticatedIdentity(
            owner_id=self.current_owner, token=f"jwt-of-{self.current_owner}"
        )
        app.dependency_overrides[get_current_owner_id] = lambda: self.current_owner
        self.addCleanup(app.dependency_overrides.clear)
        # raise_server_exceptions=False: anything the route does not turn
        # into its sanitized 502 must surface as the HTTP 500 a real
        # deployment would return, not as an exception in the test.
        self.client = TestClient(app, raise_server_exceptions=False)

        # Owner A holds two DIFFERENT papers with the same title; owner B
        # holds a third with that title too.
        self.a1 = self.seed(OWNER_A, SHARED_TITLE)
        self.a2 = self.seed(OWNER_A, SHARED_TITLE)
        self.b1 = self.seed(OWNER_B, SHARED_TITLE)

    def seed(self, owner_id, title, status="indexed"):
        paper_id = str(uuid.uuid4())
        with self.SessionLocal() as db:
            db.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash=uuid.uuid4().hex * 2,
                    storage_path=build_storage_path(owner_id, paper_id),
                    file_size_bytes=1024,
                    status=status,
                )
            )
            db.commit()
        self.qdrant.add(owner_id, paper_id)
        self.storage.add(owner_id, paper_id)
        return paper_id

    def row(self, paper_id):
        with self.SessionLocal() as db:
            return db.get(Paper, uuid.UUID(paper_id))

    def set_status(self, paper_id, status):
        with self.SessionLocal() as db:
            db.get(Paper, uuid.UUID(paper_id)).status = status
            db.commit()

    def assert_intact(self, owner_id, paper_id, status="indexed"):
        row = self.row(paper_id)
        self.assertIsNotNone(row, f"row {paper_id} was deleted")
        self.assertEqual(row.status, status)
        self.assertEqual(len(self.qdrant.ids_for(owner_id, paper_id)), VECTORS_PER_PAPER)
        self.assertIn(build_storage_path(owner_id, paper_id), self.storage.objects)

    def assert_gone(self, owner_id, paper_id):
        self.assertIsNone(self.row(paper_id), "row survived")
        self.assertEqual(self.qdrant.ids_for(owner_id, paper_id), set(), "vectors survived")
        self.assertNotIn(build_storage_path(owner_id, paper_id), self.storage.objects, "PDF survived")

    def assert_nothing_touched(self):
        self.assertEqual(self.qdrant.scroll_filters, [], "Qdrant was reached")
        self.assertEqual(self.storage.calls, [], "Storage was reached")
        for owner_id, paper_id in ((OWNER_A, self.a1), (OWNER_A, self.a2), (OWNER_B, self.b1)):
            self.assert_intact(owner_id, paper_id)


class TestLegacyTitleRoute(DeletePaperTestCase):

    def test_an_ambiguous_title_is_refused_and_nothing_is_deleted(self):
        resp = self.client.delete(f"/paper/{SHARED_TITLE}")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], AMBIGUOUS_TITLE_DETAIL)
        # Not even moved to 'deleting': both rows, all vectors, all PDFs.
        self.assert_nothing_touched()

    def test_a_title_that_names_one_paper_still_deletes_it(self):
        """Owner B has one paper with the shared title; owner A's two
        same-titled papers do not make B's title ambiguous."""
        self.current_owner = OWNER_B

        resp = self.client.delete(f"/paper/{SHARED_TITLE}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["vectors_deleted"], VECTORS_PER_PAPER)
        self.assert_gone(OWNER_B, self.b1)
        self.assert_intact(OWNER_A, self.a1)
        self.assert_intact(OWNER_A, self.a2)

    def test_another_owners_title_is_still_404(self):
        self.current_owner = OWNER_B
        resp = self.client.delete("/paper/only-owner-a-has-this.pdf")
        self.assertEqual(resp.status_code, 404)
        self.assert_nothing_touched()


class TestDeleteById(DeletePaperTestCase):

    def test_deletes_only_the_selected_paper(self):
        resp = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json(),
            {
                "status": "success",
                "message": "Paper deleted successfully.",
                "paper_id": self.a2,
                "vectors_deleted": VECTORS_PER_PAPER,
            },
        )
        self.assert_gone(OWNER_A, self.a2)
        # The other same-titled paper, and the other owner's, are intact.
        self.assert_intact(OWNER_A, self.a1)
        self.assert_intact(OWNER_B, self.b1)

    def test_every_resource_is_scoped_to_the_verified_owner_and_this_paper(self):
        self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(len(self.qdrant.scroll_filters), 1)
        conditions = {(c.key, c.match.value) for c in self.qdrant.scroll_filters[0].must}
        self.assertEqual(conditions, {("owner_id", OWNER_A), ("paper_id", self.a2)})
        self.assertEqual(
            self.storage.calls,
            [{"owner_id": OWNER_A, "paper_id": self.a2, "user_jwt": f"jwt-of-{OWNER_A}"}],
        )

    def test_the_storage_path_is_derived_not_read_from_the_row(self):
        """A row whose stored path points elsewhere still deletes only
        the object at its own conventional path."""
        with self.SessionLocal() as db:
            db.get(Paper, uuid.UUID(self.a2)).storage_path = build_storage_path(OWNER_B, self.b1)
            db.commit()

        resp = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assert_intact(OWNER_B, self.b1)

    def test_another_owners_paper_id_is_404_and_changes_nothing(self):
        resp = self.client.delete(f"/papers/{self.b1}")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"detail": "Paper not found"})
        self.assert_nothing_touched()

    def test_unknown_and_malformed_ids_are_a_safe_404(self):
        for supplied in (
            str(uuid.uuid4()),
            "not-a-uuid",
            "12345",
            "1' OR '1'='1",
            "x" * 300,
            SHARED_TITLE,
        ):
            with self.subTest(supplied=supplied[:20]):
                resp = self.client.delete(f"/papers/{supplied}")

                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json(), {"detail": "Paper not found"})
                self.assertNotIn(supplied, resp.text)
        self.assert_nothing_touched()

    def test_a_paper_being_indexed_is_refused_and_untouched(self):
        self.set_status(self.a2, "indexing")

        resp = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"], INDEXING_DETAIL)
        self.assert_intact(OWNER_A, self.a2, status="indexing")
        self.assertEqual(self.storage.calls, [])

    def test_a_qdrant_failure_leaves_a_resumable_row_and_a_retry_completes(self):
        for failing_call in ("scroll", "delete"):
            with self.subTest(failing_call=failing_call):
                target = self.seed(OWNER_A, SHARED_TITLE)
                self.qdrant.fail_next = failing_call

                failed = self.client.delete(f"/papers/{target}")

                self.assertEqual(failed.status_code, 502)
                # Recoverable: the row is still there, marked 'deleting',
                # and nothing past Qdrant was attempted.
                self.assert_intact(OWNER_A, target, status="deleting")
                self.assertNotIn(target, [c["paper_id"] for c in self.storage.calls])

                retried = self.client.delete(f"/papers/{target}")

                self.assertEqual(retried.status_code, 200, retried.text)
                self.assert_gone(OWNER_A, target)
        self.assert_intact(OWNER_A, self.a1)
        self.assert_intact(OWNER_A, self.a2)
        self.assert_intact(OWNER_B, self.b1)


def _jwt_shaped(claims: dict) -> str:
    """A key that looks like a Supabase key; never verified by anything."""
    import base64

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.{b64(b'sig')}"


TEST_SUPABASE_URL = "https://deletetests.supabase.co"
TEST_ANON_KEY = _jwt_shaped({"role": "anon"})
TEST_SERVICE_ROLE_KEY = _jwt_shaped({"role": "service_role"})


class FakeStorageAPI:
    """The two Storage calls delete_pdf makes, at the HTTP boundary:

      DELETE /object/papers       remove(), applied AS THE CALLER — a user
                                  token removes objects in its own folder
                                  only; anything else is filtered out
                                  silently (200, empty list), which is how
                                  Storage answers a delete that RLS blocked
      POST   /object/list/papers  the service-role confirmation — sees all

    It shares its object set with FakeStorage, so the base class's
    assertions read the same truth. Failures are injected per call.
    """

    def __init__(self, objects):
        self.objects = objects
        self.remove_fault = None  # see _remove()
        self.list_fault = False
        self.calls = []  # (method, caller) in order

    def _caller(self, request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token == TEST_SERVICE_ROLE_KEY:
            return "service_role"
        return token.removeprefix("jwt-of-") if token.startswith("jwt-of-") else "anon"

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        route = unquote(request.url.path).split("/storage/v1/", 1)[1]
        caller = self._caller(request)
        self.calls.append((request.method, caller))
        if request.method == "DELETE" and route == "object/papers":
            return self._remove(request, caller)
        if request.method == "POST" and route == "object/list/papers":
            return self._list(request, caller)
        raise AssertionError(f"unexpected Storage request: {request.method} {route}")

    def _remove(self, request, caller):
        fault = self.remove_fault
        if fault == "network":
            raise httpx.ConnectError("storage unreachable", request=request)
        if fault == "server_error":
            return self._error(request, 500, "InternalError", "internal error")
        if fault == "unauthorized":
            return self._error(request, 403, "Unauthorized", "invalid signature")
        removed = []
        for name in json.loads(request.content)["prefixes"]:
            in_own_folder = name.split("/")[0] == caller
            if name in self.objects and in_own_folder and fault != "rls_filtered":
                if fault != "claims_removed_but_kept":
                    self.objects.discard(name)
                removed.append({"name": name})
        return httpx.Response(200, json=removed, request=request)

    def _list(self, request, caller):
        if self.list_fault:
            return self._error(request, 500, "InternalError", "internal error")
        prefix = json.loads(request.content)["prefix"].rstrip("/") + "/"
        entries = [
            {"name": name[len(prefix):]}
            for name in self.objects
            if name.startswith(prefix)
            and "/" not in name[len(prefix):]
            and (caller == "service_role" or name.split("/")[0] == caller)
        ]
        return httpx.Response(200, json=entries, request=request)

    @staticmethod
    def _error(request, status, error, message):
        body = {"statusCode": str(status), "error": error, "message": message}
        return httpx.Response(status, json=body, request=request)


class TestStorageStepIsConfirmed(DeletePaperTestCase):
    """A delete reports success only when the PDF is confirmed gone; any
    other outcome leaves the row 'deleting' so the same request can finish
    the job once the underlying problem is resolved."""

    def setUp(self):
        super().setUp()
        # Undo the base class's delete_pdf double: the real one runs here.
        for patcher in (
            patch("app.api.delete_paper.delete_pdf", storage_module.delete_pdf),
            patch.dict(
                os.environ,
                {
                    "SUPABASE_URL": TEST_SUPABASE_URL,
                    "SUPABASE_ANON_KEY": TEST_ANON_KEY,
                    "SUPABASE_SERVICE_ROLE_KEY": TEST_SERVICE_ROLE_KEY,
                },
            ),
            # Cached per process; build it from this test's settings.
            patch.object(storage_module, "_service_client", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.api = FakeStorageAPI(self.storage.objects)
        api = self.api
        original_send = httpx.Client.send

        def send(client, request, **kwargs):
            # TestClient is itself an httpx.Client: let the test's own
            # requests through, and answer only Supabase's.
            if isinstance(client, TestClient):
                return original_send(client, request, **kwargs)
            return api.handle(request)

        transport = patch.object(httpx.Client, "send", new=send)
        transport.start()
        self.addCleanup(transport.stop)

    def assert_left_for_a_retry(self, paper_id, pdf_present=True):
        """Step 1 ran (vectors gone); the row survived, marked 'deleting'."""
        row = self.row(paper_id)
        self.assertIsNotNone(row, "the row was removed although Storage was not confirmed")
        self.assertEqual(row.status, "deleting")
        self.assertEqual(self.qdrant.ids_for(OWNER_A, paper_id), set())
        self.assertEqual(
            build_storage_path(OWNER_A, paper_id) in self.storage.objects, pdf_present
        )

    def assert_others_intact(self):
        self.assert_intact(OWNER_A, self.a1)
        self.assert_intact(OWNER_B, self.b1)

    def fail_then_retry(self, fault):
        """One failed delete with `fault`, then a retry once it is fixed."""
        self.api.remove_fault = fault

        failed = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(failed.status_code, 502)
        self.assert_left_for_a_retry(self.a2)
        self.assert_others_intact()

        self.api.remove_fault = None
        retried = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assert_others_intact()

    def test_a_server_error_from_remove_is_not_reported_as_success(self):
        self.fail_then_retry("server_error")

    def test_a_network_failure_during_remove_is_not_reported_as_success(self):
        self.fail_then_retry("network")

    def test_an_authorization_refusal_is_not_reported_as_success(self):
        self.fail_then_retry("unauthorized")

    def test_a_delete_that_rls_silently_filtered_is_not_mistaken_for_absent(self):
        """Storage answers 200 with nothing removed; only the confirmation
        reveals the PDF is still there."""
        self.fail_then_retry("rls_filtered")

    def test_a_reported_removal_that_did_not_happen_is_caught(self):
        self.fail_then_retry("claims_removed_but_kept")

    def test_an_object_that_is_already_absent_completes_the_delete(self):
        self.storage.objects.discard(build_storage_path(OWNER_A, self.a2))

        resp = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assert_others_intact()

    def test_when_absence_cannot_be_confirmed_the_row_is_kept(self):
        """remove() worked, but the confirmation itself failed: 'could not
        check' must not be treated as 'gone'."""
        self.api.list_fault = True

        failed = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(failed.status_code, 502)
        self.assert_left_for_a_retry(self.a2, pdf_present=False)

        self.api.list_fault = False
        retried = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assert_others_intact()

    def test_a_failure_after_qdrant_but_before_storage_is_recoverable(self):
        # Building the user-scoped Storage client fails: Qdrant is done,
        # Storage was never reached.
        with patch.dict(os.environ, {"SUPABASE_ANON_KEY": ""}):
            failed = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(failed.status_code, 502)
        self.assert_left_for_a_retry(self.a2)
        self.assertEqual(self.api.calls, [], "Storage was reached")

        retried = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assert_others_intact()

    def test_a_crash_after_storage_removal_is_recoverable_and_idempotent(self):
        real_delete_pdf = storage_module.delete_pdf

        def storage_done_then_crash(**kwargs):
            real_delete_pdf(**kwargs)
            raise RuntimeError("process died before the row was deleted")

        with patch("app.api.delete_paper.delete_pdf", side_effect=storage_done_then_crash):
            failed = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(failed.status_code, 502)
        self.assert_left_for_a_retry(self.a2, pdf_present=False)

        retried = self.client.delete(f"/papers/{self.a2}")
        again = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, self.a2)
        self.assertEqual(again.status_code, 404)
        self.assert_others_intact()

    def test_the_delete_runs_as_the_user_and_the_service_role_only_reads(self):
        resp = self.client.delete(f"/papers/{self.a2}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.api.calls, [("DELETE", OWNER_A), ("POST", "service_role")])

    def test_the_legacy_ambiguous_title_is_still_refused_before_storage(self):
        resp = self.client.delete(f"/paper/{SHARED_TITLE}")

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.api.calls, [])
        self.assert_nothing_touched()

    # The deployed frontend calls the legacy title route until it moves to
    # DELETE /papers/{id}, so a failed title delete must be just as
    # recoverable: by the same title, or by id.
    LEGACY_TITLE = "only-this-title.pdf"
    REMOVE_FAULTS = ("server_error", "network", "unauthorized", "rls_filtered", "claims_removed_but_kept")

    def fail_by_title(self, fault=None, list_fault=False):
        """A title delete that fails at the Storage step. Returns the paper
        left 'deleting'; every other paper is untouched."""
        paper = self.seed(OWNER_A, self.LEGACY_TITLE)
        self.api.remove_fault = fault
        self.api.list_fault = list_fault

        failed = self.client.delete(f"/paper/{self.LEGACY_TITLE}")

        self.assertEqual(failed.status_code, 502)
        self.assert_left_for_a_retry(paper, pdf_present=not list_fault)
        self.assert_others_intact()
        self.assert_intact(OWNER_A, self.a2)
        self.api.remove_fault = None
        self.api.list_fault = False
        return paper

    def test_a_failed_title_delete_is_retried_by_title(self):
        for fault in self.REMOVE_FAULTS:
            with self.subTest(fault=fault):
                paper = self.fail_by_title(fault)

                retried = self.client.delete(f"/paper/{self.LEGACY_TITLE}")

                self.assertEqual(retried.status_code, 200, retried.text)
                self.assert_gone(OWNER_A, paper)
                self.assert_others_intact()

    def test_a_title_delete_whose_absence_cannot_be_confirmed_is_retried_by_title(self):
        paper = self.fail_by_title(list_fault=True)

        retried = self.client.delete(f"/paper/{self.LEGACY_TITLE}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, paper)

    def test_a_failed_title_delete_can_be_finished_by_id(self):
        paper = self.fail_by_title("network")

        retried = self.client.delete(f"/papers/{paper}")

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assert_gone(OWNER_A, paper)
        self.assertEqual(self.client.delete(f"/paper/{self.LEGACY_TITLE}").status_code, 404)

    def test_another_owner_cannot_finish_a_deleting_row(self):
        paper = self.fail_by_title("server_error")

        self.current_owner = OWNER_B
        by_id = self.client.delete(f"/papers/{paper}")
        by_title = self.client.delete(f"/paper/{self.LEGACY_TITLE}")
        self.current_owner = OWNER_A

        self.assertEqual(by_id.status_code, 404)
        self.assertEqual(by_title.status_code, 404)
        self.assertEqual(self.row(paper).status, "deleting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
