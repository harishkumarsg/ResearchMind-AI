"""
GET /latest-report — restoring the owner's stored report.

/research has always written a durable Report row, but the Reports page
held that response in component state alone, so a browser refresh showed
an empty generation form while the report sat in Postgres untouched.
This endpoint is the read that was missing.

Pinned here:

  * the owner gets their own most recent report, and only their own;
  * an owner with no report gets a clean 404, not an error and not
    someone else's row;
  * the read NEVER generates. Restoring a page by re-running /research
    would bill a generation, add latency and insert a duplicate row on
    every refresh, so the absence of a provider call and the absence of
    a new row are both asserted, not assumed.

Fully offline: per-test in-memory SQLite injected through the
get_db_session dependency override. No Postgres, Qdrant, Voyage, Groq or
Supabase call happens in this file.
"""
import datetime
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import qa_agent
from app.core.auth import get_current_owner_id
from app.db.models import Base, Report
from app.db.session import get_db_session

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class LatestReportTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

        from app.main import app

        self.app = app

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[get_db_session] = override_db
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(self.engine.dispose)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def seed_report(self, owner_id, query, markdown, citations, minutes_old=0):
        session = self.SessionLocal()
        try:
            session.add(
                Report(
                    owner_id=uuid.UUID(owner_id),
                    query=query,
                    report_markdown=markdown,
                    citations=citations,
                    created_at=EPOCH + datetime.timedelta(minutes=minutes_old),
                )
            )
            session.commit()
        finally:
            session.close()

    def count_reports(self):
        session = self.SessionLocal()
        try:
            return session.query(Report).count()
        finally:
            session.close()


# ======================================================================
# 1. The owner gets their report back
# ======================================================================
class TestOwnerCanRetrieveTheirLatestReport(LatestReportTestCase):
    def test_returns_the_stored_report(self):
        citations = [{"paper": "a.pdf", "source": "a.pdf", "page": 3}]
        self.seed_report(OWNER_A, "unique novelty", "### Findings\n\nBody.", citations)
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["query"], "unique novelty")
        self.assertEqual(body["report_markdown"], "### Findings\n\nBody.")
        self.assertEqual(body["citations"], citations)

    def test_returns_the_most_recent_of_several(self):
        self.seed_report(OWNER_A, "older", "OLDER BODY", [], minutes_old=0)
        self.seed_report(OWNER_A, "newer", "NEWER BODY", [], minutes_old=10)
        self.authenticate_as(OWNER_A)

        body = self.client.get("/latest-report").json()

        self.assertEqual(body["query"], "newer")
        self.assertEqual(body["report_markdown"], "NEWER BODY")

    def test_a_null_citations_column_becomes_an_empty_list(self):
        self.seed_report(OWNER_A, "topic", "Body.", None)
        self.authenticate_as(OWNER_A)

        body = self.client.get("/latest-report").json()

        self.assertEqual(body["citations"], [])


# ======================================================================
# 2. Owner scoping
# ======================================================================
class TestCrossOwnerIsolation(LatestReportTestCase):
    def test_another_owners_report_is_not_returned(self):
        self.seed_report(OWNER_A, "owner A topic", "OWNER A SECRET BODY", [])
        self.authenticate_as(OWNER_B)

        resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("OWNER A SECRET BODY", resp.text)
        self.assertNotIn("owner A topic", resp.text)

    def test_each_owner_receives_only_their_own_latest(self):
        self.seed_report(OWNER_A, "A topic", "A BODY", [], minutes_old=0)
        self.seed_report(OWNER_B, "B topic", "B BODY", [], minutes_old=5)

        self.authenticate_as(OWNER_A)
        self.assertEqual(self.client.get("/latest-report").json()["report_markdown"], "A BODY")

        self.authenticate_as(OWNER_B)
        self.assertEqual(self.client.get("/latest-report").json()["report_markdown"], "B BODY")

    def test_unauthenticated_request_is_rejected(self):
        self.seed_report(OWNER_A, "a topic", "OWNER A BODY", [])

        resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("OWNER A BODY", resp.text)


# ======================================================================
# 3. The empty state
# ======================================================================
class TestOwnerWithNoReport(LatestReportTestCase):
    def test_returns_a_clean_404(self):
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["status"], "empty")
        self.assertIn("No report has been generated yet", body["message"])

    def test_the_empty_response_exposes_no_database_internals(self):
        self.authenticate_as(OWNER_A)

        text = self.client.get("/latest-report").text.lower()

        for leak in ("traceback", "sqlalchemy", "psycopg", "select ", "reports.owner_id"):
            self.assertNotIn(leak, text)


# ======================================================================
# 4. It is a read, and only a read
# ======================================================================
class TestRetrievalIsReadOnly(LatestReportTestCase):
    def test_no_report_row_is_created(self):
        self.seed_report(OWNER_A, "topic", "Body.", [])
        self.authenticate_as(OWNER_A)

        before = self.count_reports()
        self.client.get("/latest-report")
        self.client.get("/latest-report")

        # Re-running /research to restore a page would have inserted one
        # row per refresh. That is the defect this endpoint exists to
        # avoid, so the count is asserted rather than assumed.
        self.assertEqual(self.count_reports(), before)

    def test_an_empty_owner_still_has_no_rows_afterwards(self):
        self.authenticate_as(OWNER_A)

        self.client.get("/latest-report")

        self.assertEqual(self.count_reports(), 0)

    def test_no_provider_call_is_made(self):
        self.seed_report(OWNER_A, "topic", "Body.", [])
        self.authenticate_as(OWNER_A)

        with patch.object(
            qa_agent._groq_client.chat.completions, "create", MagicMock()
        ) as create:
            resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 200)
        create.assert_not_called()

    def test_no_retrieval_or_embedding_happens(self):
        self.seed_report(OWNER_A, "topic", "Body.", [])
        self.authenticate_as(OWNER_A)

        with patch("app.api.research.encode_query") as encode, patch(
            "app.api.research.client"
        ) as qdrant, patch("app.api.research.research_agent") as agent:
            resp = self.client.get("/latest-report")

        self.assertEqual(resp.status_code, 200)
        encode.assert_not_called()
        qdrant.query_points.assert_not_called()
        agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
