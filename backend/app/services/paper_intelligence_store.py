"""
Durable persistence for Paper Intelligence: exactly one current row per
(owner_id, paper_id) in `paper_intelligence` (migration 0005).

This module stores and returns ALREADY-VALIDATED objects. It does not
generate them, does not call a model, and does not reach the network —
app/services/intelligence_schema.py remains the single source of truth
for what a PaperIntelligence is, and nothing here re-implements or
relaxes it. The only thing this module decides is whether a given owner
may write to a given paper, and it decides that against the database.

Every function takes owner_id as the verified Supabase JWT `sub`
supplied by app.core.auth — never a client-supplied value — and scopes
both its reads and its writes by it. paper_id, by contrast, IS
client-supplied on the request path this will eventually serve, so it is
parsed defensively and checked for ownership before any write.

Each function opens its own short-lived session_scope(), matching
chat_store: the caller in Step 3 will be a generation path that spans a
multi-second model call, and a request-scoped session would hold a
pooled Postgres connection checked out for that whole duration.

DEFENCE IN DEPTH, NOT INSTEAD OF
--------------------------------
Postgres enforces owner isolation on this table through RLS (enabled and
FORCED in 0005, with auth.uid() bound per transaction by
app/db/session.py). The explicit `owner_id ==` term in every query below
is kept anyway, exactly as it is on every other table: the offline test
database has no RLS at all, so the application filter is what makes the
tests meaningful, and in production the two must agree.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db.models import Paper, PaperIntelligenceRow
from app.db.session import session_scope
from app.services.intelligence_schema import (
    IntelligenceValidationError,
    PaperIntelligence,
)

#: The shape of the object in the `intelligence` column, recorded on
#: every row. Deliberately a bare string and deliberately not a
#: framework: its job is to let a future change identify exactly which
#: rows predate it, which a single comparable value does. Bump it when
#: the persisted structure changes in a way a reader must notice —
#: adding a section, renaming a field, changing evidence identity.
PAPER_INTELLIGENCE_SCHEMA_VERSION = "1"


class PaperNotOwned(LookupError):
    """The paper does not exist, or it is not this owner's.

    One exception for both cases, on purpose: distinguishing them would
    tell a caller whether some other user's paper id is real. This is the
    same posture /summarize-paper and /paper-file already take.
    """


def _utcnow() -> datetime:
    """Matches app.db.models._utcnow. A named module-level function so
    tests can pin the clock."""
    return datetime.now(timezone.utc)


def _owner_uuid(owner_id) -> uuid.UUID:
    """The verified JWT `sub` as a UUID.

    Raises on a malformed value rather than returning None: owner_id
    never comes from a client, so a bad one is a programming error at the
    call site and must not be swallowed into a silent empty result.
    """
    return owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))


def _paper_uuid(paper_id) -> Optional[uuid.UUID]:
    """A client-supplied paper id as a UUID, or None if it is not one.

    None rather than an exception, mirroring resolve_owned_paper() in
    api/paper_file.py: a malformed id and an unknown id are the same
    answer to the caller, and neither is a server error.
    """
    if not paper_id:
        return None
    try:
        return paper_id if isinstance(paper_id, uuid.UUID) else uuid.UUID(str(paper_id))
    except (AttributeError, TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime.

    SQLite, the offline test database, reads timestamptz values back
    naive; every value this module writes is UTC. Without this, comparing
    a stored stamp against an incoming one raises TypeError on the
    offline suite and silently compares wall clocks in production.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class StoredIntelligence:
    """One persisted row, as plain values.

    Not an ORM object: the session that produced it is already closed by
    the time this is returned, and the caller may hold it across a long
    model call.

    `superseded` is False for every row this store wrote and True only
    when save_intelligence() declined to overwrite a newer row — see the
    staleness guard there. A caller that needs to know whether the object
    it just generated is the one now stored reads this flag; a caller
    that only wants the current intelligence can ignore it, because the
    object returned is the current one either way.
    """

    paper_id: uuid.UUID
    intelligence: PaperIntelligence
    generated_at: datetime
    model: str
    schema_version: str
    updated_at: datetime
    superseded: bool = False


def _owns_paper(db, owner_uuid: uuid.UUID, paper_uuid: uuid.UUID) -> bool:
    """Does this owner have a papers row with this id?

    Checked before every write, for the same two reasons chat_store
    checks it: paper_id is a FK, so a stale or invented one would raise a
    FK violation and sink the request, and the owner_id term stops a
    request from ever attaching intelligence to another owner's paper.

    Deliberately an application check rather than a cross-table subquery
    inside the RLS policy — see the note in migration 0005.
    """
    return (
        db.query(Paper.id)
        .filter(Paper.id == paper_uuid, Paper.owner_id == owner_uuid)
        .first()
    ) is not None


def _parse_stored(
    row: PaperIntelligenceRow, *, superseded: bool = False
) -> StoredIntelligence:
    """Turn a row into plain values, re-checking the stored JSON.

    Structure only: the evidence allowlist that validate_intelligence()
    needs belongs to the generation that produced this object and is long
    gone by read time. What this catches is a row whose JSON no longer
    matches the model at all — hand-edited, restored from an older
    schema_version, or corrupted — and it fails loudly rather than
    handing a caller a half-shaped object.

    It repairs nothing. status, summary, evidence, page, chunk_id and
    quote come back exactly as they were stored.
    """
    try:
        intelligence = PaperIntelligence.model_validate(row.intelligence)
    except ValidationError:
        raise IntelligenceValidationError(
            "invalid_structure",
            "The stored intelligence for this paper is not readable.",
        ) from None

    return StoredIntelligence(
        paper_id=row.paper_id,
        intelligence=intelligence,
        generated_at=row.generated_at,
        model=row.model,
        schema_version=row.schema_version,
        updated_at=row.updated_at,
        superseded=superseded,
    )


def get_intelligence(owner_id: str, paper_id) -> Optional[StoredIntelligence]:
    """The current intelligence for this owner's paper, or None.

    None covers every way there is nothing to return: never generated, a
    paper that does not exist, a paper belonging to someone else, and a
    paper_id that is not a UUID at all. No ownership probe is needed
    beyond the query itself — a row keyed on another owner simply does
    not match.
    """
    paper_uuid = _paper_uuid(paper_id)
    if paper_uuid is None:
        return None

    owner_uuid = _owner_uuid(owner_id)

    with session_scope(owner_id) as db:
        row = (
            db.query(PaperIntelligenceRow)
            .filter(
                PaperIntelligenceRow.owner_id == owner_uuid,
                PaperIntelligenceRow.paper_id == paper_uuid,
            )
            .first()
        )
        if row is None:
            return None
        return _parse_stored(row)


def save_intelligence(
    owner_id: str,
    paper_id,
    intelligence: PaperIntelligence,
    *,
    model: str,
    generated_at: Optional[datetime] = None,
    schema_version: str = PAPER_INTELLIGENCE_SCHEMA_VERSION,
) -> StoredIntelligence:
    """Write the current intelligence for this owner's paper.

    Upsert, not append: UNIQUE(owner_id, paper_id) means a regeneration
    replaces the row in place, so a paper can never accumulate two
    "current" objects.

    `intelligence` must be a PaperIntelligence instance — a dict is
    rejected with TypeError. That is the enforcement point for "only
    validated objects are persisted": the sole way to obtain one of these
    is validate_intelligence(), so there is no path from raw model output
    to this table that skips the validator.

    NOT last-writer-wins: if the stored row carries a `generated_at`
    later than this one's, the write is dropped and the existing newer
    row is returned with `superseded=True`. See the staleness guard
    below. `generated_at` should therefore be stamped when the
    generation run STARTS, so the ordering reflects request order.

    Raises PaperNotOwned if the paper is not this owner's.
    """
    if not isinstance(intelligence, PaperIntelligence):
        raise TypeError(
            "save_intelligence requires a validated PaperIntelligence; "
            "run validate_intelligence() on the model output first"
        )

    paper_uuid = _paper_uuid(paper_id)
    if paper_uuid is None:
        raise PaperNotOwned("No such paper for this owner.")

    owner_uuid = _owner_uuid(owner_id)
    stamp = generated_at or _utcnow()
    # mode="json" so the stored value is plain JSON types. extra="forbid"
    # on every model in the schema means this dump can contain nothing
    # but the validated fields — in particular no Qdrant point id, which
    # the schema has no field for.
    payload = intelligence.model_dump(mode="json")

    def attempt() -> StoredIntelligence:
        with session_scope(owner_id) as db:
            if not _owns_paper(db, owner_uuid, paper_uuid):
                raise PaperNotOwned("No such paper for this owner.")

            row = (
                db.query(PaperIntelligenceRow)
                .filter(
                    PaperIntelligenceRow.owner_id == owner_uuid,
                    PaperIntelligenceRow.paper_id == paper_uuid,
                )
                # Locked for the rest of the transaction so the staleness
                # comparison below is a real compare-and-set. Without the
                # lock two concurrent regenerations could both read the
                # same old stamp, both judge themselves newer, and both
                # write — which is the last-writer-wins behaviour this
                # guard exists to prevent. A no-op on SQLite, as it is
                # for the identical lock in chat_store.
                .with_for_update()
                .first()
            )

            if row is not None and _as_utc(row.generated_at) > _as_utc(stamp):
                # STALENESS GUARD.
                #
                # The stored row was generated more recently than the
                # object being offered, so this write is stale and is
                # dropped rather than applied. The case it exists for:
                # a user triggers a slow regeneration, triggers a second
                # one, the second finishes first, and the first then
                # lands and silently reverts the paper to the older
                # analysis.
                #
                # `generated_at` is stamped when a generation RUN BEGINS
                # (see api/paper_intelligence.py), not when it finishes,
                # so "newer" means "started from a more recent request" —
                # which is the ordering a user perceives.
                #
                # Strictly greater, not >=: two runs that start inside
                # one clock tick are genuinely indistinguishable, and
                # refusing both would make an ordinary back-to-back
                # regeneration fail.
                #
                # The existing, newer row is returned so the caller still
                # receives the current intelligence, flagged so it can
                # tell that its own result was not the one kept.
                return _parse_stored(row, superseded=True)

            if row is None:
                row = PaperIntelligenceRow(
                    owner_id=owner_uuid,
                    paper_id=paper_uuid,
                    intelligence=payload,
                    generated_at=stamp,
                    model=model,
                    schema_version=schema_version,
                )
                db.add(row)
            else:
                row.intelligence = payload
                row.generated_at = stamp
                row.model = model
                row.schema_version = schema_version
                # Set explicitly rather than relying on the onupdate
                # hook: regenerating a paper whose content did not change
                # leaves no column dirty, SQLAlchemy emits no UPDATE, and
                # onupdate never fires.
                row.updated_at = _utcnow()

            # flush so server- and python-side defaults are populated
            # before the values are read out, and so a UNIQUE violation
            # surfaces here rather than at commit.
            db.flush()

            # Built inside the transaction, because the row expires the
            # moment session_scope commits.
            return _parse_stored(row)

    try:
        return attempt()
    except IntegrityError:
        # UNIQUE(owner_id, paper_id). Two concurrent generations for the
        # same paper can both find no row and both insert; the loser
        # lands here. The winner's row is committed by now, so the retry
        # finds it and updates it instead. This is what that constraint
        # is for.
        return attempt()


def delete_intelligence(owner_id: str, paper_id) -> bool:
    """Remove this owner's intelligence for this paper. True if a row went.

    Scoped by owner_id and paper_id together, so a row belonging to
    another owner is simply not matched — there is no code path that
    deletes one.

    This is NOT the cleanup path for a deleted paper: paper_id is a FK
    with ON DELETE CASCADE, so removing the papers row takes the
    intelligence with it (migration 0005). This exists for explicit
    invalidation of an object whose paper is staying.
    """
    paper_uuid = _paper_uuid(paper_id)
    if paper_uuid is None:
        return False

    owner_uuid = _owner_uuid(owner_id)

    with session_scope(owner_id) as db:
        removed = (
            db.query(PaperIntelligenceRow)
            .filter(
                PaperIntelligenceRow.owner_id == owner_uuid,
                PaperIntelligenceRow.paper_id == paper_uuid,
            )
            .delete(synchronize_session=False)
        )

    return removed > 0
