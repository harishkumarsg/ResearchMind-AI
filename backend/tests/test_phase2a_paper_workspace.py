"""
Phase 2A — paper workspace backend: private PDF delivery and
paper-scoped ask.

Two new surfaces, both of which could leak across tenants if the
ownership boundary were placed wrongly:

  * GET /paper-file streams bytes that storage.fetch_pdf() reads with the
    SERVICE-ROLE client, which bypasses RLS. That function was written
    for the indexing job and is safe there because no client chooses its
    arguments. Exposed over HTTP it would read any owner's object, so
    the endpoint resolves the paper against Postgres — id AND owner_id
    from the verified JWT — before calling it.

  * /ask-stream now accepts an optional paper_id. It must NARROW
    retrieval, never widen it: the unconditional owner_id filter stays,
    and the paper term is appended to it.

A paper that does not exist and a paper belonging to someone else are
deliberately indistinguishable, so neither response can be used to probe
another owner's library.

Fully offline: Supabase Storage, Qdrant and Groq are all mocked; the
database is in-memory SQLite.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_owner_id
from app.db.models import Base, Paper
from app.memory import _store as module_store

OWNER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

PAPER_A = "11111111-1111-4111-8111-111111111111"
PAPER_B = "22222222-2222-4222-8222-222222222222"
MISSING = "33333333-3333-4333-8333-333333333333"

PDF_A = b"%PDF-1.4 OWNER A PRIVATE DOCUMENT"
PDF_B = b"%PDF-1.4 OWNER B PRIVATE DOCUMENT"


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

        patcher = patch(
            "app.db.session.get_session_factory", return_value=self.SessionLocal
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

        from app.main import app

        self.app = app
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

        self.seed_paper(OWNER_A, PAPER_A, "owner_a_paper.pdf")
        self.seed_paper(OWNER_B, PAPER_B, "owner_b_paper.pdf")

    def tearDown(self):
        module_store.clear(OWNER_A)
        module_store.clear(OWNER_B)

    def authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def seed_paper(self, owner_id, paper_id, title):
        session = self.SessionLocal()
        try:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="h" * 64,
                    storage_path=f"{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()
        finally:
            session.close()

    def get_file(self, paper_id, pdf_bytes=PDF_A):
        with patch(
            "app.api.paper_file.fetch_pdf", return_value=pdf_bytes
        ) as fetch:
            resp = self.client.get("/paper-file", params={"paper_id": paper_id})
        return resp, fetch


# ======================================================================
# 1. /paper-file — the owner, and only the owner
# ======================================================================
class TestPaperFileOwnership(WorkspaceTestCase):
    def test_an_owner_receives_their_own_pdf(self):
        self.authenticate_as(OWNER_A)

        resp, fetch = self.get_file(PAPER_A)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertEqual(resp.content, PDF_A)
        fetch.assert_called_once_with(owner_id=OWNER_A, paper_id=PAPER_A)

    def test_another_owners_pdf_is_never_fetched(self):
        # Owner B asks for Owner A's paper id.
        self.authenticate_as(OWNER_B)

        resp, fetch = self.get_file(PAPER_A)

        self.assertEqual(resp.status_code, 404)
        # The decisive assertion: the service-role download never ran, so
        # the bytes were not merely withheld — they were never read.
        fetch.assert_not_called()
        self.assertNotIn(b"OWNER A PRIVATE", resp.content)

    def test_a_missing_paper_and_a_foreign_paper_are_indistinguishable(self):
        self.authenticate_as(OWNER_B)

        foreign = self.client.get("/paper-file", params={"paper_id": PAPER_A})
        missing = self.client.get("/paper-file", params={"paper_id": MISSING})

        self.assertEqual(foreign.status_code, missing.status_code)
        self.assertEqual(foreign.json(), missing.json())

    def test_a_malformed_paper_id_is_a_clean_404(self):
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/paper-file", params={"paper_id": "not-a-uuid"})

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("not-a-uuid", resp.text)
        for leak in ("traceback", "valueerror", "uuid"):
            self.assertNotIn(leak, resp.text.lower())

    def test_an_unauthenticated_request_is_rejected(self):
        resp = self.client.get("/paper-file", params={"paper_id": PAPER_A})

        self.assertEqual(resp.status_code, 401)

    def test_a_client_supplied_owner_id_is_not_authoritative(self):
        self.authenticate_as(OWNER_B)

        resp, fetch = self.get_file(PAPER_A)

        # Even spelled out in the query string, it must not be honoured.
        resp2 = self.client.get(
            "/paper-file", params={"paper_id": PAPER_A, "owner_id": OWNER_A}
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp2.status_code, 404)
        fetch.assert_not_called()

    def test_the_storage_path_is_never_exposed(self):
        self.authenticate_as(OWNER_A)

        resp, _ = self.get_file(PAPER_A)

        for leak in ("storage", "bucket", "supabase", "original.pdf", "http"):
            self.assertNotIn(leak, str(resp.headers).lower())

    def test_the_response_is_marked_private_and_not_cached(self):
        self.authenticate_as(OWNER_A)

        resp, _ = self.get_file(PAPER_A)

        self.assertIn("private", resp.headers["cache-control"])
        self.assertIn("no-store", resp.headers["cache-control"])

    def test_a_storage_failure_returns_a_neutral_error(self):
        self.authenticate_as(OWNER_A)

        with patch(
            "app.api.paper_file.fetch_pdf",
            side_effect=RuntimeError("bucket papers/... 403 from https://x.supabase.co"),
        ):
            resp = self.client.get("/paper-file", params={"paper_id": PAPER_A})

        self.assertEqual(resp.status_code, 502)
        for leak in ("bucket", "supabase", "403", "traceback"):
            self.assertNotIn(leak, resp.text.lower())


# ======================================================================
# 2. /ask-stream paper scope — narrows, never widens
# ======================================================================
class TestPaperScopedAsk(WorkspaceTestCase):
    def ask(self, paper_id=None):
        params = {"question": "what is the method?"}
        if paper_id is not None:
            params["paper_id"] = paper_id

        with patch("app.api.ask_stream.encode_query", return_value=[0.1]), patch(
            "app.api.ask_stream.client"
        ) as qdrant, patch(
            "app.api.ask_stream.rerank_results", side_effect=lambda q, r: r
        ):
            qdrant.query_points.return_value.points = []
            resp = self.client.get("/ask-stream", params=params)
            resp.read()
        return resp, qdrant

    def filter_conditions(self, qdrant):
        flt = qdrant.query_points.call_args.kwargs["query_filter"]
        return {c.key: c.match.value for c in flt.must}

    def test_an_unscoped_ask_is_unchanged(self):
        self.authenticate_as(OWNER_A)

        resp, qdrant = self.ask()

        self.assertEqual(resp.status_code, 200)
        conditions = self.filter_conditions(qdrant)
        self.assertEqual(conditions, {"owner_id": OWNER_A})

    def test_a_scoped_ask_adds_the_paper_and_keeps_the_owner(self):
        self.authenticate_as(OWNER_A)

        resp, qdrant = self.ask(PAPER_A)

        self.assertEqual(resp.status_code, 200)
        conditions = self.filter_conditions(qdrant)
        # BOTH must hold — the paper term is appended, not substituted.
        self.assertEqual(conditions["owner_id"], OWNER_A)
        self.assertEqual(conditions["paper_id"], PAPER_A)

    def test_the_owner_filter_is_never_replaced_by_the_paper_filter(self):
        self.authenticate_as(OWNER_A)

        _, qdrant = self.ask(PAPER_A)

        flt = qdrant.query_points.call_args.kwargs["query_filter"]
        keys = [c.key for c in flt.must]
        self.assertIn("owner_id", keys)
        self.assertEqual(len(flt.must), 2)

    def test_owner_b_cannot_scope_to_owner_as_paper(self):
        self.authenticate_as(OWNER_B)

        resp, qdrant = self.ask(PAPER_A)

        self.assertEqual(resp.status_code, 404)
        # Rejected before any retrieval happened at all.
        qdrant.query_points.assert_not_called()

    def test_a_missing_paper_and_a_foreign_paper_look_the_same(self):
        self.authenticate_as(OWNER_B)

        foreign, _ = self.ask(PAPER_A)
        missing, _ = self.ask(MISSING)

        self.assertEqual(foreign.status_code, missing.status_code)
        self.assertEqual(foreign.json(), missing.json())

    def test_a_malformed_paper_id_is_rejected_safely(self):
        self.authenticate_as(OWNER_A)

        resp, qdrant = self.ask("not-a-uuid")

        self.assertEqual(resp.status_code, 404)
        qdrant.query_points.assert_not_called()
        self.assertNotIn("not-a-uuid", resp.text)

    def test_each_owner_can_scope_to_their_own_paper(self):
        self.authenticate_as(OWNER_A)
        _, qa = self.ask(PAPER_A)
        self.assertEqual(self.filter_conditions(qa)["paper_id"], PAPER_A)

        self.authenticate_as(OWNER_B)
        _, qb = self.ask(PAPER_B)
        self.assertEqual(self.filter_conditions(qb)["paper_id"], PAPER_B)

    def test_no_cross_paper_retrieval_when_a_scope_is_supplied(self):
        # Owner A owns two papers; scoping to one must constrain the query
        # to that one, not merely rank it higher.
        self.seed_paper(OWNER_A, MISSING, "owner_a_second.pdf")
        self.authenticate_as(OWNER_A)

        _, qdrant = self.ask(MISSING)

        conditions = self.filter_conditions(qdrant)
        self.assertEqual(conditions["paper_id"], MISSING)
        self.assertNotEqual(conditions["paper_id"], PAPER_A)


if __name__ == "__main__":
    unittest.main()
