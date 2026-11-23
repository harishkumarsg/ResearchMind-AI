"""
A paper's lifecycle is coordinated by atomic claims
(app/services/paper_lifecycle.py).

The defects pinned down here, each reproduced offline before the fix:
  - two deletes of one paper both ran the whole pipeline, and the one that
    finished second returned an unhandled 500;
  - an index run that had read a paper as 'uploaded' wrote its status over
    a concurrent deletion's, and could write vectors after the paper was
    gone;
  - a re-upload overwrote a deletion's 'deleting' status the same way;
  - a Qdrant or Storage failure became a plain-text 500 with no CORS
    headers, which the browser cannot read.

HOW THE INTERLEAVINGS WORK. A request runs until it reaches a named step —
a claim, a Qdrant or Storage call, a stage of indexing — and a second
request then runs to completion from inside that step before the first
continues. Two requests are interleaved deterministically, at exactly the
points where real concurrency matters. Both go through the real routes and
middleware, each with its own session and connection to a file-backed
SQLite database.

WHAT THIS PROVES AND WHAT IT DOES NOT. These tests verify the PROTOCOL:
which claims succeed, which are refused, and what every interleaving leaves
behind. SQLite serialises writers, so they cannot exercise PostgreSQL's row
locking. That two simultaneous conditional UPDATEs of one row cannot both
succeed follows from READ COMMITTED re-checking the WHERE clause once the
first commits — argued in paper_lifecycle.py, not tested here.
"""
import contextlib
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from storage3.exceptions import StorageApiError  # noqa: E402

import app.api.index_document as index_document  # noqa: E402
from app.api.delete_paper import BUSY_DETAILS, DELETE_INCOMPLETE_DETAIL  # noqa: E402
from app.api.paper_evidence import reindexed_since  # noqa: E402
from app.api.upload import (  # noqa: E402
    CONTENT_UNIQUE_CONSTRAINT,
    PAPER_BEING_REMOVED_DETAIL,
    PAPER_REMOVED_DURING_UPLOAD_DETAIL,
    UPLOAD_IN_PROGRESS_DETAIL,
    UPLOAD_NOT_SAVED_DETAIL,
    _is_duplicate_file,
)
from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id  # noqa: E402
from app.core.cors import get_allowed_origins  # noqa: E402
from app.db.models import Base, Paper  # noqa: E402
from app.db.session import get_db_session  # noqa: E402
from app.services import paper_lifecycle  # noqa: E402
from app.services.storage import StorageDeletionError, build_storage_path  # noqa: E402
from tests.test_delete_paper_by_id import FakeQdrant, FakeStorage  # noqa: E402

#: Taken at import, before any test patches the module attribute.
REAL_INDEX_ONE_PAPER = index_document.index_one_paper

OWNER_A = "aaaaaaaa-5555-4555-8555-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-5555-4555-8555-bbbbbbbbbbbb"
VECTORS_PER_PAPER = 3
ORIGIN = get_allowed_origins()[0]

# Shaped like what the real clients say; none of it may reach a response.
STORAGE_ERROR_TEXT = "new row violates row-level security policy (bucket papers, https://proj.supabase.co/storage/v1)"
QDRANT_ERROR_TEXT = "connection refused: https://cluster-1.eu.qdrant.io:6333, api-key rejected"
LEAKED_WORDS = ("row-level", "supabase", "bucket", "qdrant", "api-key", "cluster")


class Steps:
    """Runs a registered action the first time any request reaches a step."""

    def __init__(self):
        self.actions = {}

    def on(self, step, action):
        self.actions[step] = action

    def reach(self, step):
        action = self.actions.pop(step, None)
        if action is not None:
            action()


class Clock:
    """paper_lifecycle's clock. Always moves forward, so two claims never
    share a token by accident; advance() jumps it to expire a lease."""

    def __init__(self):
        self.current = datetime.now(timezone.utc)

    def now(self):
        self.current += timedelta(milliseconds=1)
        return self.current

    def advance(self, delta):
        self.current += delta


class SteppedQdrant(FakeQdrant):
    def __init__(self, steps):
        super().__init__()
        self.steps = steps

    def _maybe_fail(self, operation):
        if self.fail_next == operation:
            self.fail_next = None
            raise RuntimeError(QDRANT_ERROR_TEXT)

    def scroll(self, collection_name, scroll_filter, **kwargs):
        wanted = {c.key: c.match.value for c in scroll_filter.must}
        self.steps.reach(f"qdrant:scroll:{wanted.get('paper_id')}")
        return super().scroll(collection_name, scroll_filter, **kwargs)


class SteppedStorage(FakeStorage):
    """The Storage calls the delete and upload routes make."""

    def __init__(self, steps):
        super().__init__()
        self.steps = steps
        self.writes = []
        self.fail_with = None

    def delete_pdf(self, owner_id, paper_id, user_jwt):
        self.steps.reach(f"storage:delete:{paper_id}")
        if self.fail_with is not None:
            self.calls.append({"owner_id": owner_id, "paper_id": paper_id, "user_jwt": user_jwt})
            raise self.fail_with
        super().delete_pdf(owner_id, paper_id, user_jwt)

    def upload_pdf(self, owner_id, paper_id, user_jwt, file_bytes):
        # Also by step alone: a new paper's id is not known before upload.
        self.steps.reach(f"storage:write:{paper_id}")
        self.steps.reach("storage:write")
        self.writes.append(paper_id)
        self.add(owner_id, paper_id)
        return build_storage_path(owner_id, paper_id)

    def exists(self, owner_id, paper_id, user_jwt):
        return build_storage_path(owner_id, paper_id) in self.objects


class LifecycleTestCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # File-backed, so every session gets its own connection and
        # transaction, as in production. Disposed before the directory goes.
        engine = create_engine(
            f"sqlite:///{tmp.name}/papers.sqlite", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        # Production's papers table is unique on (owner_id, content_hash)
        # (migrations/0001); the model does not declare it, so mirror it.
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX papers_owner_id_content_hash_key ON papers (owner_id, content_hash)"
            )
        self.addCleanup(engine.dispose)
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        self.steps = Steps()
        # An interleaving whose step was never reached tested nothing.
        self.addCleanup(lambda: self.assertEqual(list(self.steps.actions), [], "steps never reached"))
        self.clock = Clock()
        self.qdrant = SteppedQdrant(self.steps)
        self.storage = SteppedStorage(self.steps)
        self.index_calls = []

        real_claim = paper_lifecycle.claim
        real_claim_for_deletion = paper_lifecycle.claim_for_deletion

        def claim(db, paper_id, owner_id, *, from_status, to_status, **values):
            self.steps.reach(f"claim:{to_status}:{paper_id}")
            return real_claim(db, paper_id, owner_id, from_status=from_status, to_status=to_status, **values)

        def claim_for_deletion(db, paper_id, owner_id):
            self.steps.reach(f"claim:deleting:{paper_id}")
            return real_claim_for_deletion(db, paper_id, owner_id)

        from app.api import upload as upload_module

        real_build_path = upload_module.build_storage_path
        real_charge = upload_module.charge_upload_unit

        def build_path(owner_id, paper_id):
            self.steps.reach("upload:new_row")
            return real_build_path(owner_id, paper_id)

        def charge(owner_id):
            self.steps.reach("upload:charge")
            return real_charge(owner_id)

        for patcher in (
            patch.object(paper_lifecycle, "now", self.clock.now),
            patch.object(paper_lifecycle, "claim", claim),
            patch.object(paper_lifecycle, "claim_for_deletion", claim_for_deletion),
            patch("app.db.session.get_session_factory", return_value=self.SessionLocal),
            patch("app.api.delete_paper.client", self.qdrant),
            patch("app.api.delete_paper.delete_pdf", self.storage.delete_pdf),
            patch("app.api.index_document.client", self.qdrant),
            patch("app.api.index_document.index_one_paper", self.fake_index_one_paper),
            # One slot per process: two runs in this one process stand for
            # two backend instances, which do not share it.
            patch("app.api.index_document.indexing_slot", contextlib.nullcontext),
            patch("app.api.upload.upload_pdf", self.storage.upload_pdf),
            patch("app.api.upload.storage_object_exists", self.storage.exists),
            patch("app.api.upload.delete_pdf", self.storage.delete_pdf),
            patch("app.api.upload.build_storage_path", build_path),
            patch("app.api.upload.charge_upload_unit", charge),
        ):
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
        self.client = TestClient(app, raise_server_exceptions=False)

    # -- the index run's provider and vector work ----------------------------

    def fake_index_one_paper(self, paper, before_provider_work=None, before_vector_writes=None):
        """Follows index_one_paper's real order: fetch the PDF, meter,
        embed, confirm the claim, then replace this paper's points."""
        paper_id, owner_id = str(paper.id), str(paper.owner_id)
        self.steps.reach(f"index:fetch:{paper_id}")
        if build_storage_path(owner_id, paper_id) not in self.storage.objects:
            raise RuntimeError("the PDF could not be fetched")
        self.index_calls.append(paper_id)
        if before_provider_work is not None:
            before_provider_work()
        self.steps.reach(f"index:embed:{paper_id}")
        point_ids = [f"{paper_id}-indexed-{i}" for i in range(VECTORS_PER_PAPER)]
        if before_vector_writes is not None:
            before_vector_writes(point_ids)
        self.steps.reach(f"index:before_write:{paper_id}")
        for stale in self.qdrant.ids_for(owner_id, paper_id):
            del self.qdrant.points[stale]
        for point_id in point_ids:
            self.qdrant.points[point_id] = {"owner_id": owner_id, "paper_id": paper_id}
        self.steps.reach(f"index:after_write:{paper_id}")
        return len(point_ids)

    # -- requests --------------------------------------------------------------

    def delete(self, paper_id):
        return self.client.delete(f"/papers/{paper_id}", headers={"Origin": ORIGIN})

    def delete_by_title(self, title):
        return self.client.delete(f"/paper/{title}", headers={"Origin": ORIGIN})

    def index(self):
        return self.client.post("/index-document")

    def upload(self, content, filename="paper.pdf"):
        return self.client.post("/upload", files={"file": (filename, content, "application/pdf")})

    def later(self, results, name, request):
        """An action that runs `request` and keeps its response."""
        return lambda: results.__setitem__(name, request())

    # -- files -------------------------------------------------------------------

    def content(self, label):
        return b"%PDF-1.4 lifecycle test " + label.encode()

    def paper_with(self, content, owner_id=OWNER_A):
        """The id of this owner's row for this file, or None. A new
        paper's id is random, so it is found by content, as /upload does."""
        with self.SessionLocal() as db:
            row = (
                db.query(Paper)
                .filter(Paper.owner_id == uuid.UUID(owner_id),
                        Paper.content_hash == hashlib.sha256(content).hexdigest())
                .first()
            )
            return str(row.id) if row is not None else None

    def seed_failed_upload(self, content, pdf=False):
        return self.seed(status="failed", vectors=False, pdf=pdf,
                         content_hash=hashlib.sha256(content).hexdigest())

    # -- state -------------------------------------------------------------------

    def seed(self, owner_id=OWNER_A, title="paper.pdf", status="indexed", *, vectors=None,
             pdf=True, paper_id=None, content_hash=None, updated_at=None, status_detail=None):
        paper_id = paper_id or str(uuid.uuid4())
        with self.SessionLocal() as db:
            db.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash=content_hash or uuid.uuid4().hex * 2,
                    storage_path=build_storage_path(owner_id, paper_id),
                    file_size_bytes=1024,
                    status=status,
                    status_detail=status_detail,
                    updated_at=updated_at or self.clock.now(),
                )
            )
            db.commit()
        with_vectors = (status == "indexed") if vectors is None else vectors
        if with_vectors:
            self.qdrant.add(owner_id, paper_id, VECTORS_PER_PAPER)
        if pdf:
            self.storage.add(owner_id, paper_id)
        return paper_id

    def row(self, paper_id):
        with self.SessionLocal() as db:
            paper = db.get(Paper, uuid.UUID(paper_id))
            if paper is None:
                return None
            return SimpleNamespace(status=paper.status, status_detail=paper.status_detail,
                                   updated_at=paper.updated_at)

    def vectors(self, paper_id, owner_id=OWNER_A):
        return len(self.qdrant.ids_for(owner_id, paper_id))

    def has_pdf(self, paper_id, owner_id=OWNER_A):
        return build_storage_path(owner_id, paper_id) in self.storage.objects

    def assert_gone(self, paper_id, owner_id=OWNER_A):
        self.assertIsNone(self.row(paper_id), "row survived")
        self.assertEqual(self.vectors(paper_id, owner_id), 0, "vectors survived")
        self.assertFalse(self.has_pdf(paper_id, owner_id), "PDF survived")

    def assert_no_orphans(self):
        """No vector or PDF outlives its paper; an indexed paper is whole."""
        with self.SessionLocal() as db:
            rows = {str(p.id): (str(p.owner_id), p.status) for p in db.query(Paper).all()}
        orphan_points = sorted(k for k, v in self.qdrant.points.items() if v["paper_id"] not in rows)
        orphan_pdfs = sorted(o for o in self.storage.objects if o.split("/")[1] not in rows)
        self.assertEqual(orphan_points, [], "vectors with no paper")
        self.assertEqual(orphan_pdfs, [], "PDFs with no paper")
        for paper_id, (owner_id, status) in rows.items():
            if status == "indexed":
                self.assertTrue(self.has_pdf(paper_id, owner_id), f"indexed paper {paper_id} has no PDF")
                self.assertGreater(self.vectors(paper_id, owner_id), 0, f"indexed paper {paper_id} has no vectors")

    def assert_busy(self, resp, status):
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json(), {"detail": BUSY_DETAILS[status]})


# ---------------------------------------------------------------------------
# Expected deletion failures: sanitized JSON, readable by the browser
# ---------------------------------------------------------------------------
class TestDeletionFailureResponses(LifecycleTestCase):

    def assert_sanitized_failure(self, resp):
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), ORIGIN)
        self.assertTrue(resp.headers["content-type"].startswith("application/json"))
        self.assertEqual(resp.json(), {"detail": DELETE_INCOMPLETE_DETAIL})
        for word in LEAKED_WORDS:
            self.assertNotIn(word, resp.text.lower())

    def assert_released(self, paper_id):
        """Left for a retry: still 'deleting', marked free to claim at once."""
        row = self.row(paper_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "deleting")
        self.assertEqual(row.status_detail, paper_lifecycle.DELETION_INCOMPLETE_DETAIL)

    def test_a_storage_failure_is_sanitized_json_with_cors_and_retryable_at_once(self):
        for failure in (
            StorageApiError(STORAGE_ERROR_TEXT, "Unauthorized", 403),
            StorageDeletionError("The PDF is still present after the delete request."),
            httpx.ConnectError(STORAGE_ERROR_TEXT),
        ):
            with self.subTest(failure=type(failure).__name__):
                paper = self.seed()
                self.storage.fail_with = failure

                failed = self.delete(paper)

                self.assert_sanitized_failure(failed)
                self.assert_released(paper)
                self.assertEqual(self.vectors(paper), 0)
                self.assertTrue(self.has_pdf(paper))

                self.storage.fail_with = None
                retried = self.delete(paper)

                self.assertEqual(retried.status_code, 200, retried.text)
                self.assert_gone(paper)

    def test_a_qdrant_failure_is_sanitized_json_with_cors_and_retryable_at_once(self):
        for operation in ("scroll", "delete"):
            with self.subTest(operation=operation):
                paper = self.seed()
                self.qdrant.fail_next = operation
                storage_calls = len(self.storage.calls)

                failed = self.delete(paper)

                self.assert_sanitized_failure(failed)
                self.assert_released(paper)
                self.assertEqual(len(self.storage.calls), storage_calls, "Storage was reached")
                self.assertTrue(self.has_pdf(paper))

                retried = self.delete(paper)

                self.assertEqual(retried.status_code, 200, retried.text)
                self.assert_gone(paper)

    def test_the_legacy_title_route_fails_the_same_way(self):
        paper = self.seed(title="only-this-one.pdf")
        self.storage.fail_with = StorageApiError(STORAGE_ERROR_TEXT, "InternalError", 500)

        failed = self.delete_by_title("only-this-one.pdf")

        self.assert_sanitized_failure(failed)
        self.storage.fail_with = None
        self.assertEqual(self.delete_by_title("only-this-one.pdf").status_code, 200)
        self.assert_gone(paper)

    def test_refusals_are_json_with_cors_too(self):
        indexing = self.seed(status="indexing")
        for resp, status in ((self.delete(str(uuid.uuid4())), 404), (self.delete(indexing), 409)):
            with self.subTest(status=status):
                self.assertEqual(resp.status_code, status)
                self.assertEqual(resp.headers.get("access-control-allow-origin"), ORIGIN)
                self.assertIn("detail", resp.json())


# ---------------------------------------------------------------------------
# Concurrent deletes
# ---------------------------------------------------------------------------
class TestConcurrentDeletes(LifecycleTestCase):

    def test_a_delete_arriving_while_another_is_in_flight_is_refused_and_does_nothing(self):
        paper, sibling = self.seed(), self.seed()
        results = {}
        self.steps.on(f"qdrant:scroll:{paper}", self.later(results, "second", lambda: self.delete(paper)))

        first = self.delete(paper)

        self.assertEqual(first.status_code, 200, first.text)
        self.assert_busy(results["second"], "deleting")
        self.assertEqual(len(self.qdrant.scroll_filters), 1, "the second delete reached Qdrant")
        self.assertEqual(len(self.storage.calls), 1, "the second delete reached Storage")
        self.assert_gone(paper)
        self.assertEqual(self.row(sibling).status, "indexed")
        self.assert_no_orphans()

    def test_two_title_deletes_that_both_found_the_paper_end_in_one_success_and_a_404(self):
        paper = self.seed(title="report.pdf")
        results = {}
        # Both requests have resolved the title; the second claims first.
        self.steps.on(f"claim:deleting:{paper}",
                      self.later(results, "second", lambda: self.delete_by_title("report.pdf")))

        first = self.delete_by_title("report.pdf")

        self.assertEqual(results["second"].status_code, 200, results["second"].text)
        self.assertEqual(first.status_code, 404, first.text)
        self.assertEqual(first.json(), {"detail": "Paper not found"})
        self.assertEqual(len(self.storage.calls), 1)
        self.assert_gone(paper)

    def test_a_claim_is_exclusive_across_connections(self):
        paper = uuid.UUID(self.seed())
        owner = uuid.UUID(OWNER_A)
        with self.SessionLocal() as one, self.SessionLocal() as two:
            self.assertIsNotNone(paper_lifecycle.claim_for_deletion(one, paper, owner))
            self.assertIsNone(paper_lifecycle.claim_for_deletion(two, paper, owner))

    def test_an_expired_claim_is_taken_over_and_the_stale_request_cannot_finish(self):
        paper = self.seed()
        results = {}

        def take_over():
            self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
            results["second"] = self.delete(paper)

        self.steps.on(f"qdrant:scroll:{paper}", take_over)

        first = self.delete(paper)

        self.assertEqual(results["second"].status_code, 200, results["second"].text)
        # The stale request repeated idempotent deletes, then found its
        # claim gone: a clean 404, never a 500.
        self.assertEqual(first.status_code, 404, first.text)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_stale_request_can_neither_finish_nor_release_a_claim_taken_over(self):
        """The stale request wakes while the request that took its claim
        over is still between its Qdrant and Storage steps."""
        paper = self.seed()
        paper_uuid, owner = uuid.UUID(paper), uuid.UUID(OWNER_A)
        with self.SessionLocal() as db:
            stale = paper_lifecycle.claim_for_deletion(db, paper_uuid, owner)  # claimed, then stalled
        self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
        attempts = {}

        def stale_request_resumes():
            with self.SessionLocal() as db:
                attempts["finish"] = paper_lifecycle.finish_deletion(db, paper_uuid, owner, stale)
                attempts["release"] = paper_lifecycle.release_deletion(db, paper_uuid, owner, stale)
            row = self.row(paper)
            attempts["row"] = (row.status, row.status_detail) if row else None

        self.steps.on(f"storage:delete:{paper}", stale_request_resumes)

        taken_over = self.delete(paper)

        # The row outlived the stale finish: its PDF was still there.
        self.assertEqual(attempts, {"finish": False, "release": False, "row": ("deleting", None)})
        self.assertEqual(taken_over.status_code, 200, taken_over.text)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_crashed_deletion_is_recovered_once_its_lease_expires(self):
        # A live claim (status_detail NULL) that was never released.
        paper = self.seed(status="deleting", updated_at=self.clock.now())

        too_soon = self.delete(paper)

        self.assert_busy(too_soon, "deleting")
        self.assertEqual(self.storage.calls, [])
        self.assertEqual(self.qdrant.scroll_filters, [])

        self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
        recovered = self.delete(paper)

        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assert_gone(paper)

    def test_the_updated_at_watermark_only_moves_forward(self):
        paper = self.seed()
        before = self.row(paper).updated_at
        self.storage.fail_with = StorageApiError(STORAGE_ERROR_TEXT, "InternalError", 500)

        self.delete(paper)  # claim, then release
        released = self.row(paper).updated_at
        self.storage.fail_with = None

        self.assertGreater(released, before)
        # What paper_evidence reads: an analysis made before the delete
        # attempt is reported as possibly stale, never as current.
        self.assertTrue(reindexed_since(released, before))


# ---------------------------------------------------------------------------
# Concurrent indexing
# ---------------------------------------------------------------------------
class TestConcurrentIndexing(LifecycleTestCase):

    def test_two_index_runs_that_both_read_a_paper_index_it_once(self):
        paper = self.seed(status="uploaded")
        results = {}
        self.steps.on(f"claim:indexing:{paper}", self.later(results, "second", self.index))

        first = self.index()

        self.assertEqual(results["second"].json()["papers_indexed"], 1)
        self.assertEqual(first.json()["papers_indexed"], 0)
        self.assertEqual(first.json()["papers_failed"], 0)
        self.assertEqual(self.index_calls, [paper], "indexed twice")
        self.assertEqual(self.vectors(paper), VECTORS_PER_PAPER)
        self.assertEqual(self.row(paper).status, "indexed")

    def test_a_run_reading_the_library_mid_index_does_not_see_the_paper(self):
        paper = self.seed(status="uploaded")
        results = {}
        self.steps.on(f"index:embed:{paper}", self.later(results, "second", self.index))

        self.assertEqual(self.index().json()["papers_indexed"], 1)
        self.assertEqual(results["second"].json()["papers_found"], 0)
        self.assertEqual(self.index_calls, [paper])


# ---------------------------------------------------------------------------
# Deletion versus indexing, at every transition
# ---------------------------------------------------------------------------
class TestDeletionVersusIndexing(LifecycleTestCase):

    def test_a_delete_before_the_index_claim_means_no_provider_work_and_no_vectors(self):
        doomed = self.seed(status="uploaded")
        survivor = self.seed(status="uploaded")
        results = {}
        self.steps.on(f"claim:indexing:{doomed}", self.later(results, "delete", lambda: self.delete(doomed)))

        run = self.index()

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertNotIn(doomed, self.index_calls, "the deleted paper was fetched or embedded")
        self.assertEqual(run.json()["papers_indexed"], 1)
        self.assert_gone(doomed)
        self.assertEqual(self.row(survivor).status, "indexed")
        self.assert_no_orphans()

    def test_an_index_run_reaching_a_paper_mid_deletion_skips_it(self):
        paper = self.seed(status="uploaded")
        results = {}
        self.steps.on(f"qdrant:scroll:{paper}", self.later(results, "index", self.index))

        deleted = self.delete(paper)

        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(results["index"].json()["papers_indexed"], 0)
        self.assertEqual(self.index_calls, [])
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_delete_while_indexing_is_refused_before_and_after_the_vector_writes(self):
        for step in ("index:fetch", "index:embed", "index:before_write", "index:after_write"):
            with self.subTest(step=step):
                paper = self.seed(status="uploaded")
                results = {}
                scrolls, storage_calls = len(self.qdrant.scroll_filters), len(self.storage.calls)
                self.steps.on(f"{step}:{paper}", self.later(results, "delete", lambda p=paper: self.delete(p)))

                run = self.index()

                self.assert_busy(results["delete"], "indexing")
                self.assertEqual(len(self.qdrant.scroll_filters), scrolls, "the refused delete reached Qdrant")
                self.assertEqual(len(self.storage.calls), storage_calls, "the refused delete reached Storage")
                self.assertEqual(run.json()["papers_indexed"], 1)
                self.assertEqual(self.row(paper).status, "indexed")

                # Once indexing is over, the delete goes through and takes
                # every vector the run wrote with it.
                self.assertEqual(self.delete(paper).status_code, 200)
                self.assert_gone(paper)
        self.assert_no_orphans()

    def test_an_index_claim_never_overwrites_a_deletion_in_flight(self):
        """The run has read the paper as 'uploaded'; a deletion claims it
        before the run's own claim, and is still in flight."""
        paper = self.seed(status="uploaded")
        claimed = {}

        def deletion_claims_now():
            with self.SessionLocal() as db:
                claimed["token"] = paper_lifecycle.claim_for_deletion(db, uuid.UUID(paper), uuid.UUID(OWNER_A))

        self.steps.on(f"claim:indexing:{paper}", deletion_claims_now)

        run = self.index()

        self.assertIsNotNone(claimed["token"])
        self.assertEqual(run.json()["papers_indexed"], 0)
        self.assertEqual(self.index_calls, [])
        row = self.row(paper)
        self.assertEqual((row.status, row.status_detail), ("deleting", None), "the deletion's claim was overwritten")
        # That deletion never finishes (its process died): after its lease
        # expires, the paper is removed in full.
        self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
        self.assertEqual(self.delete(paper).status_code, 200)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_crashed_index_run_is_recoverable_once_its_lease_expires(self):
        paper = self.seed(status="indexing", vectors=False, updated_at=self.clock.now())

        self.assert_busy(self.delete(paper), "indexing")

        self.clock.advance(paper_lifecycle.INDEXING_LEASE + timedelta(seconds=1))
        recovered = self.delete(paper)

        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assert_gone(paper)

    def test_a_stalled_run_whose_claim_was_taken_over_writes_no_vectors(self):
        """Stalled past its lease before writing; a deletion took the paper
        over and finished. The run's claim check fails: nothing is written."""
        paper = self.seed(status="uploaded")
        results = {}

        def take_over():
            self.clock.advance(paper_lifecycle.INDEXING_LEASE + timedelta(seconds=1))
            results["delete"] = self.delete(paper)

        self.steps.on(f"index:embed:{paper}", take_over)

        run = self.index()

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertEqual(run.json()["papers_indexed"], 0)
        self.assertEqual(run.json()["papers_failed"], 0)
        self.assertEqual(self.vectors(paper), 0)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_run_stalled_inside_its_write_window_removes_exactly_its_own_points(self):
        """The one interleaving that lets a write follow a completed
        deletion: a run that passes its claim check, then stalls for longer
        than INDEXING_LEASE before writing. Its final transition fails, and
        it removes the points it wrote — by id, never by filter."""
        paper = self.seed(status="uploaded")
        results = {}

        def take_over():
            self.clock.advance(paper_lifecycle.INDEXING_LEASE + timedelta(seconds=1))
            results["delete"] = self.delete(paper)

        self.steps.on(f"index:before_write:{paper}", take_over)

        run = self.index()

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertEqual(run.json()["papers_indexed"], 0)
        self.assertEqual(self.vectors(paper), 0)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_the_real_index_one_paper_confirms_its_claim_before_touching_qdrant(self):
        """The doubles above follow index_one_paper's order; this pins the
        real function to it. Nothing outside the process is reached."""
        paper = MagicMock()
        paper.id, paper.owner_id, paper.title = uuid.uuid4(), uuid.UUID(OWNER_A), "paper.pdf"
        order = MagicMock()

        with patch.object(index_document, "fetch_pdf", return_value=b"%PDF-1.4"), \
             patch.object(index_document, "extract_pdf_pages", return_value=[{"page": 1, "text": "t"}]), \
             patch.object(index_document, "_build_chunks_for_paper",
                          return_value=[{"text": "t", "page": 1}, {"text": "u", "page": 1}]), \
             patch.object(index_document, "create_embeddings", return_value=[[0.1], [0.2]]), \
             patch.object(index_document, "create_collection", order.create_collection), \
             patch.object(index_document, "_clear_existing_points_for_paper", order.clear), \
             patch.object(index_document, "client", order.client):
            # A lost claim: nothing of this paper's is touched.
            with self.assertRaises(paper_lifecycle.ClaimLost):
                REAL_INDEX_ONE_PAPER(paper, before_vector_writes=MagicMock(side_effect=paper_lifecycle.ClaimLost))
            order.clear.assert_not_called()
            order.client.upsert.assert_not_called()

            order.reset_mock()
            confirm = MagicMock()
            order.attach_mock(confirm, "confirm")
            REAL_INDEX_ONE_PAPER(paper, before_vector_writes=confirm)

        names = [c[0] for c in order.mock_calls]
        self.assertEqual(names, ["create_collection", "confirm", "clear", "client.upsert"])
        written = [p.id for p in order.client.upsert.call_args.kwargs["points"]]
        self.assertEqual(confirm.call_args.args[0], written)


# ---------------------------------------------------------------------------
# Upload and re-upload versus deletion
# ---------------------------------------------------------------------------
class TestUploadVersusDeletion(LifecycleTestCase):

    def test_a_reupload_while_a_deletion_is_in_flight_is_refused_and_writes_nothing(self):
        content = self.content("in-flight")
        paper = self.seed_failed_upload(content, pdf=True)
        results = {}
        self.steps.on(f"qdrant:scroll:{paper}", self.later(results, "upload", lambda: self.upload(content)))

        deleted = self.delete(paper)

        self.assertEqual(results["upload"].status_code, 409, results["upload"].text)
        self.assertEqual(results["upload"].json(), {"detail": PAPER_BEING_REMOVED_DETAIL})
        self.assertEqual(self.storage.writes, [])
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_reupload_that_read_the_paper_before_a_deletion_claimed_it_overwrites_nothing(self):
        content = self.content("read-first")
        paper = self.seed_failed_upload(content, pdf=True)
        results = {}
        self.steps.on(f"claim:uploading:{paper}", self.later(results, "delete", lambda: self.delete(paper)))

        uploaded = self.upload(content)

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(uploaded.json(), {"detail": PAPER_REMOVED_DURING_UPLOAD_DETAIL})
        self.assertEqual(self.storage.writes, [])
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_reupload_claim_never_overwrites_a_deletion_in_flight(self):
        """The re-upload has read the row as 'failed'; a deletion claims it
        before the re-upload's own claim, and is still in flight."""
        content = self.content("claim-race")
        paper = self.seed_failed_upload(content, pdf=True)
        claimed = {}

        def deletion_claims_now():
            with self.SessionLocal() as db:
                claimed["token"] = paper_lifecycle.claim_for_deletion(db, uuid.UUID(paper), uuid.UUID(OWNER_A))

        self.steps.on(f"claim:uploading:{paper}", deletion_claims_now)

        uploaded = self.upload(content)

        self.assertIsNotNone(claimed["token"])
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(uploaded.json(), {"detail": PAPER_BEING_REMOVED_DETAIL})
        row = self.row(paper)
        self.assertEqual((row.status, row.status_detail), ("deleting", None), "the deletion's claim was overwritten")
        self.assertEqual(self.storage.writes, [])
        self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
        self.assertEqual(self.delete(paper).status_code, 200)
        self.assert_gone(paper)
        self.assert_no_orphans()

    def test_a_delete_during_an_upload_is_refused(self):
        for label, reupload in (("new", False), ("reupload", True)):
            with self.subTest(upload=label):
                content = self.content(label)
                if reupload:
                    self.seed_failed_upload(content)
                results = {}
                self.steps.on("storage:write",
                              self.later(results, "delete", lambda c=content: self.delete(self.paper_with(c))))

                uploaded = self.upload(content)

                paper = uploaded.json()["paper_id"]
                self.assert_busy(results["delete"], "uploading")
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertEqual(uploaded.json()["status"], "uploaded")
                self.assertEqual(self.row(paper).status, "uploaded")
                self.assertTrue(self.has_pdf(paper))
        self.assert_no_orphans()

    def test_a_stalled_upload_whose_claim_was_taken_over_writes_no_pdf(self):
        content = self.content("stalled-before-write")
        results = {}

        def take_over():
            results["paper"] = self.paper_with(content)
            self.clock.advance(paper_lifecycle.UPLOAD_LEASE + timedelta(seconds=1))
            results["delete"] = self.delete(results["paper"])

        self.steps.on("upload:charge", take_over)

        uploaded = self.upload(content)

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(uploaded.json(), {"detail": PAPER_REMOVED_DURING_UPLOAD_DETAIL})
        self.assertEqual(self.storage.writes, [])
        self.assert_gone(results["paper"])
        self.assert_no_orphans()

    def test_an_upload_stalled_inside_its_write_window_removes_the_pdf_it_wrote(self):
        content = self.content("stalled-in-write")
        results = {}

        def take_over():
            results["paper"] = self.paper_with(content)
            self.clock.advance(paper_lifecycle.UPLOAD_LEASE + timedelta(seconds=1))
            results["delete"] = self.delete(results["paper"])

        self.steps.on("storage:write", take_over)

        uploaded = self.upload(content)

        self.assertEqual(results["delete"].status_code, 200, results["delete"].text)
        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(self.storage.writes, [results["paper"]])
        self.assert_gone(results["paper"])
        self.assert_no_orphans()

    def test_a_crashed_upload_is_recoverable_once_its_lease_expires(self):
        paper = self.seed(status="uploading", vectors=False, pdf=False, updated_at=self.clock.now())

        self.assert_busy(self.delete(paper), "uploading")

        self.clock.advance(paper_lifecycle.UPLOAD_LEASE + timedelta(seconds=1))
        self.assertEqual(self.delete(paper).status_code, 200)
        self.assert_gone(paper)

    def test_two_identical_new_uploads_at_once_store_one_paper(self):
        """Both have looked for an existing row and found none. The unique
        (owner_id, content_hash) key admits one insert; the other request
        answers as any duplicate upload does."""
        content = self.content("twice")
        results = {}
        self.steps.on("upload:new_row", self.later(results, "second", lambda: self.upload(content)))

        first = self.upload(content)

        second = results["second"]
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotIn("duplicate", second.json())
        paper = second.json()["paper_id"]
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["paper_id"], paper)
        self.assertIs(first.json()["duplicate"], True)
        self.assertEqual(self.storage.writes, [paper])
        self.assertEqual(self.row(paper).status, "uploaded")
        with self.SessionLocal() as db:
            self.assertEqual(db.query(Paper).count(), 1)


