"""
Durable persistence for cross-paper relationships: exactly one current row
per (owner_id, unordered paper pair) in `paper_relationships`
(migration 0006).

This module stores and returns ALREADY-VALIDATED objects. It does not
generate them, does not call a model, and does not reach the network —
app/services/relationship_schema.py remains the single source of truth for
what a PaperRelationship is, and nothing here re-implements or relaxes it.
The only things this module decides are whether a given owner may write
about a given pair of papers, and which of two concurrent writes wins.

Every function takes owner_id as the verified Supabase JWT `sub` supplied
by app.core.auth — never a client-supplied value, never a value read out
of the relationship JSON — and scopes both its reads and its writes by it.
The two paper ids ARE client-supplied on the request path this will
eventually serve, so both are parsed defensively and BOTH are checked for
ownership before any write.

THE PAIR IS UNORDERED, THE CONTENT IS NOT
-----------------------------------------
A relationship between two papers is one fact, so it is one row: the
database enforces `paper_a_id < paper_b_id`, which makes (A,B) and (B,A)
the same row and a self-pair impossible.

But the stored CONTENT is orientation-dependent — `cites_a` belongs to
whichever paper the generator called A, and the prose may say so in words.
So this store will NOT silently reorient content to fit the canonical
order: `save_relationship` requires the pair to arrive already canonical
and raises otherwise. Swapping `cites_a` and `cites_b` without rewriting
the statement would misattribute evidence while looking perfectly
well-formed, which is the single worst failure this feature could have.
Callers canonicalize with `canonical_pair()` BEFORE generating, so the
content is produced in the orientation it will be stored in.

Reads are different: `get_relationship` canonicalizes freely, because
looking a pair up in either order returns the same row and changes
nothing.

DEFENCE IN DEPTH, NOT INSTEAD OF
--------------------------------
Postgres enforces owner isolation through RLS (enabled and FORCED in
0006, with auth.uid() bound per transaction by app/db/session.py). The
explicit `owner_id ==` term in every query below is kept anyway, exactly
as it is on every other table: the offline test database has no RLS at
all, so the application filter is what makes the tests meaningful, and in
production the two must agree.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db.models import Paper, PaperRelationshipRow
from app.db.session import session_scope
from app.services.intelligence_schema import SECTION_NAMES

# PaperNotOwned is REUSED rather than redefined. "This paper is not
# yours, or does not exist" is the same fact whichever store discovers
# it, and one exception type means a caller handles it once. Importing it
# does not modify that module.
from app.services.paper_intelligence_store import PaperNotOwned
from app.services.relationship_schema import (
    PaperRelationship,
    RelationshipValidationError,
)

#: The shape of the object in the `relationship` column, recorded on every
#: row. Deliberately a bare string and deliberately not a framework: its
#: job is to let a future change identify exactly which rows predate it.
#: Bump it when the persisted structure changes in a way a reader must
#: notice — adding a relation value, renaming a field, changing evidence
#: identity.
PAPER_RELATIONSHIP_SCHEMA_VERSION = "1"


def _utcnow() -> datetime:
    """Matches app.db.models._utcnow. A named module-level function so
    tests can pin the clock."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime.

    SQLite, the offline test database, reads timestamptz values back
    naive; every value this module writes is UTC. Without this, comparing
    a stored stamp against an incoming one raises TypeError offline and
    silently compares wall clocks in production.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _owner_uuid(owner_id) -> uuid.UUID:
    """The verified JWT `sub` as a UUID.

    Raises on a malformed value rather than returning None: owner_id never
    comes from a client, so a bad one is a programming error at the call
    site and must not be swallowed into a silent empty result.
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


def canonical_pair(paper_a_id, paper_b_id) -> Tuple[uuid.UUID, uuid.UUID]:
    """The two ids in canonical (ascending) order.

    The one place pair ordering is decided. Python's uuid.UUID comparison
    is byte-wise over the 16 bytes, which is the same order Postgres uses
    for its native uuid type, so the application and the CHECK constraint
    in 0006 always agree on which id is smaller.

    Raises PaperNotOwned for a malformed id — indistinguishable from
    unknown, as everywhere else — and for a self-pair, which is not a
    relationship at all.
    """
    left = _paper_uuid(paper_a_id)
    right = _paper_uuid(paper_b_id)

    if left is None or right is None:
        raise PaperNotOwned("No such paper for this owner.")

    if left == right:
        # A paper is not in a relationship with itself, and the database
        # would refuse the row anyway.
        raise PaperNotOwned("A paper cannot be compared with itself.")

    return (left, right) if left < right else (right, left)


@dataclass(frozen=True)
class StoredRelationship:
    """One persisted row, as plain values.

    Not an ORM object: the session that produced it is already closed by
    the time this is returned, and the caller may hold it across a long
    model call.

    `paper_a_id` / `paper_b_id` are the CANONICAL pair, and the
    relationship's `cites_a` / `cites_b` are oriented to match them. A
    caller displaying the pair in the reader's own order must map labels
    itself; this store never reorients content.

    `superseded` is False for every row this store wrote and True only
    when save_relationship() declined to overwrite a newer row.
    """

    paper_a_id: uuid.UUID
    paper_b_id: uuid.UUID
    relationship: PaperRelationship
    #: The generated_at of the two source paper_intelligence rows, in
    #: CANONICAL orientation: paper_a_generated_at belongs to paper_a_id.
    #: Compare against the papers' current stamps to detect staleness —
    #: see relationship_is_stale().
    paper_a_generated_at: datetime
    paper_b_generated_at: datetime
    #: When the RELATIONSHIP generation began. Independent of both source
    #: stamps: regenerating the relationship moves this and leaves those,
    #: and re-analysing a paper moves one of those and leaves this.
    generated_at: datetime
    model: str
    schema_version: str
    updated_at: datetime
    superseded: bool = False


def _owns_both_papers(
    db, owner_uuid: uuid.UUID, left: uuid.UUID, right: uuid.UUID
) -> bool:
    """Does this owner hold BOTH papers?

    One query, and it must return two rows. Checking only one paper would
    let a relationship be written between the caller's own paper and
    somebody else's — the more dangerous half of the pair is the one it is
    tempting to forget. The owner_id term is what makes a foreign paper
    invisible rather than merely unexpected.
    """
    owned = (
        db.query(Paper.id)
        .filter(Paper.owner_id == owner_uuid, Paper.id.in_([left, right]))
        .all()
    )
    return {row[0] for row in owned} == {left, right}


def _canonical_sections(relationship: PaperRelationship) -> None:
    """Every section key must be a canonical section name.

    Checked here as well as in validate_relationship because the Pydantic
    model types `sections` as a plain mapping: `PaperRelationship` can be
    CONSTRUCTED directly with an arbitrary key, bypassing the validator.
    A persisted row with a `future_work` section would be a second,
    competing definition of what a section is.
    """
    unknown = sorted(set(relationship.sections) - set(SECTION_NAMES))
    if unknown:
        raise RelationshipValidationError(
            "unknown_section",
            "The relationship contains a section that is not a canonical section.",
        )


def _parse_stored(
    row: PaperRelationshipRow, *, superseded: bool = False
) -> StoredRelationship:
    """Turn a row into plain values, re-checking the stored JSON.

    Structure only: the two evidence allowlists that validate_relationship()
    needs belong to the generation that produced this object and are long
    gone by read time. What this catches is a row whose JSON no longer
    matches the model at all — hand-edited, restored from an older
    schema_version, or corrupted — and it fails loudly rather than handing
    a caller a half-shaped object.

    It repairs nothing. relation, statement, cites_a and cites_b come back
    exactly as they were stored.
    """
    try:
        relationship = PaperRelationship.model_validate(row.relationship)
    except ValidationError:
        raise RelationshipValidationError(
            "invalid_structure",
            "The stored relationship for this pair is not readable.",
        ) from None

    _canonical_sections(relationship)

    return StoredRelationship(
        paper_a_id=row.paper_a_id,
        paper_b_id=row.paper_b_id,
        relationship=relationship,
        paper_a_generated_at=row.paper_a_generated_at,
        paper_b_generated_at=row.paper_b_generated_at,
        generated_at=row.generated_at,
        model=row.model,
        schema_version=row.schema_version,
        updated_at=row.updated_at,
        superseded=superseded,
    )


def get_relationship(
    owner_id: str, paper_a_id, paper_b_id
) -> Optional[StoredRelationship]:
    """The current relationship for this owner's pair, or None.

    The pair is canonicalized first, so asking in either order finds the
    same row. None covers every way there is nothing to return: never
    generated, a paper that does not exist, a paper belonging to someone
    else, and an id that is not a UUID at all — a row keyed on another
    owner simply does not match.
    """
    try:
        left, right = canonical_pair(paper_a_id, paper_b_id)
    except PaperNotOwned:
        # Malformed or identical ids: nothing to find, and not an error
        # for a read.
        return None

    owner_uuid = _owner_uuid(owner_id)

    with session_scope(owner_id) as db:
        row = (
            db.query(PaperRelationshipRow)
            .filter(
                PaperRelationshipRow.owner_id == owner_uuid,
                PaperRelationshipRow.paper_a_id == left,
                PaperRelationshipRow.paper_b_id == right,
            )
            .first()
        )
        if row is None:
            return None
        return _parse_stored(row)


def save_relationship(
    owner_id: str,
    paper_a_id,
    paper_b_id,
    relationship: PaperRelationship,
    *,
    paper_a_generated_at: datetime,
    paper_b_generated_at: datetime,
    model: str,
    generated_at: Optional[datetime] = None,
    schema_version: str = PAPER_RELATIONSHIP_SCHEMA_VERSION,
) -> StoredRelationship:
    """Write the current relationship for this owner's pair.

    The pair MUST already be canonical (`paper_a_id < paper_b_id`); a
    reversed pair raises ValueError. That is deliberate and is the
    orientation guarantee: `cites_a` belongs to `paper_a_id`, and the
    store will not swap the content to fit a different order because
    swapping citations without rewriting the statement would misattribute
    evidence. Call `canonical_pair()` before generating.

    `relationship` must be a PaperRelationship instance — a dict or a
    string is rejected with TypeError. That is the enforcement point for
    "only validated objects are persisted": the sole way to obtain one
    from model output is validate_relationship(), so there is no path from
    raw output to this table that skips the validator.

    Upsert, not append: UNIQUE(owner_id, paper_a_id, paper_b_id) means a
    regeneration replaces the row in place, so a pair can never accumulate
    two "current" analyses.

    NOT last-writer-wins: if the stored row carries a `generated_at` later
    than this one's, the write is dropped and the existing newer row is
    returned with `superseded=True`. `generated_at` should therefore be
    stamped when the generation run STARTS, so the ordering reflects
    request order.

    `paper_a_generated_at` / `paper_b_generated_at` are REQUIRED keyword
    arguments and must be the `generated_at` of the two source
    paper_intelligence rows, read by the caller from those rows. They are
    what makes the stored relationship's staleness detectable later. They
    are NOT interchangeable with `generated_at`, which times the
    relationship run itself, and they are never read out of the
    relationship JSON — the schema has no field for them.

    Both are oriented to the CANONICAL pair, which the canonical-order
    requirement above makes automatic: paper_a_generated_at belongs to
    paper_a_id.

    Raises PaperNotOwned unless BOTH papers are this owner's.
    """
    if not isinstance(relationship, PaperRelationship):
        raise TypeError(
            "save_relationship requires a validated PaperRelationship; "
            "run validate_relationship() on the model output first"
        )

    _canonical_sections(relationship)

    left, right = canonical_pair(paper_a_id, paper_b_id)

    # Whether the caller's own A is the canonical A. Computed here but
    # enforced only AFTER the ownership check below, deliberately: a paper
    # that is not the caller's must always raise PaperNotOwned, never a
    # different error merely because of how the two ids happened to sort.
    # Otherwise the same foreign paper would surface as PaperNotOwned or
    # ValueError depending on uuid ordering, which is surprising to a
    # caller and awkward for an endpoint to map.
    reversed_pair = _paper_uuid(paper_a_id) != left

    for label, value in (
        ("paper_a_generated_at", paper_a_generated_at),
        ("paper_b_generated_at", paper_b_generated_at),
    ):
        # A missing or wrongly-typed source stamp would store provenance
        # that cannot be compared, which is worse than refusing: the row
        # would look checkable and never be.
        if not isinstance(value, datetime):
            raise ValueError(f"{label} must be the source intelligence datetime")

    owner_uuid = _owner_uuid(owner_id)
    stamp = generated_at or _utcnow()
    # mode="json" so the stored value is plain JSON types. extra="forbid"
    # on every model in the schema means this dump can contain nothing but
    # the validated fields — in particular no owner id, no paper id and no
    # Qdrant point id, which the schema has no fields for.
    payload = relationship.model_dump(mode="json")

    def attempt() -> StoredRelationship:
        with session_scope(owner_id) as db:
            if not _owns_both_papers(db, owner_uuid, left, right):
                raise PaperNotOwned("No such paper for this owner.")

            if reversed_pair:
                # Both papers are this owner's, but the content was
                # generated in the opposite orientation. We cannot fix
                # that here without rewriting the statement prose, so we
                # refuse rather than misattribute evidence.
                raise ValueError(
                    "save_relationship requires a canonical pair "
                    "(paper_a_id < paper_b_id); canonicalize with canonical_pair() "
                    "before generating so the content is produced in the "
                    "orientation it will be stored in"
                )

            row = (
                db.query(PaperRelationshipRow)
                .filter(
                    PaperRelationshipRow.owner_id == owner_uuid,
                    PaperRelationshipRow.paper_a_id == left,
                    PaperRelationshipRow.paper_b_id == right,
                )
                # Locked for the rest of the transaction so the staleness
                # comparison below is a real compare-and-set. Without the
                # lock two concurrent regenerations could both read the
                # same old stamp, both judge themselves newer, and both
                # write. A no-op on SQLite, as it is for the identical
                # lock in paper_intelligence_store and chat_store.
                .with_for_update()
                .first()
            )

            if row is not None and _as_utc(row.generated_at) > _as_utc(stamp):
                # STALENESS GUARD. The stored row was generated more
                # recently than the object being offered, so this write is
                # stale and is dropped rather than applied. The case it
                # exists for: a reader triggers a slow regeneration,
                # triggers a second, the second finishes first, and the
                # first then lands and reverts the pair to the older
                # analysis.
                #
                # Strictly greater, not >=: two runs that start inside one
                # clock tick are genuinely indistinguishable, and refusing
                # both would make an ordinary back-to-back regeneration
                # fail.
                return _parse_stored(row, superseded=True)

            if row is None:
                row = PaperRelationshipRow(
                    owner_id=owner_uuid,
                    paper_a_id=left,
                    paper_b_id=right,
                    relationship=payload,
                    paper_a_generated_at=paper_a_generated_at,
                    paper_b_generated_at=paper_b_generated_at,
                    generated_at=stamp,
                    model=model,
                    schema_version=schema_version,
                )
                db.add(row)
            else:
                row.relationship = payload
                row.paper_a_generated_at = paper_a_generated_at
                row.paper_b_generated_at = paper_b_generated_at
                row.generated_at = stamp
                row.model = model
                row.schema_version = schema_version
                # Set explicitly rather than relying on the onupdate hook:
                # regenerating a pair whose analysis did not change leaves
                # no column dirty, SQLAlchemy emits no UPDATE, and
                # onupdate never fires.
                row.updated_at = _utcnow()

            # flush so defaults are populated before the values are read
            # out, and so a UNIQUE or CHECK violation surfaces here rather
            # than at commit.
            db.flush()

            # Built inside the transaction, because the row expires the
            # moment session_scope commits.
            return _parse_stored(row)

    try:
        return attempt()
    except IntegrityError:
        # UNIQUE(owner_id, paper_a_id, paper_b_id). Two concurrent
        # generations for the same pair can both find no row and both
        # insert; the loser lands here. The winner's row is committed by
        # now, so the retry finds it and applies the staleness guard to it
        # instead. This is what that constraint is for.
        return attempt()


def relationship_is_stale(
    stored: StoredRelationship,
    current_paper_a_generated_at: Optional[datetime],
    current_paper_b_generated_at: Optional[datetime],
) -> bool:
    """Has either source paper been re-analysed since this relationship?

    Pure: no I/O, no provider, no clock. The caller supplies the two
    papers' CURRENT paper_intelligence `generated_at` values — in canonical
    orientation, matching `stored.paper_a_id` / `stored.paper_b_id` — and
    this reports whether the stored analysis still describes them.

    Stale means "differs", not "is older". A source stamp that moved
    BACKWARDS is just as disqualifying: it means the row in front of us is
    not the row this relationship was derived from, however it got that
    way. Treating only "newer" as stale would silently keep a relationship
    describing intelligence that no longer exists.

    A missing current stamp (None) is stale too: an analysis that has been
    removed cannot still be the one this relationship compared.

    Comparison is UTC-aware on both sides, because SQLite reads timestamps
    back naive while Postgres returns them aware, and comparing the two
    forms directly raises TypeError.

    Detection only. This gate deliberately adds no automatic regeneration,
    no API behaviour and no UI behaviour — it just makes the fact knowable.
    """
    for stored_stamp, current_stamp in (
        (stored.paper_a_generated_at, current_paper_a_generated_at),
        (stored.paper_b_generated_at, current_paper_b_generated_at),
    ):
        if current_stamp is None:
            return True
        if _as_utc(stored_stamp) != _as_utc(current_stamp):
            return True

    return False
