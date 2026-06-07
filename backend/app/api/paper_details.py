from fastapi import APIRouter

from app.rag.embedder import model
from app.rag.vector_store import client

router = APIRouter()

COLLECTION_NAME = "researchmind"


@router.get("/paper-details")
def paper_details(paper_name: str):

    try:

        query_vector = model.encode(
            paper_name
        )

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector.tolist(),
            limit=100
        ).points

        paper_chunks = []

        detected_paper = ""

        source = ""
        paper_id = None

        authors = ""
        abstract = ""
        keywords = ""

        for hit in results:

            stored_paper = hit.payload.get(
                "paper",
                ""
            )

            if paper_name.lower() in stored_paper.lower():

                paper_chunks.append(
                    hit.payload.get(
                        "text",
                        ""
                    )
                )

                detected_paper = stored_paper

                source = hit.payload.get(
                    "source",
                    source
                )

                paper_id = hit.payload.get(
                    "paper_id",
                    paper_id
                )

                authors = hit.payload.get(
                    "authors",
                    authors
                )

                abstract = hit.payload.get(
                    "abstract",
                    abstract
                )

                keywords = hit.payload.get(
                    "keywords",
                    keywords
                )

        if not paper_chunks:

            return {
                "status": "error",
                "message": f"Paper not found: {paper_name}"
            }

        return {

            "status": "success",

            "paper":
            detected_paper,

            "paper_id":
            paper_id,

            "source":
            source,

            "authors":
            authors,

            "keywords":
            keywords,

            "abstract":
            abstract,

            "total_chunks":
            len(paper_chunks),

            "preview":
            paper_chunks[0][:1000]
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }