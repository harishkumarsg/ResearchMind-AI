"""
GET /paper-evidence — the exact source text behind a stored analysis.

A stored Paper Intelligence object cites its evidence by reference:
`(page, chunk_id)`, plus an optional verified quote. This endpoint turns
those references back into the chunk text they point at, so a reader can
see the passage rather than only its page number.

READ ONLY, AND NOTHING ELSE
---------------------------
No Groq, no Voyage, no Storage, no generation, no charge against the AI
allowance, and no write of any kind. The one external read is a Qdrant
scroll under an exact owner + paper filter, with no query vector — the
same lookup the relationship feature already performs server-side, made
through the same service, unchanged.

It is deliberately NOT part of GET /paper-intelligence. That endpoint's
contract is "database only", so that opening a paper never touches a
provider; a caller asks for passages separately, and only when a reader
actually opens an evidence reference.

WHAT A CALLER CAN AND CANNOT ASK FOR
------------------------------------
The request carries a paper id and nothing else. Which chunks come back
is decided HERE, from the analysis already stored for that paper: only
the references that analysis cites, never an arbitrary page or chunk of
the caller's choosing. The endpoint cannot be used to walk a paper.

owner_id comes only from the verified JWT, via guard_cheap_read. There is
no owner_id parameter on this route. The paper id IS client-supplied, so
it is resolved against `papers` first and the RESOLVED id is what reaches
the Qdrant filter.

NOTHING IS EVER SUBSTITUTED
---------------------------
A reference that no longer resolves — the chunk is gone, two points claim
its identity, or its payload has no text — comes back with its status and
NO text. Not a neighbouring chunk, not another chunk on the same page.
Text is returned only for a reference that resolved to exactly one
well-formed chunk, and is then byte-for-byte that chunk's stored text.

The response carries no `summary` and no `quote`. The service that reads
the chunks is never given either, so model prose has no route into what
this endpoint presents as source text.
"""
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Response, status

from app.api.paper_file import resolve_owned_paper
from app.core.limits import UsageLimitError
from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import guard_cheap_read
from app.db.session import session_scope
from app.services.intelligence_schema import (
    SECTION_NAMES,
    IntelligenceValidationError,
)
from app.services.paper_intelligence_store import get_intelligence
from app.services.paper_relationship_evidence import (
    ResolvedEvidence,
    references_from_intelligence,
    rehydrate_paper_evidence,
)

router = APIRouter()

#: One authored message for every miss — unknown id, malformed id, and
#: another owner's id alike. The same wording /paper-intelligence and
#: /paper-file use, so the response cannot be used to probe whether some
#: other user holds a given id.
NOT_FOUND_MESSAGE = "Paper not found."

#: The paper is this owner's, but nothing has been analysed yet, so there
#: are no references to resolve. An empty state, not an error.
NOT_GENERATED_MESSAGE = "No analysis has been generated for this paper yet."

#: The stored row exists but no longer parses. Authored here; the stored
#: text is never rendered.
UNREADABLE_MESSAGE = (
    "The stored analysis for this paper could not be read. "
    "Generating it again will replace it."
)


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime.

    SQLite, the offline test database, reads timestamptz values back
    naive; every value these columns hold is written in UTC. Same
    assumption and same reasoning as _as_utc() in
    services/paper_intelligence_store.py.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reindexed_since(
    paper_updated_at: Optional[datetime], generated_at: Optional[datetime]
) -> bool:
    """Did the paper's record change after this analysis was started?

    `papers.updated_at` moves on every UPDATE of the row, and an index run
    cannot happen without one: the indexer sets status to "indexing" and
    then to "indexed", "uploaded" or "failed". So an index run after the
    analysis always makes this True. Both stamps are taken from this
    application's clock, in UTC.

    CONSERVATIVE, NOT A PROOF. True means the chunks MAY differ from the
    ones the analysis read — it says nothing about any single chunk, and
    a paper whose delete was interrupted reads True as well. False means
    only that no later change to the row was recorded.

    Strictly greater: a stamp equal to `generated_at` is not evidence of
    a later change. A missing stamp is not evidence of one either.
    """
    if paper_updated_at is None or generated_at is None:
        return False
    return _as_utc(paper_updated_at) > _as_utc(generated_at)


