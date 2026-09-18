"""
Phase A — caller-owned context budgets and truncation detection.

Pins the three things Phase A changes, and the several it must not:

  * the shared primitive no longer applies a blind, structure-blind cut to
    whatever context it is handed. The budget belongs to the caller; the
    old 4000-character cut survives only as the default, so Compare,
    Report and Summarize keep today's behaviour until their own phases;

  * a completion that stopped because it ran out of budget
    (finish_reason == "length") is an IncompleteGeneration, never a
    successful answer — and so is never persisted. This is the exact
    failure that stored half a report in the database as a whole one;

  * a genuine "no evidence" result stays distinguishable from a truncated
    one: an empty completion that stopped normally still yields the
    refusal, while an empty completion that ran out of budget does not.

Nothing here reaches a real provider; every completion is a fake object.
"""
import os
import re
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

from app.agents import qa_agent
from app.agents.qa_agent import (
    DEFAULT_MAX_CONTEXT_CHARS,
    REFUSAL,
    SYSTEM_PROMPT,
    GenerationResult,
    generate,
    generate_answer,
)
from app.core.auth import get_current_owner_id
from app.core.providers import (
    GENERATION_INCOMPLETE,
    IncompleteGeneration,
    ProviderFailure,
    classify_provider_error,
)
from app.memory import _store as module_store
from tests.sqlite_harness import attach_sqlite_db

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PAPER_ID = "11111111-1111-4111-8111-111111111111"

INCOMPLETE_TEXT = "The answer could not be completed. Please try again."

#: Words that must never reach a user from a classified failure.
FORBIDDEN = ("groq", "voyage", "qdrant", "billing", "credit", "http", "sk-", "api.", "key")

#: Filler characters for the budget tests. They must not appear anywhere in
#: the prompt scaffolding, or the counts pick up the template itself —
#: "CONTEXT" contains an X and "ANSWER" contains an A.
FILL = "Z"
HEAD = "M"
TAIL = "Z"


def assert_neutral(case, message):
    lowered = message.lower()
    for word in FORBIDDEN:
        case.assertNotIn(word, lowered, f"{word!r} leaked into {message!r}")


def _completion(text, finish_reason="stop", completion_tokens=None, reasoning_tokens=None):
    """A Groq completion, including the metadata Phase A now reads."""
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            completion_tokens=completion_tokens,
            completion_tokens_details=details,
        ),
    )


def _hit(page=1, text="passage text", paper="p.pdf"):
    h = MagicMock()
    h.id = str(uuid.uuid4())
    h.score = 0.9
    h.payload = {
        "paper": paper,
        "source": paper,
        "paper_id": PAPER_ID,
        "page": page,
        "chunk_id": 0,
        "text": text,
        "authors": "A. Author",
        "keywords": "k",
        "abstract": "abstract",
    }
    return h


class GenerateTestCase(unittest.TestCase):
    """Captures exactly what was sent to the model."""

    def respond_with(self, completion):
        return patch.object(
            qa_agent._groq_client.chat.completions, "create", return_value=completion
        )

    def sent_context(self, mock_create):
        """The CONTEXT block the model actually received."""
        messages = mock_create.call_args.kwargs["messages"]
        return messages[1]["content"]

    def sent_system_prompt(self, mock_create):
        return mock_create.call_args.kwargs["messages"][0]["content"]


# ======================================================================
# 1. The context budget belongs to the caller
# ======================================================================
class TestContextBudget(GenerateTestCase):
    def test_default_budget_is_still_4000_so_untouched_callers_are_unchanged(self):
        # Compare, Report and Summarize must behave exactly as before until
        # their own phases; the default is what guarantees that.
        self.assertEqual(DEFAULT_MAX_CONTEXT_CHARS, 4000)

        context = FILL * 12000
        with self.respond_with(_completion("ok")) as create:
            generate("q", context)

        self.assertEqual(self.sent_context(create).count(FILL), 4000)

    def test_a_caller_budget_is_honoured_in_full(self):
        # The regression that mattered: 12000 characters built by the
        # caller must arrive intact when the caller asks for 12000.
        context = FILL * 12000
        with self.respond_with(_completion("ok")) as create:
            generate("q", context, max_context_chars=12000)

        self.assertEqual(self.sent_context(create).count(FILL), 12000)

    def test_none_disables_truncation_entirely(self):
        context = FILL * 30000
        with self.respond_with(_completion("ok")) as create:
            generate("q", context, max_context_chars=None)

        self.assertEqual(self.sent_context(create).count(FILL), 30000)

    def test_a_context_shorter_than_the_budget_is_not_padded_or_altered(self):
        with self.respond_with(_completion("ok")) as create:
            generate("q", "short context", max_context_chars=4000)

        self.assertIn("short context", self.sent_context(create))

    def test_the_tail_is_what_gets_dropped_which_is_why_compare_lost_paper_two(self):
        # Documents the mechanism rather than the symptom: truncation keeps
        # the head, so whatever a caller appends last disappears first.
        context = (HEAD * 4000) + (TAIL * 4000)
        with self.respond_with(_completion("ok")) as create:
            generate("q", context)

        sent = self.sent_context(create)
        self.assertEqual(sent.count(HEAD), 4000)
        self.assertEqual(sent.count(TAIL), 0)


# ======================================================================
# 2. finish_reason == "length" is an incomplete generation
# ======================================================================
class TestIncompleteGeneration(GenerateTestCase):
    def test_a_length_stop_raises_instead_of_returning_truncated_text(self):
        with self.respond_with(_completion("half a sentence (", finish_reason="length")):
            with self.assertRaises(IncompleteGeneration) as caught:
                generate("q", "context")

        self.assertEqual(caught.exception.code, GENERATION_INCOMPLETE)
        self.assertEqual(caught.exception.message, INCOMPLETE_TEXT)
        self.assertEqual(caught.exception.finish_reason, "length")

    def test_the_truncated_text_is_discarded_not_returned(self):
        truncated = "The paper introduces the framework that separates dialog ("
        with self.respond_with(_completion(truncated, finish_reason="length")):
            with self.assertRaises(IncompleteGeneration) as caught:
                generate("q", "context")

        self.assertNotIn("separates dialog", str(caught.exception))
        self.assertNotIn("separates dialog", caught.exception.message)

    def test_generate_answer_raises_too_so_no_caller_can_bypass_it(self):
        with self.respond_with(_completion("cut off", finish_reason="length")):
            with self.assertRaises(IncompleteGeneration):
                generate_answer("q", "context")

    def test_length_stop_with_empty_content_is_incomplete_not_a_refusal(self):
        # The reasoning-model case: the whole budget went to reasoning, so
        # content is None. That is NOT "no evidence found".
        with self.respond_with(_completion(None, finish_reason="length")):
            with self.assertRaises(IncompleteGeneration):
                generate("q", "context")

    def test_the_message_is_neutral_and_leaks_nothing(self):
        failure = IncompleteGeneration()
        assert_neutral(self, failure.message)
        self.assertEqual(
            set(failure.to_payload()), {"status", "code", "message"}
        )
        assert_neutral(self, failure.to_payload()["message"])

    def test_it_is_not_worded_as_a_rate_limit(self):
        # The frontend's rate-limit recognition must not fire on this: it is
        # not a rate limit and must not offer a "wait and retry" path.
        frontend = re.compile(
            r"rate.?limit|too many requests|quota|insufficient credit|billing", re.I
        )
        self.assertFalse(frontend.search(INCOMPLETE_TEXT))

    def test_it_classifies_as_itself(self):
        failure = IncompleteGeneration()
        classified = classify_provider_error(failure)
        self.assertIs(classified, failure)
        self.assertIsInstance(failure, ProviderFailure)
        self.assertEqual(classified.code, GENERATION_INCOMPLETE)


# ======================================================================
# 3. A genuine refusal is still a refusal
# ======================================================================
class TestGenuineRefusalPreserved(GenerateTestCase):
    def test_empty_content_that_stopped_normally_is_still_the_refusal(self):
        with self.respond_with(_completion("", finish_reason="stop")):
            result = generate("q", "context")

        self.assertEqual(result.text, REFUSAL)
        self.assertEqual(result.finish_reason, "stop")

    def test_none_content_that_stopped_normally_is_still_the_refusal(self):
        with self.respond_with(_completion(None, finish_reason="stop")):
            self.assertEqual(generate("q", "context").text, REFUSAL)

    def test_a_model_authored_refusal_is_passed_through_unchanged(self):
        with self.respond_with(_completion(REFUSAL)):
            self.assertEqual(generate_answer("q", "context"), REFUSAL)

    def test_an_unclassified_error_still_falls_back_to_the_refusal(self):
        with patch.object(
            qa_agent._groq_client.chat.completions, "create", side_effect=ValueError("app bug")
        ):
            self.assertEqual(generate("q", "context").text, REFUSAL)


# ======================================================================
# 4. Metadata is returned
# ======================================================================
class TestGenerationMetadata(GenerateTestCase):
    def test_a_successful_completion_carries_its_metadata(self):
        with self.respond_with(
            _completion("Answer.", completion_tokens=140, reasoning_tokens=96)
        ):
            result = generate("q", "context")

        self.assertIsInstance(result, GenerationResult)
        self.assertEqual(result.text, "Answer.")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.completion_tokens, 140)
        self.assertEqual(result.reasoning_tokens, 96)

    def test_missing_usage_is_tolerated(self):
        # Older/odd responses must not crash the request.
        bare = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")]
        )
        with self.respond_with(bare):
            result = generate("q", "context")

        self.assertEqual(result.text, "ok")
        self.assertIsNone(result.completion_tokens)
        self.assertIsNone(result.reasoning_tokens)

    def test_generate_answer_still_returns_a_plain_string(self):
        # The three existing callers assign this straight into a response
        # body; it must not become an object.
        with self.respond_with(_completion("Answer.")):
            answer = generate_answer("q", "context")

        self.assertIsInstance(answer, str)
        self.assertEqual(answer, "Answer.")


