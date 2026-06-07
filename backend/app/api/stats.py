from fastapi import APIRouter
import os

from app.rag.vector_store import client, COLLECTION_NAME

router = APIRouter()

UPLOAD_DIR = "uploads/papers"


@router.get("/stats")
def get_stats():

    try:

        # ==================================
        # Qdrant Collection Info
        # ==================================

        total_chunks = 0

        try:

            info = client.get_collection(
                collection_name=COLLECTION_NAME
            )

            total_chunks = info.points_count or 0

        except Exception:
            pass

        # ==================================
        # Upload Directory
        # ==================================

        total_papers = 0
        recent_papers: list[str] = []

        if os.path.exists(UPLOAD_DIR):

            files = [
                f
                for f in os.listdir(UPLOAD_DIR)
                if f.lower().endswith(".pdf")
            ]

            total_papers = len(files)

            files_with_time = [
                (
                    f,
                    os.path.getmtime(
                        os.path.join(UPLOAD_DIR, f)
                    ),
                )
                for f in files
            ]

            files_with_time.sort(
                key=lambda x: x[1],
                reverse=True
            )

            recent_papers = [
                os.path.splitext(f)[0] for f, _ in files_with_time[:5]
            ]

        return {
            "status": "success",
            "total_papers": total_papers,
            "total_chunks": total_chunks,
            "recent_papers": recent_papers,
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }
