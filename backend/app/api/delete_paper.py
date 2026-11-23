import uuid

from fastapi import APIRouter, Depends, HTTPException
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList
from sqlalchemy.orm import Session

from app.api.paper_file import resolve_owned_paper
from app.core.auth import AuthenticatedIdentity, get_current_identity
from app.db.models import Paper
from app.db.session import get_db_session
from app.rag.vector_store import COLLECTION_NAME, client
from app.services import paper_lifecycle
from app.services.storage import delete_pdf

router = APIRouter()

#: The title is the uploaded filename, and only (owner_id, content_hash) is
#: unique — so one owner can hold two different papers with the same title.
#: A title that names more than one of them must never be resolved by
#: picking a row: `.first()` without an ORDER BY returned an arbitrary one
#: and deleted the wrong paper's row, vectors and PDF.
AMBIGUOUS_TITLE_DETAIL = (
    "More than one paper in your library has this name, so it cannot be "
    "deleted by name. Delete it from the library list instead."
)

#: Every response below is authored here. A Qdrant or Storage error can
#: name hosts, buckets, paths and policies, so none of its text is ever
#: passed through. These are raised as HTTPException, which is rendered
#: inside the CORS middleware: an unhandled exception is turned into a 500
#: outside it, without CORS headers, and the browser cannot read it at all.
DELETE_INCOMPLETE_DETAIL = (
    "This paper could not be fully removed. It is still in your library, "
    "so you can try deleting it again."
)
BUSY_DETAILS = {
    "indexing": "This paper is still being indexed — try again shortly",
    "uploading": "This paper is still being uploaded — try again shortly",
    "deleting": "This paper is already being removed — try again shortly",
}
BUSY_DETAIL = "This paper is busy — try again shortly"


@router.delete("/papers/{paper_id}")
def delete_paper_by_id(
    paper_id: str,
    identity: AuthenticatedIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db_session),
):
    """Deletes exactly one paper, named by its immutable id.

    paper_id is taken as a plain string and resolved by
    resolve_owned_paper, so a malformed id, an unknown id and another
    owner's id all get the same 404 — and, unlike a typed UUID parameter,
    a malformed value is never echoed back in a validation error.
    """
    paper = resolve_owned_paper(db, identity.owner_id, paper_id)

    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")

    # Read before anything commits: the row expires on commit.
    paper_uuid, owner_uuid = paper.id, paper.owner_id
    vectors_deleted = _delete_owned_paper(paper_uuid, owner_uuid, identity, db)

    return {
        "status": "success",
        "message": "Paper deleted successfully.",
        "paper_id": str(paper_uuid),
        "vectors_deleted": vectors_deleted,
    }


@router.delete("/paper/{paper_name}")
def delete_paper(
    paper_name: str,
    identity: AuthenticatedIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db_session),
):
    """Legacy delete by title, kept for clients that predate
    DELETE /papers/{paper_id}. It still works when the title identifies
    one paper, and refuses with 409 when it identifies more than one."""
    # Lookup is owner-scoped, not just by name — a name match belonging to
    # another user is treated identically to "does not exist" below, so a
    # malicious guess never distinguishes the two cases.
    owner_uuid = uuid.UUID(identity.owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object
    matches = (
        db.query(Paper)
        .filter(Paper.title == paper_name, Paper.owner_id == owner_uuid)
        .limit(2)
        .all()
    )

    if not matches:
        raise HTTPException(status_code=404, detail="Paper not found")

    if len(matches) > 1:
        raise HTTPException(status_code=409, detail=AMBIGUOUS_TITLE_DETAIL)

    vectors_deleted = _delete_owned_paper(matches[0].id, owner_uuid, identity, db)

    return {
        "status": "success",
        "message": f"Paper '{paper_name}' deleted successfully.",
        "vectors_deleted": vectors_deleted,
    }


def _delete_owned_paper(
    paper_uuid: uuid.UUID,
    owner_uuid: uuid.UUID,
    identity: AuthenticatedIdentity,
    db: Session,
) -> int:
    """Deletes one paper the caller has already been verified to own:
    vectors, then the Storage object, then the row. Returns the number of
    vectors removed.

    `paper_uuid` must come from an owner-scoped lookup. Every resource below
    is addressed by this paper's own id and owner, never by anything the
    client sent, so neither route can reach another paper's data.

    The row is claimed first (app/services/paper_lifecycle.py): only one
    request at a time can delete a paper, and none while it is being
    uploaded or indexed. The row is removed last, and only while this
    request still holds the claim, so it never disappears while its vectors
    or PDF remain.
    """
    token = paper_lifecycle.claim_for_deletion(db, paper_uuid, owner_uuid)
    if token is None:
        _refuse(db, paper_uuid, owner_uuid)

    paper_id = str(paper_uuid)
    owner_id = str(owner_uuid)

    try:
        # 1. Qdrant vectors. owner_id is in the filter as defense-in-depth:
        #    paper_id is already owner-scoped by construction.
        vectors_deleted = _delete_vectors(paper_id, owner_id)

        # 2. Storage object. delete_pdf returns only once the object is
        #    confirmed absent — one already removed by an earlier, partial
        #    attempt counts — and raises otherwise. The path is rebuilt
        #    from this paper's owner and id, never read from the row.
        delete_pdf(owner_id=owner_id, paper_id=paper_id, user_jwt=identity.token)

        # 3. The row, LAST, and only under this request's claim.
        finished = paper_lifecycle.finish_deletion(db, paper_uuid, owner_uuid, token)
    except Exception as exc:
        # Recoverable by design: the row stays 'deleting', and releasing
        # the claim lets the same request be retried at once.
        print(f"Paper deletion did not complete: {type(exc).__name__}")
        _release(db, paper_uuid, owner_uuid, token)
        raise HTTPException(status_code=502, detail=DELETE_INCOMPLETE_DETAIL)

    if not finished:
        # The claim expired and another request took the deletion over:
        # this one's steps were repeats of the same idempotent deletes.
        _refuse(db, paper_uuid, owner_uuid)

    return vectors_deleted


def _delete_vectors(paper_id: str, owner_id: str) -> int:
    ids_to_delete: list = []
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="paper_id", match=MatchValue(value=paper_id)),
                    FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
                ]
            ),
            limit=500,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        ids_to_delete.extend([p.id for p in points])
        if next_offset is None:
            break

    if ids_to_delete:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=PointIdsList(points=ids_to_delete),
        )
    return len(ids_to_delete)


def _release(db: Session, paper_uuid: uuid.UUID, owner_uuid: uuid.UUID, token) -> None:
    """Best effort. If even this fails, the claim's lease expires instead."""
    try:
        db.rollback()
        paper_lifecycle.release_deletion(db, paper_uuid, owner_uuid, token)
    except Exception as exc:
        db.rollback()
        print(f"Paper deletion claim not released: {type(exc).__name__}")


def _refuse(db: Session, paper_uuid: uuid.UUID, owner_uuid: uuid.UUID):
    """The paper could not be claimed: gone, or another request holds it."""
    status = paper_lifecycle.current_status(db, paper_uuid, owner_uuid)
    if status is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    raise HTTPException(status_code=409, detail=BUSY_DETAILS.get(status, BUSY_DETAIL))