# ---------------------------------------------------------------------------
# Paper incarnations: the same file uploaded after a delete is a new paper
# ---------------------------------------------------------------------------
class TestPaperIncarnations(LifecycleTestCase):
    """Each incarnation has its own id, and so its own Storage path and its
    own Qdrant paper_id. With ids derived from content, a re-upload reused
    the deleted paper's: a worker still running on the old incarnation —
    its claim expired and taken over — then cleared, deleted or overwrote
    the new one's vectors and PDF. Each stale-worker test below fails if
    the two incarnations share an id."""

    #: The namespace /upload derived ids from before U3.3.
    LEGACY_NAMESPACE = uuid.UUID("6ba7b813-9dad-11d1-80b4-00c04fd430c8")

    def upload_and_index(self, content):
        uploaded = self.upload(content)
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        paper = uploaded.json()["paper_id"]
        self.assertEqual(self.index().json()["papers_indexed"], 1)
        return paper

    def assert_whole(self, paper):
        self.assertEqual(self.row(paper).status, "indexed")
        self.assertEqual(self.vectors(paper), VECTORS_PER_PAPER)
        self.assertTrue(self.has_pdf(paper))

    def test_the_same_file_uploaded_after_a_delete_is_a_new_paper(self):
        content = self.content("again")
        first = self.upload_and_index(content)
        self.assertEqual(self.delete(first).status_code, 200)

        second = self.upload_and_index(content)

        self.assertNotEqual(second, first)
        self.assert_gone(first)
        self.assert_whole(second)
        # Every resource is addressed by the new id.
        self.assertEqual(self.storage.objects, {build_storage_path(OWNER_A, second)})
        self.assertEqual({p["paper_id"] for p in self.qdrant.points.values()}, {second})
        self.assert_no_orphans()

    def test_a_new_paper_id_is_random_not_derived_from_the_file(self):
        content = self.content("random")
        paper = uuid.UUID(self.upload(content).json()["paper_id"])
        legacy = uuid.uuid5(self.LEGACY_NAMESPACE, f"{OWNER_A}:{hashlib.sha256(content).hexdigest()}")
        self.assertEqual(paper.version, 4)
        self.assertNotEqual(paper, legacy)

    def test_a_duplicate_upload_still_returns_the_existing_paper(self):
        content = self.content("duplicate")
        first = self.upload(content)
        again = self.upload(content)

        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["paper_id"], first.json()["paper_id"])
        self.assertIs(again.json()["duplicate"], True)
        self.assertEqual(len(self.storage.writes), 1)
        with self.SessionLocal() as db:
            self.assertEqual(db.query(Paper).count(), 1)

    def test_the_same_file_from_two_owners_is_two_papers(self):
        content = self.content("shared-file")
        mine = self.upload(content).json()["paper_id"]
        self.current_owner = OWNER_B
        theirs = self.upload(content)

        self.assertEqual(theirs.status_code, 200, theirs.text)
        self.assertNotIn("duplicate", theirs.json())
        self.assertNotEqual(theirs.json()["paper_id"], mine)

    def test_papers_created_before_fresh_ids_keep_theirs(self):
        for status, after in (("failed", "uploaded"), ("indexed", "indexed")):
            with self.subTest(status=status):
                content = self.content(f"legacy-{status}")
                content_hash = hashlib.sha256(content).hexdigest()
                legacy = str(uuid.uuid5(self.LEGACY_NAMESPACE, f"{OWNER_A}:{content_hash}"))
                self.seed(status=status, paper_id=legacy, content_hash=content_hash, pdf=status == "indexed")

                resp = self.upload(content)

                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["paper_id"], legacy)
                self.assertEqual(self.row(legacy).status, after)
                self.assertTrue(self.has_pdf(legacy))

    def test_a_stale_deletion_cannot_touch_the_next_incarnation(self):
        """A deletion stalls past its lease after its claim; meanwhile the
        paper is deleted by another request, uploaded again and indexed.
        The stale deletion wakes holding the OLD id."""
        for step in ("qdrant:scroll", "storage:delete"):
            with self.subTest(stale_before=step):
                content = self.content(f"stale-delete-{step}")
                old = self.upload_and_index(content)
                results = {}

                def meanwhile(old=old, content=content):
                    self.clock.advance(paper_lifecycle.DELETION_LEASE + timedelta(seconds=1))
                    results["takeover"] = self.delete(old)
                    results["new"] = self.upload_and_index(content)

                self.steps.on(f"{step}:{old}", meanwhile)

                stale = self.delete(old)

                self.assertEqual(results["takeover"].status_code, 200)
                self.assertNotEqual(results["new"], old)
                self.assertEqual(stale.status_code, 404, stale.text)
                self.assert_whole(results["new"])
                self.assert_gone(old)
        self.assert_no_orphans()

    def test_a_stale_index_run_cannot_clear_or_overwrite_the_next_incarnation(self):
        """The run passed its claim check, then stalled past its lease
        before clearing the paper's points and writing its own."""
        content = self.content("stale-index")
        old = self.upload(content).json()["paper_id"]
        results = {}

        def meanwhile():
            self.clock.advance(paper_lifecycle.INDEXING_LEASE + timedelta(seconds=1))
            results["takeover"] = self.delete(old)
            results["new"] = self.upload_and_index(content)

        self.steps.on(f"index:before_write:{old}", meanwhile)

        stale_run = self.index()

        self.assertEqual(results["takeover"].status_code, 200)
        self.assertEqual(stale_run.json()["papers_indexed"], 0)
        self.assert_whole(results["new"])
        self.assert_gone(old)
        self.assert_no_orphans()

    def test_a_stale_uploads_cleanup_cannot_remove_the_next_incarnations_pdf(self):
        """The upload passed its claim check, then stalled past its lease
        before its Storage write landed."""
        content = self.content("stale-upload")
        results = {}

        def meanwhile():
            results["old"] = self.paper_with(content)
            self.clock.advance(paper_lifecycle.UPLOAD_LEASE + timedelta(seconds=1))
            results["takeover"] = self.delete(results["old"])
            results["new"] = self.upload(content)

        self.steps.on("storage:write", meanwhile)

        stale = self.upload(content)

        old, new = results["old"], results["new"].json()["paper_id"]
        self.assertEqual(results["takeover"].status_code, 200)
        self.assertEqual(results["new"].status_code, 200, results["new"].text)
        self.assertNotEqual(new, old)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.row(new).status, "uploaded")
        self.assertTrue(self.has_pdf(new))
        self.assert_gone(old)
        self.assert_no_orphans()


