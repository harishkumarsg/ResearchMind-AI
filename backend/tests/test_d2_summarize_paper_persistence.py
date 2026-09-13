"""
D2 step 7 — /summarize-paper records the detected paper durably in
chat_sessions.current_paper_id, which /ask-stream's follow-up lock reads.

Before this step that link lived only in in-process UserMemory, so
ask_stream carried a title-based fallback to honour it. The decisive
test here is test_summarised_paper_locks_a_later_followup: it drives
summarise-then-ask across a cleared in-process cache and shows the lock
still holds.

Fully offline and deterministic: in-memory SQLite behind the real
session_scope(), with Qdrant, the embedder, the reranker and the
Groq-backed agents mocked. No Postgres, no network.
"""
import json
import os
import sys
import unittest
import uuid
from types import SimpleNamespace
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
from app.db.models import Base, ChatMessage, ChatSession, Paper
from app.memory import _store as module_store
from app.services.chat_store import set_current_paper

USER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
USER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

PAPER_A_ID = "11111111-1111-1111-1111-111111111111"
PAPER_B_ID = "22222222-2222-2222-2222-222222222222"
UNKNOWN_PAPER_ID = "99999999-9999-9999-9999-999999999999"

PAPER_A = "Attention Is All You Need"
PAPER_B = "BERT Pre-training"


def _hit(paper=PAPER_A, page=1, paper_id=PAPER_A_ID, text="Body text."):
    hit = MagicMock()
    hit.id = uuid.uuid4().hex
    hit.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "page": page,
        "paper_id": paper_id,
        "chunk_id": 0,
        "text": text,
        "authors": "A. Author",
        "abstract": "An abstract.",
        "keywords": "kw",
    }
    return hit


def _groq_stream(answer_text):
    return iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])
    ])


class SummarizePersistenceTestCase(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False
        )

        patcher = patch(
            "app.db.session.get_session_factory", return_value=self.factory
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        from app.main import app
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        self._current_owner = USER_A

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()
        for owner in (USER_A, USER_B):
            module_store.clear(owner)

    def authenticate_as(self, owner_id):
        self._current_owner = owner_id
        self.app.dependency_overrides[get_current_owner_id] = (
            lambda: self._current_owner
        )

    def seed_paper(self, paper_id, owner_id, title):
        session = self.factory()
        try:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="0" * 64,
                    storage_path=f"papers/{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=100,
                    status="indexed",
                )
            )
            session.commit()
        finally:
            session.close()

    def sessions(self):
        s = self.factory()
        try:
            return s.query(ChatSession).all()
        finally:
            s.close()

    def messages(self):
        s = self.factory()
        try:
            return s.query(ChatMessage).all()
        finally:
            s.close()

    def summarize(self, paper_name, hits=None, **params):
        hits = [_hit()] if hits is None else hits
        with patch("app.api.summarize_paper.encode_query", return_value=[0.1] * 1024), \
             patch("app.api.summarize_paper.client") as mock_qdrant, \
             patch("app.api.summarize_paper.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.summarize_paper.generate_answer", return_value="A summary."):

            mock_qdrant.query_points.return_value.points = hits
            return self.client.get(
                "/summarize-paper",
                params={"paper_name": paper_name, **params},
            )

    def ask(self, question, hits=None, answer="An answer [Page 1]."):
        hits = [_hit()] if hits is None else hits
        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024), \
             patch("app.api.ask_stream.client") as mock_qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.ask_stream._groq_client") as mock_groq:

            mock_qdrant.query_points.return_value.points = hits
            mock_groq.chat.completions.create.return_value = _groq_stream(answer)
            return self.client.get("/ask-stream", params={"question": question})

    def events(self, resp):
        out = []
        for frame in resp.text.split("\n\n"):
            frame = frame.strip()
            if frame.startswith("data:"):
                out.append(json.loads(frame[len("data:"):].strip()))
        return out


class TestSummarizePersistsCurrentPaper(SummarizePersistenceTestCase):

    def test_detected_paper_id_is_written_to_the_session(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        resp = self.summarize("Attention")

        self.assertEqual(resp.json()["status"], "success")
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].current_paper_id, uuid.UUID(PAPER_A_ID))

    def test_summarising_records_no_chat_message(self):
        """A summary is not a conversational turn."""
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.summarize("Attention")

        self.assertEqual(self.messages(), [])

    def test_reuses_the_existing_session_rather_than_creating_a_second(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.seed_paper(PAPER_B_ID, USER_A, PAPER_B)
        self.authenticate_as(USER_A)

        self.ask("What is attention?")
        self.summarize("Attention")
        self.summarize("BERT", hits=[_hit(paper=PAPER_B, paper_id=PAPER_B_ID)])

        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].current_paper_id, uuid.UUID(PAPER_B_ID))

    def test_updated_at_advances(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.summarize("Attention")

        self.assertIsNotNone(self.sessions()[0].updated_at)


class TestSummarizePersistenceIsGuarded(SummarizePersistenceTestCase):

    def test_paper_owned_by_someone_else_is_not_written(self):
        self.seed_paper(PAPER_B_ID, USER_B, PAPER_B)
        self.authenticate_as(USER_A)

        resp = self.summarize(
            "BERT", hits=[_hit(paper=PAPER_B, paper_id=PAPER_B_ID)]
        )

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(
            self.sessions(), [],
            "a paper this owner does not have must not create or point a session",
        )

    def test_unknown_paper_id_is_ignored(self):
        self.authenticate_as(USER_A)

        resp = self.summarize(
            "Ghost", hits=[_hit(paper_id=UNKNOWN_PAPER_ID)]
        )

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(self.sessions(), [])

    def test_malformed_paper_id_is_ignored(self):
        self.authenticate_as(USER_A)

        resp = self.summarize("Broken", hits=[_hit(paper_id="not-a-uuid")])

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(self.sessions(), [])

    def test_missing_paper_id_is_ignored(self):
        self.authenticate_as(USER_A)
        hit = _hit()
        del hit.payload["paper_id"]

        resp = self.summarize("No id", hits=[hit])

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(self.sessions(), [])

    def test_client_supplied_owner_id_is_not_authoritative(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.summarize("Attention", owner_id=USER_B, user_id=USER_B)

        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_id, uuid.UUID(USER_A))

    def test_unauthenticated_request_persists_nothing(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)

        resp = self.client.get(
            "/summarize-paper", params={"paper_name": "Attention"}
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.sessions(), [])


class TestSummarizeThenFollowUp(SummarizePersistenceTestCase):
    """The behaviour the removed title-based fallback used to provide."""

    def test_summarised_paper_locks_a_later_followup(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.seed_paper(PAPER_B_ID, USER_A, PAPER_B)
        self.authenticate_as(USER_A)

        # Establish history on paper B, then summarise paper A.
        self.ask(
            "What is BERT?", hits=[_hit(paper=PAPER_B, paper_id=PAPER_B_ID)]
        )
        self.summarize("Attention")
        self.assertEqual(
            self.sessions()[0].current_paper_id, uuid.UUID(PAPER_A_ID)
        )

        # A follow-up retrieving both papers must narrow to the
        # summarised one.
        resp = self.ask(
            "How does it perform?",
            hits=[
                _hit(paper=PAPER_B, page=2, paper_id=PAPER_B_ID),
                _hit(paper=PAPER_A, page=7, paper_id=PAPER_A_ID),
            ],
        )

        done = [e for e in self.events(resp) if e["type"] == "done"][0]
        self.assertEqual({c["paper"] for c in done["citations"]}, {PAPER_A})

    def test_lock_survives_cleared_in_process_memory(self):
        """The old fallback read UserMemory, which a restart wiped. The
        durable pointer must not care."""
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.seed_paper(PAPER_B_ID, USER_A, PAPER_B)
        self.authenticate_as(USER_A)

        self.ask(
            "What is BERT?", hits=[_hit(paper=PAPER_B, paper_id=PAPER_B_ID)]
        )
        self.summarize("Attention")

        module_store.clear(USER_A)

        resp = self.ask(
            "How does it perform?",
            hits=[
                _hit(paper=PAPER_B, page=2, paper_id=PAPER_B_ID),
                _hit(paper=PAPER_A, page=7, paper_id=PAPER_A_ID),
            ],
        )

        done = [e for e in self.events(resp) if e["type"] == "done"][0]
        self.assertEqual({c["paper"] for c in done["citations"]}, {PAPER_A})


class TestSetCurrentPaperDirectly(SummarizePersistenceTestCase):

    def test_none_is_a_no_op(self):
        set_current_paper(USER_A, None)
        self.assertEqual(self.sessions(), [])

    def test_creates_a_session_when_none_exists(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)

        set_current_paper(USER_A, uuid.UUID(PAPER_A_ID))

        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_id, uuid.UUID(USER_A))
        self.assertEqual(rows[0].current_paper_id, uuid.UUID(PAPER_A_ID))

    def test_owners_are_isolated(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.seed_paper(PAPER_B_ID, USER_B, PAPER_B)

        set_current_paper(USER_A, uuid.UUID(PAPER_A_ID))
        set_current_paper(USER_B, uuid.UUID(PAPER_B_ID))

        by_owner = {s.owner_id: s.current_paper_id for s in self.sessions()}
        self.assertEqual(by_owner[uuid.UUID(USER_A)], uuid.UUID(PAPER_A_ID))
        self.assertEqual(by_owner[uuid.UUID(USER_B)], uuid.UUID(PAPER_B_ID))

    def test_unique_race_recovers_without_a_second_session(self):
        from sqlalchemy.exc import IntegrityError
        import app.services.chat_store as chat_store

        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        original = chat_store.session_scope
        calls = {"n": 0}

        def flaky_scope():
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("duplicate key", None, Exception())
            return original()

        with patch.object(chat_store, "session_scope", side_effect=flaky_scope):
            set_current_paper(USER_A, uuid.UUID(PAPER_A_ID))

        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.sessions()), 1)


class TestDeadChatFieldsRemoved(unittest.TestCase):
    """The fields ask_stream used to mirror are gone, and nothing
    references them any more."""

    REMOVED = (
        "chat_history", "current_topic", "topic_context",
        "current_paper", "last_question", "last_answer",
        "last_exported_file",
    )
    KEPT = (
        "last_summary", "last_summary_paper", "last_comparison",
        "last_compared_paper1", "last_compared_paper2",
        "last_research_query", "last_research_report",
        "last_research_context", "last_research_sources", "last_citations",
    )

    def test_dead_fields_are_gone(self):
        from app.memory import UserMemory
        memory = UserMemory()
        for name in self.REMOVED:
            self.assertFalse(
                hasattr(memory, name), f"UserMemory.{name} should be removed"
            )

    def test_live_fields_are_preserved(self):
        from app.memory import UserMemory
        memory = UserMemory()
        for name in self.KEPT:
            self.assertTrue(
                hasattr(memory, name), f"UserMemory.{name} must be preserved"
            )

    def test_ask_stream_no_longer_touches_user_memory(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "ask_stream.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn("get_user_memory", source)
        self.assertNotIn("user_memory", source)

    def test_summarize_paper_no_longer_writes_current_paper_to_memory(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "summarize_paper.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn("user_memory.current_paper", source)
        self.assertIn("set_current_paper", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
