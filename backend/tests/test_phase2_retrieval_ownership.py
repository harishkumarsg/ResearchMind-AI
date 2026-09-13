"""
Phase 2 tests: every retrieval endpoint must supply a server-constructed,
unconditional owner_id filter to Qdrant, derived only from the verified
JWT identity — never from client-supplied data.

Fully mocked: embedder.encode_query, the Qdrant client, and (for
research.py specifically) research_agent are all mocked. No real Voyage
call, no real Qdrant call, no real Groq call happens anywhere in this
file.
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

from app.core.auth import get_current_owner_id
from tests.sqlite_harness import attach_sqlite_db

TEST_OWNER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
MALICIOUS_OWNER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _empty_query_points_result():
    result = MagicMock()
    result.points = []
    return result


def _assert_owner_filter_present(query_filter, expected_owner_id):
    assert isinstance(query_filter, Filter), f"query_filter was not a Filter: {query_filter!r}"
    conditions = query_filter.must or []
    matching = [
        c for c in conditions
        if isinstance(c, FieldCondition)
        and c.key == "owner_id"
        and isinstance(c.match, MatchValue)
        and c.match.value == expected_owner_id
    ]
    assert matching, f"No owner_id={expected_owner_id!r} condition found in filter: {query_filter!r}"


class RetrievalOwnershipTestCase(unittest.TestCase):

    def setUp(self):
        from app.main import app
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id


class TestSearchOwnership(RetrievalOwnershipTestCase):

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/search", params={"query": "AI"})
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.search.client")
    @patch("app.api.search.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_matches_authenticated_identity(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get("/search", params={"query": "AI"})

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)

    @patch("app.api.search.client")
    @patch("app.api.search.encode_query", return_value=[0.1] * 1024)
    def test_client_supplied_owner_id_cannot_override(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        # Attempt to smuggle a different owner via extra query params —
        # the route doesn't even declare such a parameter, so this can't
        # bind to anything, but we verify the actual filter used anyway.
        self.client.get(
            "/search",
            params={"query": "AI", "owner_id": MALICIOUS_OWNER, "user_id": MALICIOUS_OWNER},
        )

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)
        with self.assertRaises(AssertionError):
            _assert_owner_filter_present(kwargs["query_filter"], MALICIOUS_OWNER)


class TestAskStreamOwnership(RetrievalOwnershipTestCase):

    def setUp(self):
        super().setUp()
        # ask_stream.py reads and writes chat state now. A private
        # in-memory database keeps this offline; without it the endpoint
        # would reach the live Supabase project.
        attach_sqlite_db(self)

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/ask-stream", params={"question": "What is AI?"})
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.ask_stream.client")
    @patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_matches_authenticated_identity(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get("/ask-stream", params={"question": "What is AI?"})

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)


class TestResearchOwnership(RetrievalOwnershipTestCase):

    def setUp(self):
        super().setUp()
        # research.py writes these files as a pre-existing side effect,
        # unrelated to this phase's scope — clean up afterward so this
        # test suite leaves no clutter. Filenames are owner-scoped as of
        # the memory-isolation fix, so this must match TEST_OWNER.
        self._report_files = [
            f"latest_report_{TEST_OWNER}.txt",
            f"latest_sources_{TEST_OWNER}.txt",
        ]

    def tearDown(self):
        super().tearDown()
        for fname in self._report_files:
            if os.path.exists(fname):
                os.remove(fname)

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/research", params={"query": "AI in healthcare"})
        self.assertEqual(resp.status_code, 401)

    # session_scope is patched out because this TestCase deliberately has
    # no database harness — it exists to assert the Qdrant filter, nothing
    # else. research.py now persists a Report row, and without this patch
    # the call would build a real engine from the .env DATABASE_URL loaded
    # at the top of this file and INSERT into the live Supabase database.
    # Report persistence itself is covered in test_d2_report_persistence.py
    # against in-memory SQLite.
    @patch("app.api.research.session_scope")
    @patch("app.api.research.research_agent", return_value="dummy report")
    @patch("app.api.research.client")
    @patch("app.api.research.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_matches_authenticated_identity(
        self, mock_encode, mock_client, mock_agent, mock_scope
    ):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get("/research", params={"query": "AI in healthcare"})

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)


class TestSummarizePaperOwnership(RetrievalOwnershipTestCase):

    def setUp(self):
        super().setUp()
        # /summarize-paper now records the detected paper in
        # chat_sessions. The mocked Qdrant below returns no results, so
        # the endpoint returns early and never reaches that write — but
        # relying on that would leave this suite one mock-change away
        # from writing to the live Supabase project.
        attach_sqlite_db(self)

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/summarize-paper", params={"paper_name": "Some Paper"})
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.summarize_paper.client")
    @patch("app.api.summarize_paper.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_matches_authenticated_identity(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get("/summarize-paper", params={"paper_name": "Some Paper"})

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)


class TestComparePapersOwnership(RetrievalOwnershipTestCase):

    def test_unauthenticated_rejected(self):
        resp = self.client.get(
            "/compare-papers", params={"paper1": "Paper A", "paper2": "Paper B"}
        )
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.compare_papers.client")
    @patch("app.api.compare_papers.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_applied_to_both_paper_lookups(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get(
            "/compare-papers", params={"paper1": "Paper A", "paper2": "Paper B"}
        )

        # get_paper_context returns early (no context) for paper1 given
        # empty results, so query_points is called at least once — for
        # every call made, the owner filter must be present.
        self.assertGreaterEqual(mock_client.query_points.call_count, 1)
        for call in mock_client.query_points.call_args_list:
            _assert_owner_filter_present(call.kwargs["query_filter"], TEST_OWNER)


class TestPaperDetailsOwnership(RetrievalOwnershipTestCase):

    def test_unauthenticated_rejected(self):
        resp = self.client.get("/paper-details", params={"paper_name": "Some Paper"})
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.paper_details.client")
    @patch("app.api.paper_details.encode_query", return_value=[0.1] * 1024)
    def test_owner_filter_matches_authenticated_identity(self, mock_encode, mock_client):
        mock_client.query_points.return_value = _empty_query_points_result()
        self._authenticate_as(TEST_OWNER)

        self.client.get("/paper-details", params={"paper_name": "Some Paper"})

        kwargs = mock_client.query_points.call_args.kwargs
        _assert_owner_filter_present(kwargs["query_filter"], TEST_OWNER)


class TestAskRouteFullyRemoved(unittest.TestCase):
    """Confirms the confirmed-dead-code /ask (non-streaming) endpoint was
    actually deleted, not just left unfixed."""

    def test_ask_route_returns_404(self):
        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ask", params={"question": "test"})
        self.assertEqual(resp.status_code, 404)

    def test_ask_module_file_does_not_exist(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "ask.py")
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