def resolve_paper(
    owner_id: str, paper_id: str
) -> Optional[Tuple[str, Optional[datetime]]]:
    """This owner's paper with this exact id, as (paper_id, updated_at).

    Delegates to /paper-file's resolver, the authoritative by-id lookup:
    exact `papers.id` AND `papers.owner_id`, None for a malformed id, an
    unknown id and another owner's id alike.
    """
    with session_scope(owner_id) as db:
        paper = resolve_owned_paper(db, owner_id, paper_id)
        if paper is None:
            return None
        # Read out inside the session; the row expires on commit.
        return str(paper.id), paper.updated_at


def _item(resolved: ResolvedEvidence) -> dict:
    """One reference as the client sees it.

    `text` only for a reference that resolved. The service already leaves
    it unset otherwise; checking again here means an unresolved reference
    cannot carry text even if that ever changed.
    """
    return {
        "page": resolved.page,
        "chunk_id": resolved.chunk_id,
        "status": resolved.status,
        "text": resolved.text if resolved.found else None,
    }


@router.get("/paper-evidence")
def read_paper_evidence(
    paper_id: str,
    response: Response,
    owner_id: str = Depends(guard_cheap_read),
):
    """The source text for the references in this paper's stored analysis.

    CHEAP_READ: a burst-guarded read with no daily allowance, exactly
    like GET /paper-intelligence. It spends no AI generation.

    Outcomes:

      404  — the paper does not exist, is malformed, or belongs to
             someone else. One response for all three.
      200 status=not_generated — the paper IS this owner's, but there is
             no stored analysis. Qdrant is not read.
      200 status=success — one item per distinct cited (page, chunk_id),
             page then chunk ascending. `text` is present only where
             `status` is "found".
    """
    try:
        resolved = resolve_paper(owner_id, paper_id)

        if resolved is None:
            response.status_code = status.HTTP_404_NOT_FOUND
            return {"status": "error", "message": NOT_FOUND_MESSAGE}

        resolved_paper_id, paper_updated_at = resolved

        # Owner-scoped by construction: get_intelligence filters on both
        # owner_id and paper_id, so another owner's row cannot match.
        stored = get_intelligence(owner_id, resolved_paper_id)

        if stored is None:
            return {
                "status": "not_generated",
                "paper_id": resolved_paper_id,
                "message": NOT_GENERATED_MESSAGE,
            }

        # References only. references_from_intelligence reads
        # evidence[].page and evidence[].chunk_id and never `summary` or
        # `quote`, and it is the stored analysis — not the request — that
        # decides which chunks are looked up.
        references = references_from_intelligence(
            stored.intelligence.model_dump(mode="json"), SECTION_NAMES
        )

        # The service's own default budget. Every cited chunk was part of
        # a generation context capped at the same figure, so in the
        # ordinary case all of them fit; one that does not is reported as
        # over_budget with no text rather than cut.
        evidence = rehydrate_paper_evidence(owner_id, resolved_paper_id, references)

        return {
            "status": "success",
            "paper_id": resolved_paper_id,
            "generated_at": _as_utc(stored.generated_at).isoformat(),
            "reindexed_since_analysis": reindexed_since(
                paper_updated_at, stored.generated_at
            ),
            "items": [_item(item) for item in evidence.items],
        }

    except IntelligenceValidationError as e:
        # The stored JSON no longer matches the schema. Reported as a
        # readable state rather than a 500; the row is left alone.
        print(
            f"[paper-evidence] unreadable stored row code={e.code} "
            f"paper={paper_id}"
        )
        return {"status": "error", "code": e.code, "message": UNREADABLE_MESSAGE}

    except UsageLimitError:
        # Must reach the handler in app/main.py as a normalized 429/503.
        raise

    except Exception as e:
        print(f"Paper Evidence Read Error: {str(e)}")

        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return internal_error_payload()
