"""
D2 step 2 — /research must persist a durable, owner-scoped Report row.

Fully offline and deterministic. Qdrant, the embedder, the reranker and
the Groq-backed research_agent are all mocked, and the database is a
per-test in-memory SQLite instance reached by patching
app.db.session.get_session_factory. The REAL session_scope() runs, so
these tests exercise the actual helper rather than a stand-in.

No Voyage, Groq, Qdrant, Supabase or Postgres call happens in this file.
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
from app.db.models import Base, Report

TEST_OWNER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OTHER_OWNER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
MALICIOUS_OWNER = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _hit(paper, page, text="some passage text"):
    hit = MagicMock()
    hit.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "page": page,
        "chunk_id": 0,
        "text": text,
        "authors": "A. Author",
        "abstract": "An abstract.",
        "keywords": "kw",
    }
    return hit


def _query_points_result(hits):
    result = MagicMock()
    result.points = hits
    return result


class ReportPersistenceTestCase(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False
        )

        from app.main import app
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

        # Real session_scope(), SQLite factory underneath it.
        self._factory_patch = patch(
            "app.db.session.get_session_factory", return_value=self.SessionLocal
        )
        self._factory_patch.start()
        self.addCleanup(self._factory_patch.stop)

        self._owners_used = set()

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()
        # research.py still writes owner-scoped .txt files as a fallback
        # for export_report.py; clean them up so the suite leaves nothing.
        for owner in self._owners_used:
            for fname in (
                f"latest_report_{owner}.txt",
                f"latest_sources_{owner}.txt",
            ):
                if os.path.exists(fname):
                    os.remove(fname)

    def authenticate_as(self, owner_id):
        self._owners_used.add(owner_id)
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def stored_reports(self):
        session = self.SessionLocal()
        try:
            return session.query(Report).all()
        finally:
            session.close()


class TestReportRowIsPersisted(ReportPersistenceTestCase):

    @patch("app.api.research.research_agent", return_value="# Findings\n\nBody.")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_creates_one_row_with_query_markdown_and_citations(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        mock_client.query_points.return_value = _query_points_result(
            [_hit("Attention Is All You Need", 3), _hit("BERT", 7)]
        )
        self.authenticate_as(TEST_OWNER)

        resp = self.client.get("/research", params={"query": "transformers"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

        rows = self.stored_reports()
        self.assertEqual(len(rows), 1, "exactly one Report row expected")

        row = rows[0]
        self.assertEqual(row.owner_id, uuid.UUID(TEST_OWNER))
        self.assertEqual(row.query, "transformers")
        self.assertEqual(row.report_markdown, "# Findings\n\nBody.")
        self.assertIsNotNone(row.created_at)

        self.assertEqual(
            row.citations,
            [
                {"paper": "Attention Is All You Need",
                 "source": "Attention Is All You Need.pdf", "page": 3},
                {"paper": "BERT", "source": "BERT.pdf", "page": 7},
            ],
            "persisted citations must match the citations returned to the client",
        )

    @patch("app.api.research.research_agent", return_value="report body")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_persisted_citations_match_the_response_exactly(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        """The stored row is what export will later render, so it must not
        drift from what the caller was shown."""
        mock_client.query_points.return_value = _query_points_result(
            [_hit("Paper A", 1), _hit("Paper B", 2)]
        )
        self.authenticate_as(TEST_OWNER)

        body = self.client.get("/research", params={"query": "q"}).json()

        self.assertEqual(self.stored_reports()[0].citations, body["citations"])

    @patch("app.api.research.research_agent", return_value="report with no sources")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_persists_even_when_retrieval_returned_nothing(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        mock_client.query_points.return_value = _query_points_result([])
        self.authenticate_as(TEST_OWNER)

        self.client.get("/research", params={"query": "obscure topic"})

        rows = self.stored_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].citations, [])

    @patch("app.api.research.research_agent", return_value="body")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_each_call_inserts_a_new_row_rather_than_overwriting(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        mock_client.query_points.return_value = _query_points_result([_hit("P", 1)])
        self.authenticate_as(TEST_OWNER)

        self.client.get("/research", params={"query": "first"})
        self.client.get("/research", params={"query": "second"})

        rows = self.stored_reports()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.query for r in rows}, {"first", "second"})


class TestReportOwnership(ReportPersistenceTestCase):

    @patch("app.api.research.research_agent", return_value="body")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_owner_id_comes_from_jwt_not_client_input(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        """A caller must not be able to attribute a report to another user
        by smuggling an owner_id through the query string."""
        mock_client.query_points.return_value = _query_points_result([_hit("P", 1)])
        self.authenticate_as(TEST_OWNER)

        self.client.get(
            "/research",
            params={
                "query": "q",
                "owner_id": MALICIOUS_OWNER,
                "user_id": MALICIOUS_OWNER,
            },
        )

        rows = self.stored_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_id, uuid.UUID(TEST_OWNER))
        self.assertNotEqual(rows[0].owner_id, uuid.UUID(MALICIOUS_OWNER))

    @patch("app.api.research.research_agent", return_value="body")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_two_users_reports_are_attributed_separately(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        mock_client.query_points.return_value = _query_points_result([_hit("P", 1)])

        self.authenticate_as(TEST_OWNER)
        self.client.get("/research", params={"query": "owner one topic"})

        self.authenticate_as(OTHER_OWNER)
        self.client.get("/research", params={"query": "owner two topic"})

        rows = self.stored_reports()
        self.assertEqual(len(rows), 2)

        by_owner = {r.owner_id: r.query for r in rows}
        self.assertEqual(by_owner[uuid.UUID(TEST_OWNER)], "owner one topic")
        self.assertEqual(by_owner[uuid.UUID(OTHER_OWNER)], "owner two topic")

    def test_unauthenticated_request_persists_nothing(self):
        resp = self.client.get("/research", params={"query": "q"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.stored_reports(), [])


class TestReportPersistenceFailureHandling(ReportPersistenceTestCase):

    @patch("app.api.research.research_agent", return_value="body")
    @patch("app.api.research.rerank_results", side_effect=lambda q, r: r)
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_write_failure_is_reported_not_silently_swallowed(
        self, mock_encode, mock_client, mock_rerank, mock_agent
    ):
        """If the row cannot be stored, the caller must not be told the
        research succeeded — from the next step onward Postgres is what
        export reads, so an unpersisted report is an invisible one."""
        mock_client.query_points.return_value = _query_points_result([_hit("P", 1)])
        self.authenticate_as(TEST_OWNER)

        with patch(
            "app.api.research.session_scope",
            side_effect=RuntimeError("database unavailable"),
        ):
            body = self.client.get("/research", params={"query": "q"}).json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "internal_error")
        self.assertEqual(self.stored_reports(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
