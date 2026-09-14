"""
D2 step 4 — durable chat_sessions / chat_messages persistence.

Fully offline and deterministic. The database is per-test in-memory
SQLite, reached by patching app.db.session.get_session_factory so the
REAL session_scope() and the REAL chat_store functions run. Qdrant, the
embedder, the reranker and Groq are mocked. No Postgres, no network.
"""
import datetime
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
from app.services.chat_store import persist_assistant_turn, persist_user_turn

USER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
USER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
MALICIOUS = "ffffffff-ffff-ffff-ffff-ffffffffffff"

PAPER_ID = "11111111-1111-1111-1111-111111111111"
FOREIGN_PAPER_ID = "22222222-2222-2222-2222-222222222222"


def _hit(paper="Attention Is All You Need", page=3, paper_id=PAPER_ID):
    hit = MagicMock()
    hit.id = uuid.uuid4().hex
    hit.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "page": page,
        "paper_id": paper_id,
        "chunk_id": 0,
        "text": "A passage of retrieved evidence.",
    }
    return hit


def _groq_stream(answer_text):
    return iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])
    ])


class TrackingFactory:
    """Wraps a sessionmaker and counts sessions that are open right now,
    so a test can assert nothing is held during the LLM stream."""

    def __init__(self, real):
        self._real = real
        self.open_now = 0
        self.peak = 0
        self.created = 0

    def __call__(self):
        session = self._real()
        self.created += 1
        self.open_now += 1
        self.peak = max(self.peak, self.open_now)

        original_close = session.close

        def close():
            self.open_now -= 1
            original_close()

        session.close = close
        return session


class ChatPersistenceTestCase(unittest.TestCase):

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

    def seed_paper(self, paper_id, owner_id, title="Attention Is All You Need"):
        session = self.factory()
        try:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="0" * 64,
                    storage_path=f"papers/{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=1234,
                    status="indexed",
                )
            )
            session.commit()
        finally:
            session.close()

    # -- read helpers -------------------------------------------------

    def sessions(self):
        s = self.factory()
        try:
            return s.query(ChatSession).all()
        finally:
            s.close()

    def messages(self):
        s = self.factory()
        try:
            return s.query(ChatMessage).order_by(ChatMessage.created_at).all()
        finally:
            s.close()

    # -- endpoint driver ----------------------------------------------

    def ask(self, question, hits=None, answer="A grounded answer [Page 3].", **params):
        hits = [_hit()] if hits is None else hits
        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024), \
             patch("app.api.ask_stream.client") as mock_qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.ask_stream._groq_client") as mock_groq:

            mock_qdrant.query_points.return_value.points = hits
            mock_groq.chat.completions.create.return_value = _groq_stream(answer)

            return self.client.get(
                "/ask-stream", params={"question": question, **params}
            )


class TestSessionCreationAndReuse(ChatPersistenceTestCase):

    def test_first_question_creates_exactly_one_session(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_id, uuid.UUID(USER_A))

    def test_repeated_questions_reuse_the_same_session(self):
        self.authenticate_as(USER_A)
        self.ask("First question?")
        self.ask("Second question?")
        self.ask("Third question?")

        rows = self.sessions()
        self.assertEqual(
            len(rows), 1,
            "an owner must never accumulate more than one chat session",
        )

        session_ids = {m.session_id for m in self.messages()}
        self.assertEqual(
            session_ids, {rows[0].id},
            "every message must hang off that one session",
        )

    def test_unique_violation_race_recovers_without_a_second_session(self):
        """Two concurrent first-ever requests can both see no row and both
        insert; chat_sessions.owner_id UNIQUE makes the loser fail, and
        persist_user_turn retries onto the winner's row."""
        from sqlalchemy.exc import IntegrityError

        self.authenticate_as(USER_A)

        real_scope_target = "app.services.chat_store.session_scope"
        original = __import__(
            "app.services.chat_store", fromlist=["session_scope"]
        ).session_scope

        calls = {"n": 0}

        def flaky_scope(owner_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("duplicate key", None, Exception())
            return original(owner_id)

        with patch(real_scope_target, side_effect=flaky_scope):
            session_id = persist_user_turn(USER_A, "hello")

        self.assertEqual(calls["n"], 2, "the retry must actually happen")
        self.assertEqual(len(self.sessions()), 1)
        self.assertIsNotNone(session_id)


class TestTurnPersistence(ChatPersistenceTestCase):

    def test_user_turn_is_persisted(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        user_msgs = [m for m in self.messages() if m.role == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0].content, "What is attention?")
        self.assertEqual(user_msgs[0].owner_id, uuid.UUID(USER_A))

    def test_assistant_turn_is_persisted(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?", answer="Attention weights tokens [Page 3].")

        assistant_msgs = [m for m in self.messages() if m.role == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(
            assistant_msgs[0].content, "Attention weights tokens [Page 3]."
        )
        self.assertEqual(assistant_msgs[0].owner_id, uuid.UUID(USER_A))

    def test_turns_are_ordered_user_then_assistant(self):
        self.authenticate_as(USER_A)
        self.ask("Question one?", answer="Answer one.")
        self.ask("Question two?", answer="Answer two.")

        ordered = [(m.role, m.content) for m in self.messages()]
        self.assertEqual(
            ordered,
            [
                ("user", "Question one?"),
                ("assistant", "Answer one."),
                ("user", "Question two?"),
                ("assistant", "Answer two."),
            ],
        )

    def test_user_turn_survives_when_retrieval_finds_nothing(self):
        """The endpoint returns early with an error event, but the
        question was still genuinely asked."""
        self.authenticate_as(USER_A)
        self.ask("Nothing matches this", hits=[])

        roles = [m.role for m in self.messages()]
        self.assertEqual(roles, ["user"])


class TestSessionPointerPersistence(ChatPersistenceTestCase):

    def test_current_topic_is_persisted_for_a_non_followup(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")

        self.assertEqual(self.sessions()[0].current_topic, "What is explainable AI?")

    def test_followup_does_not_overwrite_current_topic(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")
        # "it" is a follow-up word, and history now exists.
        self.ask("How does it work?")

        self.assertEqual(
            self.sessions()[0].current_topic,
            "What is explainable AI?",
            "a follow-up must keep the established topic",
        )

    def test_current_paper_id_is_persisted_when_the_paper_is_owned(self):
        self.seed_paper(PAPER_ID, USER_A)
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        self.assertEqual(self.sessions()[0].current_paper_id, uuid.UUID(PAPER_ID))

    def test_paper_owned_by_someone_else_is_not_pointed_at(self):
        """A Qdrant payload must never be able to point this session at
        another owner's paper row."""
        self.seed_paper(FOREIGN_PAPER_ID, USER_B, title="Someone else's paper")
        self.authenticate_as(USER_A)
        self.ask("What is attention?", hits=[_hit(paper_id=FOREIGN_PAPER_ID)])

        self.assertIsNone(self.sessions()[0].current_paper_id)

    def test_unknown_paper_id_is_ignored_rather_than_failing(self):
        """No papers row exists for this id — writing it would be a FK
        violation that sinks an otherwise good answer."""
        self.authenticate_as(USER_A)
        resp = self.ask("What is attention?")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.sessions()[0].current_paper_id)
        self.assertEqual(len(self.messages()), 2)

    def test_malformed_paper_id_in_payload_is_ignored(self):
        self.authenticate_as(USER_A)
        resp = self.ask("What is attention?", hits=[_hit(paper_id="not-a-uuid")])

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.sessions()[0].current_paper_id)


class TestSessionUpdatedAt(ChatPersistenceTestCase):
    """updated_at must mean "last chat activity", which the model's
    onupdate hook alone cannot deliver: a pure follow-up dirties no
    column, so SQLAlchemy emits no UPDATE and onupdate never fires."""

    @staticmethod
    def _as_utc(value):
        # SQLite has no timezone storage, so DateTime(timezone=True)
        # reads back naive. Normalise so the assertion holds on both
        # SQLite and Postgres.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value

    def test_updated_at_advances_on_a_pure_followup_turn(self):
        self.authenticate_as(USER_A)

        # First question establishes the topic, so the follow-up below
        # passes current_topic=None. No paper is seeded, so the unknown
        # paper_id is ignored and current_paper_id stays None too —
        # making the second turn a genuinely pure follow-up.
        self.ask("What is explainable AI?")
        before = self._as_utc(self.sessions()[0].updated_at)

        marker = datetime.datetime(
            2030, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
        )
        with patch("app.services.chat_store._utcnow", return_value=marker):
            self.ask("How does it work?")

        session = self.sessions()[0]

        # Confirm this really was a pure follow-up.
        self.assertEqual(session.current_topic, "What is explainable AI?")
        self.assertIsNone(session.current_paper_id)

        after = self._as_utc(session.updated_at)
        self.assertEqual(
            after, marker,
            "updated_at must be written on a follow-up assistant turn",
        )
        self.assertNotEqual(after, before)

    def test_updated_at_advances_on_a_topic_setting_turn_too(self):
        self.authenticate_as(USER_A)

        marker = datetime.datetime(
            2031, 6, 1, 8, 30, 0, tzinfo=datetime.timezone.utc
        )
        with patch("app.services.chat_store._utcnow", return_value=marker):
            self.ask("What is explainable AI?")

        self.assertEqual(
            self._as_utc(self.sessions()[0].updated_at), marker
        )

    def test_updated_at_moves_forward_across_successive_turns(self):
        """Real clock, no patching — successive turns must not go
        backwards, and the follow-up must not be left behind."""
        self.authenticate_as(USER_A)

        self.ask("What is explainable AI?")
        first = self._as_utc(self.sessions()[0].updated_at)

        self.ask("How does it work?")
        second = self._as_utc(self.sessions()[0].updated_at)

        self.assertGreaterEqual(second, first)

    def test_updated_at_is_not_advanced_when_the_assistant_turn_fails(self):
        self.authenticate_as(USER_A)
        self.ask("What is explainable AI?")
        before = self._as_utc(self.sessions()[0].updated_at)

        marker = datetime.datetime(
            2032, 1, 1, tzinfo=datetime.timezone.utc
        )
        with patch("app.services.chat_store._utcnow", return_value=marker), \
             patch(
                 "app.services.chat_store.ChatMessage",
                 side_effect=RuntimeError("boom"),
             ):
            with self.assertRaises(RuntimeError):
                persist_assistant_turn(
                    self.sessions()[0].id, USER_A, "an answer"
                )

        after = self._as_utc(self.sessions()[0].updated_at)
        self.assertEqual(after, before, "a rolled-back turn must not advance it")
        self.assertNotEqual(after, marker)


class TestOwnerIsolation(ChatPersistenceTestCase):

    def test_each_owner_gets_their_own_session(self):
        self.authenticate_as(USER_A)
        self.ask("User A question?")
        self.authenticate_as(USER_B)
        self.ask("User B question?")

        rows = self.sessions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r.owner_id for r in rows},
            {uuid.UUID(USER_A), uuid.UUID(USER_B)},
        )

    def test_messages_are_attributed_to_the_asking_owner(self):
        self.authenticate_as(USER_A)
        self.ask("User A question?", answer="Answer for A.")
        self.authenticate_as(USER_B)
        self.ask("User B question?", answer="Answer for B.")

        by_owner = {}
        for m in self.messages():
            by_owner.setdefault(m.owner_id, []).append(m.content)

        self.assertEqual(
            by_owner[uuid.UUID(USER_A)], ["User A question?", "Answer for A."]
        )
        self.assertEqual(
            by_owner[uuid.UUID(USER_B)], ["User B question?", "Answer for B."]
        )

    def test_one_owners_messages_never_land_on_anothers_session(self):
        self.authenticate_as(USER_A)
        self.ask("User A question?")
        self.authenticate_as(USER_B)
        self.ask("User B question?")

        sessions_by_owner = {s.owner_id: s.id for s in self.sessions()}
        for message in self.messages():
            self.assertEqual(
                message.session_id,
                sessions_by_owner[message.owner_id],
                "a message must belong to its own owner's session",
            )

    def test_client_supplied_owner_id_is_not_authoritative(self):
        self.authenticate_as(USER_A)
        self.ask(
            "What is attention?",
            owner_id=MALICIOUS,
            user_id=MALICIOUS,
        )

        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_id, uuid.UUID(USER_A))
        for message in self.messages():
            self.assertEqual(message.owner_id, uuid.UUID(USER_A))

    def test_unauthenticated_request_persists_nothing(self):
        resp = self.client.get("/ask-stream", params={"question": "What is AI?"})

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.messages(), [])


