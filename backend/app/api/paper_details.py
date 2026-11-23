"""
GET /paper-details — one indexed paper's metadata and opening text.

No provider call. The paper is resolved by title in Postgres, and its chunks
are read from Qdrant by payload filter (scroll), not by vector search. The
route used to embed the paper's file name with Voyage on every page load to
search for its own chunks; on Voyage's free tier (3 requests a minute) that
alone was enough to answer most paper pages with "busy".
"""
import logging
import uuid

from fastapi import APIRouter, Depends, Response, status
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy import func

from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import guard_cheap_read
from app.db.models import Paper
from app.db.session import session_scope
from app.rag.vector_store import COLLECTION_NAME, client
from app.services.retrieval_guard import LIVE_STATUS

router = APIRouter()

#: Never pass the paper name or an exception's message to this logger: the
#: name is the user's file name, and a driver's or provider's text can carry
#: hosts and request details. Failure codes and type names only.
logger = logging.getLogger(__name__)

#: Points per scroll page. A paper has at most MAX_CHUNKS_PER_PAPER chunks
#: (150 by default), so one page normally covers it.
SCROLL_PAGE_SIZE = 256

#: Hard stop on scroll pages, so a filter that somehow matched far more than
#: one paper cannot keep this request reading.
MAX_SCROLL_PAGES = 20

PREVIEW_CHARS = 1000

AMBIGUOUS_TITLE_CODE = "ambiguous_title"
NOT_FOUND_CODE = "paper_not_found"

#: The same rule as the legacy DELETE /paper/{paper_name}: a title that
#: names more than one of the owner's papers is never resolved by picking
#: one of them.
AMBIGUOUS_TITLE_MESSAGE = (
    "More than one paper in your library has this name, so its details "
    "cannot be shown by name."
)


def resolve_indexed_papers_by_title(owner_id: str, paper_name: str) -> list:
    """Up to two of this owner's indexed papers with this title, as
    (id, title) rows: none, the one, or enough to know it is ambiguous.

    Exact title first, then a case-insensitive exact match, as
    /summarize-paper resolves titles. Never a substring match: "report.pdf"
    must not select "final report.pdf". Only 'indexed' papers count, so the
    leftover row of a paper being removed, or one still uploading, never
    competes with the live paper that shares its title.
    """
    owner_uuid = uuid.UUID(owner_id)

    with session_scope(owner_id) as db:
        base = db.query(Paper.id, Paper.title).filter(
            Paper.owner_id == owner_uuid,
            Paper.status == LIVE_STATUS,
        )
        rows = base.filter(Paper.title == paper_name).limit(2).all()
        if not rows:
            rows = (
                base.filter(func.lower(Paper.title) == paper_name.lower())
                .limit(2)
                .all()
            )
        return [(str(row.id), row.title) for row in rows]


def _reading_order(payload):
    """Page, then chunk index; a missing or malformed value sorts last."""
    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 10 ** 9

    return as_int(payload.get("page")), as_int(payload.get("chunk_id"))


def scroll_paper_chunks(owner_id: str, paper_id: str) -> list:
    """Every chunk payload of ONE paper, in reading order. No vector.

    owner_id comes from the verified JWT; paper_id from an owner-scoped,
    indexed row. Both are in the filter, so no other owner's and no other
    paper's chunk can be returned.
    """
    scroll_filter = Filter(
        must=[
            FieldCondition(key="paper_id", match=MatchValue(value=paper_id)),
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
        ]
    )

    payloads = []
    offset = None
    for _ in range(MAX_SCROLL_PAGES):
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend(p.payload for p in points if p.payload)
        if offset is None:
            break

    payloads.sort(key=_reading_order)
    return payloads


def _first(payloads, key):
    """The first non-empty value of a paper-level field. Authors, abstract,
    keywords and source are written identically on every chunk."""
    for payload in payloads:
        value = payload.get(key)
        if value:
            return value
    return ""


def _not_found(paper_name: str) -> dict:
    return {
        "status": "error",
        "code": NOT_FOUND_CODE,
        "message": f"Paper not found: {paper_name}",
    }


@router.get("/paper-details")
def paper_details(
    paper_name: str,
    response: Response,
    owner_id: str = Depends(guard_cheap_read),
):

    try:

        matches = resolve_indexed_papers_by_title(owner_id, paper_name)

        if not matches:
            return _not_found(paper_name)

        if len(matches) > 1:
            response.status_code = status.HTTP_409_CONFLICT
            return {
                "status": "error",
                "code": AMBIGUOUS_TITLE_CODE,
                "message": AMBIGUOUS_TITLE_MESSAGE,
            }

        paper_id, title = matches[0]
        payloads = scroll_paper_chunks(owner_id, paper_id)

        if not payloads:
            return _not_found(paper_name)

        return {
            "status": "success",
            "paper": title,
            "paper_id": paper_id,
            "source": _first(payloads, "source"),
            "authors": _first(payloads, "authors"),
            "keywords": _first(payloads, "keywords"),
            "abstract": _first(payloads, "abstract"),
            "total_chunks": len(payloads),
            "preview": (payloads[0].get("text") or "")[:PREVIEW_CHARS],
        }

    except Exception as e:

        # A provider failure gets a neutral, application-authored message
        # instead of the raw exception text.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            logger.warning("paper-details failed: %s", provider_failure.code)
            return provider_failure.to_payload()

        logger.warning("paper-details failed: internal_error (%s)", type(e).__name__)
        return internal_error_payload()
