"""
D2 step 5 — /ask-stream reads its conversation state from Postgres
(chat_sessions / chat_messages), not from in-process UserMemory.

The decisive test here is test_survives_cleared_in_process_memory: it
wipes UserMemory between requests and shows follow-up detection, topic
carry-over and history reconstruction all still work. That is what
"Postgres is the durable source" actually means, and it could not pass
before this step.

Fully offline and deterministic: in-memory SQLite behind the real
session_scope(), with Qdrant, the embedder, the reranker and Groq
mocked. No Postgres, no network.
"""
import datetime
import itertools
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
from sqlalchemy.orm import Query, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_owner_id
from app.db.models import Base, ChatMessage, ChatSession, Paper
from app.memory import _store as module_store
from app.services.chat_store import ChatState, load_chat_state

USER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
USER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
MALICIOUS = "ffffffff-ffff-ffff-ffff-ffffffffffff"

PAPER_A_ID = "11111111-1111-1111-1111-111111111111"
PAPER_B_ID = "22222222-2222-2222-2222-222222222222"

PAPER_A = "Attention Is All You Need"
PAPER_B = "BERT Pre-training"


def _hit(paper=PAPER_A, page=3, paper_id=PAPER_A_ID, text="Evidence passage."):
    hit = MagicMock()
    hit.id = uuid.uuid4().hex
    hit.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "page": page,
        "paper_id": paper_id,
        "chunk_id": 0,
        "text": text,
    }
    return hit


def _groq_stream(answer_text):
    return iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])
    ])


class TrackingFactory:
    def __init__(self, real):
        self._real = real
        self.open_now = 0
        self.peak = 0

    def __call__(self):
        session = self._real()
        self.open_now += 1
        self.peak = max(self.peak, self.open_now)
        original_close = session.close

        def close():
            self.open_now -= 1
            original_close()

        session.close = close
        return session


class ChatReadTestCase(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = TrackingFactory(
            sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
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

        self.last_search_query = None
        self.last_prompt = None

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()
        for owner in (USER_A, USER_B, MALICIOUS):
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

    def ask(self, question, hits=None, answer="An answer [Page 3].", **params):
        """Drives the real endpoint, capturing the search query actually
        embedded and the prompt actually sent to the model."""
        hits = [_hit()] if hits is None else hits

        with patch("app.api.ask_stream.encode_query") as mock_encode, \
             patch("app.api.ask_stream.client") as mock_qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.ask_stream._groq_client") as mock_groq:

            mock_encode.return_value = [0.1] * 1024
            mock_qdrant.query_points.return_value.points = hits
            mock_groq.chat.completions.create.return_value = _groq_stream(answer)

            resp = self.client.get(
                "/ask-stream", params={"question": question, **params}
            )

            if mock_encode.call_args is not None:
                self.last_search_query = mock_encode.call_args.args[0]
            create_call = mock_groq.chat.completions.create.call_args
            if create_call is not None:
                self.last_prompt = create_call.kwargs["messages"][1]["content"]

            return resp

    def events(self, resp):
        out = []
        for frame in resp.text.split("\n\n"):
            frame = frame.strip()
            if frame.startswith("data:"):
                out.append(json.loads(frame[len("data:"):].strip()))
        return out


class TestFreshUser(ChatReadTestCase):

    def test_no_session_yields_empty_state(self):
        state = load_chat_state(USER_A)
        self.assertEqual(state, ChatState())
        self.assertEqual(state.history, [])
        self.assertEqual(state.current_topic, "")
        self.assertIsNone(state.current_paper_id)
        self.assertIsNone(state.session_id)

    def test_first_question_is_never_a_followup(self):
        self.authenticate_as(USER_A)
        # "it" is a follow-up word, but there is no history to follow up on.
        self.ask("Explain it simply")

        self.assertEqual(self.last_search_query, "Explain it simply")

    def test_first_question_has_empty_conversation_history(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        history_block = self.last_prompt.split("Conversation History:\n", 1)[1]
        history_block = history_block.split("\n\nCurrent Retrieved Context:", 1)[0]
        self.assertEqual(history_block.strip(), "")


class TestHistoryReconstruction(ChatReadTestCase):

    def test_multi_turn_history_is_rebuilt_in_order(self):
        self.authenticate_as(USER_A)
        self.ask("First question?", answer="First answer.")
        self.ask("Second question?", answer="Second answer.")
        self.ask("Third question?", answer="Third answer.")

        history_block = self.last_prompt.split("Conversation History:\n", 1)[1]
        history_block = history_block.split("\n\nCurrent Retrieved Context:", 1)[0]

        self.assertEqual(
            history_block.strip().split("\n"),
            [
                "User: First question?",
                "Assistant: First answer.",
                "User: Second question?",
                "Assistant: Second answer.",
            ],
        )

    def test_history_is_capped_at_the_configured_limit(self):
        self.authenticate_as(USER_A)
        for n in range(6):
            self.ask(f"Question {n}?", answer=f"Answer {n}.")

        state = load_chat_state(USER_A)
        self.assertEqual(len(state.history), 6)
        self.assertEqual(
            state.history,
            [
                "User: Question 3?", "Assistant: Answer 3.",
                "User: Question 4?", "Assistant: Answer 4.",
                "User: Question 5?", "Assistant: Answer 5.",
            ],
        )

    def assert_messages_strictly_ordered(self, expected_contents):
        """Stored created_at values are unique, and ordering by them alone
        reproduces the order the turns were written in."""
        session = self.factory()
        try:
            rows = session.query(ChatMessage).order_by(ChatMessage.created_at).all()
        finally:
            session.close()
        stamps = [row.created_at for row in rows]
        self.assertEqual(len(set(stamps)), len(stamps), "two messages share a created_at")
        self.assertEqual([row.content for row in rows], expected_contents)

    def test_history_order_survives_a_clock_that_does_not_advance(self):
        """Every turn read from a frozen clock: the stored timestamps must
        still strictly increase, so history is rebuilt in order without
        falling back on the random message id."""
        self.authenticate_as(USER_A)
        frozen = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)

        with patch("app.services.chat_store._utcnow", return_value=frozen):
            for n in range(4):
                self.ask(f"Question {n}?", answer=f"Answer {n}.")

        self.assert_messages_strictly_ordered(
            [text for n in range(4) for text in (f"Question {n}?", f"Answer {n}.")]
        )
        self.assertEqual(
            load_chat_state(USER_A).history,
            [
                "User: Question 1?", "Assistant: Answer 1.",
                "User: Question 2?", "Assistant: Answer 2.",
                "User: Question 3?", "Assistant: Answer 3.",
            ],
        )

    def test_history_order_survives_a_clock_that_steps_backwards(self):
        self.authenticate_as(USER_A)
        start = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)
        ticks = itertools.count()

        def backwards_clock():
            return start - datetime.timedelta(seconds=next(ticks))

        with patch("app.services.chat_store._utcnow", side_effect=backwards_clock):
            for n in range(3):
                self.ask(f"Question {n}?", answer=f"Answer {n}.")

        self.assert_messages_strictly_ordered(
            [text for n in range(3) for text in (f"Question {n}?", f"Answer {n}.")]
        )
        self.assertEqual(
            load_chat_state(USER_A).history,
            [
                "User: Question 0?", "Assistant: Answer 0.",
                "User: Question 1?", "Assistant: Answer 1.",
                "User: Question 2?", "Assistant: Answer 2.",
            ],
        )

    def test_message_writes_lock_the_session_row(self):
        """Strictly increasing timestamps hold across concurrent requests
        only because each message is stamped under the session row lock.
        SQLite ignores FOR UPDATE, so assert the lock is requested."""
        locked = []
        original = Query.with_for_update

        def spy(query, *args, **kwargs):
            locked.append(query.column_descriptions[0]["entity"])
            return original(query, *args, **kwargs)

        self.authenticate_as(USER_A)
        with patch.object(Query, "with_for_update", spy):
            self.ask("First question?", answer="First answer.")

        # One lock for the user turn, one for the assistant turn.
        self.assertEqual(locked, [ChatSession, ChatSession])

    def test_current_question_is_not_in_its_own_history(self):
        """load_chat_state must run before persist_user_turn."""
        self.authenticate_as(USER_A)
        self.ask("First question?", answer="First answer.")
        self.ask("Second question?", answer="Second answer.")

        self.assertNotIn("User: Second question?", self.last_prompt)
        self.assertIn("User: First question?", self.last_prompt)


class TestTopicAndPaperCarryOver(ChatReadTestCase):

    def test_current_topic_carries_into_a_followup_search(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")
        self.ask("What are its limitations?")

        self.assertEqual(
            self.last_search_query,
            "What is explainable AI? What are its limitations?",
        )

    def test_non_followup_does_not_prepend_the_topic(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")
        # Careful when changing this phrase: FOLLOW_UP_WORDS is matched as
        # a plain substring, so e.g. "networks" contains "work" and would
        # be classified as a follow-up. That quirk predates this step.
        self.ask("Define gradient descent")

        self.assertEqual(self.last_search_query, "Define gradient descent")

    def test_current_paper_locks_a_followup_to_that_paper(self):
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.ask("What is attention?", hits=[_hit(paper=PAPER_A)])
        self.assertEqual(
            self.sessions()[0].current_paper_id, uuid.UUID(PAPER_A_ID)
        )

        # A follow-up whose retrieval spans two papers must be narrowed
        # to the locked one.
        resp = self.ask(
            "How does it perform?",
            hits=[
                _hit(paper=PAPER_B, page=1, paper_id=PAPER_B_ID),
                _hit(paper=PAPER_A, page=7, paper_id=PAPER_A_ID),
            ],
        )

        done = [e for e in self.events(resp) if e["type"] == "done"][0]
        self.assertTrue(done["citations"])
        self.assertEqual(
            {c["paper"] for c in done["citations"]}, {PAPER_A},
            "the follow-up must stay locked to the persisted paper",
        )

    def test_paper_lock_falls_back_when_no_hit_matches(self):
        """Existing behaviour: an unmatched lock keeps the unfiltered
        results rather than answering from nothing."""
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.ask("What is attention?", hits=[_hit(paper=PAPER_A)])
        resp = self.ask(
            "How does it perform?",
            hits=[_hit(paper=PAPER_B, page=1, paper_id=PAPER_B_ID)],
        )

        done = [e for e in self.events(resp) if e["type"] == "done"][0]
        self.assertEqual({c["paper"] for c in done["citations"]}, {PAPER_B})


class TestSurvivesProcessStateLoss(ChatReadTestCase):

    def test_survives_cleared_in_process_memory(self):
        """The point of the whole step: wipe UserMemory, and follow-up
        detection, topic carry-over and history must all still work
        because they come from the database."""
        self.seed_paper(PAPER_A_ID, USER_A, PAPER_A)
        self.authenticate_as(USER_A)

        self.ask("What is explainable AI?", answer="It is interpretability.")

        # Simulate a restart / evicted cache.
        module_store.clear(USER_A)
        self.assertEqual(
            module_store.get(USER_A).last_summary, "",
            "precondition: in-process memory really was reset",
        )

        self.ask("What are its limitations?")

        self.assertEqual(
            self.last_search_query,
            "What is explainable AI? What are its limitations?",
            "topic must come from chat_sessions, not memory",
        )
        self.assertIn("User: What is explainable AI?", self.last_prompt)
        self.assertIn("Assistant: It is interpretability.", self.last_prompt)

    def test_followup_detection_survives_cleared_memory(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")

        module_store.clear(USER_A)

        self.ask("Explain it simply")
        self.assertTrue(
            self.last_search_query.startswith("What is explainable AI?"),
            "history existence must be judged from chat_messages",
        )


class TestOwnerIsolationOnReads(ChatReadTestCase):

    def test_second_owner_does_not_inherit_history_or_topic(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?", answer="A private answer for A.")

        self.authenticate_as(USER_B)
        self.ask("Explain it simply")

        self.assertEqual(self.last_search_query, "Explain it simply")
        self.assertNotIn("explainable AI", self.last_prompt)
        self.assertNotIn("A private answer for A.", self.last_prompt)

    def test_load_chat_state_is_scoped_to_the_owner(self):
        self.authenticate_as(USER_A)
        self.ask("A question from A", answer="Answer for A.")

        self.assertEqual(load_chat_state(USER_B), ChatState())

        state_a = load_chat_state(USER_A)
        self.assertEqual(
            state_a.history, ["User: A question from A", "Assistant: Answer for A."]
        )

    def test_interleaved_owners_keep_separate_topics(self):
        self.authenticate_as(USER_A)
        self.ask("Tell me about transformers")

        self.authenticate_as(USER_B)
        self.ask("Tell me about databases")

        self.authenticate_as(USER_A)
        self.ask("What about its attention mechanism?")

        self.assertIn("Tell me about transformers", self.last_search_query)
        self.assertNotIn("databases", self.last_search_query)

    def test_client_supplied_owner_id_cannot_redirect_the_read(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?", answer="A private answer for A.")

        self.authenticate_as(USER_B)
        self.ask("Explain it simply", owner_id=USER_A, user_id=USER_A)

        self.assertEqual(self.last_search_query, "Explain it simply")
        self.assertNotIn("A private answer for A.", self.last_prompt)

    def test_unauthenticated_request_reads_nothing(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")

        self.app.dependency_overrides.clear()
        resp = self.client.get("/ask-stream", params={"question": "Explain it"})

        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("explainable AI", resp.text)


class TestNoLongLivedConnectionOnReads(ChatReadTestCase):

    def test_no_session_open_during_the_llm_stream(self):
        self.authenticate_as(USER_A)
        self.ask("Seed the conversation")

        observed = []

        def watching_stream(*args, **kwargs):
            observed.append(self.factory.open_now)
            return _groq_stream("An answer [Page 3].")

        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024), \
             patch("app.api.ask_stream.client") as mock_qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.ask_stream._groq_client") as mock_groq:

            mock_qdrant.query_points.return_value.points = [_hit()]
            mock_groq.chat.completions.create.side_effect = watching_stream

            resp = self.client.get(
                "/ask-stream", params={"question": "What about it?"}
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            observed, [0],
            "the read session must be closed before generation starts",
        )
        self.assertEqual(self.factory.open_now, 0)

    def test_read_and_writes_never_overlap(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        self.assertEqual(
            self.factory.peak, 1,
            "load + user-turn + assistant-turn must be three separate "
            "short-lived sessions, never nested",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