class TestPersistenceFailureRollsBack(ChatPersistenceTestCase):

    def test_failed_user_turn_leaves_no_session_and_no_message(self):
        """Session creation and the user message share one transaction,
        so a failed message write must not leave an orphan session."""
        self.authenticate_as(USER_A)

        with patch(
            "app.services.chat_store.ChatMessage",
            side_effect=RuntimeError("write failed"),
        ):
            resp = self.ask("What is attention?")

        self.assertEqual(resp.status_code, 200)  # SSE transport succeeded
        self.assertIn("write failed", resp.text)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.messages(), [])

    def test_failed_assistant_turn_does_not_advance_session_pointers(self):
        self.seed_paper(PAPER_ID, USER_A)
        self.authenticate_as(USER_A)

        with patch(
            "app.services.chat_store.persist_assistant_turn",
            side_effect=RuntimeError("assistant write failed"),
        ), patch(
            "app.api.ask_stream.persist_assistant_turn",
            side_effect=RuntimeError("assistant write failed"),
        ):
            resp = self.ask("What is attention?")

        self.assertIn("assistant write failed", resp.text)

        # The user turn was committed in its own earlier transaction and
        # legitimately survives; nothing from the assistant turn does.
        roles = [m.role for m in self.messages()]
        self.assertEqual(roles, ["user"])

        session = self.sessions()[0]
        self.assertIsNone(session.current_topic)
        self.assertIsNone(session.current_paper_id)

    def test_no_partial_write_when_session_pointer_update_fails(self):
        self.authenticate_as(USER_A)
        session_id = persist_user_turn(USER_A, "seed question")

        with patch(
            "app.services.chat_store.ChatMessage",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                persist_assistant_turn(
                    session_id, USER_A, "an answer", current_topic="new topic"
                )

        self.assertEqual([m.role for m in self.messages()], ["user"])
        self.assertIsNone(self.sessions()[0].current_topic)


class TestNoLongLivedConnection(ChatPersistenceTestCase):

    def test_no_session_is_open_while_the_llm_streams(self):
        """The whole reason chat_store uses session_scope() instead of a
        request-scoped dependency: a pooled Postgres connection must not
        be held for the duration of generation."""
        self.authenticate_as(USER_A)
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

            resp = self.client.get("/ask-stream", params={"question": "q?"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            observed, [0],
            "a database session was open when the LLM call started",
        )
        self.assertEqual(
            self.factory.open_now, 0, "every session must be closed at the end"
        )

    def test_streaming_still_produces_tokens_and_a_done_event(self):
        self.authenticate_as(USER_A)
        resp = self.ask("What is attention?", answer="Attention weights tokens [Page 3].")

        events = []
        for frame in resp.text.split("\n\n"):
            frame = frame.strip()
            if frame.startswith("data:"):
                events.append(json.loads(frame[len("data:"):].strip()))

        types = [e["type"] for e in events]
        self.assertIn("token", types)
        self.assertIn("done", types)

        answer = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertEqual(answer, "Attention weights tokens [Page 3].")

        done = [e for e in events if e["type"] == "done"][0]
        self.assertTrue(done["citations"])

    def test_each_request_opens_only_short_lived_sessions(self):
        self.authenticate_as(USER_A)
        self.ask("What is attention?")

        self.assertEqual(
            self.factory.peak, 1,
            "the two persistence transactions must never overlap",
        )
        self.assertEqual(self.factory.open_now, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
