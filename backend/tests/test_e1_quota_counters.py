"""
E1 mechanism B: durable per-user daily quotas.

These run against a real SQLite database rather than a mock, because the
property under test lives in the SQL itself — a conditional UPSERT whose
WHERE clause makes rejection non-consuming. Mocking the database would
assert nothing about the behaviour that matters.

A file-backed database (not :memory:) is used so the concurrency test
gets genuinely separate connections racing on the same row.
"""
import os
import shutil
import tempfile
import threading
import unittest
import uuid
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import limits as limits_config
from app.core.limits import QuotaExceeded, QuotaUnavailable
from app.core.quota import charge, check_remaining, ensure_within_quota, peek
from app.db.models import Base

AI = limits_config.AI_GENERATION
OWNER_A = str(uuid.uuid4())
OWNER_B = str(uuid.uuid4())


class QuotaTestCase(unittest.TestCase):
    """Gives each test its own on-disk SQLite database, patched in behind
    the real session_scope() so the production code path is exercised."""

    def setUp(self):
        # tests/__init__.py disables enforcement for the wider suite; these
        # tests are precisely about enforcement, so switch it back on.
        enforcement = patch.dict("os.environ", {"QUOTA_ENFORCEMENT": "on"}, clear=False)
        enforcement.start()
        self.addCleanup(enforcement.stop)

        self.tmpdir = tempfile.mkdtemp(prefix="e1quota")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        path = os.path.join(self.tmpdir, "usage.db").replace("\\", "/")

        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"timeout": 30, "check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

        patcher = patch("app.db.session.get_session_factory", return_value=self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def stored_count(self, owner_id, metric, day=None):
        with self.factory() as db:
            from app.db.models import UsageCounter

            row = (
                db.query(UsageCounter)
                .filter(
                    UsageCounter.owner_id == uuid.UUID(owner_id),
                    UsageCounter.metric == metric,
                    UsageCounter.day == (day or limits_config.utc_today()),
                )
                .one_or_none()
            )
            return row.count if row else 0


class TestChargeBoundary(QuotaTestCase):
    def test_n_succeed_and_n_plus_one_is_rejected(self):
        for expected in range(1, 4):
            self.assertEqual(charge(OWNER_A, AI, limit=3), expected)

        with self.assertRaises(QuotaExceeded) as ctx:
            charge(OWNER_A, AI, limit=3)

        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.assertEqual(ctx.exception.details["limit"], 3)
        self.assertEqual(ctx.exception.details["remaining"], 0)

    def test_rejection_does_not_increment_the_counter(self):
        for _ in range(3):
            charge(OWNER_A, AI, limit=3)

        for _ in range(5):
            with self.assertRaises(QuotaExceeded):
                charge(OWNER_A, AI, limit=3)

        # The WHERE clause blocked every rejected attempt: the stored
        # count is the limit exactly, not limit + rejected attempts.
        self.assertEqual(self.stored_count(OWNER_A, AI), 3)

    def test_quota_error_carries_reset_time_and_retry_after(self):
        charge(OWNER_A, AI, limit=1)
        with self.assertRaises(QuotaExceeded) as ctx:
            charge(OWNER_A, AI, limit=1)

        payload = ctx.exception.to_payload()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "quota_exceeded")
        self.assertGreaterEqual(payload["retry_after_seconds"], 1)
        self.assertTrue(payload["resets_at"].endswith("Z"))

    def test_no_provider_billing_wording_reaches_the_message(self):
        charge(OWNER_A, AI, limit=1)
        with self.assertRaises(QuotaExceeded) as ctx:
            charge(OWNER_A, AI, limit=1)

        text = str(ctx.exception.to_payload())
        for forbidden in ("RPM", "TPM", "billing", "Voyage", "Groq", "credit"):
            self.assertNotIn(forbidden, text)


class TestOwnerIsolation(QuotaTestCase):
    def test_two_users_do_not_share_an_allowance(self):
        for _ in range(2):
            charge(OWNER_A, AI, limit=2)
        with self.assertRaises(QuotaExceeded):
            charge(OWNER_A, AI, limit=2)

        # B is untouched by A exhausting its own allowance.
        self.assertEqual(charge(OWNER_B, AI, limit=2), 1)
        self.assertEqual(self.stored_count(OWNER_A, AI), 2)
        self.assertEqual(self.stored_count(OWNER_B, AI), 1)

    def test_charging_one_owner_never_mutates_another_owners_row(self):
        charge(OWNER_B, AI, limit=5)
        before = self.stored_count(OWNER_B, AI)

        for _ in range(3):
            charge(OWNER_A, AI, limit=5)

        self.assertEqual(self.stored_count(OWNER_B, AI), before)

    def test_metrics_are_counted_separately(self):
        charge(OWNER_A, AI, limit=1)
        with self.assertRaises(QuotaExceeded):
            charge(OWNER_A, AI, limit=1)
        # A different metric has its own row and its own allowance.
        self.assertEqual(charge(OWNER_A, limits_config.INDEX_RUN, limit=1), 1)


class TestClientInputCannotKeyACounter(QuotaTestCase):
    """The owner id must come from the verified JWT only. At this layer
    that is a structural property: the module takes an argument and has
    no access to a request."""

    def test_quota_module_never_touches_a_request(self):
        import inspect

        from app.core import quota

        source = inspect.getsource(quota)
        for forbidden in ("fastapi", "Request", "request.headers", "Depends"):
            self.assertNotIn(forbidden, source)

    def test_a_forged_owner_value_only_ever_charges_that_row(self):
        # Simulates a caller passing an attacker-supplied id: it can only
        # spend the forged owner's own allowance, never another's, and it
        # cannot read or reduce OWNER_A's counter.
        charge(OWNER_A, AI, limit=5)
        forged = str(uuid.uuid4())

        charge(forged, AI, limit=5)

        self.assertEqual(self.stored_count(OWNER_A, AI), 1)
        self.assertEqual(self.stored_count(forged, AI), 1)

    def test_non_uuid_owner_is_rejected_outright(self):
        with self.assertRaises(ValueError):
            charge("'; drop table usage_counters; --", AI, limit=5)


class TestDayRollover(QuotaTestCase):
    def test_a_new_utc_day_starts_a_fresh_allowance(self):
        with patch.object(limits_config, "utc_today", return_value=date(2026, 9, 16)):
            charge(OWNER_A, AI, limit=2)
            charge(OWNER_A, AI, limit=2)
            with self.assertRaises(QuotaExceeded):
                charge(OWNER_A, AI, limit=2)

        with patch.object(limits_config, "utc_today", return_value=date(2026, 9, 17)):
            self.assertEqual(charge(OWNER_A, AI, limit=2), 1)

        # Yesterday's row is left intact rather than reset in place.
        self.assertEqual(self.stored_count(OWNER_A, AI, day=date(2026, 9, 16)), 2)
        self.assertEqual(self.stored_count(OWNER_A, AI, day=date(2026, 9, 17)), 1)


class TestFailClosed(QuotaTestCase):
    def test_a_non_database_failure_propagates_rather_than_being_swallowed(self):
        # Only SQLAlchemy errors are translated. A programming error must
        # stay visible — it still fails closed at the endpoint (no provider
        # call happens), but it is not disguised as an infrastructure blip.
        with patch("app.db.session.get_session_factory", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                charge(OWNER_A, AI, limit=5)

    def test_database_error_is_translated_to_quota_unavailable(self):
        from sqlalchemy.exc import OperationalError

        with patch(
            "app.core.quota.session_scope",
            side_effect=OperationalError("select 1", {}, Exception("down")),
        ):
            with self.assertRaises(QuotaUnavailable) as ctx:
                charge(OWNER_A, AI, limit=5)

        self.assertEqual(ctx.exception.code, "quota_unavailable")
        self.assertEqual(ctx.exception.http_status, 503)
        # The driver's text must not leak into the user-facing message.
        self.assertNotIn("select 1", ctx.exception.message)

    def test_unavailable_is_distinguishable_from_exceeded(self):
        self.assertNotEqual(QuotaUnavailable.code, QuotaExceeded.code)
        self.assertEqual(QuotaUnavailable.http_status, 503)
        self.assertEqual(QuotaExceeded.http_status, 429)


class TestConcurrency(QuotaTestCase):
    def test_concurrent_charges_can_never_exceed_the_limit(self):
        limit = 10
        workers = 20
        succeeded = []
        lock = threading.Lock()
        start = threading.Barrier(workers)

        def worker():
            start.wait()
            try:
                charge(OWNER_A, AI, limit=limit)
            except QuotaExceeded:
                return
            except QuotaUnavailable:  # pragma: no cover - SQLite lock exhaustion
                return
            with lock:
                succeeded.append(1)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(succeeded), limit)
        self.assertEqual(self.stored_count(OWNER_A, AI), limit)


class TestPeekAndEnforcement(QuotaTestCase):
    def test_peek_reports_usage_without_consuming(self):
        charge(OWNER_A, AI, limit=5)
        charge(OWNER_A, AI, limit=5)

        self.assertEqual(peek(OWNER_A, AI), 2)
        self.assertEqual(peek(OWNER_A, AI), 2)  # repeated reads change nothing
        self.assertEqual(self.stored_count(OWNER_A, AI), 2)

    def test_peek_on_an_unused_metric_is_zero(self):
        self.assertEqual(peek(OWNER_B, AI), 0)

    def test_ensure_within_quota_raises_only_once_the_allowance_is_spent(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "2"}, clear=False):
            ensure_within_quota(OWNER_A, AI)  # nothing used yet
            charge(OWNER_A, AI)
            ensure_within_quota(OWNER_A, AI)  # one left
            charge(OWNER_A, AI)
            with self.assertRaises(QuotaExceeded):
                ensure_within_quota(OWNER_A, AI)
        # The pre-check consumed nothing of its own.
        self.assertEqual(self.stored_count(OWNER_A, AI), 2)

    def test_check_remaining_reports_usage_and_limit(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "7"}, clear=False):
            charge(OWNER_A, AI)
            used, daily_limit = check_remaining(OWNER_A, AI)
        self.assertEqual((used, daily_limit), (1, 7))

    def test_env_override_sets_the_effective_limit(self):
        with patch.dict("os.environ", {"AI_QUOTA_PER_DAY": "1"}, clear=False):
            charge(OWNER_A, AI)
            with self.assertRaises(QuotaExceeded):
                charge(OWNER_A, AI)


class TestKillSwitch(QuotaTestCase):
    def test_enforcement_off_skips_the_database_entirely(self):
        with patch.dict("os.environ", {"QUOTA_ENFORCEMENT": "off"}, clear=False):
            for _ in range(25):
                self.assertEqual(charge(OWNER_A, AI, limit=1), 0)

        # No row was written at all — the counter path was not reached.
        self.assertEqual(self.stored_count(OWNER_A, AI), 0)

    def test_enforcement_off_reports_no_usage(self):
        with patch.dict("os.environ", {"QUOTA_ENFORCEMENT": "off"}, clear=False):
            self.assertEqual(check_remaining(OWNER_A, AI)[0], 0)
            ensure_within_quota(OWNER_A, AI)  # never raises while off


if __name__ == "__main__":
    unittest.main(verbosity=2)
