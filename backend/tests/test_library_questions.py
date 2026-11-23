"""
Questions about the library itself, answered on /ask-stream from Postgres.

"How many papers are there?" used to reach the model, which sees only the
passages retrieved for that question and answered with the number of
papers in them ("There are 9 papers referenced in the provided context").
These tests pin the deterministic path that replaced that:

  * the matcher accepts questions about the library and rejects content
    questions, including ones that start the same way;
  * a match is answered from the owner's indexed papers, in the ordinary
    SSE shape, with no Voyage, Qdrant or Groq call and no AI allowance;
  * the burst limit still applies, and paper_id always takes the ordinary
    path.

Fully offline: an in-memory SQLite database, and every provider mocked.
"""
import json
import os
import sys
import unittest
import uuid
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from fastapi.testclient import TestClient

from app.api.ask_stream import LIBRARY_SCOPE_RESPONSE, SYSTEM_PROMPT
from app.core import limits as limits_config
from app.core.auth import get_current_owner_id
from app.core.rate_limit import get_limiter
from app.services.library_questions import (
    LIBRARY_LIST_LIMIT,
    is_library_question,
    library_reply_parts,
)
from tests.sqlite_harness import attach_sqlite_db

OWNER_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

LIBRARY_QUESTIONS = (
    "how many papers are there?",
    "How many papers?",
    "How many documents are there?",
    "how many documents do I have",
    "How many papers are in my library?",
    "How many indexed papers do I have?",
    "How many papers have I uploaded?",
    "how many PDFs are in the library",
    "How many papers are there in total?",
    "Can you tell me how many papers I have?",
    "list my papers",
    "List all papers.",
    "List all my papers",
    "Show me all my papers",
    "Can you list my papers please?",
    "count my papers",
    "What papers do I have?",
    "Which papers do I have?",
    "Which papers are in my library?",
    "What papers are in my library?",
    "What papers have I uploaded?",
    "Which documents have I uploaded?",
    "which documents are indexed",
    "What are my papers?",
    "What's in my library?",
    "what do i have in my library",
)

CONTENT_QUESTIONS = (
    "Which papers use vision-language models?",
    "Which paper proposes EIAD?",
    "Which papers use transformers for anomaly detection?",
    "What papers discuss anomaly detection?",
    "Which papers are cited in the related work?",
    "Which papers do I have about diffusion models?",
    "What papers have I uploaded on reinforcement learning?",
    "How many papers report results on ImageNet?",
    "How many papers are there that use CNNs?",
    "How many papers does the survey review?",
    "how many papers are in the benchmark",
    "How many parameters does the model have?",
    "How many layers are there?",
    "List the limitations of the proposed method",
    "list papers that use transformers",
    "Show me the results table",
    "what are the papers about",
    "What datasets are in the library?",
    "what files are in the dataset",
    "which paper is the most recent",
    "What is the main contribution?",
    "",
)


class TestMatcher(unittest.TestCase):

    def test_questions_about_the_library_match(self):
        for question in LIBRARY_QUESTIONS:
            with self.subTest(question=question):
                self.assertTrue(is_library_question(question))

    def test_content_questions_do_not_match(self):
        for question in CONTENT_QUESTIONS:
            with self.subTest(question=question):
                self.assertFalse(is_library_question(question))


class TestReplyText(unittest.TestCase):

    def test_count_then_titles_as_a_list(self):
        reply = "".join(library_reply_parts(["Alpha", "Beta"]))
        self.assertEqual(reply, "You have 2 indexed papers:\n\n- Alpha\n- Beta")

    def test_one_paper_is_singular(self):
        self.assertTrue("".join(library_reply_parts(["Alpha"])).startswith(
            "You have 1 indexed paper:"
        ))

    def test_an_empty_library_says_so(self):
        reply = "".join(library_reply_parts([]))
        self.assertIn("no indexed papers", reply)

    def test_titles_are_markdown_escaped(self):
        reply = "".join(library_reply_parts(["my_paper *v2* [draft]"]))
        self.assertIn(r"my\_paper \*v2\* \[draft\]", reply)

    def test_a_large_library_is_capped_with_the_full_count(self):
        titles = [f"Paper {n:03d}" for n in range(LIBRARY_LIST_LIMIT + 7)]
        reply = "".join(library_reply_parts(titles))
        self.assertTrue(reply.startswith(f"You have {LIBRARY_LIST_LIMIT + 7} indexed papers:"))
        self.assertEqual(reply.count("\n- "), LIBRARY_LIST_LIMIT)
        self.assertIn("and 7 more", reply)

    def test_the_prompt_rule_stays_as_a_fallback(self):
        self.assertIn(LIBRARY_SCOPE_RESPONSE, SYSTEM_PROMPT)


def _events(response):
    events = []
    for frame in response.text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            events.append(json.loads(frame[len("data:"):].strip()))
    return events


