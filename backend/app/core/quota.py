"""
Mechanism B: durable per-user daily usage quotas.

One row per (owner_id, day, metric) in `usage_counters`, incremented by a
single conditional statement:

    insert ... values (..., 1)
    on conflict (owner_id, day, metric) do update
       set count = usage_counters.count + 1
       where usage_counters.count < :limit
    returning count

Three properties come from that one statement, with no application lock
and no reservation bookkeeping:

  * Atomic — concurrent chargers serialise on the row, so `count` can
    never exceed the limit no matter how many requests race.
  * Non-consuming rejection — when the WHERE fails, no row is returned
    and nothing is incremented, so a blocked caller does not inflate the
    counter it is waiting on.
  * Portable — the same syntax works on PostgreSQL and on the SQLite
    harness the offline tests use, so the tests exercise the real SQL.

`owner_id` is always supplied by the caller and must be the owner id
already derived from a verified JWT (app/core/auth.py). This module
imports nothing from FastAPI and never inspects a request, header, body
or query parameter, so there is no path by which a client value could key
a counter. Writes go through session_scope(), which binds the verified
identity to the transaction, so the usage_counters RLS policy applies
exactly as it does to every other table.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import Date, DateTime, Integer, String, Uuid, bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.core import limits as limits_config
from app.core.limits import QuotaExceeded, QuotaUnavailable
from app.db.session import session_scope

#: Seconds a client should wait before retrying after a counter failure.
#: Short: this is an infrastructure blip, not a user-facing allowance.
_UNAVAILABLE_RETRY_SECONDS = 30

_CHARGE_SQL = text(
    """
    insert into usage_counters (owner_id, day, metric, count, updated_at)
    values (:owner_id, :day, :metric, 1, :now)
    on conflict (owner_id, day, metric) do update
       set count = usage_counters.count + 1,
           updated_at = :now
     where usage_counters.count < :limit
    returning count
    """
).bindparams(
    bindparam("owner_id", type_=Uuid),
    bindparam("day", type_=Date),
    bindparam("metric", type_=String),
    bindparam("now", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer),
)

_PEEK_SQL = text(
    """
    select count from usage_counters
     where owner_id = :owner_id and day = :day and metric = :metric
    """
).bindparams(
    bindparam("owner_id", type_=Uuid),
    bindparam("day", type_=Date),
    bindparam("metric", type_=String),
)


def _owner_uuid(owner_id: str) -> uuid.UUID:
    """The verified JWT `sub` as a UUID. A malformed value is a
    programming error at the call site, never client-controlled input."""
    return owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))


def _quota_exceeded(metric: str, limit: int) -> QuotaExceeded:
    label = {
        limits_config.AI_GENERATION: "AI answers",
        limits_config.INDEX_RUN: "indexing runs",
        limits_config.QUERY_EMBEDDING: "searches",
        limits_config.UPLOAD: "uploads",
    }.get(metric, "requests")
    resets_at = limits_config.utc_midnight_after()
    return QuotaExceeded(
        f"You've reached today's limit for {label} ({limit}). It resets at 00:00 UTC.",
        retry_after_seconds=limits_config.seconds_until_utc_midnight(),
        limit=limit,
        remaining=0,
        resets_at=resets_at.isoformat().replace("+00:00", "Z"),
    )


def charge(owner_id: str, metric: str, limit: Optional[int] = None) -> int:
    """Consumes one unit of today's allowance for (owner_id, metric).

    Returns the new count. Raises QuotaExceeded when the allowance is
    already spent — in which case NOTHING was incremented. Raises
    QuotaUnavailable if the counter store cannot be reached, which every
    expensive endpoint treats as fail-closed.

    Returns 0 without touching the database when enforcement is off or
    the metric has no daily limit.
    """
    if not limits_config.enforcement_enabled():
        return 0
    effective_limit = limits_config.limits_for(metric).per_day if limit is None else limit
    if not effective_limit:
        return 0

    params = {
        "owner_id": _owner_uuid(owner_id),
        "day": limits_config.utc_today(),
        "metric": metric,
        "now": datetime.now(timezone.utc),
        "limit": effective_limit,
    }

    try:
        with session_scope(owner_id) as db:
            row = db.execute(_CHARGE_SQL, params).fetchone()
    except SQLAlchemyError as exc:
        # Deliberately not re-raised as-is: the driver's message can name
        # schema internals, and the caller needs a distinct failure type
        # so it can fail closed rather than treat this as "over quota".
        raise QuotaUnavailable(
            "Usage tracking is temporarily unavailable. Please try again shortly.",
            retry_after_seconds=_UNAVAILABLE_RETRY_SECONDS,
        ) from exc

    if row is None:
        raise _quota_exceeded(metric, effective_limit)
    return int(row[0])


def peek(owner_id: str, metric: str) -> int:
    """Today's count for (owner_id, metric) WITHOUT consuming anything.

    Used where a proper HTTP rejection must be produced before work
    begins — notably /ask-stream, which cannot send a 429 once the
    streaming response has started.
    """
    params = {
        "owner_id": _owner_uuid(owner_id),
        "day": limits_config.utc_today(),
        "metric": metric,
    }
    try:
        with session_scope(owner_id) as db:
            row = db.execute(_PEEK_SQL, params).fetchone()
    except SQLAlchemyError as exc:
        raise QuotaUnavailable(
            "Usage tracking is temporarily unavailable. Please try again shortly.",
            retry_after_seconds=_UNAVAILABLE_RETRY_SECONDS,
        ) from exc
    return int(row[0]) if row is not None else 0


def check_remaining(owner_id: str, metric: str) -> Tuple[int, Optional[int]]:
    """(used_today, daily_limit) for this owner and metric."""
    daily_limit = limits_config.limits_for(metric).per_day
    if not limits_config.enforcement_enabled() or not daily_limit:
        return 0, daily_limit
    return peek(owner_id, metric), daily_limit


def ensure_within_quota(owner_id: str, metric: str) -> None:
    """Read-only pre-check. Raises QuotaExceeded if the allowance is
    already spent, without consuming a unit."""
    used, daily_limit = check_remaining(owner_id, metric)
    if daily_limit and used >= daily_limit:
        raise _quota_exceeded(metric, daily_limit)
