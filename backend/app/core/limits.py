"""
Usage-limit configuration and the shared error vocabulary.

Four mechanisms are kept deliberately separate, because they fail
differently and are stored differently:

  A. burst rate limiting  — app/core/rate_limit.py  (in-process, per user)
  B. daily usage quotas   — app/core/quota.py       (Postgres, durable)
  C. indexing concurrency — Phase 3                 (in-process semaphore)
  D. per-paper caps       — Phase 3                 (input validation)

Every number here is an INITIAL ENGINEERING DEFAULT, not product policy.
Each is overridable by a server-side environment variable, and each is
read at CALL time rather than bound at import, so an operator override —
or a test patch of the module constant — actually takes effect. This
mirrors the existing convention in app/rag/embedder.py.

QUOTA_ENFORCEMENT is read from the server environment only. It is never
sent to, or read from, a client. When it is off, mechanisms A and B are
skipped entirely; C and D stay mandatory once wired, because they
protect the provider's shared rate limit and the request duration
rather than per-user fairness.
"""
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional

# ----------------------------------------------------------------------
# Metric names. These are the ONLY valid keys for a counter or a bucket.
# ----------------------------------------------------------------------
AI_GENERATION = "ai_generation"  # ask-stream, research, compare-papers, summarize-paper
INDEX_RUN = "index_run"  # index-document
QUERY_EMBEDDING = "query_embedding"  # search, paper-details — defined, NOT yet enforced
UPLOAD = "upload"  # upload — defined, NOT yet enforced
CHEAP_READ = "cheap_read"  # stats, papers, export, delete — defined, NOT yet enforced

#: Metrics wired into endpoints in Phase 3. The others are configured
#: here so the limits are reviewable now, but nothing reads them yet.
ENFORCED_METRICS = (AI_GENERATION, INDEX_RUN)

#: metric -> (per-minute env var, per-minute default, per-day env var, per-day default)
#: A None on either side means "this metric has no limit of that kind".
_LIMIT_SPECS = {
    AI_GENERATION: ("AI_RATE_PER_MINUTE", 5, "AI_QUOTA_PER_DAY", 50),
    INDEX_RUN: ("INDEX_RATE_PER_MINUTE", 3, "INDEX_QUOTA_PER_DAY", 20),
    QUERY_EMBEDDING: ("QUERY_RATE_PER_MINUTE", 20, "QUERY_QUOTA_PER_DAY", 200),
    UPLOAD: ("UPLOAD_RATE_PER_MINUTE", 5, "UPLOAD_QUOTA_PER_DAY", 10),
    CHEAP_READ: ("READ_RATE_PER_MINUTE", 60, None, None),
}


def _int_env(name: str, default: int) -> int:
    """Reads a positive integer setting, falling back to the default for
    anything unparseable rather than crashing the request path."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class MetricLimits:
    metric: str
    per_minute: Optional[int]
    per_day: Optional[int]


def limits_for(metric: str) -> MetricLimits:
    """Current limits for a metric, resolved from the environment."""
    try:
        minute_env, minute_default, day_env, day_default = _LIMIT_SPECS[metric]
    except KeyError:
        raise ValueError(f"unknown usage metric: {metric!r}") from None
    return MetricLimits(
        metric=metric,
        per_minute=_int_env(minute_env, minute_default) if minute_env else None,
        per_day=_int_env(day_env, day_default) if day_env else None,
    )


def enforcement_enabled() -> bool:
    """Server-side kill switch for mechanisms A and B.

    Anything other than an explicit off value leaves enforcement ON, so a
    typo fails safe (limits still applied) rather than silently disabling
    spend protection.
    """
    return os.environ.get("QUOTA_ENFORCEMENT", "on").strip().lower() not in {
        "off",
        "false",
        "0",
        "no",
    }


def index_slot_wait_seconds() -> float:
    """How long an indexing request may wait for the global slot (C).

    Short by design: a real indexing run takes minutes, so this waits only
    for a departing job's final upsert, never for a whole run.
    """
    return float(_int_env("INDEX_SLOT_WAIT_SECONDS", 5))


def max_pages_per_paper() -> int:
    """Cap D: bounds embedding work, and therefore request duration."""
    return _int_env("MAX_PAGES_PER_PAPER", 40)


def max_chunks_per_paper() -> int:
    """Cap D: backstop for text-dense pages."""
    return _int_env("MAX_CHUNKS_PER_PAPER", 150)


def utc_today() -> date:
    """The quota day. UTC so a counter cannot be reset by travelling, and
    computed in Python so Postgres and the SQLite test harness agree."""
    return datetime.now(timezone.utc).date()


def utc_midnight_after(now: Optional[datetime] = None) -> datetime:
    """The next UTC midnight — when the current day's counters stop applying."""
    now = now or datetime.now(timezone.utc)
    midnight_today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return midnight_today + timedelta(days=1)


def seconds_until_utc_midnight(now: Optional[datetime] = None) -> int:
    """Whole seconds until the daily counters roll over. Always >= 1 so a
    Retry-After header is never zero."""
    now = now or datetime.now(timezone.utc)
    return max(1, math.ceil((utc_midnight_after(now) - now).total_seconds()))


# ----------------------------------------------------------------------
# Error vocabulary. Every mechanism raises one of these; the message is
# always written here, never interpolated from a provider's response, so
# no upstream billing or quota wording can reach a user.
# ----------------------------------------------------------------------
class UsageLimitError(Exception):
    """Base for every limit rejection. Carries everything the HTTP layer
    needs to render the normalized body defined in the E1 plan."""

    code = "usage_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after_seconds: int, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.details = {k: v for k, v in details.items() if v is not None}

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "retry_after_seconds": self.retry_after_seconds,
        }
        payload.update(self.details)
        return payload


class RateLimited(UsageLimitError):
    """Mechanism A: too many requests in the current minute."""

    code = "rate_limited"


class QuotaExceeded(UsageLimitError):
    """Mechanism B: the daily allowance for this metric is spent."""

    code = "quota_exceeded"


class IndexingBusy(UsageLimitError):
    """Mechanism C: another indexing run holds the only slot.

    Defined here so the vocabulary is complete; raised in Phase 3.
    """

    code = "indexing_busy"


class QuotaUnavailable(UsageLimitError):
    """The counter store could not be reached.

    Expensive endpoints fail CLOSED on this: an unmetered request can
    spend real money at a provider, so refusing is safer than proceeding.
    """

    code = "quota_unavailable"
    http_status = 503
