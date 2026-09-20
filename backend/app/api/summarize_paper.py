import uuid

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy import func

from app.core.limits import UsageLimitError
from app.db.models import Paper
from app.db.session import session_scope
from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import charge_ai_unit, precheck_ai_generation
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results

from app.agents.qa_agent import generate_answer

from app.services.chat_store import coerce_paper_id, set_current_paper

from app.memory import get_user_memory

router = APIRouter()

MAX_CONTEXT_LENGTH = 20000

#: Summarize's own budgets, replacing the shared 4000-character default
#: that used to discard four fifths of the context assembled just above.
#: 12000 rather than the full MAX_CONTEXT_LENGTH: a single-paper summary
#: shows diminishing returns past roughly this point, and 20000 would make
#: the cheapest of the three operations the most expensive.
SUMMARY_CONTEXT_CHARS = 12000

#: One multi-section summary, plus the reasoning tokens this model spends
#: from the same completion budget.
SUMMARY_MAX_TOKENS = 3000

#: Upper bound on how many of one paper's chunks are fetched. Comfortably
#: above a normal paper (a few hundred chunks), so the filter rather than
#: the limit is what decides the result.
PAPER_FETCH_LIMIT = 1024

#: Ceiling on the title/authors/keywords/abstract header. That header is
#: part of the prompt rather than the context, so it is not covered by
#: SUMMARY_CONTEXT_CHARS; this stops an unusually long stored abstract
#: from enlarging the request by an unbounded amount.
MAX_METADATA_CHARS = 2000


def resolve_paper_in_database(owner_id, paper_name):
    """This owner's paper with this exact title, as (paper_id, title).

    `papers` is the system of record: papers.id is what Qdrant stores as
    paper_id, and papers.title is what it stores as `paper`, both written
    from the same row at index time. Resolving here therefore yields the
    exact id the chunk filter needs, without a provider call.

    Exact title first, then a case-insensitive exact match. Substring
    matching is deliberately NOT used — "report.pdf" must never select
    "final report.pdf".

    Returns ("", "") when this owner has no such paper.
    """
    owner_uuid = uuid.UUID(owner_id)

    with session_scope(owner_id) as db:

        row = (
            db.query(Paper.id, Paper.title)
            .filter(
                Paper.owner_id == owner_uuid,
                Paper.title == paper_name
            )
            .first()
        )

        if row is None:

            row = (
                db.query(Paper.id, Paper.title)
                .filter(
                    Paper.owner_id == owner_uuid,
                    func.lower(Paper.title) == paper_name.lower()
                )
                .first()
            )

        if row is None:
            return "", ""

        return str(row.id), row.title


def resolve_paper_in_index(owner_id, paper_name, query_vector):
    """Fallback identification, for a paper indexed but not in `papers`.

    Still exact and still owner-scoped: the FILTER decides, and a single
    point is enough because every chunk of a paper carries the same
    paper/paper_id pair. The ranking is incidental — it cannot promote a
    differently titled paper, because a differently titled paper cannot
    pass the filter at all.

    Returns ("", "") when this owner has no such paper.
    """
    points = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=Filter(
            must=[
                FieldCondition(
                    key="owner_id",
                    match=MatchValue(value=owner_id)
                ),
                FieldCondition(
                    key="paper",
                    match=MatchValue(value=paper_name)
                )
            ]
        ),
        limit=1
    ).points

    for point in points:

        payload = point.payload or {}

        title = payload.get(
            "paper",
            ""
        )

        if title:

            return (
                payload.get("paper_id", ""),
                title
            )

    return "", ""


def _reading_order(payload):
    """Document order: page, then chunk index within the page.

    Both are written as integers at index time but are read defensively:
    a missing or non-numeric value sorts last rather than raising, so one
    malformed point cannot fail an entire summary.
    """
    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 10 ** 9

    return (
        as_int(payload.get("page")),
        as_int(payload.get("chunk_id"))
    )


def fetch_paper_chunks(owner_id, paper_id, paper_title, query_vector):
    """Every stored chunk of ONE paper, in reading order.

    Summarising is not a search. The evidence a summary needs is the
    whole document, so the paper is selected by FILTER and then put back
    into reading order here. The previous implementation embedded the
    FILE NAME, ranked the owner's whole library against it, and passed
    the result through rerank_results(), which keeps only its first
    MAX_RERANK hits — that is how a summary could name the dataset while
    reporting "Information not specified" for the paper's own
    methodology, results and conclusion.

    Two things make the ranking irrelevant rather than harmful here: the
    filter pins the result to one paper, and PAPER_FETCH_LIMIT sits well
    above a normal paper's chunk count, so the whole paper comes back and
    is re-sorted by page. The caller's existing query_vector is reused so
    this costs no extra embedding call, and rerank_results() is
    deliberately NOT applied.

    The filter always pins owner_id, and pins paper_id when the payload
    carries one, so no other owner's and no other paper's chunk can enter
    the result.
    """
    must = [
        FieldCondition(
            key="owner_id",
            match=MatchValue(value=owner_id)
        )
    ]

    if paper_id:

        must.append(
            FieldCondition(
                key="paper_id",
                match=MatchValue(value=paper_id)
            )
        )

    else:

        # Points indexed before paper_id existed: the title is then the
        # only discriminator available, and it stays owner-scoped.
        must.append(
            FieldCondition(
                key="paper",
                match=MatchValue(value=paper_title)
            )
        )

    points = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=Filter(must=must),
        limit=PAPER_FETCH_LIMIT
    ).points

    payloads = [
        point.payload
        for point in points
        if point.payload
    ]

    payloads.sort(key=_reading_order)

    return payloads


