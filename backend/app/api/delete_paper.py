from fastapi import APIRouter, HTTPException
import os

from qdrant_client.models import (
    Filter,
    FieldCondition,
    MatchValue,
    PointIdsList,
)

from app.rag.vector_store import client, COLLECTION_NAME

router = APIRouter()

UPLOAD_DIR = "uploads/papers"


@router.delete("/paper/{paper_name}")
def delete_paper(paper_name: str):

    try:

        # ----------------------------------------
        # Delete PDF file from disk
        # ----------------------------------------

        pdf_path = os.path.join(UPLOAD_DIR, paper_name + ".pdf")
        file_deleted = False

        if os.path.exists(pdf_path):
            os.remove(pdf_path)
            file_deleted = True

        # ----------------------------------------
        # Find all Qdrant point IDs for this paper
        # ----------------------------------------

        ids_to_delete: list = []
        next_offset = None

        while True:

            points, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="paper",
                            match=MatchValue(value=paper_name),
                        )
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

        vectors_deleted = len(ids_to_delete)

        if ids_to_delete:
            client.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=ids_to_delete),
            )

        return {
            "status": "success",
            "message": f"Paper '{paper_name}' deleted successfully.",
            "file_deleted": file_deleted,
            "vectors_deleted": vectors_deleted,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