# ======================================================================
# 5. The system prompt is caller-selectable
# ======================================================================
class TestSystemPrompt(GenerateTestCase):
    def test_the_qa_prompt_is_still_the_default(self):
        with self.respond_with(_completion("ok")) as create:
            generate("q", "context")

        self.assertEqual(self.sent_system_prompt(create), SYSTEM_PROMPT)

    def test_a_caller_can_supply_its_own(self):
        with self.respond_with(_completion("ok")) as create:
            generate("q", "context", system_prompt="CUSTOM PROMPT")

        self.assertEqual(self.sent_system_prompt(create), "CUSTOM PROMPT")


# ======================================================================
# 6. Endpoints: nothing incomplete is ever persisted
# ======================================================================
class EndpointTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)

    def tearDown(self):
        module_store.clear(USER_A)

    def count(self, model, **filters):
        with self.factory() as db:
            q = db.query(model)
            for key, value in filters.items():
                q = q.filter(getattr(model, key) == value)
            return q.count()

    def groq_returns(self, completion):
        return patch.object(
            qa_agent._groq_client.chat.completions, "create", return_value=completion
        )


class TestResearchDoesNotPersistIncompleteReports(EndpointTestCase):
    def run_research(self, completion):
        with patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch(
            "app.api.research.rerank_results", side_effect=lambda q, r: r
        ), self.groq_returns(completion):
            qdrant.query_points.return_value.points = [_hit()]
            return self.client.get("/research", params={"query": "vision models"})

    def test_a_length_stopped_report_is_not_saved(self):
        from app.db.models import Report

        resp = self.run_research(
            _completion("### Executive Summary\n\nThe paper introduces (", finish_reason="length")
        )

        self.assertEqual(
            resp.json(),
            {"status": "error", "code": GENERATION_INCOMPLETE, "message": INCOMPLETE_TEXT},
        )
        # The exact defect: a half-written report reaching the database.
        self.assertEqual(self.count(Report), 0)
        self.assertNotIn("Executive Summary", resp.text)

    def test_a_complete_report_is_still_saved_exactly_once(self):
        # Guards against over-correcting into "never persist anything".
        from app.db.models import Report

        resp = self.run_research(_completion("### Executive Summary\n\nComplete report."))

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(self.count(Report), 1)

    def test_the_response_leaks_no_truncated_text(self):
        resp = self.run_research(_completion("secret partial sentence (", finish_reason="length"))
        self.assertNotIn("secret partial", resp.text)
        assert_neutral(self, resp.json()["message"])


class TestOtherEndpointsReportIncompleteNeutrally(EndpointTestCase):
    def test_compare(self):
        hits = [_hit(paper="a.pdf"), _hit(paper="b.pdf")]
        with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
            "app.api.compare_papers.client"
        ) as qdrant, patch(
            "app.api.compare_papers.rerank_results", side_effect=lambda q, r: r
        ), self.groq_returns(_completion("| Category |", finish_reason="length")):
            qdrant.query_points.return_value.points = hits
            resp = self.client.get(
                "/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"}
            )

        self.assertEqual(
            resp.json(),
            {"status": "error", "code": GENERATION_INCOMPLETE, "message": INCOMPLETE_TEXT},
        )
        # It must not look like "no evidence found", which is what sent the
        # production smoke test chasing a retrieval bug.
        self.assertNotIn("could not find", resp.text)

    def test_summarize(self):
        with patch("app.api.summarize_paper.encode_query", return_value=[0.1]), patch(
            "app.api.summarize_paper.client"
        ) as qdrant, patch(
            "app.api.summarize_paper.rerank_results", side_effect=lambda q, r: r
        ), self.groq_returns(_completion("Partial summary (", finish_reason="length")):
            qdrant.query_points.return_value.points = [_hit(paper="p.pdf")]
            resp = self.client.get("/summarize-paper", params={"paper_name": "p.pdf"})

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)
        self.assertNotIn("could not find", resp.text)


# ======================================================================
# 7. Ask is untouched by Phase A
# ======================================================================
class TestAskUnchanged(unittest.TestCase):
    def test_ask_stream_does_not_use_the_shared_primitive(self):
        from app.api import ask_stream

        self.assertFalse(hasattr(ask_stream, "generate_answer"))
        self.assertFalse(hasattr(ask_stream, "generate"))

    def test_ask_keeps_its_own_4000_character_evidence_budget(self):
        from app.api.ask_stream import MAX_CONTEXT, SEARCH_LIMIT, TOP_CHUNKS

        self.assertEqual(MAX_CONTEXT, 4000)
        self.assertEqual(SEARCH_LIMIT, 8)
        self.assertEqual(TOP_CHUNKS, 4)


if __name__ == "__main__":
    unittest.main()
