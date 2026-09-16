"""
E1 Phase 3: the five guarded endpoints.

Every provider call is mocked — no Voyage, Qdrant or Groq request is made
anywhere in this file. Each test asserts both the HTTP contract and, where
it matters, whether the provider was invoked at all: the point of a quota
is that rejected work costs nothing upstream.

Enforcement is switched on explicitly here, because tests/__init__.py
turns it off for the rest of the suite.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import limits as limits_config
from app.core.auth import get_current_owner_id
from app.core.rate_limit import get_limiter
from app.db.models import Base

OWNER_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

PROVIDER_WORDS = ("RPM", "TPM", "billing", "Voyage", "Groq", "credit")


def _no_hits():
    result = MagicMock()
    result.points = []
    return result


class EnforcementTestCase(unittest.TestCase):
    """Shared harness: enforcement on, a private SQLite counter store, a
    mocked-out provider layer, and an authenticated client."""

    def setUp(self):
        enforcement = patch.dict(
            "os.environ",
            {"QUOTA_ENFORCEMENT": "on", "AI_RATE_PER_MINUTE": "100", "INDEX_RATE_PER_MINUTE": "100"},
            clear=False,
        )
        enforcement.start()
        self.addCleanup(enforcement.stop)

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        factory_patch = patch(
            "app.db.session.get_session_factory", return_value=self.factory
        )
        factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.addCleanup(self.engine.dispose)

        from app.main import app

        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.authenticate_as(OWNER_A)
        self.addCleanup(self.app.dependency_overrides.clear)

    def authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def usage(self, owner_id, metric):
        from app.db.models import UsageCounter

        with self.factory() as db:
            row = (
                db.query(UsageCounter)
                .filter(
                    UsageCounter.owner_id == uuid.UUID(owner_id),
                    UsageCounter.metric == metric,
                    UsageCounter.day == limits_config.utc_today(),
                )
                .one_or_none()
            )
            return row.count if row else 0

    def assertNoProviderWording(self, response):
        body = response.text
        for word in PROVIDER_WORDS:
            self.assertNotIn(word, body, f"provider wording {word!r} leaked into a response")


class TestAiGenerationEndpoints(EnforcementTestCase):
    """/research, /summarize-paper and /compare-papers all charge one AI
    generation up front, because their first act is a provider call."""

    def call_research(self):
        with patch("app.api.research.encode_query", return_value=[0.1]) as embed, patch(
            "app.api.research.client"
        ) as qdrant, patch("app.api.research.research_agent", return_value="report"):
            qdrant.query_points.return_value = _no_hits()
            resp = self.client.get("/research", params={"query": "vision models"})
        return resp, embed

    def test_research_charges_exactly_one_unit_per_call(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            self.call_research()
            self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)
            self.call_research()
            self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 2)

    def test_research_returns_normalized_429_once_the_allowance_is_spent(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "1"}, clear=False):
            self.call_research()
            resp, embed = self.call_research()

        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["remaining"], 0)
        self.assertEqual(resp.headers["Retry-After"], str(body["retry_after_seconds"]))
        self.assertNoProviderWording(resp)
        # The decisive property: no embedding was attempted.
        embed.assert_not_called()

    def test_burst_limit_returns_rate_limited_without_spending_daily_quota(self):
        with patch.dict(
            "os.environ",
            {"AI_RATE_PER_MINUTE": "1", "AI_QUOTA_PER_DAY": "50"},
            clear=False,
        ):
            self.call_research()
            resp, embed = self.call_research()

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "rate_limited")
        embed.assert_not_called()
        # Burst rejection happens before the counter, so only the first
        # call consumed daily allowance.
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_summarize_paper_is_guarded(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "1"}, clear=False):
            with patch("app.api.summarize_paper.encode_query", return_value=[0.1]), patch(
                "app.api.summarize_paper.client"
            ) as qdrant:
                qdrant.query_points.return_value = _no_hits()
                self.client.get("/summarize-paper", params={"paper_name": "p.pdf"})

            with patch("app.api.summarize_paper.encode_query") as embed:
                resp = self.client.get("/summarize-paper", params={"paper_name": "p.pdf"})

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        embed.assert_not_called()

    def test_compare_papers_is_guarded_and_costs_one_unit_not_two(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
                "app.api.compare_papers.client"
            ) as qdrant:
                qdrant.query_points.return_value = _no_hits()
                self.client.get(
                    "/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"}
                )

        # Two embeddings happen inside one comparison; the metric counts
        # user-visible operations, so this is one unit.
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_provider_failure_after_the_charge_still_consumes_the_unit(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.research.encode_query", side_effect=RuntimeError("provider down")
            ):
                resp = self.client.get("/research", params={"query": "x"})

        # The endpoint reports its own error shape, and the attempt is
        # still billed: the provider call was made.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "error")
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_an_ordinary_application_error_is_not_a_quota_error(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
                "app.api.compare_papers.client"
            ) as qdrant:
                qdrant.query_points.return_value = _no_hits()
                resp = self.client.get(
                    "/compare-papers", params={"paper1": "missing.pdf", "paper2": "b.pdf"}
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertNotIn("code", body)  # not a limit rejection
        self.assertIn("Paper not found", body["message"])

    def test_an_application_error_before_provider_work_consumes_zero_units(self):
        # get_user_memory() runs before the charge in all three handlers, so
        # a deterministic failure there must cost nothing: no provider call
        # was made, so there is nothing to bill.
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.research.get_user_memory", side_effect=RuntimeError("no memory")
            ), patch("app.api.research.encode_query") as embed:
                resp = self.client.get("/research", params={"query": "x"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "error")
        embed.assert_not_called()
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_summarize_application_error_before_provider_work_costs_nothing(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.summarize_paper.get_user_memory", side_effect=RuntimeError("boom")
            ), patch("app.api.summarize_paper.encode_query") as embed:
                self.client.get("/summarize-paper", params={"paper_name": "p.pdf"})

        embed.assert_not_called()
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_compare_application_error_before_provider_work_costs_nothing(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.compare_papers.get_user_memory", side_effect=RuntimeError("boom")
            ), patch("app.api.compare_papers.encode_query") as embed:
                self.client.get(
                    "/compare-papers", params={"paper1": "a.pdf", "paper2": "b.pdf"}
                )

        embed.assert_not_called()
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_losing_a_race_for_the_last_unit_returns_429_not_a_200_error_body(self):
        # The handlers wrap their body in `except Exception` and return a
        # 200 error shape. A usage rejection must escape that and surface
        # as the normalized 429, or the frontend cannot tell the two apart.
        from app.core.limits import QuotaExceeded

        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.research.charge_ai_unit",
                side_effect=QuotaExceeded(
                    "Out of allowance.", retry_after_seconds=120, limit=5, remaining=0
                ),
            ), patch("app.api.research.encode_query") as embed:
                resp = self.client.get("/research", params={"query": "x"})

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        self.assertEqual(resp.headers["Retry-After"], "120")
        embed.assert_not_called()

    def test_two_users_have_independent_allowances(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "1"}, clear=False):
            self.call_research()
            resp_a, _ = self.call_research()
            self.assertEqual(resp_a.status_code, 429)

            self.authenticate_as(OWNER_B)
            resp_b, _ = self.call_research()

        self.assertEqual(resp_b.status_code, 200)
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)
        self.assertEqual(self.usage(OWNER_B, limits_config.AI_GENERATION), 1)

    def test_a_client_supplied_owner_id_cannot_key_the_counter(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch("app.api.research.encode_query", return_value=[0.1]), patch(
                "app.api.research.client"
            ) as qdrant, patch("app.api.research.research_agent", return_value="r"):
                qdrant.query_points.return_value = _no_hits()
                self.client.get(
                    "/research",
                    params={"query": "x", "owner_id": OWNER_B, "user_id": OWNER_B},
                    headers={"X-Owner-Id": OWNER_B},
                )

        # Only the verified identity was billed; the forged values did
        # nothing at all.
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)
        self.assertEqual(self.usage(OWNER_B, limits_config.AI_GENERATION), 0)

    def test_counter_outage_fails_closed_with_503_and_no_provider_call(self):
        from sqlalchemy.exc import OperationalError

        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.core.quota.session_scope",
                side_effect=OperationalError("select 1", {}, Exception("down")),
            ):
                with patch("app.api.research.encode_query") as embed:
                    resp = self.client.get("/research", params={"query": "x"})

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], "quota_unavailable")
        self.assertEqual(resp.headers["Retry-After"], "30")
        embed.assert_not_called()
        self.assertNoProviderWording(resp)

    def test_kill_switch_off_disables_charging_entirely(self):
        with patch.dict(
            "os.environ",
            {"QUOTA_ENFORCEMENT": "off", "AI_QUOTA_PER_DAY": "1"},
            clear=False,
        ):
            for _ in range(4):
                resp, _ = self.call_research()
                self.assertEqual(resp.status_code, 200)

        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 0)


class TestAskStream(EnforcementTestCase):
    """Streaming needs two steps: a read-only pre-check that can still
    produce a real 429, and the charge itself just before provider work."""

    def _patched_stream(self):
        return (
            patch("app.api.ask_stream.load_chat_state", return_value=MagicMock(history=[], current_topic=None, current_paper_id=None)),
            patch("app.api.ask_stream.persist_user_turn", return_value=uuid.uuid4()),
        )

    def test_exhausted_quota_returns_http_429_before_streaming_starts(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "1"}, clear=False):
            from app.core.quota import charge

            charge(OWNER_A, limits_config.AI_GENERATION)  # spend the allowance

            with patch("app.api.ask_stream.encode_query") as embed:
                resp = self.client.get("/ask-stream", params={"question": "what?"})

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        embed.assert_not_called()
        self.assertNoProviderWording(resp)

    def test_a_successful_ask_charges_exactly_one_unit(self):
        chat_state, persist = self._patched_stream()
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with chat_state, persist, patch(
                "app.api.ask_stream.encode_query", return_value=[0.1]
            ), patch("app.api.ask_stream.client") as qdrant:
                qdrant.query_points.return_value = _no_hits()
                resp = self.client.get("/ask-stream", params={"question": "what?"})
                self.assertEqual(resp.status_code, 200)
                self.assertIn("data:", resp.text)

        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_losing_the_race_emits_an_sse_error_frame_and_skips_the_provider(self):
        from app.core.limits import QuotaExceeded

        chat_state, persist = self._patched_stream()
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with chat_state, persist, patch(
                "app.api.ask_stream.charge_quota",
                side_effect=QuotaExceeded("Out of allowance.", retry_after_seconds=60, limit=5),
            ), patch("app.api.ask_stream.encode_query") as embed:
                resp = self.client.get("/ask-stream", params={"question": "what?"})

        # Streaming had already begun, so the status stays 200 and the
        # rejection arrives in-band.
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"type": "error"', resp.text)
        self.assertIn('"code": "quota_exceeded"', resp.text)
        embed.assert_not_called()
        self.assertNoProviderWording(resp)

    def test_a_database_failure_before_provider_work_does_not_burn_quota(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "5"}, clear=False):
            with patch(
                "app.api.ask_stream.load_chat_state", side_effect=RuntimeError("db down")
            ), patch("app.api.ask_stream.encode_query") as embed:
                resp = self.client.get("/ask-stream", params={"question": "what?"})

        self.assertEqual(resp.status_code, 200)  # stream opened, then errored
        embed.assert_not_called()
        # The charge sits after that failure point, so nothing was spent.
        self.assertEqual(self.usage(OWNER_A, limits_config.AI_GENERATION), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
