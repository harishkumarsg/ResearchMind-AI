from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.auth import get_current_owner_id
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results

router = APIRouter()


@router.get("/search")
def search(query: str, owner_id: str = Depends(get_current_owner_id)):

    try:

        # ----------------------------------
        # Create Query Embedding
        # ----------------------------------

        query_vector = encode_query(
            query
        )

        # ----------------------------------
        # Vector Search — owner_id filter is server-constructed from the
        # verified JWT identity, never from any client-supplied value.
        # ----------------------------------

        owner_filter = Filter(
            must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
        )

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=owner_filter,
            limit=50
        ).points

        if not results:

            return {

                "status":
                "success",

                "query":
                query,

                "total_results":
                0,

                "results":
                []
            }

        # ----------------------------------
        # Cross Encoder Reranking
        # ----------------------------------

        results = rerank_results(
            query,
            results
        )

        # ----------------------------------
        # One Result Per Paper
        # ----------------------------------

        unique_results = []

        seen_papers = set()

        for hit in results:

            paper = hit.payload.get(
                "paper",
                "Unknown"
            )

            if paper in seen_papers:
                continue

            seen_papers.add(
                paper
            )

            unique_results.append(
                hit
            )

        unique_results = unique_results[:10]

        # ----------------------------------
        # Build Response
        # ----------------------------------

        formatted_results = []

        for hit in unique_results:

            payload = hit.payload

            formatted_results.append(

                {

                    "score":
                    float(
                        getattr(
                            hit,
                            "score",
                            0.0
                        )
                    ),

                    "paper":
                    payload.get(
                        "paper",
                        "Unknown"
                    ),

                    "paper_id":
                    payload.get(
                        "paper_id",
                        ""
                    ),

                    "source":
                    payload.get(
                        "source",
                        "Unknown"
                    ),

                    "page":
                    payload.get(
                        "page",
                        "Unknown"
                    ),

                    "chunk_id":
                    payload.get(
                        "chunk_id",
                        "Unknown"
                    ),

                    "chunk_count":
                    payload.get(
                        "chunk_count",
                        "Unknown"
                    ),

                    "authors":
                    payload.get(
                        "authors",
                        ""
                    ),

                    "keywords":
                    payload.get(
                        "keywords",
                        ""
                    ),

                    "abstract":
                    payload.get(
                        "abstract",
                        ""
                    )[:500],

                    "text":
                    payload.get(
                        "text",
                        ""
                    )[:500]
                }
            )

        # ----------------------------------
        # Response
        # ----------------------------------

        return {

            "status":
            "success",

            "query":
            query,

            "vector_results":
            len(results),

            "total_results":
            len(formatted_results),

            "results":
            formatted_results
        }

    except Exception as e:

        print(
            f"Search Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }