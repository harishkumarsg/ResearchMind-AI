"""
Atomic claims on a paper's lifecycle.

Deleting, indexing and uploading a paper each change something outside
Postgres — Qdrant vectors, the Storage PDF — through calls that can take
seconds or minutes. Doing that on the strength of a status read earlier
is a race: two deletes ran the whole pipeline side by side, and an index
run could write vectors for a paper a concurrent delete had already
removed. So before touching anything external, a request CLAIMS the row
with one conditional UPDATE and goes ahead only if exactly one row
changed. Every later write it makes is conditioned on that claim, so a
request whose claim has been taken over can no longer change the row.

THE CLAIM TOKEN is the `updated_at` value the claim wrote; no column is
added for it.

  - Exact. Postgres timestamptz and Python datetimes both carry
    microseconds, so the value round-trips unchanged, and a token is only
    ever compared for equality — clock skew between instances can never
    make one request's claim pass for another's.
  - Forward only. papers.updated_at is also the "row changed since this
    analysis" watermark that paper_evidence.reindexed_since reads, so
    nothing here moves it backwards: a write made under a claim sets
    max(now, token + 1µs).
  - Expiry is the one place clocks are compared. A claim that is not
    renewed within its lease may be taken over — that is how a crashed
    process's claim is recovered. Skew between instances shifts expiry by
    the skew: milliseconds, against leases of minutes.

A deletion that fails partway is RELEASED, not rolled back: the row stays
'deleting', with DELETION_INCOMPLETE_DETAIL in status_detail. A live
deletion claim always has status_detail NULL, so the detail is what makes
a released row claimable again at once. A deletion whose process died was
never released; its lease expires instead.

Every statement here touches one row, keyed by (id, owner_id), and is
committed immediately: no transaction or row lock is ever held across an
external call. Under PostgreSQL READ COMMITTED, a competing UPDATE or
DELETE of the same row waits for the first to commit and then re-checks
its WHERE clause against the committed row, so of two claims whose
conditions exclude each other exactly one changes a row; RLS's owner
predicate is part of that re-check. The offline suite runs these same
statements on SQLite, which serialises writers: it verifies the protocol,
not Postgres's locking.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Paper

#: How long each in-progress status holds a paper before another request
#: may take it over. Each is far longer than the work it covers: a delete
#: is two Qdrant calls (120s timeout each) and two Storage calls; an
#: upload, one Storage write; an index run, a PDF fetch, extraction,
#: embedding with bounded retries, and three Qdrant calls.
DELETION_LEASE = timedelta(minutes=10)
UPLOAD_LEASE = timedelta(minutes=15)
INDEXING_LEASE = timedelta(minutes=60)

#: Statuses a deletion may claim at once. 'uploading' and 'indexing' are
#: claimable only after their lease has expired: until then another
#: request may still be writing the PDF or the vectors.
DELETABLE_STATUSES = ("uploaded", "indexed", "failed")

#: Written to status_detail when a deletion stops partway. Authored here:
#: status_detail is shown to the user.
DELETION_INCOMPLETE_DETAIL = "Removal did not finish. Delete this paper again to complete it."


class ClaimLost(Exception):
    """This request's claim on the paper has been taken over."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _after(token: datetime) -> datetime:
    """A fresh updated_at that never moves the watermark backwards."""
    return max(now(), token + timedelta(microseconds=1))


def _owned(paper_id: uuid.UUID, owner_id: uuid.UUID):
    return (Paper.id == paper_id, Paper.owner_id == owner_id)


def _apply(db: Session, statement) -> int:
    """Runs one single-row statement, commits it, and returns how many rows
    it changed."""
    result = db.execute(statement.execution_options(synchronize_session=False))
    changed = result.rowcount
    db.commit()
    return changed