# ---------------------------------------------------------------------------
# Upload integrity errors: only the file's unique conflict is a duplicate race
# ---------------------------------------------------------------------------
class PostgresError(Exception):
    """What psycopg raises: the violated constraint is named on .diag."""

    def __init__(self, message, constraint_name):
        super().__init__(message)
        self.diag = SimpleNamespace(constraint_name=constraint_name)


# Driver messages as Postgres and SQLite word them; none may reach a client.
POSTGRES_UNIQUE_TEXT = (
    f'duplicate key value violates unique constraint "{CONTENT_UNIQUE_CONSTRAINT}"'
)
POSTGRES_FOREIGN_KEY_TEXT = (
    'insert or update on table "papers" violates foreign key constraint '
    '"papers_owner_id_fkey" DETAIL: Key (owner_id) is not present in table "users".'
)
POSTGRES_NOT_NULL_TEXT = 'null value in column "title" of relation "papers" violates not-null constraint'


class TestUploadIntegrityErrors(LifecycleTestCase):
    """The unique (owner_id, content_hash) conflict means another request
    inserted the same file first, and keeps duplicate-upload behaviour. Any
    other integrity failure is a fault, answered as one, and never with the
    driver's text."""

    @staticmethod
    def integrity_error(original):
        return IntegrityError("INSERT INTO papers ...", {}, original)

    def test_only_the_files_unique_conflict_counts_as_a_duplicate(self):
        cases = {
            "postgres: unique (owner_id, content_hash)":
                (PostgresError(POSTGRES_UNIQUE_TEXT, CONTENT_UNIQUE_CONSTRAINT), True),
            "sqlite: unique (owner_id, content_hash)":
                (sqlite3.IntegrityError("UNIQUE constraint failed: papers.owner_id, papers.content_hash"), True),
            "postgres: primary key":
                (PostgresError('duplicate key value violates unique constraint "papers_pkey"', "papers_pkey"), False),
            "postgres: foreign key":
                (PostgresError(POSTGRES_FOREIGN_KEY_TEXT, "papers_owner_id_fkey"), False),
            "postgres: not null":
                (PostgresError(POSTGRES_NOT_NULL_TEXT, None), False),
            # Postgres names the constraint; its message alone never decides.
            "postgres: another constraint, with a message naming ours":
                (PostgresError(POSTGRES_UNIQUE_TEXT, "papers_other_key"), False),
            "sqlite: primary key":
                (sqlite3.IntegrityError("UNIQUE constraint failed: papers.id"), False),
            "sqlite: not null":
                (sqlite3.IntegrityError("NOT NULL constraint failed: papers.title"), False),
            "sqlite: another table's unique":
                (sqlite3.IntegrityError("UNIQUE constraint failed: chat_sessions.owner_id"), False),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(case=name):
                self.assertIs(_is_duplicate_file(self.integrity_error(original)), expected)

    def insert_raises(self, original, before_raising=None):
        """/upload's session raises `original`, wrapped as SQLAlchemy does,
        when it commits a new paper row; every other commit is real."""
        def commit_or_fail(session):
            if any(isinstance(obj, Paper) for obj in session.new):
                if before_raising is not None:
                    before_raising()
                raise IntegrityError("INSERT INTO papers ...", {}, original)
            Session.commit(session)

        failing = type("FailingInsertSession", (Session,), {"commit": commit_or_fail})
        maker = sessionmaker(bind=self.SessionLocal.kw["bind"], class_=failing,
                             autoflush=False, autocommit=False)

        def override_db():
            session = maker()
            try:
                yield session
            finally:
                session.close()

        from app.main import app

        app.dependency_overrides[get_db_session] = override_db

    def post(self, content):
        return self.client.post("/upload", headers={"Origin": ORIGIN},
                                files={"file": ("paper.pdf", content, "application/pdf")})

    def test_an_unrelated_integrity_error_is_a_sanitized_fault_not_a_duplicate(self):
        for name, original in (
            ("postgres foreign key", PostgresError(POSTGRES_FOREIGN_KEY_TEXT, "papers_owner_id_fkey")),
            ("postgres not null", PostgresError(POSTGRES_NOT_NULL_TEXT, None)),
            ("sqlite not null", sqlite3.IntegrityError("NOT NULL constraint failed: papers.title")),
        ):
            with self.subTest(error=name):
                content = self.content(f"fault-{name}")
                self.insert_raises(original)

                with self.assertLogs("app.api.upload", level="WARNING"):
                    resp = self.post(content)

                self.assertEqual(resp.status_code, 500, resp.text)
                self.assertEqual(resp.json(), {"detail": UPLOAD_NOT_SAVED_DETAIL})
                self.assertEqual(resp.headers.get("access-control-allow-origin"), ORIGIN)
                for word in ("papers_owner_id_fkey", "users", "not-null", "NOT NULL", "papers.title"):
                    self.assertNotIn(word, resp.text)
                self.assertIsNone(self.paper_with(content))
                self.assertEqual(self.storage.writes, [])

    def test_an_unrelated_integrity_error_is_logged_by_driver_type_only(self):
        """One warning, a fixed operation and the driver exception's type:
        never the message, the SQL or the values."""
        for name, original, expected in (
            ("postgres foreign key", PostgresError(POSTGRES_FOREIGN_KEY_TEXT, "papers_owner_id_fkey"),
             "database upload row insert failed: type=PostgresError"),
            ("postgres not null", PostgresError(POSTGRES_NOT_NULL_TEXT, None),
             "database upload row insert failed: type=PostgresError"),
            ("sqlite not null", sqlite3.IntegrityError("NOT NULL constraint failed: papers.title"),
             "database upload row insert failed: type=IntegrityError"),
        ):
            with self.subTest(error=name):
                self.insert_raises(original)

                with self.assertLogs(level="DEBUG") as captured:
                    resp = self.post(self.content(f"logged-{name}"))

                self.assertEqual(resp.status_code, 500, resp.text)
                self.assertEqual(
                    [r.getMessage() for r in captured.records if r.name == "app.api.upload"],
                    [expected],
                )
                logged = "\n".join(
                    f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in captured.records
                )
                for word in (str(original), "violates", "papers_owner_id_fkey", "users",
                             "not-null", "NOT NULL", "papers.title", "INSERT INTO", OWNER_A):
                    self.assertNotIn(word, logged)
                for record in captured.records:
                    # A traceback would print the message and the SQL.
                    self.assertIsNone(record.exc_info, record.getMessage())

    def test_the_duplicate_race_is_not_logged_as_a_fault(self):
        import logging

        content = self.content("pg-duplicate-quiet")
        content_hash = hashlib.sha256(content).hexdigest()
        self.insert_raises(
            PostgresError(POSTGRES_UNIQUE_TEXT, CONTENT_UNIQUE_CONSTRAINT),
            before_raising=lambda: self.seed(status="uploaded", content_hash=content_hash, vectors=False),
        )

        with patch.object(logging.getLogger("app.api.upload"), "warning") as warning:
            resp = self.post(content)

        self.assertEqual(resp.status_code, 200, resp.text)
        warning.assert_not_called()

    def test_the_logged_type_is_the_drivers_for_a_wrapped_error(self):
        from app.db.errors import database_failure_type

        wrapped = self.integrity_error(PostgresError(POSTGRES_FOREIGN_KEY_TEXT, "papers_owner_id_fkey"))
        self.assertEqual(database_failure_type(wrapped), "PostgresError")
        self.assertEqual(database_failure_type(RuntimeError("anything")), "RuntimeError")

    def test_the_files_unique_conflict_answers_as_a_duplicate(self):
        """Postgres reports the conflict by constraint name. The other
        request's row is committed in the moment before ours fails."""
        content = self.content("pg-duplicate")
        content_hash = hashlib.sha256(content).hexdigest()
        other = {}

        def the_other_request_inserts_first():
            other["paper"] = self.seed(status="uploaded", content_hash=content_hash, vectors=False)

        self.insert_raises(PostgresError(POSTGRES_UNIQUE_TEXT, CONTENT_UNIQUE_CONSTRAINT),
                           before_raising=the_other_request_inserts_first)

        resp = self.post(content)

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["paper_id"], other["paper"])
        self.assertIs(resp.json()["duplicate"], True)
        self.assertEqual(self.storage.writes, [])

    def test_a_unique_conflict_whose_row_is_not_visible_yet_is_an_upload_in_progress(self):
        self.insert_raises(PostgresError(POSTGRES_UNIQUE_TEXT, CONTENT_UNIQUE_CONSTRAINT))

        resp = self.post(self.content("pg-race"))

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json(), {"detail": UPLOAD_IN_PROGRESS_DETAIL})


