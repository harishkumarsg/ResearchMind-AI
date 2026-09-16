from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.limits import UsageLimitError
from app.core.usage_guard import charge_ai_unit, precheck_ai_generation
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

from app.services.chat_store import coerce_paper_id, set_current_paper

from app.memory import get_user_memory

router = APIRouter()


@router.get("/summarize-paper")
def summarize_paper(
    paper_name: str,
    owner_id: str = Depends(precheck_ai_generation),
):

    try:

        user_memory = get_user_memory(owner_id)

        # -----------------------------------
        # Search — owner_id filter is server-constructed from the
        # verified JWT identity, never from any client-supplied value.
        # -----------------------------------

        # Last safe point: the next statement is the provider call.
        charge_ai_unit(owner_id)

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

        if not results:

            return {

                "status": "error",

                "message":
                "No papers found"
            }

        # -----------------------------------
        # Rerank
        # -----------------------------------

        results = rerank_results(
            paper_name,
            results
        )

        if not results:

            return {

                "status": "error",

                "message":
                "No matching papers found"
            }

        # -----------------------------------
        # Detect Paper
        # -----------------------------------

        detected_paper = ""

        # Captured alongside the title so the paper can be recorded
        # durably below. payload["paper"] is papers.title and
        # payload["paper_id"] is str(papers.id) — both written from the
        # same row at index time, so they always agree.
        detected_paper_id = ""

        for hit in results:

            paper = hit.payload.get(
                "paper",
                ""
            )

            if (
                paper_name.lower()
                in paper.lower()
            ):

                detected_paper = paper

                detected_paper_id = hit.payload.get(
                    "paper_id",
                    ""
                )

                break

        if not detected_paper:

            detected_paper = results[0].payload.get(
                "paper",
                ""
            )

            detected_paper_id = results[0].payload.get(
                "paper_id",
                ""
            )

        if not detected_paper:

            return {

                "status": "error",

                "message":
                "Unable to identify paper"
            }

        # -----------------------------------
        # Collect Paper Chunks
        # -----------------------------------

        paper_chunks = []

        seen_chunks = set()

        authors = ""

        keywords = ""

        abstract = ""

        source = ""

        pages = set()

        for hit in results:

            payload = hit.payload

            stored_paper = payload.get(
                "paper",
                ""
            )

            if stored_paper != detected_paper:
                continue

            text = payload.get(
                "text",
                ""
            )

            if text in seen_chunks:
                continue

            seen_chunks.add(
                text
            )

            page = payload.get(
                "page",
                "Unknown"
            )

            pages.add(
                str(page)
            )

            paper_chunks.append(
                f"""
Page:
{page}

Content:
{text}
"""
            )

            if not authors:

                authors = payload.get(
                    "authors",
                    ""
                )

            if not keywords:

                keywords = payload.get(
                    "keywords",
                    ""
                )

            if not abstract:

                abstract = payload.get(
                    "abstract",
                    ""
                )

            if not source:

                source = payload.get(
                    "source",
                    ""
                )

        if not paper_chunks:

            return {

                "status": "error",

                "message":
                f"Paper not found: {paper_name}"
            }

        # -----------------------------------
        # Context
        # -----------------------------------

        context = "\n\n".join(
            paper_chunks[:25]
        )

        context = context[:20000]

        # -----------------------------------
        # Summary Prompt
        # -----------------------------------

        prompt = f"""
Create an academic summary of this research paper.

Paper Title:

{detected_paper}

Rules:

1. Use ONLY the supplied context.

2. Never invent information.

3. Never assume missing information.

4. If information is missing write:

Information not specified in the reviewed paper.

5. Use the following structure.

### Title

### Research Problem

### Objective

### Background

### Methodology

### Technologies Used

### Hardware Used

### Algorithms / Models Used

### Dataset Information

### Experimental Results

### Key Findings

### Advantages

### Limitations

### Future Scope

### Conclusion

6. Use bullet points where appropriate.

7. Keep the summary professional.

8. Avoid repetition.

9. Use complete sentences.

10. Do not create references.

Context:

{context}
"""

        # -----------------------------------
        # Generate Summary
        # -----------------------------------

        summary = generate_answer(
            prompt,
            context
        )

        # -----------------------------------
        # Memory — scoped to this user only
        # -----------------------------------

        user_memory.last_summary = summary

        user_memory.last_summary_paper = (
            detected_paper
        )

        # -----------------------------------
        # Durable current-paper pointer
        #
        # Summarising establishes which paper the conversation is about.
        # /ask-stream's follow-up lock reads that from
        # chat_sessions.current_paper_id, so it is recorded here rather
        # than in in-process memory, which was lost on every restart.
        # owner_id is the verified JWT value; a payload that is missing,
        # malformed, or points at a paper this owner does not have is
        # ignored rather than written.
        # -----------------------------------

        set_current_paper(
            owner_id,
            coerce_paper_id(detected_paper_id)
        )

        # -----------------------------------
        # Response
        # -----------------------------------

        return {

            "status":
            "success",

            "paper":
            detected_paper,

            "source":
            source,

            "authors":
            authors,

            "keywords":
            keywords,

            "abstract":
            abstract,

            "summary":
            summary,

            "pages_found":
            sorted(
                list(pages)
            ),

            "chunks_used":
            len(paper_chunks),

            "summary_size":
            len(summary),

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
            f"Summary Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }