"""
E1 mechanism A: in-process burst rate limiting.

Time is injected rather than slept, so these run in milliseconds and the
window boundary is exact instead of approximate.
"""
import threading
import unittest
from unittest.mock import patch

from app.core.limits import RateLimited
from app.core.rate_limit import SlidingWindowLimiter, bucket_key, enforce_burst, get_limiter


class FakeClock:
    """Monotonic clock under test control."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestSlidingWindow(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = SlidingWindowLimiter(window_seconds=60.0, clock=self.clock)

    def test_n_allowed_then_n_plus_one_rejected(self):
        for _ in range(5):
            self.limiter.consume("k", 5)

        with self.assertRaises(RateLimited) as ctx:
            self.limiter.consume("k", 5)

        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertEqual(ctx.exception.details["limit"], 5)

    def test_rejection_does_not_consume_capacity(self):
        for _ in range(5):
            self.limiter.consume("k", 5)

        for _ in range(3):
            with self.assertRaises(RateLimited):
                self.limiter.consume("k", 5)

        # Still exactly the five accepted entries — rejected attempts were
        # not recorded, so they cannot push recovery further away.
        self.assertEqual(self.limiter.entries_for("k"), 5)

    def test_retry_after_counts_down_with_the_oldest_entry(self):
        self.limiter.consume("k", 1)

        self.clock.advance(20)
        with self.assertRaises(RateLimited) as ctx:
            self.limiter.consume("k", 1)
        self.assertEqual(ctx.exception.retry_after_seconds, 40)

        self.clock.advance(35)
        with self.assertRaises(RateLimited) as ctx:
            self.limiter.consume("k", 1)
        self.assertEqual(ctx.exception.retry_after_seconds, 5)

    def test_retry_after_is_never_below_one_second(self):
        self.limiter.consume("k", 1)
        self.clock.advance(59.9)
        with self.assertRaises(RateLimited) as ctx:
            self.limiter.consume("k", 1)
        self.assertGreaterEqual(ctx.exception.retry_after_seconds, 1)

    def test_window_expiry_frees_capacity(self):
        for _ in range(5):
            self.limiter.consume("k", 5)
        with self.assertRaises(RateLimited):
            self.limiter.consume("k", 5)

        self.clock.advance(61)
        self.limiter.consume("k", 5)  # must not raise
        self.assertEqual(self.limiter.entries_for("k"), 1)

    def test_window_slides_rather_than_resetting_in_blocks(self):
        self.limiter.consume("k", 2)
        self.clock.advance(30)
        self.limiter.consume("k", 2)

        with self.assertRaises(RateLimited):
            self.limiter.consume("k", 2)

        # Only the first entry has aged out at t+61: one slot, not two.
        self.clock.advance(31)
        self.limiter.consume("k", 2)
        with self.assertRaises(RateLimited):
            self.limiter.consume("k", 2)

    def test_keys_are_independent(self):
        for _ in range(5):
            self.limiter.consume("a", 5)
        self.limiter.consume("b", 5)  # different key, unaffected
        self.assertEqual(self.limiter.entries_for("b"), 1)

    def test_idle_keys_are_reclaimed_so_memory_stays_bounded(self):
        for owner in range(50):
            self.limiter.consume(f"m:{owner}", 5)
        self.assertEqual(self.limiter.tracked_keys(), 50)

        self.clock.advance(61)
        # Touching one key drops that key's stale window; the others are
        # dropped as soon as they are touched again.
        self.limiter.consume("m:0", 5)
        self.assertEqual(self.limiter.entries_for("m:0"), 1)

        for owner in range(50):
            self.limiter.consume(f"m:{owner}", 5)
        self.assertEqual(self.limiter.tracked_keys(), 50)

    def test_entries_per_key_never_exceed_the_limit(self):
        for _ in range(100):
            try:
                self.limiter.consume("k", 3)
            except RateLimited:
                pass
        self.assertLessEqual(self.limiter.entries_for("k"), 3)

    def test_zero_limit_is_a_no_op_rather_than_a_lockout(self):
        self.limiter.consume("k", 0)
        self.assertEqual(self.limiter.entries_for("k"), 0)

    def test_concurrent_consumers_cannot_exceed_the_limit(self):
        limiter = SlidingWindowLimiter(window_seconds=60.0, clock=self.clock)
        allowed = []
        lock = threading.Lock()
        start = threading.Barrier(20)

        def worker():
            start.wait()
            try:
                limiter.consume("shared", 10)
            except RateLimited:
                return
            with lock:
                allowed.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(allowed), 10)
        self.assertEqual(limiter.entries_for("shared"), 10)


class TestEnforceBurst(unittest.TestCase):
    """The endpoint-facing wrapper: kill switch and metric resolution."""

    def setUp(self):
        # tests/__init__.py disables enforcement for the wider suite.
        enforcement = patch.dict("os.environ", {"QUOTA_ENFORCEMENT": "on"}, clear=False)
        enforcement.start()
        self.addCleanup(enforcement.stop)

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

    def test_bucket_key_is_owner_and_metric_only(self):
        self.assertEqual(bucket_key("owner-a", "ai_generation"), "ai_generation:owner-a")

    def test_enforces_the_configured_per_minute_limit(self):
        with patch.dict("os.environ", {"AI_RATE_PER_MINUTE": "2"}, clear=False):
            enforce_burst("owner-a", "ai_generation")
            enforce_burst("owner-a", "ai_generation")
            with self.assertRaises(RateLimited):
                enforce_burst("owner-a", "ai_generation")

    def test_two_owners_do_not_share_a_bucket(self):
        with patch.dict("os.environ", {"AI_RATE_PER_MINUTE": "1"}, clear=False):
            enforce_burst("owner-a", "ai_generation")
            with self.assertRaises(RateLimited):
                enforce_burst("owner-a", "ai_generation")
            enforce_burst("owner-b", "ai_generation")  # unaffected

    def test_metrics_do_not_share_a_bucket(self):
        with patch.dict(
            "os.environ",
            {"AI_RATE_PER_MINUTE": "1", "INDEX_RATE_PER_MINUTE": "1"},
            clear=False,
        ):
            enforce_burst("owner-a", "ai_generation")
            enforce_burst("owner-a", "index_run")  # different metric, own window

    def test_kill_switch_disables_burst_limiting(self):
        with patch.dict(
            "os.environ",
            {"AI_RATE_PER_MINUTE": "1", "QUOTA_ENFORCEMENT": "off"},
            clear=False,
        ):
            for _ in range(10):
                enforce_burst("owner-a", "ai_generation")  # never raises

    def test_typo_in_kill_switch_leaves_enforcement_on(self):
        with patch.dict(
            "os.environ",
            {"AI_RATE_PER_MINUTE": "1", "QUOTA_ENFORCEMENT": "offf"},
            clear=False,
        ):
            enforce_burst("owner-a", "ai_generation")
            with self.assertRaises(RateLimited):
                enforce_burst("owner-a", "ai_generation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
