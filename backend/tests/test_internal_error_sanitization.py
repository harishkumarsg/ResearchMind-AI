"""
Unclassified exceptions must not reach the client as raw text.

Five endpoints ended their generic `except Exception as e:` with
`{"status": "error", "message": str(e)}`. A provider failure was already
neutral — classify_provider_error() saw to that — but anything the
application did NOT classify was rendered verbatim: a SQLAlchemy driver
error naming a table or column, an attribute error naming an internal
symbol, a uuid ValueError echoing its input.

Those now return an authored message instead. The full text still goes
to the server log; only the client-facing body changed.

Pinned here:

  * the raw text never appears in the response;
  * the body is status/code/message with code == "internal_error";
  * classified provider failures keep their OWN codes — the neutral
    internal message must not swallow a timeout and make a provider
    outage look like an application bug;
  * a UsageLimitError still propagates to the 429/503 handler rather
    than being flattened into a 200 error body;
  * an unclassified failure in /research persists no report.

Fully offline: providers are fake objects, Qdrant is a MagicMock and the
database is in-memory SQLite.
"""
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

from app.agents import qa_agent
from app.core.auth import get_current_owner_id
from app.core.limits import QuotaExceeded
from app.core.providers import (
    INTERNAL_ERROR,
    PROVIDER_TIMEOUT,
    internal_error_payload,
)
from app.memory import _store as module_store
from tests.sqlite_harness import attach_sqlite_db

OWNER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PAPER_ID = "11111111-1111-4111-8111-111111111111"
PAPER = "p.pdf"

NEUTRAL_TEXT = "Something went wrong on our side. Please try again."

#: Text that must never survive into a response body.
RAW_MESSAGES = {
    "runtime": "boom",
    "sqlalchemy": 'relation "usage_counters" does not exist LINE 1: select count',
    "attribute": "'NoneType' object has no attribute 'payload'",
    "uuid": "badly formed hexadecimal UUID string",
}

#: Same vocabulary the P2A suite forbids in a user-facing message.
FORBIDDEN = ("groq", "voyage", "qdrant", "billing", "credit", "http", "sk-", "api.", "key")


def _completion(text, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(completion_tokens=None, completion_tokens_details=None),
    )


def _hit(paper=PAPER, text="passage text", page=1):
    h = MagicMock()
    h.id = str(uuid.uuid4())
    h.score = 0.9
    h.payload = {
        "owner_id": OWNER_A,
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


class SanitizationTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)

    def tearDown(self):
        module_store.clear(OWNER_A)

    def assert_sanitized(self, resp, raw):
        """The body is the authored payload, and the raw text is gone."""
        body = resp.json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], INTERNAL_ERROR)
        self.assertEqual(body["message"], NEUTRAL_TEXT)
        self.assertNotIn(raw, resp.text)

        lowered = body["message"].lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, lowered, f"{word!r} leaked into {body['message']!r}")


# ======================================================================
# 1. The payload helper on its own
# ======================================================================
class TestInternalErrorPayload(unittest.TestCase):
    def test_the_shape_matches_the_provider_failure_contract(self):
        self.assertEqual(
            internal_error_payload(),
            {"status": "error", "code": INTERNAL_ERROR, "message": NEUTRAL_TEXT},
        )

    def test_the_code_is_distinct_from_every_provider_code(self):
        self.assertNotEqual(INTERNAL_ERROR, PROVIDER_TIMEOUT)

    def test_it_is_not_an_exception_type(self):
        from app.core.providers import ProviderFailure

        # A dict, deliberately: making it a ProviderFailure would let an
        # application bug be caught by branches meant for provider trouble.
        self.assertNotIsInstance(internal_error_payload(), ProviderFailure)

    def test_the_message_carries_no_forbidden_vocabulary(self):
        lowered = internal_error_payload()["message"].lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, lowered)


# ======================================================================
# 2. Every affected endpoint
# ======================================================================
class TestResearch(SanitizationTestCase):
    def run_research(self, exc):
        with patch("app.api.research.encode_query", side_effect=exc):
            return self.client.get("/research", params={"query": "vision models"})

    def test_a_runtime_error_is_sanitized(self):
        raw = RAW_MESSAGES["runtime"]
        self.assert_sanitized(self.run_research(RuntimeError(raw)), raw)

    def test_a_database_style_error_is_sanitized(self):
        raw = RAW_MESSAGES["sqlalchemy"]
        resp = self.run_research(Exception(raw))

        self.assert_sanitized(resp, raw)
        # The distinctive fragments must be gone, not merely the whole string.
        self.assertNotIn("usage_counters", resp.text)
        self.assertNotIn("relation", resp.text)

    def test_an_attribute_error_is_sanitized(self):
        raw = RAW_MESSAGES["attribute"]
        self.assert_sanitized(self.run_research(AttributeError(raw)), raw)

    def test_no_report_is_persisted_on_an_unclassified_failure(self):
        from app.db.models import Report

        self.run_research(RuntimeError(RAW_MESSAGES["runtime"]))

        with self.factory() as db:
            self.assertEqual(db.query(Report).count(), 0)


