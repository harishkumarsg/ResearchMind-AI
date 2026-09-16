"""
Mechanism A: in-process per-user burst rate limiting.

A sliding-window log per (metric, owner). Each accepted request appends a
timestamp; a request is rejected when the window already holds `limit`
entries. Rejection therefore never consumes capacity, and the window
frees continuously rather than in fixed jumps.

Deliberately in-process, not Postgres: production runs a single uvicorn
worker (no --workers flag in backend/railway.toml), so one process sees
every request, and a database write per request would add latency to the
cheapest endpoints for no benefit at this scale. The honest limitation is
that this state is per-process: it resets on restart, and it would become
per-replica if the service is ever scaled out. Durable limits are
mechanism B's job (app/core/quota.py), which is exactly why the two are
separate.

Memory is bounded by construction: at most `limit` timestamps per active
key, and a key is dropped as soon as its window empties.
"""
import math
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict

from app.core import limits as limits_config
from app.core.limits import RateLimited

WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    """Thread-safe sliding-window counter.

    The clock is injectable so tests can advance time deterministically
    instead of sleeping. Endpoints run in FastAPI's threadpool (they are
    sync `def`), so every mutation is guarded by one lock; the critical
    section is a few list operations and is not a contention concern at
    this request volume.
    """

    def __init__(
        self,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: Dict[str, Deque[float]] = {}

    def consume(self, key: str, limit: int) -> None:
        """Records one request against `key`, or raises RateLimited.

        Raising leaves the window untouched, so a rejected caller cannot
        push its own recovery further away by retrying.
        """
        if limit <= 0:
            return
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            entries = self._windows.get(key)
            if entries is not None:
                while entries and entries[0] <= cutoff:
                    entries.popleft()
                if not entries:
                    # Drop the key entirely so idle users cost no memory.
                    del self._windows[key]
                    entries = None

            if entries is not None and len(entries) >= limit:
                retry_after = math.ceil(self._window - (now - entries[0]))
                raise RateLimited(
                    f"Too many requests. Please wait about {max(1, retry_after)} seconds "
                    "and try again.",
                    retry_after_seconds=retry_after,
                    limit=limit,
                )

            if entries is None:
                entries = deque()
                self._windows[key] = entries
            entries.append(now)

    def tracked_keys(self) -> int:
        """Number of keys currently holding state — asserted by tests to
        prove idle keys are reclaimed."""
        with self._lock:
            return len(self._windows)

    def entries_for(self, key: str) -> int:
        with self._lock:
            return len(self._windows.get(key, ()))

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


#: Process-wide limiter. One instance so every endpoint shares the window.
_limiter = SlidingWindowLimiter()


def bucket_key(owner_id: str, metric: str) -> str:
    """Bucket identity. `owner_id` must be the verified JWT `sub` supplied
    by the caller — this module never reads a request, header or body."""
    return f"{metric}:{owner_id}"


def enforce_burst(owner_id: str, metric: str) -> None:
    """Applies mechanism A for one request, honouring the kill switch.

    Raises RateLimited when the per-minute allowance for this metric is
    already spent. Does nothing when QUOTA_ENFORCEMENT is off, or when the
    metric has no per-minute limit configured.
    """
    if not limits_config.enforcement_enabled():
        return
    per_minute = limits_config.limits_for(metric).per_minute
    if not per_minute:
        return
    _limiter.consume(bucket_key(owner_id, metric), per_minute)


def get_limiter() -> SlidingWindowLimiter:
    """Accessor for the shared limiter, so tests can inspect or reset it
    without reaching into a private name."""
    return _limiter
