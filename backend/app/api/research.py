from fastapi import APIRouter

from app.rag.embedder import model
from app.rag.vector_store import client
from app.rag.reranker import rerank_results

from app.agents.research_agent import research_agent

import app.memory as memory

router = APIRouter()

COLLECTION_NAME = "researchmind"

MAX_RESULTS = 30
MAX_CONTEXT_CHUNKS = 15
MAX_CONTEXT_LENGTH = 12000


@router.get("/research")
def research(query: str):

    try:

        # ==================================
        # Query Embedding
        # ==================================

        query_vector = model.encode(query)

        # ==================================
        # Vector Search
        # ==================================

        search_results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector.tolist(),
            limit=MAX_RESULTS
        ).points

        print("\n========== RESEARCH DEBUG ==========")
        print(f"Query: {query}")
        print(
            f"Vector Results Retrieved: {len(search_results)}"
        )

        # ==================================
        # Reranking
        # ==================================

        ranked_results = rerank_results(
            query,
            search_results
        )

        print(
            f"Reranked Results: {len(ranked_results)}"
        )

        # ==================================
        # Build Context
        # ==================================

        context_parts = []

        citations = []

        seen_chunks = set()
        seen_citations = set()

        chunks_used = 0

        for hit in ranked_results:

            if chunks_used >= MAX_CONTEXT_CHUNKS:
                break

            payload = hit.payload

            text = payload.get(
                "text",
                ""
            ).strip()

            if not text:
                continue

            # ------------------------------
            # Remove Duplicate Chunks
            # ------------------------------

            chunk_key = (
                payload.get("paper"),
                payload.get("page"),
                payload.get("chunk_id")
            )

            if chunk_key in seen_chunks:
                continue

            seen_chunks.add(chunk_key)

            paper = payload.get(
                "paper",
                "Unknown Paper"
            )

            source = payload.get(
                "source",
                "Unknown Source"
            )

            page = payload.get(
                "page",
                "Unknown"
            )

            authors = payload.get(
                "authors",
                ""
            )

            abstract = payload.get(
                "abstract",
                ""
            )

            keywords = payload.get(
                "keywords",
                ""
            )

            # ------------------------------
            # Rich Context
            # ------------------------------

            context_parts.append(
                f"""
PAPER TITLE:
{paper}

AUTHORS:
{authors}

KEYWORDS:
{keywords}

ABSTRACT:
{abstract}

PAGE:
{page}

CONTENT:
{text}
"""
            )

            chunks_used += 1

            # ------------------------------
            # Citations
            # ------------------------------

            citation_key = (
                paper,
                page
            )

            if citation_key not in seen_citations:

                seen_citations.add(
                    citation_key
                )

                citations.append(
                    {
                        "paper": paper,
                        "source": source,
                        "page": page
                    }
                )

        print(
            f"Citations Collected: {len(citations)}"
        )

        # ==================================
        # Final Context
        # ==================================

        context = "\n\n".join(
            context_parts
        )

        context = context[
            :MAX_CONTEXT_LENGTH
        ]

        print(
            f"Context Length: {len(context)}"
        )

        # ==================================
        # Generate Report
        # ==================================

        report = research_agent(
            query=query,
            context=context,
            citations=citations
        )

        # ==================================
        # Save Memory
        # ==================================

        memory.last_research_query = query

        memory.last_research_report = report

        memory.last_research_sources = citations

        memory.last_citations = citations

        memory.last_research_context = context

        # ==================================
        # Save Report File
        # ==================================

        with open(
            "latest_report.txt",
            "w",
            encoding="utf-8"
        ) as f:

            f.write(report)

        # ==================================
        # Save Citation File
        # ==================================

        with open(
            "latest_sources.txt",
            "w",
            encoding="utf-8"
        ) as f:

            for item in citations:

                f.write(
                    f"Paper: {item['paper']}\n"
                )

                f.write(
                    f"Source: {item['source']}\n"
                )

                f.write(
                    f"Page: {item['page']}\n\n"
                )

        print(
            "latest_sources.txt written."
        )

        print(
            "===================================\n"
        )

        # ==================================
        # API Response
        # ==================================

        return {

            "status":
            "success",

            "query":
            query,

            "report":
            report,

            "citations":
            citations,

            "chunks_used":
            chunks_used,

            "sources_used":
            len(citations),

            "citations_available":
            len(citations) > 0,

            "stored_in_memory":
            True,

            "context_length":
            len(context)
        }

    except Exception as e:

        print(
            f"Research Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }