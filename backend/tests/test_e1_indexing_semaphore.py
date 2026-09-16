"""
E1 mechanism C: the single global indexing slot, as seen from
/index-document.

The slot only serialises this service's own indexing runs. It does not
enforce the provider's rate limit — that remains the bounded backoff in
app/rag/embedder.py, which these tests never touch.
"""
import os
import sys
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import limits as limits_config
from app.core.auth import get_current_owner_id
from app.core.indexing_slot import get_slot
from app.core.rate_limit import get_limiter
from app.db.models import Base, Paper

OWNER = "cccccccc-3333-4333-8333-cccccccccccc"


class IndexingTestCase(unittest.TestCase):
    def setUp(self):
        enforcement = patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "INDEX_RATE_PER_MINUTE": "100",
                "INDEX_QUOTA_PER_DAY": "20",
                "INDEX_SLOT_WAIT_SECONDS": "1",
            },
            clear=False,
        )
        enforcement.start()
        self.addCleanup(enforcement.stop)

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        factory_patch = patch("app.db.session.get_session_factory", return_value=self.factory)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.addCleanup(self.engine.dispose)

        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def seed_pending_paper(self, title="paper.pdf"):
        with self.factory() as db:
            db.add(
                Paper(
                    id=uuid.uuid4(),
                    owner_id=uuid.UUID(OWNER),
                    title=title,
                    content_hash="0" * 64,
                    storage_path=f"{OWNER}/x/original.pdf",
                    file_size_bytes=1234,
                    status="uploaded",
                )
            )
            db.commit()

    def usage(self, metric=limits_config.INDEX_RUN):
        from app.db.models import UsageCounter

        with self.factory() as db:
            row = (
                db.query(UsageCounter)
                .filter(
                    UsageCounter.owner_id == uuid.UUID(OWNER),
                    UsageCounter.metric == metric,
                    UsageCounter.day == limits_config.utc_today(),
                )
                .one_or_none()
            )
            return row.count if row else 0


def fake_index_one_paper(points=3):
    """Stand-in for index_one_paper that fires the metering hook exactly
    as the real one does, immediately before its (mocked) provider work."""

    def _run(paper, before_provider_work=None):
        if before_provider_work is not None:
            before_provider_work()
        return points

    return _run


class TestIndexingSlot(IndexingTestCase):
    def test_busy_slot_returns_429_indexing_busy_without_spending_quota(self):
        self.seed_pending_paper()
        slot = get_slot()
        self.assertTrue(slot.acquire(timeout=1))
        try:
            started = time.monotonic()
            resp = self.client.post("/index-document")
            waited = time.monotonic() - started
        finally:
            slot.release()

        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["code"], "indexing_busy")
        self.assertEqual(resp.headers["Retry-After"], "60")
        self.assertEqual(body["retry_after_seconds"], 60)
        # Being turned away costs nothing.
        self.assertEqual(self.usage(), 0)
        # And the wait was bounded by INDEX_SLOT_WAIT_SECONDS, not by the
        # length of the run holding the slot.
        self.assertLess(waited, 5)

    def test_busy_response_carries_no_provider_wording(self):
        self.seed_pending_paper()
        slot = get_slot()
        self.assertTrue(slot.acquire(timeout=1))
        try:
            resp = self.client.post("/index-document")
        finally:
            slot.release()

        for word in ("RPM", "TPM", "billing", "Voyage", "Groq"):
            self.assertNotIn(word, resp.text)

    def test_slot_is_released_after_a_successful_run(self):
        self.seed_pending_paper()
        with patch(
            "app.api.index_document.index_one_paper", side_effect=fake_index_one_paper()
        ):
            resp = self.client.post("/index-document")
        self.assertEqual(resp.status_code, 200)

        # Free again immediately for the next caller.
        self.assertTrue(get_slot().acquire(timeout=0.1))
        get_slot().release()

    def test_slot_is_released_even_when_the_run_raises(self):
        self.seed_pending_paper()
        with patch(
            "app.api.index_document.index_one_paper", side_effect=RuntimeError("boom")
        ):
            resp = self.client.post("/index-document")

        # The per-paper failure path absorbed it; the run still returns 200.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["papers_failed"], 1)
        self.assertTrue(get_slot().acquire(timeout=0.1))
        get_slot().release()

    def test_the_slot_still_applies_when_quota_enforcement_is_off(self):
        self.seed_pending_paper()
        with patch.dict("os.environ", {"QUOTA_ENFORCEMENT": "off"}, clear=False):
            slot = get_slot()
            self.assertTrue(slot.acquire(timeout=1))
            try:
                resp = self.client.post("/index-document")
            finally:
                slot.release()

        # C protects upstream pressure and request duration, so the kill
        # switch for per-user limits must not disable it.
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "indexing_busy")


class TestIndexRunQuota(IndexingTestCase):
    def test_one_paper_consumes_exactly_one_index_unit(self):
        self.seed_pending_paper()
        with patch(
            "app.api.index_document.index_one_paper", side_effect=fake_index_one_paper(5)
        ):
            resp = self.client.post("/index-document")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["papers_indexed"], 1)
        self.assertEqual(self.usage(), 1)

    def test_each_paper_accepted_for_work_consumes_its_own_unit(self):
        # Quota model B: cost scales with papers embedded, so the unit does
        # too. Three papers in one request cost three units, not one.
        for title in ("a.pdf", "b.pdf", "c.pdf"):
            self.seed_pending_paper(title)
        with patch(
            "app.api.index_document.index_one_paper", side_effect=fake_index_one_paper(2)
        ):
            resp = self.client.post("/index-document")

        self.assertEqual(resp.json()["papers_indexed"], 3)
        self.assertEqual(self.usage(), 3)

    def test_allowance_exhausted_mid_batch_indexes_some_and_leaves_the_rest_pending(self):
        for title in ("a.pdf", "b.pdf", "c.pdf"):
            self.seed_pending_paper(title)

        with patch.dict("os.environ", {"INDEX_QUOTA_PER_DAY": "2"}, clear=False):
            with patch(
                "app.api.index_document.index_one_paper",
                side_effect=fake_index_one_paper(4),
            ):
                resp = self.client.post("/index-document")

        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["papers_indexed"], 2)
        self.assertEqual(body["papers_failed"], 0)
        self.assertEqual(len(body["skipped"]), 1)
        self.assertEqual(self.usage(), 2)

        # The skipped paper must stay "uploaded" so the next run retries it.
        # Marking it failed would drop it out of the pending query forever.
        from app.db.models import Paper as PaperModel

        with self.factory() as db:
            statuses = sorted(p.status for p in db.query(PaperModel).all())
        self.assertEqual(statuses, ["indexed", "indexed", "uploaded"])

    def test_nothing_indexable_within_the_allowance_reports_429_not_success(self):
        self.seed_pending_paper("a.pdf")
        with patch.dict("os.environ", {"INDEX_QUOTA_PER_DAY": "1"}, clear=False):
            from app.core.quota import charge

            charge(OWNER, limits_config.INDEX_RUN)  # spend it elsewhere first

            with patch(
                "app.api.index_document.index_one_paper",
                side_effect=fake_index_one_paper(1),
            ):
                resp = self.client.post("/index-document")

        # A "success with zero papers" would be read by the frontend as
        # "already indexed", which would be a lie.
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")

    def test_nothing_pending_consumes_no_quota_and_keeps_the_old_shape(self):
        with patch("app.api.index_document.index_one_paper") as work:
            resp = self.client.post("/index-document")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["papers_found"], 0)
        self.assertEqual(body["papers_indexed"], 0)
        self.assertEqual(body["papers_failed"], 0)
        self.assertEqual(body["indexed"], [])
        self.assertEqual(body["failed"], [])
        work.assert_not_called()
        self.assertEqual(self.usage(), 0)

    def test_exhausted_index_quota_is_rejected_before_any_work(self):
        # The read-only pre-check in the dependency turns the second run
        # away before the handler body runs at all — no slot, no work.
        self.seed_pending_paper()
        with patch.dict("os.environ", {"INDEX_QUOTA_PER_DAY": "1"}, clear=False):
            with patch(
                "app.api.index_document.index_one_paper",
                side_effect=fake_index_one_paper(1),
            ):
                first = self.client.post("/index-document")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(self.usage(), 1)

            self.seed_pending_paper("second.pdf")
            with patch("app.api.index_document.index_one_paper") as work:
                resp = self.client.post("/index-document")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "quota_exceeded")
        work.assert_not_called()
        # The blocked run consumed nothing further.
        self.assertEqual(self.usage(), 1)

    def test_index_and_ai_allowances_are_separate(self):
        self.seed_pending_paper()
        with patch(
            "app.api.index_document.index_one_paper", side_effect=fake_index_one_paper(1)
        ):
            self.client.post("/index-document")

        self.assertEqual(self.usage(limits_config.INDEX_RUN), 1)
        self.assertEqual(self.usage(limits_config.AI_GENERATION), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