class TestSummarize(SanitizationTestCase):
    def test_a_runtime_error_is_sanitized(self):
        raw = RAW_MESSAGES["runtime"]

        with patch("app.api.summarize_paper.encode_query", side_effect=RuntimeError(raw)):
            resp = self.client.get("/summarize-paper", params={"paper_name": PAPER})

        self.assert_sanitized(resp, raw)


class TestComparePapers(SanitizationTestCase):
    def test_a_runtime_error_is_sanitized(self):
        raw = RAW_MESSAGES["attribute"]

        with patch("app.api.compare_papers.encode_query", side_effect=AttributeError(raw)):
            resp = self.client.get(
                "/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"}
            )

        self.assert_sanitized(resp, raw)


class TestPaperDetails(SanitizationTestCase):
    def test_a_runtime_error_is_sanitized(self):
        raw = RAW_MESSAGES["uuid"]

        with patch("app.api.paper_details.encode_query", side_effect=ValueError(raw)):
            resp = self.client.get("/paper-details", params={"paper_name": PAPER})

        self.assert_sanitized(resp, raw)


class TestSearch(SanitizationTestCase):
    def test_a_runtime_error_is_sanitized(self):
        raw = RAW_MESSAGES["runtime"]

        with patch("app.api.search.encode_query", side_effect=RuntimeError(raw)):
            resp = self.client.get("/search", params={"query": "x"})

        self.assert_sanitized(resp, raw)


# ======================================================================
# 3. What must NOT be swallowed by the new branch
# ======================================================================
class TestClassifiedFailuresKeepTheirOwnCodes(SanitizationTestCase):
    def test_a_provider_timeout_still_returns_provider_timeout(self):
        import groq

        with patch(
            "app.api.search.encode_query",
            side_effect=groq.APITimeoutError(request=MagicMock()),
        ):
            resp = self.client.get("/search", params={"query": "x"})

        body = resp.json()
        # A provider outage must stay distinguishable from an application
        # bug, or an operator reading logs cannot tell them apart.
        self.assertEqual(body["code"], PROVIDER_TIMEOUT)
        self.assertNotEqual(body["code"], INTERNAL_ERROR)
        self.assertNotEqual(body["message"], NEUTRAL_TEXT)

    def test_a_length_stopped_generation_still_returns_its_own_code(self):
        from app.core.providers import GENERATION_INCOMPLETE

        with patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch(
            "app.api.research.rerank_results", side_effect=lambda q, r: r
        ), patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=_completion("half a report (", finish_reason="length"),
        ):
            qdrant.query_points.return_value.points = [_hit()]
            resp = self.client.get("/research", params={"query": "vision models"})

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)


class TestUsageLimitStillPropagates(SanitizationTestCase):
    def test_a_quota_rejection_is_not_flattened_into_an_internal_error(self):
        # It must reach the app-level handler as a real 429, not be caught
        # by the generic except and rendered as a 200 internal error.
        limit = QuotaExceeded("Daily limit reached.", retry_after_seconds=60)

        with patch("app.api.research.encode_query", side_effect=limit):
            resp = self.client.get("/research", params={"query": "vision models"})

        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertNotEqual(body["code"], INTERNAL_ERROR)
        self.assertIn("Retry-After", resp.headers)


# ======================================================================
# 4. The success path is untouched
# ======================================================================
class TestSuccessfulCallsUnchanged(SanitizationTestCase):
    def test_a_normal_research_call_still_succeeds(self):
        with patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch(
            "app.api.research.rerank_results", side_effect=lambda q, r: r
        ), patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=_completion("### Findings\n\nA complete report."),
        ):
            qdrant.query_points.return_value.points = [_hit()]
            resp = self.client.get("/research", params={"query": "vision models"})

        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertNotIn("code", body)


if __name__ == "__main__":
    unittest.main()
