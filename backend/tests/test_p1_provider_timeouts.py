"""
P1 provider timeout hardening.

Pins three things:

  * every provider client is built with explicit, bounded timeouts, and
    Groq's retry count is deliberately reduced to 1 (at most 2 attempts);
  * a provider timeout or outage becomes a bounded, neutral failure on
    every endpoint instead of a hang, a raw exception string, or — in the
    case of generate_answer — a fake "no information" refusal;
  * everything that must NOT change does not: the Voyage rate-limit
    backoff, quota accounting, successful responses, /health.

Nothing here reaches a real provider. The Groq retry tests drive the real
Groq SDK against an offline httpx.MockTransport; everything else is mocked
at the call site. Streaming time is faked, so no test sleeps for real.
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

import groq
import httpx
import voyageai.error as voyage_error
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.core import providers
from app.core.auth import get_current_owner_id
from app.core.providers import (
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    ProviderFailure,
    classify_provider_error,
    groq_client_options,
    voyage_client_options,
)
from app.memory import _store as module_store
from tests.sqlite_harness import attach_sqlite_db

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PAPER_ID = "11111111-1111-4111-8111-111111111111"

TIMEOUT_TEXT = "The research service took too long to respond. Please try again."
UNAVAILABLE_TEXT = "The research service is temporarily unavailable. Please try again shortly."
RATE_LIMITED_TEXT = "The research service is rate-limited right now. Please wait a moment and try again."

#: Words that must never reach a user from a classified provider failure.
FORBIDDEN = ("groq", "voyage", "qdrant", "billing", "credit", "http", "sk-", "api.", "key")

_REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _groq_timeout():
    return groq.APITimeoutError(request=_REQ)


def _hit(page=1, text="passage text", paper="p.pdf", point_id=None):
    h = MagicMock()
    h.id = point_id or str(uuid.uuid4())
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


def _completion(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeStream:
    """Stands in for the Groq SDK Stream: iterable, and records close()."""

    def __init__(self, chunks=(), raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self.closed = False
        self.consumed = 0

    def __iter__(self):
        for text in self._chunks:
            self.consumed += 1
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
        if self._raise_after is not None:
            raise self._raise_after

    def close(self):
        self.closed = True


def _sse_events(text):
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            events.append(json.loads(frame[len("data:") :].strip()))
    return events


def assert_neutral(case, message):
    lowered = message.lower()
    for word in FORBIDDEN:
        case.assertNotIn(word, lowered, f"{word!r} leaked into {message!r}")


# ======================================================================
# 1. Client configuration
# ======================================================================
class TestClientConfiguration(unittest.TestCase):
    def test_both_groq_clients_use_30s_read_5s_connect_and_one_retry(self):
        from app.agents import qa_agent
        from app.api import ask_stream

        expected = groq.Timeout(30.0, connect=5.0)
        for name, client in (("qa_agent", qa_agent._groq_client), ("ask_stream", ask_stream._groq_client)):
            with self.subTest(client=name):
                self.assertEqual(client.timeout, expected)
                self.assertEqual(client.max_retries, 1)

    def test_one_retry_is_an_intentional_reduction_from_the_sdk_default(self):
        self.assertEqual(groq.DEFAULT_MAX_RETRIES, 2)
        self.assertEqual(groq_client_options()["max_retries"], 1)

    def test_voyage_embedder_client_timeout_30_and_sdk_retries_off(self):
        from app.rag import embedder

        embedder._client = None
        self.addCleanup(setattr, embedder, "_client", None)
        with patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"}, clear=False):
            client = embedder._get_client()

        self.assertEqual(client._params["request_timeout"], 30.0)
        self.assertEqual(client.max_retries, 0)

    def test_voyage_reranker_client_timeout_30_and_sdk_retries_off(self):
        from app.rag import reranker

        reranker._client = None
        self.addCleanup(setattr, reranker, "_client", None)
        with patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"}, clear=False):
            client = reranker._get_client()

        self.assertEqual(client._params["request_timeout"], 30.0)
        self.assertEqual(client.max_retries, 0)

    def test_qdrant_timeout_remains_120(self):
        from app.rag.vector_store import client

        self.assertEqual(client._client._timeout, 120)

    def test_environment_overrides_are_honoured(self):
        with patch.dict(
            "os.environ",
            {
                "GROQ_TIMEOUT_SECONDS": "12",
                "GROQ_CONNECT_TIMEOUT_SECONDS": "3",
                "GROQ_MAX_RETRIES": "0",
                "GROQ_STREAM_DEADLINE_SECONDS": "45",
                "VOYAGE_TIMEOUT_SECONDS": "8",
            },
            clear=False,
        ):
            options = groq_client_options()
            self.assertEqual(options["timeout"], groq.Timeout(12.0, connect=3.0))
            self.assertEqual(options["max_retries"], 0)  # zero is valid: retries off
            self.assertEqual(providers.groq_stream_deadline_seconds(), 45.0)
            self.assertEqual(voyage_client_options(), {"timeout": 8.0, "max_retries": 0})

    def test_invalid_values_fall_back_to_safe_defaults(self):
        with patch.dict(
            "os.environ",
            {"GROQ_TIMEOUT_SECONDS": "banana", "GROQ_MAX_RETRIES": "-3", "VOYAGE_TIMEOUT_SECONDS": "0"},
            clear=False,
        ):
            self.assertEqual(groq_client_options()["timeout"], groq.Timeout(30.0, connect=5.0))
            self.assertEqual(groq_client_options()["max_retries"], 1)
            self.assertEqual(providers.voyage_timeout_seconds(), 30.0)


# ======================================================================
# 2-3. Groq SDK retry semantics, against the real SDK, offline
# ======================================================================
class TestGroqSdkRetrySemantics(unittest.TestCase):
    def build_client(self, handler):
        return groq.Groq(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            **groq_client_options(),
        )

    def call(self, client):
        return client.chat.completions.create(
            model="test-model", messages=[{"role": "user", "content": "hi"}]
        )

    def test_a_timeout_is_attempted_exactly_twice_then_raises(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            raise httpx.ReadTimeout("timed out", request=request)

        client = self.build_client(handler)
        with patch("groq._base_client.time.sleep"):
            with self.assertRaises(groq.APITimeoutError):
                self.call(client)

        self.assertEqual(len(attempts), 2)

    def test_rate_limit_is_retried_once_under_the_intentional_max_retries_1(self):
        # Deliberately NOT "unchanged": the SDK default would make 3
        # attempts. With GROQ_MAX_RETRIES=1 a 429 is attempted twice.
        attempts = []

        def handler(request):
            attempts.append(request)
            return httpx.Response(429, json={"error": {"message": "rate limit"}}, request=request)

        client = self.build_client(handler)
        with patch("groq._base_client.time.sleep"):
            with self.assertRaises(groq.RateLimitError):
                self.call(client)

        self.assertEqual(len(attempts), 2)

    def test_retryable_5xx_is_also_bounded_to_two_attempts(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            return httpx.Response(503, json={"error": {"message": "down"}}, request=request)

        client = self.build_client(handler)
        with patch("groq._base_client.time.sleep"):
            with self.assertRaises(groq.InternalServerError):
                self.call(client)

        self.assertEqual(len(attempts), 2)

    def test_retries_can_be_disabled_entirely(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            raise httpx.ReadTimeout("timed out", request=request)

        with patch.dict("os.environ", {"GROQ_MAX_RETRIES": "0"}, clear=False):
            client = self.build_client(handler)
        with self.assertRaises(groq.APITimeoutError):
            self.call(client)

        self.assertEqual(len(attempts), 1)


# ======================================================================
# 10. Classification and leakage
# ======================================================================
class TestProviderErrorClassification(unittest.TestCase):
    SECRET = "Groq voyage billing credit https://api.groq.com sk-secret-key"

    def cases(self):
        response_429 = httpx.Response(429, request=_REQ)
        response_500 = httpx.Response(500, request=_REQ)
        return [
            ("groq timeout", groq.APITimeoutError(request=_REQ), PROVIDER_TIMEOUT),
            ("groq connection", groq.APIConnectionError(message=self.SECRET, request=_REQ), PROVIDER_UNAVAILABLE),
            ("groq rate limit", groq.RateLimitError(self.SECRET, response=response_429, body=None), PROVIDER_RATE_LIMITED),
            ("groq 5xx", groq.InternalServerError(self.SECRET, response=response_500, body=None), PROVIDER_UNAVAILABLE),
            ("httpx read timeout (mid-stream)", httpx.ReadTimeout(self.SECRET, request=_REQ), PROVIDER_TIMEOUT),
            ("voyage timeout", voyage_error.Timeout(self.SECRET), PROVIDER_TIMEOUT),
            ("voyage connection", voyage_error.APIConnectionError(self.SECRET), PROVIDER_UNAVAILABLE),
            ("voyage rate limit", voyage_error.RateLimitError(self.SECRET), PROVIDER_RATE_LIMITED),
            ("voyage unavailable", voyage_error.ServiceUnavailableError(self.SECRET), PROVIDER_UNAVAILABLE),
            ("voyage server error", voyage_error.ServerError(self.SECRET), PROVIDER_UNAVAILABLE),
            ("qdrant timeout", ResponseHandlingException(httpx.ReadTimeout(self.SECRET, request=_REQ)), PROVIDER_TIMEOUT),
            ("qdrant connection", ResponseHandlingException(httpx.ConnectError(self.SECRET, request=_REQ)), PROVIDER_UNAVAILABLE),
            ("qdrant 503", UnexpectedResponse(503, "Service Unavailable", self.SECRET.encode(), httpx.Headers()), PROVIDER_UNAVAILABLE),
            ("qdrant 429", UnexpectedResponse(429, "Too Many Requests", self.SECRET.encode(), httpx.Headers()), PROVIDER_RATE_LIMITED),
        ]

    def test_every_provider_failure_is_classified_with_the_right_code(self):
        for label, exc, code in self.cases():
            with self.subTest(label):
                failure = classify_provider_error(exc)
                self.assertIsNotNone(failure, f"{label} was not classified")
                self.assertEqual(failure.code, code)

    def test_no_classified_message_leaks_provider_detail(self):
        for label, exc, _ in self.cases():
            with self.subTest(label):
                failure = classify_provider_error(exc)
                assert_neutral(self, failure.message)
                self.assertNotIn(self.SECRET, failure.message)
                payload = failure.to_payload()
                self.assertEqual(set(payload), {"status", "code", "message"})
                assert_neutral(self, json.dumps(payload))

    def test_rate_limit_wording_still_matches_the_frontend_rate_limit_check(self):
        import re

        frontend = re.compile(r"rate.?limit|too many requests|quota|insufficient credit|billing", re.I)
        self.assertTrue(frontend.search(RATE_LIMITED_TEXT))
        self.assertFalse(frontend.search(TIMEOUT_TEXT))
        self.assertFalse(frontend.search(UNAVAILABLE_TEXT))

    def test_an_ordinary_application_error_is_not_classified(self):
        for exc in (ValueError("bad input"), KeyError("x"), RuntimeError("boom")):
            self.assertIsNone(classify_provider_error(exc))

    def test_qdrant_4xx_other_than_429_is_not_a_provider_outage(self):
        exc = UnexpectedResponse(404, "Not Found", b"", httpx.Headers())
        self.assertIsNone(classify_provider_error(exc))


# ======================================================================
# generate_answer: provider failures re-raised, refusal otherwise
# ======================================================================
class TestGenerateAnswer(unittest.TestCase):
    def test_a_provider_failure_is_raised_not_disguised_as_a_refusal(self):
        from app.agents import qa_agent

        with patch.object(qa_agent._groq_client.chat.completions, "create", side_effect=_groq_timeout()):
            with self.assertRaises(ProviderFailure) as ctx:
                qa_agent.generate_answer("q", "context")
        self.assertEqual(ctx.exception.code, PROVIDER_TIMEOUT)

    def test_an_unclassified_failure_keeps_the_existing_refusal(self):
        from app.agents import qa_agent

        with patch.object(qa_agent._groq_client.chat.completions, "create", side_effect=RuntimeError("x")):
            answer = qa_agent.generate_answer("q", "context")
        self.assertEqual(answer, "I could not find that information in the indexed papers.")

    def test_a_successful_call_is_unchanged(self):
        from app.agents import qa_agent

        with patch.object(qa_agent._groq_client.chat.completions, "create", return_value=_completion("Answer.")):
            self.assertEqual(qa_agent.generate_answer("q", "context"), "Answer.")


# ======================================================================
# Endpoint harness
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


# ======================================================================
# 4. Groq non-streaming timeout on the three JSON endpoints
# ======================================================================
class TestGroqNonStreamingTimeout(EndpointTestCase):
    def groq_times_out(self):
        from app.agents import qa_agent

        return patch.object(qa_agent._groq_client.chat.completions, "create", side_effect=_groq_timeout())

    def test_compare_returns_the_neutral_provider_timeout(self):
        hits = [_hit(paper="a.pdf"), _hit(paper="b.pdf")]
        with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
            "app.api.compare_papers.client"
        ) as qdrant, patch("app.api.compare_papers.rerank_results", side_effect=lambda q, r: r), self.groq_times_out():
            qdrant.query_points.return_value.points = hits
            resp = self.client.get("/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"})

        body = resp.json()
        self.assertEqual(body, {"status": "error", "code": PROVIDER_TIMEOUT, "message": TIMEOUT_TEXT})
        self.assertNotIn("could not find", resp.text)

    def test_summarize_returns_the_neutral_provider_timeout(self):
        with patch("app.api.summarize_paper.encode_query", return_value=[0.1]), patch(
            "app.api.summarize_paper.client"
        ) as qdrant, patch("app.api.summarize_paper.rerank_results", side_effect=lambda q, r: r), self.groq_times_out():
            qdrant.query_points.return_value.points = [_hit(paper="p.pdf")]
            resp = self.client.get("/summarize-paper", params={"paper_name": "p.pdf"})

        self.assertEqual(resp.json()["code"], PROVIDER_TIMEOUT)
        self.assertEqual(resp.json()["message"], TIMEOUT_TEXT)
        self.assertNotIn("could not find", resp.text)

    def test_research_returns_the_neutral_timeout_and_saves_no_report(self):
        from app.db.models import Report

        with patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch("app.api.research.rerank_results", side_effect=lambda q, r: r), self.groq_times_out():
            qdrant.query_points.return_value.points = [_hit()]
            resp = self.client.get("/research", params={"query": "vision models"})

        self.assertEqual(resp.json()["code"], PROVIDER_TIMEOUT)
        # Previously the refusal string was persisted as if it were a report.
        self.assertEqual(self.count(Report), 0)


# ======================================================================
# 5. Groq streaming
# ======================================================================
class TestGroqStreaming(EndpointTestCase):
    def run_ask(self, create_side_effect=None, stream=None, fake_clock=None):
        from app.api import ask_stream

        patches = [
            patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024),
            patch("app.api.ask_stream.client"),
            patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r),
            patch.object(ask_stream._groq_client.chat.completions, "create"),
        ]
        if fake_clock is not None:
            patches.append(patch("app.api.ask_stream.time", SimpleNamespace(monotonic=fake_clock)))

        entered = [p.__enter__() for p in patches]
        try:
            qdrant, create = entered[1], entered[3]
            qdrant.query_points.return_value.points = [_hit(text="grounded evidence")]
            if create_side_effect is not None:
                create.side_effect = create_side_effect
            else:
                create.return_value = stream
            resp = self.client.get("/ask-stream", params={"question": "What is the research about?"})
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
        return resp, _sse_events(resp.text)

    def assert_provider_timeout_frame(self, events):
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], PROVIDER_TIMEOUT)
        self.assertEqual(errors[0]["text"], TIMEOUT_TEXT)
        self.assertFalse(any(e.get("type") == "done" for e in events))

    def assistant_turns(self):
        from app.db.models import ChatMessage

        return self.count(ChatMessage, role="assistant")

    def test_timeout_establishing_the_stream(self):
        resp, events = self.run_ask(create_side_effect=_groq_timeout())

        self.assertEqual(resp.status_code, 200)
        self.assert_provider_timeout_frame(events)
        self.assertEqual(self.assistant_turns(), 0)

    def test_read_timeout_midway_through_the_stream(self):
        stream = FakeStream(chunks=["Partial "], raise_after=httpx.ReadTimeout("stalled", request=_REQ))

        _, events = self.run_ask(stream=stream)

        self.assertEqual([e["text"] for e in events if e.get("type") == "token"], ["Partial "])
        self.assert_provider_timeout_frame(events)
        self.assertTrue(stream.closed)
        self.assertEqual(self.assistant_turns(), 0)
        assert_neutral(self, json.dumps(events[-1]))

    def test_slow_but_continuous_stream_is_stopped_at_the_deadline(self):
        # A fake clock: 0s when the request is made, then 40s more per
        # chunk. The third check (120s) is past the 90s deadline.
        ticks = iter([0.0, 40.0, 80.0, 120.0, 160.0, 200.0])
        stream = FakeStream(chunks=["a", "b", "c", "d", "e"])

        _, events = self.run_ask(stream=stream, fake_clock=lambda: next(ticks))

        self.assertEqual([e["text"] for e in events if e.get("type") == "token"], ["a", "b"])
        self.assert_provider_timeout_frame(events)
        # Consumption stopped at the deadline rather than draining the stream,
        # and the upstream response was closed.
        self.assertEqual(stream.consumed, 3)
        self.assertTrue(stream.closed)
        self.assertEqual(self.assistant_turns(), 0)

    def test_a_normal_stream_is_unchanged_and_still_closed(self):
        stream = FakeStream(chunks=["The ", "answer."])

        _, events = self.run_ask(stream=stream)

        self.assertEqual([e["text"] for e in events if e.get("type") == "token"], ["The ", "answer."])
        self.assertTrue(any(e.get("type") == "done" for e in events))
        self.assertFalse(any(e.get("type") == "error" for e in events))
        self.assertTrue(stream.closed)
        self.assertEqual(self.assistant_turns(), 1)


# ======================================================================
# 6. Voyage query-embedding timeout
# ======================================================================
class TestVoyageQueryTimeout(EndpointTestCase):
    def voyage_times_out(self):
        voyage = MagicMock()
        voyage.embed.side_effect = voyage_error.Timeout("Request timed out: https://api.voyageai.com")
        return patch("app.rag.embedder._get_client", return_value=voyage), voyage

    def test_search(self):
        ctx, voyage = self.voyage_times_out()
        with ctx:
            resp = self.client.get("/search", params={"query": "transformers"})

        self.assertEqual(resp.json(), {"status": "error", "code": PROVIDER_TIMEOUT, "message": TIMEOUT_TEXT})
        self.assertEqual(voyage.embed.call_count, 1)

    def test_paper_details(self):
        ctx, voyage = self.voyage_times_out()
        with ctx:
            resp = self.client.get("/paper-details", params={"paper_name": "p.pdf"})

        self.assertEqual(resp.json()["code"], PROVIDER_TIMEOUT)
        self.assertEqual(voyage.embed.call_count, 1)
        assert_neutral(self, resp.json()["message"])

    def test_ask_stream(self):
        ctx, voyage = self.voyage_times_out()
        with ctx:
            resp = self.client.get("/ask-stream", params={"question": "What is the research about?"})

        errors = [e for e in _sse_events(resp.text) if e.get("type") == "error"]
        self.assertEqual(errors[0]["code"], PROVIDER_TIMEOUT)
        self.assertEqual(voyage.embed.call_count, 1)


# ======================================================================
# 7. Voyage indexing timeout
# ======================================================================
class TestVoyageIndexingTimeout(EndpointTestCase):
    def test_paper_marked_failed_with_neutral_detail_and_slot_released(self):
        from app.core.indexing_slot import get_slot
        from app.db.models import Paper

        with self.factory() as db:
            db.add(
                Paper(
                    id=uuid.UUID(PAPER_ID),
                    owner_id=uuid.UUID(USER_A),
                    title="p.pdf",
                    content_hash="0" * 64,
                    storage_path=f"{USER_A}/{PAPER_ID}/original.pdf",
                    file_size_bytes=1024,
                    status="uploaded",
                )
            )
            db.commit()

        voyage = MagicMock()
        voyage.embed.side_effect = voyage_error.Timeout("Request timed out: https://api.voyageai.com")
        with patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4"), patch(
            "app.api.index_document.extract_pdf_pages", return_value=[{"page": 1, "text": "t"}]
        ), patch(
            "app.api.index_document._build_chunks_for_paper",
            return_value=[{"text": "chunk one", "page": 1}, {"text": "chunk two", "page": 1}],
        ), patch("app.rag.embedder._get_client", return_value=voyage):
            resp = self.client.post("/index-document")

        body = resp.json()
        self.assertEqual(body["papers_failed"], 1)
        self.assertEqual(body["failed"][0]["error"], TIMEOUT_TEXT)
        # A timeout is not a RateLimitError, so the embedder never retries it.
        self.assertEqual(voyage.embed.call_count, 1)

        with self.factory() as db:
            paper = db.query(Paper).one()
        self.assertEqual(paper.status, "failed")
        self.assertEqual(paper.status_detail, TIMEOUT_TEXT)

        slot = get_slot()
        self.assertTrue(slot.acquire(timeout=0.1))
        slot.release()


# ======================================================================
# 8. Voyage rerank timeout
# ======================================================================
class TestVoyageRerankTimeout(EndpointTestCase):
    def test_rerank_timeout_is_neutral(self):
        from app.rag import reranker

        voyage = MagicMock()
        voyage.rerank.side_effect = voyage_error.Timeout("Request timed out")
        with patch.dict("os.environ", {"RERANK_ENABLED": "true"}, clear=False), patch(
            "app.api.search.encode_query", return_value=[0.1]
        ), patch("app.api.search.client") as qdrant, patch.object(reranker, "_get_client", return_value=voyage):
            qdrant.query_points.return_value.points = [_hit(), _hit(page=2)]
            resp = self.client.get("/search", params={"query": "transformers"})

        self.assertEqual(resp.json()["code"], PROVIDER_TIMEOUT)
        voyage.rerank.assert_called_once()


# ======================================================================
# 9. Voyage rate-limit retry — unchanged
# ======================================================================
class TestVoyageRateLimitRetryUnchanged(unittest.TestCase):
    def test_rate_limit_backoff_still_makes_the_same_number_of_attempts(self):
        from app.rag import embedder

        self.assertEqual(embedder.MAX_RATE_LIMIT_RETRIES, 5)
        voyage = MagicMock()
        voyage.embed.side_effect = voyage_error.RateLimitError("rate limited", None, 429, None, None)
        with patch("app.rag.embedder._get_client", return_value=voyage), patch("time.sleep"):
            with self.assertRaises(voyage_error.RateLimitError):
                embedder.encode_passages(["one chunk"])

        self.assertEqual(voyage.embed.call_count, embedder.MAX_RATE_LIMIT_RETRIES + 1)

    def test_a_timeout_is_never_retried_by_that_backoff(self):
        from app.rag import embedder

        voyage = MagicMock()
        voyage.embed.side_effect = voyage_error.Timeout("slow")
        with patch("app.rag.embedder._get_client", return_value=voyage), patch("time.sleep") as sleep:
            with self.assertRaises(voyage_error.Timeout):
                embedder.encode_passages(["one chunk"])

        self.assertEqual(voyage.embed.call_count, 1)
        sleep.assert_not_called()


# ======================================================================
# 11. Quota unchanged
# ======================================================================
class TestQuotaUnchanged(EndpointTestCase):
    def test_a_provider_timeout_after_the_charge_still_consumes_the_unit(self):
        from app.agents import qa_agent
        from app.core import limits as limits_config
        from app.core.rate_limit import get_limiter
        from app.db.models import UsageCounter

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)
        with patch.dict(
            "os.environ",
            {"QUOTA_ENFORCEMENT": "on", "AI_RATE_PER_MINUTE": "100", "AI_QUOTA_PER_DAY": "5"},
            clear=False,
        ), patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch("app.api.research.rerank_results", side_effect=lambda q, r: r), patch.object(
            qa_agent._groq_client.chat.completions, "create", side_effect=_groq_timeout()
        ):
            qdrant.query_points.return_value.points = [_hit()]
            resp = self.client.get("/research", params={"query": "vision models"})

        self.assertEqual(resp.json()["code"], PROVIDER_TIMEOUT)
        with self.factory() as db:
            row = db.query(UsageCounter).filter(UsageCounter.metric == limits_config.AI_GENERATION).one()
        self.assertEqual(row.count, 1)


# ======================================================================
# 12. Successful calls unchanged
# ======================================================================
class TestSuccessfulCallsUnchanged(EndpointTestCase):
    def test_compare_success_path(self):
        from app.agents import qa_agent

        hits = [_hit(paper="a.pdf"), _hit(paper="b.pdf")]
        with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
            "app.api.compare_papers.client"
        ) as qdrant, patch("app.api.compare_papers.rerank_results", side_effect=lambda q, r: r), patch.object(
            qa_agent._groq_client.chat.completions, "create", return_value=_completion("| table |")
        ):
            qdrant.query_points.return_value.points = hits
            resp = self.client.get("/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"})

        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["comparison"], "| table |")

    def test_an_unclassified_endpoint_error_is_sanitized(self):
        """Previously asserted that str(e) reached the client verbatim.

        That was the documented behaviour at the time, and it is what
        leaked a driver's wording about schema internals. The endpoints
        now return an authored message instead; the raw text still goes
        to the server log. This test's purpose is unchanged — it pins
        what an UNCLASSIFIED error does — only the expectation moved.
        """
        from app.core.providers import INTERNAL_ERROR, internal_error_payload

        with patch("app.api.search.encode_query", side_effect=RuntimeError("application failure")):
            resp = self.client.get("/search", params={"query": "x"})

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertEqual(resp.json()["code"], INTERNAL_ERROR)
        self.assertNotIn("application failure", resp.text)


# ======================================================================
# 14. Health
# ======================================================================
class TestHealthUnaffected(unittest.TestCase):
    def test_health_is_200_and_calls_no_provider(self):
        from app.agents import qa_agent
        from app.api import ask_stream
        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        boom = AssertionError("health must not call a provider")
        with patch("app.rag.embedder._get_client", side_effect=boom) as voyage, patch.object(
            qa_agent._groq_client.chat.completions, "create", side_effect=boom
        ) as groq_create, patch.object(ask_stream._groq_client.chat.completions, "create", side_effect=boom):
            resp = client.get("/health")

        self.assertEqual(resp.status_code, 200)
        voyage.assert_not_called()
        groq_create.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
