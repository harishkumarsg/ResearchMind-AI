import uuid

from fastapi import APIRouter, Depends, HTTPException
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedIdentity, get_current_identity
from app.db.models import Paper
from app.db.session import get_db_session
from app.rag.vector_store import COLLECTION_NAME, client
from app.services.storage import delete_pdf

router = APIRouter()


@router.delete("/paper/{paper_name}")
def delete_paper(
    paper_name: str,
    identity: AuthenticatedIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db_session),
):
    # Lookup is owner-scoped, not just by name — a name match belonging to
    # another user is treated identically to "does not exist" below, so a
    # malicious guess never distinguishes the two cases.
    owner_uuid = uuid.UUID(identity.owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object
    paper = (
        db.query(Paper)
        .filter(Paper.title == paper_name, Paper.owner_id == owner_uuid)
        .first()
    )

    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")

    if paper.status == "indexing":
        raise HTTPException(
            status_code=409,
            detail="This paper is still being indexed — try again shortly",
        )

    paper_id = str(paper.id)
    owner_id = str(paper.owner_id)

    paper.status = "deleting"
    db.commit()

    # 1. Qdrant vectors first. owner_id is merged into the filter as
    #    defense-in-depth even though paper_id should already be
    #    owner-scoped by construction (it can only have been written by
    #    index_document.py, which always sets owner_id from the verified
    #    identity that owns the paper).
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

    # 2. Storage object. Idempotent — safe even if it was already removed
    #    by a previous, partially-completed delete attempt.
    delete_pdf(owner_id=owner_id, paper_id=paper_id, user_jwt=identity.token)

    # 3. Postgres row LAST. Keeping it present until every external
    #    resource is confirmed gone means a failure partway through this
    #    function leaves a resumable 'deleting' row, never an invisible
    #    orphaned vector/file with nothing pointing back to it.
    db.delete(paper)
    db.commit()

    return {
        "status": "success",
        "message": f"Paper '{paper_name}' deleted successfully.",
        "vectors_deleted": len(ids_to_delete),
    }
