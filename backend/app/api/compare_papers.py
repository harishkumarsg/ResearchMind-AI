from fastapi import APIRouter

from app.rag.embedder import model
from app.rag.vector_store import client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

import app.memory as memory

router = APIRouter()

COLLECTION_NAME = "researchmind"

MAX_CHUNKS_PER_PAPER = 25
MAX_CONTEXT_LENGTH = 15000


@router.get("/compare-papers")
def compare_papers(
    paper1: str,
    paper2: str
):

    try:

        # ==================================
        # Helper
        # ==================================

        def get_paper_context(
            paper_name: str
        ):

            query_vector = model.encode(
                paper_name
            )

            results = client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector.tolist(),
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

        full_context = f"""
========================
PAPER 1 CONTEXT
========================

{context1}

========================
PAPER 2 CONTEXT
========================

{context2}
"""

        # ==================================
        # Generate Comparison
        # ==================================

        comparison = generate_answer(
            prompt,
            full_context
        )

        # ==================================
        # Memory
        # ==================================

        memory.last_comparison = comparison

        memory.last_compared_paper1 = (
            detected_paper1
        )

        memory.last_compared_paper2 = (
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

    except Exception as e:

        print(
            f"Compare Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }