from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.limits import UsageLimitError
from app.core.providers import classify_provider_error
from app.core.usage_guard import charge_ai_unit, precheck_ai_generation
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

from app.memory import get_user_memory

router = APIRouter()

MAX_CHUNKS_PER_PAPER = 25
MAX_CONTEXT_LENGTH = 15000

#: Ceiling on the ASSEMBLED context, banners included. Two papers at
#: MAX_CONTEXT_LENGTH each would be 30000 characters; the point of this
#: number is that both papers are fitted into it deliberately, instead of
#: being concatenated and then cut blind.
COMPARE_TOTAL_CONTEXT_CHARS = 16000

#: Thirteen comparison categories, each answered for both papers.
COMPARE_MAX_TOKENS = 6000

#: The banners are part of the budget. Allocating half of the total to
#: each paper and THEN adding banners overflows the ceiling and re-arms
#: the tail cut on Paper 2 -- the same off-by-envelope mistake in a new
#: place. The template is defined once so its overhead can be measured
#: rather than estimated.
_CONTEXT_TEMPLATE = """
========================
PAPER 1 CONTEXT
========================

{paper1}

========================
PAPER 2 CONTEXT
========================

{paper2}
"""

_ENVELOPE_CHARS = len(_CONTEXT_TEMPLATE.format(paper1="", paper2=""))


def allocate_paper_budgets(len1, len2, total=COMPARE_TOTAL_CONTEXT_CHARS):
    """Split the available characters between two papers.

    Equal shares with reallocation of whatever a short paper does not
    use. Comparison is a symmetric operation, so each paper is guaranteed
    half the space before either can have more: a proportional split
    would let a verbose Paper 1 starve Paper 2, which is the original
    defect wearing a more respectable name. A short paper may give its
    slack away; it can never take from the other's floor.
    """
    available = max(total - _ENVELOPE_CHARS, 0)
    floor = available // 2

    if len1 <= floor and len2 <= floor:
        return len1, len2

    if len1 <= floor:
        return len1, min(len2, available - len1)

    if len2 <= floor:
        return min(len1, available - len2), len2

    # Both oversized: neither can be favoured by assembly order.
    return floor, floor


