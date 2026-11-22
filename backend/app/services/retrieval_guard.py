"""
Vector hits count as evidence only for papers that are live.

Qdrant can hold vectors whose paper is not usable: an index run whose claim
was taken over and whose cleanup failed, a deletion that stopped partway, a
paper that is still uploading or indexing, or one that failed. Retrieval
used to filter on owner_id alone, so any of those vectors could appear in
search results and in the passages an answer is built from. Postgres is the
authority on which papers exist and are ready: a hit is kept only if its
payload names one of the verified owner's papers whose status is 'indexed'.

The owner filter on the Qdrant query itself is unchanged and still comes
only from the verified identity; this is a second, independent check, made
against the database under that same identity (session_scope binds it, so
RLS applies too).

One database round trip per request in practice, never one per hit: the
distinct paper ids among the hits — at most the query's limit — are looked
up together, LOOKUP_BATCH ids per query.

A database failure is raised, never answered with "no results": callers
turn it into their usual sanitized error.
"""
import uuid
from typing import Iterable, List, Optional, Set

from app.db.models import Paper
from app.db.session import session_scope

#: Upper bound on ids per IN (...) query. Every caller asks Qdrant for at
#: most PAPER_FETCH_LIMIT hits of one paper, or 100 across the library, so
#: one query covers them; the bound keeps a larger caller from building an
#: unbounded statement.
LOOKUP_BATCH = 500

#: The one status whose papers are evidence. Also what the by-id resolvers
#: for intelligence, relationships and evidence require, so "live" has one
#: definition across the backend.
LIVE_STATUS = "indexed"


def _paper_uuid(hit) -> Optional[uuid.UUID]:
    """The hit's paper id, or None if it has none or it is malformed — a
    hit that cannot be tied to a row is never evidence."""
    payload = getattr(hit, "payload", None) or {}
    try:
        return uuid.UUID(str(payload.get("paper_id")))
    except (ValueError, TypeError, AttributeError):
        return None


def live_paper_ids(owner_id: str, paper_ids: Iterable[uuid.UUID]) -> Set[uuid.UUID]:
    """Which of these ids name this owner's papers with status 'indexed'."""
    wanted = list(dict.fromkeys(paper_ids))  # distinct, in first-seen order
    if not wanted:
        return set()

    owner_uuid = uuid.UUID(owner_id)
    live: Set[uuid.UUID] = set()
    with session_scope(owner_id) as db:
        for start in range(0, len(wanted), LOOKUP_BATCH):
            batch = wanted[start:start + LOOKUP_BATCH]
            rows = (
                db.query(Paper.id)
                .filter(
                    Paper.owner_id == owner_uuid,
                    Paper.status == LIVE_STATUS,
                    Paper.id.in_(batch),
                )
                .all()
            )
            live.update(row.id for row in rows)
    return live


def keep_live_hits(owner_id: str, hits: List) -> List:
    """The hits whose paper is live, in their original order."""
    ids = [_paper_uuid(hit) for hit in hits]
    live = live_paper_ids(owner_id, (paper_id for paper_id in ids if paper_id is not None))
    return [hit for hit, paper_id in zip(hits, ids) if paper_id in live]
