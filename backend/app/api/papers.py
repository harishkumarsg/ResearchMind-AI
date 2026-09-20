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

    # Every paper this owner has, in any state. The owner_id filter is
    # the verified JWT `sub`; another owner's row cannot appear here, and
    # neither can their status_detail.
    owned = (
        db.query(Paper)
        .filter(Paper.owner_id == owner_uuid)
        .order_by(Paper.title)
        .all()
    )

    indexed = [p for p in owned if p.status == "indexed"]

    return {
        "status": "success",

        # Unchanged contract: the titles that are ready to query. Every
        # existing consumer reads this and must keep working.
        "papers": [p.title for p in indexed],

        # Additive. Lets the UI show uploading/indexing/failed papers,
        # which were previously invisible because they are not "indexed".
        # status_detail is written by upload.py and index_document.py and
        # is always application-authored — never a raw exception.
        "papers_detailed": [
            {
                "paper_id": str(p.id),
                "title": p.title,
                "status": p.status,
                "status_detail": p.status_detail,
            }
            for p in owned
        ],
    }