def claim(
    db: Session,
    paper_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    from_status: str,
    to_status: str,
    **values,
) -> Optional[datetime]:
    """Moves the paper from `from_status` to `to_status` only if it is still
    in `from_status`. Returns the claim token, or None if the paper was not
    in that status (or is not this owner's)."""
    token = now()
    statement = (
        update(Paper)
        .where(*_owned(paper_id, owner_id), Paper.status == from_status)
        .values(status=to_status, updated_at=token, **values)
    )
    return token if _apply(db, statement) == 1 else None


def claim_for_deletion(
    db: Session, paper_id: uuid.UUID, owner_id: uuid.UUID
) -> Optional[datetime]:
    """Claims the paper for deletion, or returns None.

    Claimable: a paper at rest; a deletion that was released, or whose
    lease expired; an upload or index run whose lease expired. Never a
    live upload, index run or deletion.
    """
    token = now()
    claimable = or_(
        Paper.status.in_(DELETABLE_STATUSES),
        and_(
            Paper.status == "deleting",
            or_(
                Paper.status_detail.is_not(None),
                Paper.updated_at < token - DELETION_LEASE,
            ),
        ),
        and_(Paper.status == "uploading", Paper.updated_at < token - UPLOAD_LEASE),
        and_(Paper.status == "indexing", Paper.updated_at < token - INDEXING_LEASE),
    )
    statement = (
        update(Paper)
        .where(*_owned(paper_id, owner_id), claimable)
        .values(status="deleting", status_detail=None, updated_at=token)
    )
    return token if _apply(db, statement) == 1 else None


def renew(
    db: Session, paper_id: uuid.UUID, owner_id: uuid.UUID, status: str, token: datetime
) -> Optional[datetime]:
    """Confirms this request still holds its claim and restarts its lease.
    Returns the new token, or None if the claim was taken over."""
    fresh = _after(token)
    statement = (
        update(Paper)
        .where(*_owned(paper_id, owner_id), Paper.status == status, Paper.updated_at == token)
        .values(updated_at=fresh)
    )
    return fresh if _apply(db, statement) == 1 else None


def transition(
    db: Session,
    paper_id: uuid.UUID,
    owner_id: uuid.UUID,
    status: str,
    token: datetime,
    *,
    to_status: str,
    status_detail: Optional[str] = None,
) -> bool:
    """Ends this request's claim by moving the paper to `to_status`.
    False if the claim was taken over; the row is then left alone."""
    statement = (
        update(Paper)
        .where(*_owned(paper_id, owner_id), Paper.status == status, Paper.updated_at == token)
        .values(status=to_status, status_detail=status_detail, updated_at=_after(token))
    )
    return _apply(db, statement) == 1


def release_deletion(
    db: Session, paper_id: uuid.UUID, owner_id: uuid.UUID, token: datetime
) -> bool:
    """Marks a deletion that stopped partway as free to retry at once. The
    row stays 'deleting': some of its resources may already be gone."""
    statement = (
        update(Paper)
        .where(
            *_owned(paper_id, owner_id),
            Paper.status == "deleting",
            Paper.updated_at == token,
            Paper.status_detail.is_(None),
        )
        .values(status_detail=DELETION_INCOMPLETE_DETAIL, updated_at=_after(token))
    )
    return _apply(db, statement) == 1


def finish_deletion(
    db: Session, paper_id: uuid.UUID, owner_id: uuid.UUID, token: datetime
) -> bool:
    """Deletes the row, only while this request still holds the claim."""
    statement = delete(Paper).where(
        *_owned(paper_id, owner_id),
        Paper.status == "deleting",
        Paper.updated_at == token,
        Paper.status_detail.is_(None),
    )
    return _apply(db, statement) == 1


def current_status(db: Session, paper_id: uuid.UUID, owner_id: uuid.UUID) -> Optional[str]:
    """The paper's status, or None if this owner has no such paper."""
    status = db.execute(select(Paper.status).where(*_owned(paper_id, owner_id))).scalar_one_or_none()
    db.commit()
    return status
