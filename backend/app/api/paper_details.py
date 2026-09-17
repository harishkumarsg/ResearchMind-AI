from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.auth import get_current_owner_id
from app.core.providers import classify_provider_error
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client

router = APIRouter()


@router.get("/paper-details")
def paper_details(paper_name: str, owner_id: str = Depends(get_current_owner_id)):

    try:

        query_vector = encode_query(
            paper_name
        )

        # owner_id filter is server-constructed from the verified JWT
        # identity, never from any client-supplied value.
        owner_filter = Filter(
            must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
        )

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=owner_filter,
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

        # A provider failure gets a neutral, application-authored message
        # instead of the raw exception text.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return {
            "status": "error",
            "message": str(e)
        }