import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
from app.db.models import Paper
from app.db.session import get_db_session

router = APIRouter()


@router.get("/papers")
def list_papers(
    owner_id: str = Depends(get_current_owner_id),
    db: Session = Depends(get_db_session),
):
    owner_uuid = uuid.UUID(owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object

    papers = (
        db.query(Paper)
        .filter(Paper.owner_id == owner_uuid, Paper.status == "indexed")
        .order_by(Paper.title)
        .all()
    )

    return {
        "status": "success",
        "papers": [p.title for p in papers],
    }