class AskStreamLibraryTestCase(unittest.TestCase):
    """The real endpoint, quota enforcement on, a private SQLite database
    and every provider patched so that any call is visible."""

    def setUp(self):
        env = patch.dict(
            "os.environ",
            {"QUOTA_ENFORCEMENT": "on", "AI_RATE_PER_MINUTE": "100", "AI_QUOTA_PER_DAY": "5"},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

        self.factory = attach_sqlite_db(self)
        from app.db.models import Paper

        self.paper_ids = {}
        with self.factory() as db:
            for owner, title, status in (
                (OWNER_A, "Zeta Networks", "indexed"),
                (OWNER_A, "Alpha_Vision", "indexed"),
                (OWNER_A, "Still Indexing", "indexing"),
                (OWNER_A, "Broken Upload", "failed"),
                (OWNER_B, "Someone Else's Paper", "indexed"),
            ):
                paper_id = uuid.uuid4()
                self.paper_ids[title] = str(paper_id)
                db.add(Paper(
                    id=paper_id, owner_id=uuid.UUID(owner), title=title,
                    content_hash=paper_id.hex * 2,
                    storage_path=f"{owner}/{paper_id}/original.pdf",
                    file_size_bytes=1024, status=status,
                ))
            db.commit()

        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

        self.embed = self._start(patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024))
        self.qdrant = self._start(patch("app.api.ask_stream.client"))
        self.qdrant.query_points.return_value.points = []
        self.rerank = self._start(patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r))
        self.groq = self._start(patch("app.api.ask_stream._groq_client"))

    def _start(self, patcher):
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def ask(self, question, **params):
        return self.client.get("/ask-stream", params={"question": question, **params})

    def ai_units_used(self):
        from app.db.models import UsageCounter

        with self.factory() as db:
            row = (
                db.query(UsageCounter)
                .filter(
                    UsageCounter.owner_id == uuid.UUID(OWNER_A),
                    UsageCounter.metric == limits_config.AI_GENERATION,
                    UsageCounter.day == limits_config.utc_today(),
                )
                .one_or_none()
            )
            return row.count if row else 0

    def spend_ai_allowance(self):
        from app.core.quota import charge

        for _ in range(5):
            charge(OWNER_A, limits_config.AI_GENERATION)

    def assertNoProviderCall(self):
        self.embed.assert_not_called()
        self.qdrant.query_points.assert_not_called()
        self.rerank.assert_not_called()
        self.groq.chat.completions.create.assert_not_called()


class TestLibraryAnswer(AskStreamLibraryTestCase):

    def test_answers_from_the_owners_indexed_papers_without_a_provider(self):
        resp = self.ask("how many papers are there?")

        self.assertEqual(resp.status_code, 200)
        events = _events(resp)
        tokens = [e for e in events if e["type"] == "token"]
        self.assertTrue(tokens)
        self.assertEqual(events[-1], {
            "type": "done", "citations": [], "sources": [], "is_followup": False,
        })
        self.assertEqual(events[: len(tokens)], tokens)  # tokens, then done
        self.assertEqual(
            "".join(e["text"] for e in tokens),
            "You have 2 indexed papers:\n\n- Alpha\\_Vision\n- Zeta Networks",
        )
        self.assertNoProviderCall()

    def test_other_owners_and_unindexed_papers_are_not_counted(self):
        text = "".join(e.get("text", "") for e in _events(self.ask("list my papers")))
        self.assertNotIn("Someone Else", text)
        self.assertNotIn("Still Indexing", text)
        self.assertNotIn("Broken Upload", text)

    def test_no_ai_unit_is_charged(self):
        self.ask("How many papers do I have?")
        self.assertEqual(self.ai_units_used(), 0)

    def test_answered_even_when_the_ai_allowance_is_spent(self):
        self.spend_ai_allowance()

        resp = self.ask("Which papers are in my library?")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("You have 2 indexed papers", "".join(
            e.get("text", "") for e in _events(resp)
        ))
        self.assertNoProviderCall()

    def test_a_content_question_is_still_refused_when_the_allowance_is_spent(self):
        self.spend_ai_allowance()

        resp = self.ask("Which papers use vision-language models?")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        self.assertNoProviderCall()

    def test_the_burst_limit_still_applies(self):
        with patch.dict("os.environ", {"AI_RATE_PER_MINUTE": "1"}, clear=False):
            first = self.ask("how many papers are there?")
            second = self.ask("how many papers are there?")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "rate_limited")

    def test_both_turns_are_recorded_without_moving_the_session_pointers(self):
        from app.db.models import ChatMessage, ChatSession

        self.ask("how many papers are there?")

        with self.factory() as db:
            messages = db.query(ChatMessage).order_by(ChatMessage.created_at).all()
            session = db.query(ChatSession).one()
            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].content, "how many papers are there?")
            self.assertTrue(messages[1].content.startswith("You have 2 indexed papers"))
            self.assertIsNone(session.current_topic)
            self.assertIsNone(session.current_paper_id)

    def test_a_database_failure_is_a_sanitised_error_frame(self):
        with patch(
            "app.api.ask_stream.indexed_paper_titles",
            side_effect=RuntimeError("connection to db.internal:5432 refused"),
        ):
            resp = self.ask("how many papers are there?")

        events = _events(resp)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertEqual(events[0]["code"], "internal_error")
        self.assertNotIn("db.internal", resp.text)
        self.assertNoProviderCall()


class TestOrdinaryPathUnchanged(AskStreamLibraryTestCase):

    def test_a_content_question_goes_to_retrieval_and_is_charged(self):
        with patch("app.api.ask_stream.indexed_paper_titles") as library:
            self.ask("Which papers use vision-language models?")

        library.assert_not_called()
        self.embed.assert_called_once()
        self.assertEqual(self.ai_units_used(), 1)

    def test_with_paper_id_a_library_question_takes_the_ordinary_path(self):
        with patch("app.api.ask_stream.indexed_paper_titles") as library:
            self.ask("how many papers are there?", paper_id=self.paper_ids["Zeta Networks"])

        library.assert_not_called()
        self.embed.assert_called_once()

    def test_with_paper_id_the_allowance_is_still_prechecked(self):
        self.spend_ai_allowance()

        resp = self.ask("how many papers are there?", paper_id=self.paper_ids["Zeta Networks"])

        self.assertEqual(resp.status_code, 429)
        self.assertNoProviderCall()


if __name__ == "__main__":
    unittest.main()
