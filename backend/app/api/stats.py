import uuid

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
from app.db.models import Paper
from app.db.session import get_db_session
from app.rag.vector_store import COLLECTION_NAME, client

router = APIRouter()


@router.get("/stats")
def get_stats(
    owner_id: str = Depends(get_current_owner_id),
    db: Session = Depends(get_db_session),
):
    owner_uuid = uuid.UUID(owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object

    papers = (
        db.query(Paper)
        .filter(Paper.owner_id == owner_uuid, Paper.status == "indexed")
        .order_by(Paper.created_at.desc())
        .all()
    )

    total_chunks = 0
    try:
        # Owner-scoped count — never the whole collection's point count.
        result = client.count(
            collection_name=COLLECTION_NAME,
            count_filter=Filter(
                must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
            ),
            exact=True,
        )
        total_chunks = result.count
    except Exception:
        pass

    return {
        "status": "success",
        "total_papers": len(papers),
        "total_chunks": total_chunks,
        "recent_papers": [p.title for p in papers[:5]],
    }
