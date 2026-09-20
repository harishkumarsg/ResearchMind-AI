"""
GET /paper-file — the owner's own PDF, streamed through the API.

The `papers` bucket is private and stays private. There is no signed
URL and no public object: the bytes are read server-side and returned
over an already-authenticated request, so the Storage path never reaches
the browser and the service-role key never leaves the process.

SECURITY — the ownership boundary lives HERE, not in storage.py.

storage.fetch_pdf() uses the service-role client, which bypasses RLS. It
was written for the indexing job, which has no live user JWT to scope
to, and its only other caller is this module. Handing it a
client-supplied paper_id without checking would therefore read ANY
owner's object. So this endpoint resolves the paper against Postgres
first — `papers.id` AND `papers.owner_id`, the latter from the verified
JWT — and only calls fetch_pdf() once that row has been found.

A paper that does not exist and a paper belonging to someone else are
deliberately indistinguishable: both return the same 404, so the
response cannot be used to probe whether another user holds a given id.
"""
import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
from app.db.models import Paper
from app.db.session import get_db_session
from app.services.storage import fetch_pdf

router = APIRouter()

#: One authored message for every miss. See the module docstring: the
#: wording must not distinguish "no such paper" from "not yours".
NOT_FOUND_MESSAGE = "Paper not found."

#: Authored, never interpolated from the Storage client's exception.
UNAVAILABLE_MESSAGE = "The paper file is currently unavailable. Please try again."


def resolve_owned_paper(db: Session, owner_id: str, paper_id: str):
    """The owner's paper row for this id, or None.

    Returns None for a malformed id, an unknown id, and another owner's
    id alike — the caller renders one response for all three.
    """
    try:
        wanted = uuid.UUID(paper_id)
        owner_uuid = uuid.UUID(owner_id)
    except (ValueError, AttributeError, TypeError):
        # A malformed id is a miss, not a 500, and the echo of the bad
        # value never reaches the client.
        return None

    return (
        db.query(Paper)
        .filter(Paper.id == wanted, Paper.owner_id == owner_uuid)
        .first()
    )


@router.get("/paper-file")
def paper_file(
    paper_id: str,
    response: Response,
    owner_id: str = Depends(get_current_owner_id),
    db: Session = Depends(get_db_session),
):
    paper = resolve_owned_paper(db, owner_id, paper_id)

    if paper is None:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"status": "error", "message": NOT_FOUND_MESSAGE}

    try:
        # Safe now, and only now: the row above proves this owner holds
        # this paper. owner_id is the verified JWT value, never the
        # client's, so the Storage path cannot be pointed elsewhere.
        pdf_bytes = fetch_pdf(owner_id=owner_id, paper_id=str(paper.id))
    except Exception:
        # The Storage client's message can name buckets, paths and URLs.
        response.status_code = status.HTTP_502_BAD_GATEWAY
        return {"status": "error", "message": UNAVAILABLE_MESSAGE}

    if not pdf_bytes:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"status": "error", "message": NOT_FOUND_MESSAGE}

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            # inline so the browser's own viewer renders it rather than
            # downloading; the filename is the owner's own title.
            "Content-Disposition": f'inline; filename="{paper.title}"',
            # This is one user's private document.
            "Cache-Control": "private, no-store",
        },
    )