@router.get("/compare-papers")
def compare_papers(
    paper1: str,
    paper2: str,
    owner_id: str = Depends(precheck_ai_generation),
):

    try:

        user_memory = get_user_memory(owner_id)

        # ==================================
        # Helper — owner_id filter is server-constructed from the verified
        # JWT identity (threaded in from the outer scope), never from any
        # client-supplied value.
        # ==================================

        def get_paper_context(
            paper_name: str
        ):

            query_vector = encode_query(
                paper_name
            )

            owner_filter = Filter(
                must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
            )

            results = client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector,
                query_filter=owner_filter,
                limit=100
            ).points

            ranked = rerank_results(
                paper_name,
                results
            )

            chunks = []

            seen_chunks = set()

            detected_paper = ""
            detected_source = ""

            for hit in ranked:

                payload = hit.payload

                stored_paper = payload.get(
                    "paper",
                    ""
                )

                if paper_name.lower() not in stored_paper.lower():
                    continue

                text = payload.get(
                    "text",
                    ""
                ).strip()

                if not text:
                    continue

                chunk_key = (
                    payload.get("page"),
                    payload.get("chunk_id")
                )

                if chunk_key in seen_chunks:
                    continue

                seen_chunks.add(
                    chunk_key
                )

                page = payload.get(
                    "page",
                    "Unknown"
                )

                authors = payload.get(
                    "authors",
                    ""
                )

                keywords = payload.get(
                    "keywords",
                    ""
                )

                abstract = payload.get(
                    "abstract",
                    ""
                )

                chunks.append(
                    f"""
PAPER:
{stored_paper}

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

                detected_paper = stored_paper

                detected_source = payload.get(
                    "source",
                    ""
                )

                if len(chunks) >= MAX_CHUNKS_PER_PAPER:
                    break

            context = "\n\n".join(
                chunks
            )

            context = context[
                :MAX_CONTEXT_LENGTH
            ]

            return (
                context,
                detected_paper,
                detected_source,
                len(chunks)
            )

        # ==================================
        # Paper 1
        # ==================================

        # Last safe point: get_paper_context() embeds immediately, and a
        # comparison is one billable operation even though it embeds twice.
        charge_ai_unit(owner_id)

        (
            context1,
            detected_paper1,
            source1,
            chunk_count1
        ) = get_paper_context(
            paper1
        )

        if not context1:

            return {
                "status": "error",
                "message":
                f"Paper not found: {paper1}"
            }

        # ==================================
        # Paper 2
        # ==================================

        (
            context2,
            detected_paper2,
            source2,
            chunk_count2
        ) = get_paper_context(
            paper2
        )

        if not context2:

            return {
                "status": "error",
                "message":
                f"Paper not found: {paper2}"
            }

        # ==================================
        # Comparison Prompt
        # ==================================

        prompt = f"""
You are ResearchMind AI.

You are comparing two academic research papers.

STRICT RULES:

1. Use ONLY the supplied paper contexts.

2. Never use external knowledge.

3. Never infer information.

4. Never assume information exists.

5. Never write:

- could include
- might include
- possibly
- likely
- assumed
- inferred
- generally

6. If information is missing write exactly:

Information not specified in the reviewed papers.

7. Do not merge information between papers.

8. Keep paper-specific evidence separate.

9. Be objective and academic.

10. Produce ONLY the table.

Compare the papers using:

| Category | Paper 1 | Paper 2 |

Categories:

- Objective
- Problem Statement
- Methodology
- Technologies Used
- Hardware Used
- Algorithms Used
- Dataset Information
- Experimental Results
- Key Findings
- Advantages
- Limitations
- Future Scope
- Conclusion

Paper 1:
{detected_paper1}

Paper 2:
{detected_paper2}
"""

        # Budget each paper BEFORE assembly. The previous code joined
        # both contexts and handed the result to a primitive that cut it
        # to 4000 characters -- a cut that landed inside Paper 1, so the
        # model compared one paper against nothing.
        budget1, budget2 = allocate_paper_budgets(
            len(context1),
            len(context2)
        )

        full_context = _CONTEXT_TEMPLATE.format(
            paper1=context1[:budget1],
            paper2=context2[:budget2]
        )

        # ==================================
        # Generate Comparison
        # ==================================

        comparison = generate_answer(
            prompt,
            full_context,
            max_context_chars=COMPARE_TOTAL_CONTEXT_CHARS,
            max_tokens=COMPARE_MAX_TOKENS,
        )

        # ==================================
        # Memory — scoped to this user only
        # ==================================

        user_memory.last_comparison = comparison

        user_memory.last_compared_paper1 = (
            detected_paper1
        )

        user_memory.last_compared_paper2 = (
            detected_paper2
        )

        # ==================================
        # Response
        # ==================================

        return {

            "status":
            "success",

            "paper1":
            detected_paper1,

            "paper2":
            detected_paper2,

            "source1":
            source1,

            "source2":
            source2,

            "comparison":
            comparison,

            "paper1_chunks":
            chunk_count1,

            "paper2_chunks":
            chunk_count2,

            "stored_in_memory":
            True
        }

    except UsageLimitError:
        # A usage rejection is not an application failure. It must reach the
        # handler in app/main.py as a normalized 429/503 rather than being
        # flattened into this endpoint's 200 {"status": "error"} shape,
        # which is how the frontend tells the two apart.
        raise

    except Exception as e:

        print(
            f"Compare Error: {str(e)}"
        )

        # A provider failure gets a neutral, application-authored message
        # instead of the raw exception text.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return {

            "status":
            "error",

            "message":
            str(e)
        }