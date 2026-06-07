from fastapi import APIRouter
import os

router = APIRouter()

UPLOAD_DIR = "uploads/papers"


@router.get("/papers")
def list_papers():

    try:

        if not os.path.exists(UPLOAD_DIR):

            return {
                "status": "success",
                "papers": []
            }

        files = [
            os.path.splitext(f)[0]
            for f in os.listdir(UPLOAD_DIR)
            if f.lower().endswith(".pdf")
        ]

        files.sort()

        return {
            "status": "success",
            "papers": files
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }
