"""
Phase 1A hardening — six residual items, each previously defined but not
enforced, or enforced too loosely.

  A. /export-report answered 200 with a JSON error body when the owner
     had no report, so response.ok stayed true and the browser saved
     JSON under the name research-report.pdf.

  B. Papers that failed to upload or index were invisible: /papers
     returned only status == "indexed" titles. Worse, status_detail —
     a field rendered in the UI — was written as str(e).

  C. Follow-up detection tested `word in question_lower`, a raw
     substring search, so "network", "workflow" and "framework" all
     matched "work" and silently prepended a stale topic.

  D. /search and /paper-details embed with Voyage on every call and
     were unmetered.

  E. /upload was unmetered.

  F. CHEAP_READ was configured and unused.

Fully offline: no provider, Qdrant, Storage or Postgres call happens in
this file.
"""
import datetime
import io
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import quota
from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id
from app.core.limits import (
    CHEAP_READ,
    QUERY_EMBEDDING,
    UPLOAD,
    enforcement_enabled,
    limits_for,
)
from app.core.rate_limit import get_limiter
from app.db.models import Base, Paper, Report

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

PDF_BYTES = b"%PDF-1.4 minimal"


class HardeningTestCase(unittest.TestCase):
    """Real in-memory SQLite, real quota counters, no network."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

        # One factory for every session, matching the E1 quota suites.
        patcher = patch(
            "app.db.session.get_session_factory", return_value=self.SessionLocal
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

        # The burst limiter is process-wide; a leftover window from
        # another test would make these rejections non-deterministic.
        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

        # MUST opt in. tests/__init__.py sets QUOTA_ENFORCEMENT=off for
        # the whole suite by design, so a quota test that forgets this
        # does not fail — it silently asserts nothing: every guard
        # no-ops, counters stay 0 and no 429 is ever raised. That is
        # exactly what happened to this suite once already, producing ten
        # failures that looked like a production defect.
        enforcement = patch.dict(
            "os.environ", {"QUOTA_ENFORCEMENT": "on"}, clear=False
        )
        enforcement.start()
        self.addCleanup(enforcement.stop)

        # Fail loudly rather than vacuously if that opt-in ever regresses.
        assert enforcement_enabled(), (
            "QUOTA_ENFORCEMENT is off — these tests would pass without "
            "exercising any quota behaviour at all"
        )

        from app.main import app

        self.app = app
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id
        self.app.dependency_overrides[get_current_identity] = lambda: AuthenticatedIdentity(
            owner_id=owner_id, token="test-token"
        )

    def seed_report(self, owner_id, query="topic", markdown="# Body"):
        session = self.SessionLocal()
        try:
            session.add(
                Report(
                    owner_id=uuid.UUID(owner_id),
                    query=query,
                    report_markdown=markdown,
                    citations=[],
                    created_at=EPOCH,
                )
            )
            session.commit()
        finally:
            session.close()

    def seed_paper(self, owner_id, title, status, detail=None):
        session = self.SessionLocal()
        try:
            session.add(
                Paper(
                    id=uuid.uuid4(),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="h" * 64,
                    storage_path=f"{owner_id}/{title}",
                    file_size_bytes=1024,
                    status=status,
                    status_detail=detail,
                )
            )
            session.commit()
        finally:
            session.close()

    def exhaust_daily(self, owner_id, metric):
        """Spend the whole daily allowance for a metric."""
        for _ in range(limits_for(metric).per_day):
            quota.charge(owner_id, metric)

    def counter(self, owner_id, metric):
        return quota.peek(owner_id, metric)


# ======================================================================
# A. Export semantics
# ======================================================================
class TestExportSemantics(HardeningTestCase):
    def test_no_report_returns_404(self):
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("No research report found", body["message"])

    def test_a_valid_report_still_exports_a_pdf(self):
        self.seed_report(OWNER_A)
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF-"))

    def test_cross_owner_report_is_not_exported(self):
        self.seed_report(OWNER_A, markdown="OWNER A SECRET BODY")
        self.authenticate_as(OWNER_B)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(b"OWNER A SECRET BODY", resp.content)

    def test_unauthenticated_export_is_rejected(self):
        self.seed_report(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 401)


# ======================================================================
# B. Failed paper visibility
# ======================================================================
class TestPapersDetailed(HardeningTestCase):
    def test_the_existing_papers_contract_is_unchanged(self):
        self.seed_paper(OWNER_A, "ready.pdf", "indexed")
        self.seed_paper(OWNER_A, "broken.pdf", "failed", "Processing failed.")
        self.authenticate_as(OWNER_A)

        body = self.client.get("/papers").json()

        # Still a plain list of ready titles — every existing consumer
        # reads this shape.
        self.assertEqual(body["papers"], ["ready.pdf"])

    def test_papers_detailed_exposes_every_state(self):
        self.seed_paper(OWNER_A, "a-ready.pdf", "indexed")
        self.seed_paper(OWNER_A, "b-working.pdf", "indexing")
        self.seed_paper(OWNER_A, "c-broken.pdf", "failed", "Processing failed.")
        self.authenticate_as(OWNER_A)

        detailed = self.client.get("/papers").json()["papers_detailed"]
        by_title = {d["title"]: d for d in detailed}

        self.assertEqual(len(detailed), 3)
        self.assertEqual(by_title["a-ready.pdf"]["status"], "indexed")
        self.assertEqual(by_title["b-working.pdf"]["status"], "indexing")
        self.assertEqual(by_title["c-broken.pdf"]["status"], "failed")
        self.assertEqual(by_title["c-broken.pdf"]["status_detail"], "Processing failed.")
        self.assertIsNone(by_title["a-ready.pdf"]["status_detail"])

    def test_each_entry_carries_its_paper_id(self):
        self.seed_paper(OWNER_A, "one.pdf", "indexed")
        self.authenticate_as(OWNER_A)

        entry = self.client.get("/papers").json()["papers_detailed"][0]

        uuid.UUID(entry["paper_id"])  # raises if not a real id

    def test_another_owners_papers_never_appear(self):
        self.seed_paper(OWNER_B, "other-secret.pdf", "failed", "OWNER B DETAIL")
        self.seed_paper(OWNER_A, "mine.pdf", "indexed")
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/papers")

        self.assertNotIn("other-secret.pdf", resp.text)
        self.assertNotIn("OWNER B DETAIL", resp.text)
        self.assertEqual(len(resp.json()["papers_detailed"]), 1)


class TestStatusDetailIsAuthored(unittest.TestCase):
    """status_detail is rendered in the UI, so it must never be str(e)."""

    def test_upload_storage_failure_detail_is_authored(self):
        from app.api.upload import STORAGE_FAILED_DETAIL

        self.assertNotIn("{", STORAGE_FAILED_DETAIL)
        for leak in ("traceback", "exception", "http", "supabase", "/", "\\"):
            self.assertNotIn(leak, STORAGE_FAILED_DETAIL.lower())

    def test_indexing_failure_detail_is_authored(self):
        from app.api.index_document import INDEXING_FAILED_DETAIL

        self.assertNotIn("{", INDEXING_FAILED_DETAIL)
        for leak in ("traceback", "exception", "voyage", "groq", "qdrant"):
            self.assertNotIn(leak, INDEXING_FAILED_DETAIL.lower())

    def test_neither_module_interpolates_an_exception_into_status_detail(self):
        import app.api.index_document as index_mod
        import app.api.upload as upload_mod

        for mod in (upload_mod, index_mod):
            source = io.open(mod.__file__, encoding="utf-8").read()
            for line in source.splitlines():
                if "status_detail" in line and "=" in line:
                    self.assertNotIn("str(e)", line, line.strip())
                    self.assertNotIn("{e}", line, line.strip())


# ======================================================================
# C. Follow-up detection
# ======================================================================
class TestFollowUpDetection(unittest.TestCase):
    def setUp(self):
        from app.api.ask_stream import is_follow_up_question

        self.is_follow_up = is_follow_up_question

    def test_the_required_positive_examples(self):
        for question in (
            "what about the dataset?",
            "and what are the limitations?",
            "tell me more about that",
            "how about the methodology?",
            "why?",
        ):
            with self.subTest(question=question):
                self.assertTrue(self.is_follow_up(question))

    def test_substring_false_positives_are_gone(self):
        # Each of these contains a follow-up token as a SUBSTRING only:
        # "work" inside network/workflow/framework.
        for question in (
            "network",
            "workflow",
            "framework",
            "What is a framework?",
            "Explain the workflow diagram",
            "Describe the convolutional neural network",
        ):
            with self.subTest(question=question):
                self.assertFalse(self.is_follow_up(question))

    def test_multi_word_phrases_still_match(self):
        self.assertTrue(self.is_follow_up("what is the future scope?"))
        self.assertTrue(self.is_follow_up("any future work?"))

    def test_a_phrase_split_across_the_question_does_not_match(self):
        # "future" and "work" both present, but not adjacent.
        self.assertFalse(self.is_follow_up("in future, how does networking function"))

    def test_ordinary_research_questions_are_not_follow_ups(self):
        for question in (
            "What datasets were benchmarked in PCB inspection?".replace("datasets", "corpora"),
            "Which models were evaluated?",
            "Summarize the paper",
            "",
        ):
            with self.subTest(question=question):
                self.assertFalse(self.is_follow_up(question))

    def test_punctuation_and_case_do_not_matter(self):
        self.assertTrue(self.is_follow_up("WHY?"))
        self.assertTrue(self.is_follow_up("...and its limitations!"))


# ======================================================================
# D. Query-embedding quota
# ======================================================================
class TestQueryEmbeddingQuota(HardeningTestCase):
    def call_search(self, encode=None):
        with patch(
            "app.api.search.encode_query", encode or MagicMock(return_value=[0.1])
        ) as enc, patch("app.api.search.client") as qdrant, patch(
            "app.api.search.rerank_results", side_effect=lambda q, r: r
        ):
            qdrant.query_points.return_value.points = []
            resp = self.client.get("/search", params={"query": "x"})
        return resp, enc

    def test_an_allowed_search_succeeds_and_charges_one_unit(self):
        self.authenticate_as(OWNER_A)

        resp, enc = self.call_search()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(enc.call_count, 1)
        self.assertEqual(self.counter(OWNER_A, QUERY_EMBEDDING), 1)

    def test_paper_details_is_also_metered(self):
        self.authenticate_as(OWNER_A)

        with patch("app.api.paper_details.encode_query", return_value=[0.1]), patch(
            "app.api.paper_details.client"
        ) as qdrant:
            qdrant.query_points.return_value.points = []
            self.client.get("/paper-details", params={"paper_name": "p.pdf"})

        self.assertEqual(self.counter(OWNER_A, QUERY_EMBEDDING), 1)

    def test_quota_exhaustion_returns_429_and_calls_no_provider(self):
        self.authenticate_as(OWNER_A)
        self.exhaust_daily(OWNER_A, QUERY_EMBEDDING)
        before = self.counter(OWNER_A, QUERY_EMBEDDING)

        resp, enc = self.call_search()

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        # The decisive assertion: Voyage was never reached.
        enc.assert_not_called()
        self.assertEqual(self.counter(OWNER_A, QUERY_EMBEDDING), before)

    def test_the_quota_is_per_owner(self):
        self.authenticate_as(OWNER_A)
        self.exhaust_daily(OWNER_A, QUERY_EMBEDDING)

        self.authenticate_as(OWNER_B)
        resp, enc = self.call_search()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(enc.call_count, 1)

    def test_a_provider_failure_stays_a_provider_failure(self):
        import groq

        self.authenticate_as(OWNER_A)

        resp, _ = self.call_search(
            encode=MagicMock(side_effect=groq.APITimeoutError(request=MagicMock()))
        )

        # Not reclassified as a quota problem.
        self.assertEqual(resp.json()["code"], "provider_timeout")
        self.assertNotEqual(resp.status_code, 429)

    def test_one_request_charges_exactly_once(self):
        self.authenticate_as(OWNER_A)

        self.call_search()
        self.call_search()

        self.assertEqual(self.counter(OWNER_A, QUERY_EMBEDDING), 2)


# ======================================================================
# E. Upload quota
# ======================================================================
class TestUploadQuota(HardeningTestCase):
    def upload(self, filename="paper.pdf", content=PDF_BYTES):
        with patch("app.api.upload.upload_pdf") as storage, patch(
            "app.api.upload.storage_object_exists", return_value=False
        ):
            resp = self.client.post(
                "/upload",
                files={"file": (filename, content, "application/pdf")},
            )
        return resp, storage

    def test_a_successful_upload_charges_one_unit(self):
        self.authenticate_as(OWNER_A)

        resp, storage = self.upload()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(storage.call_count, 1)
        self.assertEqual(self.counter(OWNER_A, UPLOAD), 1)

    def test_quota_exhaustion_returns_429_and_writes_no_storage_object(self):
        self.authenticate_as(OWNER_A)
        self.exhaust_daily(OWNER_A, UPLOAD)

        resp, storage = self.upload()

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        # Nothing reached Supabase Storage...
        storage.assert_not_called()
        # ...and no paper row was created for indexing to pick up.
        with self.SessionLocal() as db:
            self.assertEqual(db.query(Paper).count(), 0)

    def test_a_validation_failure_consumes_no_upload_unit(self):
        self.authenticate_as(OWNER_A)

        resp, storage = self.upload(content=b"not a pdf at all")

        self.assertEqual(resp.status_code, 400)
        storage.assert_not_called()
        # The deterministic rejection cost nothing: precheck does not
        # charge, and charge_upload_unit is never reached.
        self.assertEqual(self.counter(OWNER_A, UPLOAD), 0)

    def test_the_upload_quota_is_per_owner(self):
        self.authenticate_as(OWNER_A)
        self.exhaust_daily(OWNER_A, UPLOAD)

        self.authenticate_as(OWNER_B)
        resp, storage = self.upload()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(storage.call_count, 1)

    def test_the_rejection_body_is_safe(self):
        self.authenticate_as(OWNER_A)
        self.exhaust_daily(OWNER_A, UPLOAD)

        resp, _ = self.upload()

        body = resp.json()
        self.assertIn("retry_after_seconds", body)
        self.assertIn("Retry-After", resp.headers)
        for leak in ("traceback", "supabase", "sql", "token"):
            self.assertNotIn(leak, resp.text.lower())


# ======================================================================
# F. Cheap reads
# ======================================================================
class TestCheapReadGuard(HardeningTestCase):
    def test_export_report_is_burst_guarded(self):
        self.authenticate_as(OWNER_A)
        per_minute = limits_for(CHEAP_READ).per_minute

        for _ in range(per_minute):
            self.client.get("/export-report")

        resp = self.client.get("/export-report")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "rate_limited")

    def test_latest_report_is_burst_guarded(self):
        self.authenticate_as(OWNER_A)
        per_minute = limits_for(CHEAP_READ).per_minute

        for _ in range(per_minute):
            self.client.get("/latest-report")

        resp = self.client.get("/latest-report")
        self.assertEqual(resp.status_code, 429)

    def test_the_two_guarded_reads_share_one_cheap_read_window(self):
        self.authenticate_as(OWNER_A)
        per_minute = limits_for(CHEAP_READ).per_minute

        for _ in range(per_minute):
            self.client.get("/latest-report")

        # Same metric, same bucket — export is rejected too.
        self.assertEqual(self.client.get("/export-report").status_code, 429)

    def test_papers_is_not_rate_limited(self):
        self.authenticate_as(OWNER_A)
        per_minute = limits_for(CHEAP_READ).per_minute

        for _ in range(per_minute + 5):
            resp = self.client.get("/papers")

        # /papers is fetched on every authenticated page load; metering it
        # would surface 429s during ordinary navigation.
        self.assertEqual(resp.status_code, 200)

    def test_stats_is_not_rate_limited(self):
        self.authenticate_as(OWNER_A)
        per_minute = limits_for(CHEAP_READ).per_minute

        with patch("app.api.stats.client") as qdrant:
            qdrant.count.return_value.count = 0
            for _ in range(per_minute + 5):
                resp = self.client.get("/stats")

        self.assertEqual(resp.status_code, 200)

    def test_health_is_not_rate_limited(self):
        per_minute = limits_for(CHEAP_READ).per_minute

        for _ in range(per_minute + 5):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 200)


# ======================================================================
# Enforcement roster
# ======================================================================
class TestEnforcedMetrics(unittest.TestCase):
    def test_the_roster_reflects_what_is_actually_enforced(self):
        from app.core.limits import AI_GENERATION, ENFORCED_METRICS, INDEX_RUN

        self.assertEqual(
            set(ENFORCED_METRICS),
            {AI_GENERATION, INDEX_RUN, QUERY_EMBEDDING, UPLOAD, CHEAP_READ},
        )

    def test_the_configured_limits_are_unchanged(self):
        self.assertEqual((limits_for(QUERY_EMBEDDING).per_minute,
                          limits_for(QUERY_EMBEDDING).per_day), (20, 200))
        self.assertEqual((limits_for(UPLOAD).per_minute,
                          limits_for(UPLOAD).per_day), (5, 10))
        self.assertEqual(limits_for(CHEAP_READ).per_minute, 60)
        self.assertIsNone(limits_for(CHEAP_READ).per_day)


if __name__ == "__main__":
    unittest.main()
