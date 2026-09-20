import uuid

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.limits import UsageLimitError
from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import charge_ai_unit, precheck_ai_generation
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results

from app.agents.research_agent import research_agent

from app.db.models import Report
from app.db.session import session_scope

from app.memory import get_user_memory

router = APIRouter()

MAX_RESULTS = 30
MAX_CONTEXT_CHUNKS = 15
MAX_CONTEXT_LENGTH = 12000

#: Report's context budget is deliberately the same number as the context
#: it assembles above, not a second independent limit: the defect was a
#: 4000-character cut applied on top of a 12000-character context, and two
#: constants that can drift apart would let it back in. Widening retrieval
#: means changing MAX_CONTEXT_LENGTH, and this follows.
REPORT_CONTEXT_CHARS = MAX_CONTEXT_LENGTH

#: Thirteen required sections, plus the reasoning tokens this model draws
#: from the same completion budget. At the old 512 the report could not
#: physically finish, which is what produced a stored report ending
#: mid-sentence.
REPORT_MAX_TOKENS = 5000


@router.get("/research")
def research(query: str, owner_id: str = Depends(precheck_ai_generation)):

    try:

        user_memory = get_user_memory(owner_id)

        # ==================================
        # Query Embedding
        # ==================================

        # Last safe point: the next statement is the provider call.
        charge_ai_unit(owner_id)

        query_vector = encode_query(query)

        # ==================================
        # Vector Search — owner_id filter is server-constructed from the
        # verified JWT identity, never from any client-supplied value.
        # ==================================

        owner_filter = Filter(
            must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
        )

        search_results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=owner_filter,
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
            citations=citations,
            max_context_chars=REPORT_CONTEXT_CHARS,
            max_tokens=REPORT_MAX_TOKENS,
        )

        # ==================================
        # Persist Report — the durable record
        #
        # owner_id comes from get_current_owner_id, i.e. the verified JWT
        # `sub`, and is never read from a query parameter or request body.
        # A caller cannot write a report attributed to anyone else.
        #
        # Each /research call INSERTs a new row rather than overwriting a
        # per-owner one: `reports` has no unique constraint on owner_id,
        # and a research history is worth more than a single latest slot.
        # Export (a later step) reads the owner's most recent row.
        #
        # session_scope() rather than a request-scoped dependency keeps
        # the connection lease to just this write, and commits or rolls
        # back as one unit. A failure here propagates to the handler's
        # except block below, so the response reports an error instead of
        # claiming success for a report that was never stored.
        # ==================================

        with session_scope(owner_id) as db:

            db.add(
                Report(
                    owner_id=uuid.UUID(owner_id),
                    query=query,
                    report_markdown=report,
                    citations=citations
                )
            )

        # ==================================
        # Save Memory — scoped to this user only
        # ==================================

        user_memory.last_research_query = query

        user_memory.last_research_report = report

        user_memory.last_research_sources = citations

        user_memory.last_citations = citations

        user_memory.last_research_context = context

        # The latest_report_<owner>.txt / latest_sources_<owner>.txt
        # files that used to be written here are gone. They existed only
        # as export_report.py's fallback, and export now reads the
        # Postgres row written above, so these were dead writes to a disk
        # that is ephemeral in deployment anyway.

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

    except UsageLimitError:
        # A usage rejection is not an application failure. It must reach the
        # handler in app/main.py as a normalized 429/503 rather than being
        # flattened into this endpoint's 200 {"status": "error"} shape,
        # which is how the frontend tells the two apart.
        raise

    except Exception as e:

        print(
            f"Research Error: {str(e)}"
        )

        # A provider failure gets a neutral, application-authored message
        # instead of the raw exception text.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return internal_error_payload()