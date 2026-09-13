"""
Regression tests for the Phase 2 security-audit blocker: app.memory's
global module state was shared across every authenticated user. These
tests prove the fix — owner_id-keyed isolation — holds, both at the
store level directly and through the real /ask-stream endpoint.

Fully mocked at the HTTP level: no real Voyage, Qdrant, or Groq calls.
"""
import os
import sys
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from fastapi.testclient import TestClient

from app.core.auth import get_current_owner_id
from app.memory import UserMemoryStore, get_user_memory, _store as module_store
from tests.sqlite_harness import attach_sqlite_db

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


class TestUserMemoryStoreIsolation(unittest.TestCase):
    """Unit-level: directly against the store, no HTTP involved."""

    def test_user_b_starts_with_clean_state(self):
        store = UserMemoryStore()
        mem_a = store.get(USER_A)
        mem_a.last_summary = "User A's private summary"
        mem_a.last_citations.append({"paper": "confidential"})
        mem_a.last_research_report = "User A's private report"

        mem_b = store.get(USER_B)
        self.assertEqual(mem_b.last_summary, "")
        self.assertEqual(mem_b.last_citations, [])
        self.assertEqual(mem_b.last_research_report, "")

    def test_user_a_state_not_observable_by_user_b(self):
        store = UserMemoryStore()
        store.get(USER_A).last_summary = "User A's summary"
        store.get(USER_B).last_summary = "User B's summary"

        self.assertEqual(store.get(USER_A).last_summary, "User A's summary")
        self.assertEqual(store.get(USER_B).last_summary, "User B's summary")

    def test_same_owner_gets_the_same_object_across_calls(self):
        """The SAME owner's second request must see the state their
        first request wrote. Chat state has moved to Postgres, but
        summary/comparison/report scratch state still relies on this."""
        store = UserMemoryStore()
        first_call = store.get(USER_A)
        first_call.last_summary = "explainable AI"

        second_call = store.get(USER_A)
        self.assertEqual(second_call.last_summary, "explainable AI")
        self.assertIs(first_call, second_call)

    def test_concurrent_access_from_different_owners_does_not_cross_contaminate(self):
        store = UserMemoryStore()
        errors = []

        def worker(owner_id, marker):
            try:
                for i in range(200):
                    mem = store.get(owner_id)
                    mem.last_summary = f"{marker}-{i}"
                    if not mem.last_summary.startswith(marker):
                        errors.append(f"cross-contamination detected: {mem.last_summary}")
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=worker, args=(USER_A, "A")),
            threading.Thread(target=worker, args=(USER_B, "B")),
            threading.Thread(target=worker, args=(USER_A, "A")),
            threading.Thread(target=worker, args=(USER_B, "B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Isolation violated under concurrency: {errors}")

    def test_module_level_store_is_keyed_by_owner_id_only(self):
        module_store.clear(USER_A)
        mem1 = get_user_memory(USER_A)
        mem2 = get_user_memory(USER_A)
        self.assertIs(mem1, mem2)
        module_store.clear(USER_A)

    def test_empty_owner_id_rejected(self):
        store = UserMemoryStore()
        with self.assertRaises(ValueError):
            store.get("")


def _make_hit(paper="Paper A", text="relevant content"):
    hit = MagicMock()
    hit.payload = {
        "paper": paper, "text": text, "chunk_id": 0, "page": 1,
        "source": "a.pdf", "paper_id": "p1",
    }
    return hit


class TestAskStreamMemoryIsolation(unittest.TestCase):
    """Integration-level: through the real /ask-stream endpoint."""

    def setUp(self):
        from app.main import app
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        self._current_owner = USER_A
        self.app.dependency_overrides[get_current_owner_id] = lambda: self._current_owner

        # /ask-stream now reads its history and topic from the database,
        # so these isolation assertions run against a real (in-memory)
        # one. Without it the endpoint would reach the live Supabase
        # project; with the persistence functions merely stubbed out, the
        # follow-up tests below could not pass at all.
        attach_sqlite_db(self)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        module_store.clear(USER_A)
        module_store.clear(USER_B)

    def _authenticate_as(self, owner_id):
        self._current_owner = owner_id

    def _ask(self, question):
        return self.client.get("/ask-stream", params={"question": question})

    @patch("app.api.ask_stream._groq_client")
    @patch("app.api.ask_stream.rerank_results")
    @patch("app.api.ask_stream.client")
    @patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024)
    def test_user_b_does_not_inherit_user_as_topic_or_history(
        self, mock_encode, mock_qdrant, mock_rerank, mock_groq
    ):
        hit = _make_hit()
        mock_qdrant.query_points.return_value.points = [hit]
        mock_rerank.return_value = [hit]
        mock_groq.chat.completions.create.return_value = iter([])

        # User A asks a first, non-follow-up question — establishes topic.
        self._authenticate_as(USER_A)
        self._ask("What is explainable AI?")

        mock_encode.reset_mock()

        # User B — who has NEVER asked anything — asks a question
        # containing a follow-up word ("it"). If isolation were broken,
        # User B would inherit User A's chat_history/current_topic and
        # this would incorrectly be treated as a follow-up, prepending
        # User A's topic to User B's query.
        self._authenticate_as(USER_B)
        self._ask("Explain it simply")

        search_query_used = mock_encode.call_args.args[0]
        self.assertEqual(search_query_used, "Explain it simply")
        self.assertNotIn("explainable AI", search_query_used.lower())

    @patch("app.api.ask_stream._groq_client")
    @patch("app.api.ask_stream.rerank_results")
    @patch("app.api.ask_stream.client")
    @patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024)
    def test_user_a_followup_still_uses_their_own_prior_topic(
        self, mock_encode, mock_qdrant, mock_rerank, mock_groq
    ):
        hit = _make_hit()
        mock_qdrant.query_points.return_value.points = [hit]
        mock_rerank.return_value = [hit]
        mock_groq.chat.completions.create.return_value = iter([])

        self._authenticate_as(USER_A)
        self._ask("What is explainable AI?")

        mock_encode.reset_mock()
        self._ask("What are its limitations?")

        search_query_used = mock_encode.call_args.args[0]
        self.assertIn("What is explainable AI?", search_query_used)

    @patch("app.api.ask_stream._groq_client")
    @patch("app.api.ask_stream.rerank_results")
    @patch("app.api.ask_stream.client")
    @patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024)
    def test_sequential_requests_from_two_owners_do_not_cross_contaminate(
        self, mock_encode, mock_qdrant, mock_rerank, mock_groq
    ):
        hit = _make_hit()
        mock_qdrant.query_points.return_value.points = [hit]
        mock_rerank.return_value = [hit]
        mock_groq.chat.completions.create.return_value = iter([])

        self._authenticate_as(USER_A)
        self._ask("Tell me about transformers")
        self._authenticate_as(USER_B)
        self._ask("Tell me about databases")
        self._authenticate_as(USER_A)
        self._ask("What about its attention mechanism?")

        search_query_used = mock_encode.call_args.args[0]
        self.assertIn("transformers", search_query_used.lower())
        self.assertNotIn("databases", search_query_used.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