def _joined_length(blocks):
    """Length of "\n\n".join(blocks) without building the string."""
    return sum(len(b) for b in blocks) + 2 * max(len(blocks) - 1, 0)


def select_within_budget(blocks, budget):
    """As much of the paper as the budget allows, spread across it.

    Taking the first N blocks would spend the whole budget on front
    matter and leave Results, Limitations and Conclusion unsupported —
    the same empty fields the old retrieval produced, arrived at by a
    different route. An even stride keeps the beginning, middle and end
    represented, and the selection stays in reading order.
    """
    if not blocks:
        return []

    if _joined_length(blocks) <= budget:
        return blocks

    # Start from the stride the budget implies rather than searching up
    # from 1, then widen only if the estimate was optimistic.
    total = _joined_length(blocks)
    stride = max(1, -(-total // max(budget, 1)))

    while stride <= len(blocks):

        picked = blocks[::stride]

        if _joined_length(picked) <= budget:
            return picked

        stride += 1

    return blocks[:1]


@router.get("/summarize-paper")
def summarize_paper(
    paper_name: str,
    owner_id: str = Depends(precheck_ai_generation),
):

    try:

        user_memory = get_user_memory(owner_id)

        # -----------------------------------
        # Identify the paper — exactly, and owner-scoped.
        #
        # owner_id is the verified JWT identity, never a client-supplied
        # value. papers.id is the system of record and is what Qdrant
        # stores as paper_id, so the database is consulted first and no
        # provider call is needed to answer it.
        # -----------------------------------

        detected_paper_id, detected_paper = resolve_paper_in_database(
            owner_id,
            paper_name
        )

        # Last safe point: the next statement is the provider call.
        charge_ai_unit(owner_id)

        query_vector = encode_query(
            paper_name
        )

        if not detected_paper:

            # A paper present in the index but missing from `papers` is a
            # consistency edge case, not a reason to fail. The fallback
            # is still an EXACT, owner-scoped title match — never a
            # substring, and never the top hit of a ranking.
            detected_paper_id, detected_paper = resolve_paper_in_index(
                owner_id,
                paper_name,
                query_vector
            )

        if not detected_paper:

            # No match for this owner. Returning here rather than
            # summarising something else is the point: the previous
            # implementation fell back to results[0], the first hit of a
            # similarity ranking over the owner's whole library, so a
            # request for one paper could be answered with another.
            return {

                "status": "error",

                "message":
                f"Paper not found: {paper_name}"
            }

        # -----------------------------------
        # Collect Paper Chunks
        # -----------------------------------

        # Scrolled by owner_id + paper_id, in reading order, rather than
        # taken from the similarity search above. That search ranked the
        # library against the FILE NAME and rerank_results() then kept
        # only its first few hits, so most of the paper never reached the
        # model. The search above is still what identifies the paper; it
        # is no longer what supplies the evidence.
        payloads = fetch_paper_chunks(
            owner_id,
            detected_paper_id,
            detected_paper,
            query_vector
        )

        paper_chunks = []

        seen_chunks = set()

        authors = ""

        keywords = ""

        abstract = ""

        source = ""

        pages = set()

        for payload in payloads:

            text = payload.get(
                "text",
                ""
            )

            if not text or text in seen_chunks:
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

        # The budget now decides how much of the paper is used, so the
        # old fixed chunk count no longer applies. MAX_CONTEXT_LENGTH
        # stays as a backstop.
        context = "\n\n".join(
            select_within_budget(
                paper_chunks,
                SUMMARY_CONTEXT_CHARS
            )
        )

        context = context[:MAX_CONTEXT_LENGTH]

        # -----------------------------------
        # Summary Prompt
        # -----------------------------------

        # Authors, keywords and abstract are already stored on every
        # point and were previously read only to be echoed in the
        # response. Passing them to the model too gives Title, Research
        # Problem and Objective a source even when the title page itself
        # falls outside the context budget. They are evidence from the
        # paper, not new claims, so rule 1 still holds.
        metadata_parts = []

        if authors:
            metadata_parts.append(f"Authors:\n\n{authors}")

        if keywords:
            metadata_parts.append(f"Keywords:\n\n{keywords}")

        if abstract:
            metadata_parts.append(f"Abstract:\n\n{abstract}")

        paper_metadata = "\n\n".join(
            metadata_parts
        )[:MAX_METADATA_CHARS]

        prompt = f"""
Create an academic summary of this research paper.

Paper Title:

{detected_paper}

{paper_metadata}

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
            context,
            max_context_chars=SUMMARY_CONTEXT_CHARS,
            max_tokens=SUMMARY_MAX_TOKENS,
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

        # A provider failure gets a neutral, application-authored message
        # instead of the raw exception text.
        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return internal_error_payload()