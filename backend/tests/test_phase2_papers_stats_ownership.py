"""
Ownership + auth tests for /papers and /stats.

These two endpoints were the last surface still reading the pre-Phase-1.5
local uploads/papers directory and an unfiltered whole-collection Qdrant
count — confirmed via manual testing to leak the same paper list/count to
every signed-in account regardless of identity. This suite confirms the
fix: unauthenticated access is rejected, one owner never sees another
owner's papers or stats, and a brand-new owner with no papers gets an
empty/zero response.
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
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_owner_id
from app.db.models import Base, Paper
from app.db.session import get_db_session

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NEW_USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _new_sqlite_engine_and_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine, factory


class PapersStatsOwnershipTestCase(unittest.TestCase):

    def setUp(self):
        self._engine, self.SessionLocal = _new_sqlite_engine_and_session_factory()

        from app.main import app
        self.app = app

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        # Always override the DB session, including for the unauthenticated
        # tests below — they must be rejected by the auth dependency before
        # ever reaching the database, but this guarantees no test can ever
        # touch a real database regardless of FastAPI's dependency-solving
        # order.
        self.app.dependency_overrides[get_db_session] = override_db

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._engine.dispose()

    def _authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def _seed_paper(self, owner_id, title, status="indexed"):
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=uuid.uuid4(),
                owner_id=uuid.UUID(owner_id),
                title=title,
                content_hash="a" * 64,
                storage_path=f"{owner_id}/x/original.pdf",
                file_size_bytes=100,
                status=status,
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()


class TestPapersOwnership(PapersStatsOwnershipTestCase):

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/papers")
        self.assertEqual(resp.status_code, 401)

    def test_account_a_sees_only_its_own_papers(self):
        self._seed_paper(USER_A, "A_Paper_One")
        self._seed_paper(USER_A, "A_Paper_Two")
        self._seed_paper(USER_B, "B_Confidential_Paper")

        self._authenticate_as(USER_A)
        resp = self.client.get("/papers")

        self.assertEqual(resp.status_code, 200)
        papers = resp.json()["papers"]
        self.assertEqual(sorted(papers), ["A_Paper_One", "A_Paper_Two"])
        self.assertNotIn("B_Confidential_Paper", papers)

    def test_account_b_sees_only_its_own_papers(self):
        self._seed_paper(USER_A, "A_Paper_One")
        self._seed_paper(USER_B, "B_Paper_One")

        self._authenticate_as(USER_B)
        resp = self.client.get("/papers")

        self.assertEqual(resp.json()["papers"], ["B_Paper_One"])

    def test_new_owner_sees_zero_papers(self):
        self._seed_paper(USER_A, "A_Paper_One")

        self._authenticate_as(NEW_USER)
        resp = self.client.get("/papers")

        self.assertEqual(resp.json()["papers"], [])

    def test_not_yet_indexed_papers_are_excluded(self):
        self._seed_paper(USER_A, "Still_Uploading", status="uploaded")
        self._seed_paper(USER_A, "Fully_Indexed", status="indexed")

        self._authenticate_as(USER_A)
        resp = self.client.get("/papers")

        self.assertEqual(resp.json()["papers"], ["Fully_Indexed"])

    def test_client_supplied_owner_id_cannot_override(self):
        self._seed_paper(USER_A, "A_Paper_One")
        self._authenticate_as(USER_B)

        # The route doesn't even declare such a parameter, so this can't
        # bind to anything — confirms the server-derived identity is what
        # actually governs the result regardless.
        resp = self.client.get("/papers", params={"owner_id": USER_A, "user_id": USER_A})

        self.assertEqual(resp.json()["papers"], [])


class TestStatsOwnership(PapersStatsOwnershipTestCase):

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/stats")
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.stats.client")
    def test_stats_are_owner_scoped(self, mock_qdrant):
        self._seed_paper(USER_A, "A_Paper_One")
        self._seed_paper(USER_A, "A_Paper_Two")
        self._seed_paper(USER_B, "B_Paper_One")

        count_result = MagicMock()
        count_result.count = 42
        mock_qdrant.count.return_value = count_result

        self._authenticate_as(USER_A)
        resp = self.client.get("/stats")

        body = resp.json()
        self.assertEqual(body["total_papers"], 2)
        self.assertEqual(body["total_chunks"], 42)
        self.assertEqual(sorted(body["recent_papers"]), ["A_Paper_One", "A_Paper_Two"])

        kwargs = mock_qdrant.count.call_args.kwargs
        count_filter = kwargs["count_filter"]
        self.assertIsInstance(count_filter, Filter)
        matching = [
            c for c in count_filter.must
            if isinstance(c, FieldCondition)
            and c.key == "owner_id"
            and isinstance(c.match, MatchValue)
            and c.match.value == USER_A
        ]
        self.assertTrue(matching, f"No owner_id={USER_A!r} filter found: {count_filter!r}")

    @patch("app.api.stats.client")
    def test_new_owner_gets_zero_stats(self, mock_qdrant):
        self._seed_paper(USER_A, "A_Paper_One")

        count_result = MagicMock()
        count_result.count = 0
        mock_qdrant.count.return_value = count_result

        self._authenticate_as(NEW_USER)
        resp = self.client.get("/stats")

        body = resp.json()
        self.assertEqual(body["total_papers"], 0)
        self.assertEqual(body["total_chunks"], 0)
        self.assertEqual(body["recent_papers"], [])

    @patch("app.api.stats.client")
    def test_qdrant_outage_does_not_break_paper_stats(self, mock_qdrant):
        mock_qdrant.count.side_effect = RuntimeError("qdrant unreachable")
        self._seed_paper(USER_A, "A_Paper_One")

        self._authenticate_as(USER_A)
        resp = self.client.get("/stats")

        body = resp.json()
        self.assertEqual(body["total_papers"], 1)
        self.assertEqual(body["total_chunks"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