# ---------------------------------------------------------------------------
# Owner isolation and duplicate titles
# ---------------------------------------------------------------------------
class TestOwnerIsolation(LifecycleTestCase):

    def test_no_claim_or_claimed_write_reaches_another_owners_paper(self):
        theirs = uuid.UUID(self.seed(owner_id=OWNER_B, status="uploaded"))
        before = self.row(str(theirs))
        me = uuid.UUID(OWNER_A)
        token = self.clock.now()  # any token: the owner predicate fails first

        with self.SessionLocal() as db:
            self.assertIsNone(paper_lifecycle.claim_for_deletion(db, theirs, me))
            self.assertIsNone(paper_lifecycle.claim(db, theirs, me, from_status="uploaded", to_status="indexing"))
            self.assertIsNone(paper_lifecycle.renew(db, theirs, me, "uploaded", token))
            self.assertFalse(paper_lifecycle.transition(db, theirs, me, "uploaded", token, to_status="failed"))
            self.assertFalse(paper_lifecycle.finish_deletion(db, theirs, me, token))
            self.assertIsNone(paper_lifecycle.current_status(db, theirs, me))

        after = self.row(str(theirs))
        self.assertEqual((after.status, after.updated_at), (before.status, before.updated_at))

    def test_another_owner_cannot_delete_or_finish_a_paper_by_id(self):
        theirs = self.seed(owner_id=OWNER_B)
        resp = self.delete(theirs)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.row(theirs).status, "indexed")
        self.assertEqual(self.storage.calls, [])

    def test_same_titled_papers_are_claimed_independently(self):
        doomed = self.seed(title="paper.pdf", status="uploaded")
        twin = self.seed(title="paper.pdf", status="uploaded")
        results = {}
        self.steps.on(f"claim:indexing:{doomed}", self.later(results, "delete", lambda: self.delete(doomed)))

        run = self.index()

        self.assertEqual(results["delete"].status_code, 200)
        self.assertEqual(run.json()["papers_indexed"], 1)
        self.assertEqual(self.row(twin).status, "indexed")
        self.assertEqual(self.vectors(twin), VECTORS_PER_PAPER)
        self.assert_gone(doomed)
        # The title still names one paper, so the legacy route can take it.
        self.assertEqual(self.delete_by_title("paper.pdf").status_code, 200)
        self.assert_gone(twin)
        self.assert_no_orphans()


# ---------------------------------------------------------------------------
# No orphan, whichever way an index run and a deletion interleave
# ---------------------------------------------------------------------------
class TestNoOrphanedVectorsOrPdfs(LifecycleTestCase):

    def test_every_interleaving_leaves_no_orphan(self):
        index_steps = ("claim:indexing", "index:fetch", "index:embed", "index:before_write", "index:after_write")
        delete_steps = ("claim:deleting", "qdrant:scroll", "storage:delete")
        for step in index_steps:
            with self.subTest(index_paused_at=step):
                paper = self.seed(status="uploaded")
                self.steps.on(f"{step}:{paper}", lambda p=paper: self.delete(p))
                self.index()
                self.delete(paper)  # whatever the interleaving refused
                self.assert_gone(paper)
                self.assert_no_orphans()
        for step in delete_steps:
            with self.subTest(delete_paused_at=step):
                paper = self.seed(status="uploaded")
                self.steps.on(f"{step}:{paper}", self.index)
                self.assertEqual(self.delete(paper).status_code, 200)
                self.index()
                self.assert_gone(paper)
                self.assert_no_orphans()


if __name__ == "__main__":
    unittest.main()
