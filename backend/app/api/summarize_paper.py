from fastapi import APIRouter

from app.rag.embedder import model
from app.rag.vector_store import client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

import app.memory as memory

router = APIRouter()

COLLECTION_NAME = "researchmind"


@router.get("/summarize-paper")
def summarize_paper(
    paper_name: str
):

    try:

        # -----------------------------------
        # Search
        # -----------------------------------

        query_vector = model.encode(
            paper_name
        )

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector.tolist(),
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

                break

        if not detected_paper:

            detected_paper = results[0].payload.get(
                "paper",
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
        # Memory
        # -----------------------------------

        memory.last_summary = summary

        memory.last_summary_paper = (
            detected_paper
        )

        memory.current_paper = (
            detected_paper
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