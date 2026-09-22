"""
POST /paper-intelligence — generate the structured analysis of ONE paper.

The pipeline, in the order it must happen:

    verified owner
  → exact owned paper (by id, from `papers`)
  → that paper's chunks only (Qdrant scroll, owner + paper filter)
  → deterministic reading order, deduplicated
  → bounded context, and an evidence allowlist built from EXACTLY the
    chunks that context contains
  → one Groq generation
  → strict JSON parse
  → validate_intelligence() against that allowlist
  → save_intelligence(), and only then

WHY THE ALLOWLIST IS BUILT FROM THE SELECTED CHUNKS
---------------------------------------------------
Not from everything retrieved. If the budget drops a chunk, the model
never saw it, so a citation of it would be fabricated rather than
merely unlucky — and an allowlist wider than the context is precisely
the gap through which a plausible-looking invented citation passes. The
allowlist is therefore built after selection, from the same list that
produced the prompt.

WHY THERE IS NO EMBEDDING CALL
------------------------------
This is not a search. The paper is already known by id, so the chunks
are fetched with client.scroll() under an exact payload filter. The
older /summarize-paper path reuses its caller's query_vector because it
has one; here there is no query, and embedding the title purely to
satisfy a vector argument would be a Voyage call that buys nothing and
that ranks the result for no reason.

WHAT IS NOT TRUSTED
-------------------
owner_id comes only from the verified JWT, via precheck_ai_generation.
paper_id is client-supplied and is resolved against `papers` before it
is used anywhere — including in the Qdrant filter, which is built from
the RESOLVED id. The model is trusted for prose and for references into
the supplied context, and for nothing else; intelligence_schema.py
decides what survives.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.agents.qa_agent import GROQ_MODEL, REFUSAL, generate
from app.api.paper_file import resolve_owned_paper
from app.api.summarize_paper import select_within_budget
from app.core.limits import UsageLimitError
from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import charge_ai_unit, precheck_ai_generation
from app.db.session import session_scope
from app.rag.vector_store import COLLECTION_NAME, client
from app.services.intelligence_schema import (
    SECTION_NAMES,
    IntelligenceValidationError,
    build_allowlist,
    validate_intelligence,
)
from app.services.paper_intelligence_store import PaperNotOwned, save_intelligence

router = APIRouter()

#: Points requested per scroll round trip.
SCROLL_PAGE_SIZE = 256

#: Hard ceiling on chunks considered for one paper. Comfortably above a
#: normal paper, so the FILTER rather than this limit decides the result;
#: it exists so a pathological document cannot make one request unbounded.
PAPER_CHUNK_CAP = 1024

#: Context budget, deliberately the same number /summarize-paper uses.
#: That figure is already proven in production against the configured
#: GROQ_TIMEOUT_SECONDS, and a second independent constant would be free
#: to drift away from it.
INTELLIGENCE_CONTEXT_CHARS = 12000

#: Completion budget. Ten sections of prose plus their evidence arrays,
#: and the reasoning tokens this model draws from the same budget. Held
#: at /summarize-paper's proven 3000 rather than raised: the prompt below
#: suppresses the optional `quote` field, which is what would otherwise
#: have pushed a ten-section object past it. See the prompt's rule 8.
INTELLIGENCE_MAX_TOKENS = 3000

#: One authored message for every miss — unknown id, malformed id, and
#: another owner's id alike. Matching /paper-file: the wording must not
#: let a caller probe whether some other user holds a given id.
NOT_FOUND_MESSAGE = "Paper not found."

#: The paper exists but has no indexed chunks, so there is nothing to
#: analyse. Distinct from NOT_FOUND because it is about the caller's own
#: paper and is actionable — re-index it.
NOT_INDEXED_MESSAGE = (
    "This paper has no indexed content yet. Index it and try again."
)

#: A generation that produced nothing usable. Authored here; the model's
#: own output is never rendered to a client.
GENERATION_FAILED_CODE = "intelligence_generation_failed"
GENERATION_FAILED_MESSAGE = (
    "The paper analysis could not be completed. Please try again."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# 1-2. Owner and paper
# ----------------------------------------------------------------------
def resolve_paper(owner_id: str, paper_id: str) -> Optional[Tuple[str, str]]:
    """This owner's paper with this exact id, as (paper_id, title).

    Delegates to /paper-file's resolver, which is the authoritative
    by-id lookup in this codebase: exact `papers.id` AND `papers.owner_id`,
    no substring match, no similarity, None for a malformed id.

    Deliberately NOT a title lookup and NOT a Qdrant lookup. `papers` is
    the system of record — papers.id is exactly what the indexer writes
    into every point as paper_id — so resolving here yields the id the
    chunk filter needs with no provider call and no ranking involved.
    """
    with session_scope(owner_id) as db:
        paper = resolve_owned_paper(db, owner_id, paper_id)
        if paper is None:
            return None
        # Read out inside the session; the row expires on commit.
        return str(paper.id), paper.title


# ----------------------------------------------------------------------
# 3. Retrieval — this paper's chunks, and nothing else
# ----------------------------------------------------------------------
def fetch_paper_chunks(owner_id: str, paper_id: str) -> List[dict]:
    """Every stored chunk of ONE paper, by exact payload filter.

    Both conditions are unconditional. /summarize-paper falls back to
    matching on the title when a point carries no paper_id, for points
    indexed before that field existed; there is no such fallback here,
    because a title is a weaker identifier than an id and this path has
    a resolved id in hand every time. A pre-paper_id point is simply not
    analysed, which is the safe direction to fail.

    No vector, no ranking: scroll returns the whole filtered set.
    """
    scroll_filter = Filter(
        must=[
            # From the verified JWT, never from the request.
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
            # The RESOLVED id, never the raw client-supplied string.
            FieldCondition(key="paper_id", match=MatchValue(value=paper_id)),
        ]
    )

    payloads: List[dict] = []
    offset = None

    while len(payloads) < PAPER_CHUNK_CAP:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        payloads.extend(point.payload for point in points if point.payload)

        # offset is None once the collection is exhausted; the empty-page
        # check guards a server that returns an offset but no points,
        # which would otherwise spin.
        if offset is None or not points:
            break

    return payloads[:PAPER_CHUNK_CAP]


def _citable(payload: dict) -> Optional[Tuple[int, int]]:
    """(page, chunk_id) if this payload can be cited at all, else None.

    Qdrant payloads are untyped. A chunk whose page or chunk_id is
    missing or non-numeric cannot be referenced by the pair that is this
    feature's evidence identity, so it is dropped rather than guessed at:
    including its text while it has no usable citation would invite the
    model to describe evidence it cannot point to.
    """
    try:
        page = int(payload["page"])
        chunk_id = int(payload["chunk_id"])
    except (KeyError, TypeError, ValueError):
        return None

    if page < 1 or chunk_id < 0:
        return None

    return page, chunk_id


def ordered_unique_chunks(payloads: List[dict]) -> List[dict]:
    """Citable chunks, deduplicated by (page, chunk_id), in reading order.

    Deduplication is not cosmetic. Point ids are regenerated on every
    index run (index_document.py assigns a fresh uuid4 per chunk), so a
    paper re-indexed without its old points being removed holds two
    copies of every chunk. The allowlist is a dict keyed on the pair, so
    duplicates would silently collapse there anyway — doing it here
    means the context the model reads and the allowlist it is judged
    against are built from exactly the same list.

    Order is page ascending, then chunk_id ascending: document order,
    and deterministic for a given set of chunks.
    """
    seen: Dict[Tuple[int, int], dict] = {}

    for payload in payloads:
        key = _citable(payload)
        if key is None:
            continue
        if not (payload.get("text") or "").strip():
            continue
        # First occurrence wins; a later duplicate carries the same text.
        seen.setdefault(key, payload)

    return [seen[key] for key in sorted(seen)]


# ----------------------------------------------------------------------
# 4-5. Context and allowlist
# ----------------------------------------------------------------------
def render_chunk(payload: dict) -> str:
    """One context block, headed by the citation the model must use.

    The header is the whole contract: the model is told to cite a block
    by copying the P and C it sees here, and validate_intelligence()
    then checks that pair against the allowlist. Nothing else in the
    block is an identifier.
    """
    page, chunk_id = _citable(payload)
    return f"[page={page} chunk={chunk_id}]\n{payload.get('text', '')}"


def select_context(chunks: List[dict], budget: int) -> List[dict]:
    """As much of the paper as the budget allows, spread across it.

    Reuses /summarize-paper's select_within_budget, which takes an even
    stride rather than the first N blocks — taking the first N would
    spend the whole budget on front matter and leave Results,
    Limitations and Reproducibility with no evidence, which is exactly
    the failure that feature was fixed for.

    Deterministic: the same chunks and budget always select the same
    subset, because the stride is computed from the measured total and
    the input is already in reading order.
    """
    if not chunks:
        return []

    blocks = [render_chunk(chunk) for chunk in chunks]

    # Safe because ordered_unique_chunks() has already made (page,
    # chunk_id) unique, and that pair is in every block's header.
    by_block = {block: chunk for block, chunk in zip(blocks, chunks)}

    return [by_block[block] for block in select_within_budget(blocks, budget)]


def build_context(chunks: List[dict]) -> str:
    return "\n\n".join(render_chunk(chunk) for chunk in chunks)


def resolve_total_pages(chunks: List[dict], highest_page: int) -> int:
    """The paper's page count, for validate_intelligence()'s range check.

    Prefers `total_pages` from the payload, which the indexer writes as
    len(pages) — the real count. Falls back to the highest page actually
    supplied, and never goes below it, so the bound can only ever be
    tighter than the truth, never looser.

    This check is a courtesy, not the gate: a page inside the range but
    never supplied still fails on the allowlist. It exists so an
    obviously impossible citation gets its own error code.
    """
    best = highest_page

    for chunk in chunks:
        try:
            declared = int(chunk["total_pages"])
        except (KeyError, TypeError, ValueError):
            continue
        if declared >= best:
            best = declared
            break

    return max(best, 1)


# ----------------------------------------------------------------------
# 6. The prompt
# ----------------------------------------------------------------------
_SECTION_LIST = "\n".join(f"  {i}. {n}" for i, n in enumerate(SECTION_NAMES, 1))

INTELLIGENCE_SYSTEM_PROMPT = f"""
You are ResearchMind AI, extracting a structured analysis of ONE research paper.

Return ONLY a single JSON object. No explanation before or after it. At
most one ``` fence around the whole object is tolerated; anything else
is rejected.

The object must have EXACTLY these ten keys, and no others:

{_SECTION_LIST}

Each value is an object of exactly this shape:

{{
  "status": "answered" or "not_specified",
  "summary": a string, or null,
  "evidence": [ {{"page": <integer>, "chunk_id": <integer>}} ]
}}

RULES:

1. Use ONLY the provided CONTEXT. Never use outside knowledge.
2. Every context block begins with a header like [page=3 chunk=1]. To
   cite that block, use page 3 and chunk_id 1, as integers.
3. Cite ONLY blocks that actually appear in the CONTEXT. Never invent a
   page number or a chunk id, and never adjust one to look plausible.
4. status "answered" requires a non-empty summary AND at least one
   evidence entry.
5. status "not_specified" requires summary null and an EMPTY evidence
   list. Use it whenever the paper does not state the information.
   "not_specified" is a correct, expected answer — never guess in order
   to avoid it.
6. Add no field beyond status, summary and evidence.
7. Keep each summary to 1-3 sentences.
8. Do NOT include a "quote" field. Cite by page and chunk_id only.
"""

INTELLIGENCE_QUESTION = (
    "Produce the structured JSON analysis of this paper, following every "
    "rule above exactly."
)


# ----------------------------------------------------------------------
# Endpoint
# ----------------------------------------------------------------------
@router.post("/paper-intelligence")
def paper_intelligence(
    paper_id: str,
    owner_id: str = Depends(precheck_ai_generation),
):
    # Diagnostic-only, and bound BEFORE the try so the rejection handler
    # can always log. Today every IntelligenceValidationError comes from
    # validate_intelligence(), by which point all three are assigned —
    # but that is an invariant nothing enforces, and one edit that
    # raised it earlier (an empty allowlist, say) would make the HANDLER
    # raise UnboundLocalError. The endpoint would then return a bare 500
    # with no body instead of its sanitized payload, turning a clean
    # rejection into an unhandled fault.
    #
    # These never reach the client: they appear only in the server-side
    # log line below.
    allowed_evidence: Dict[Tuple[int, int], str] = {}
    total_pages = 0
    raw = ""

    try:
        # -- 2. exact, owner-scoped paper resolution --------------------
        resolved = resolve_paper(owner_id, paper_id)

        if resolved is None:
            # Unknown, malformed, or someone else's — one response for
            # all three.
            return {"status": "error", "message": NOT_FOUND_MESSAGE}

        resolved_paper_id, paper_title = resolved

        # -- 3. this paper's chunks, and only this paper's --------------
        payloads = fetch_paper_chunks(owner_id, resolved_paper_id)
        chunks = ordered_unique_chunks(payloads)

        if not chunks:
            return {"status": "error", "message": NOT_INDEXED_MESSAGE}

        # -- 4. bounded context, then the allowlist FROM it -------------
        selected = select_context(chunks, INTELLIGENCE_CONTEXT_CHARS)
        allowed_evidence, highest_page = build_allowlist(selected)
        total_pages = resolve_total_pages(selected, highest_page)
        context = build_context(selected)

        if not allowed_evidence:
            # Nothing citable survived, so every section would have to be
            # not_specified. Not worth a generation.
            return {"status": "error", "message": NOT_INDEXED_MESSAGE}

        # Stamped at RUN START, before the provider call, because
        # save_intelligence() orders concurrent regenerations by this
        # value: a slow run that started earlier must not overwrite a
        # faster one that started later.
        generated_at = _utcnow()

        # -- 5. generate ------------------------------------------------
        # Last safe point: the next statement is the provider call.
        charge_ai_unit(owner_id)

        result = generate(
            INTELLIGENCE_QUESTION,
            context,
            system_prompt=INTELLIGENCE_SYSTEM_PROMPT,
            # None, not a number: the context above is already budgeted,
            # and a second structure-blind cut here is what truncated
            # Compare's whole second paper once before.
            max_context_chars=None,
            max_tokens=INTELLIGENCE_MAX_TOKENS,
        )

        raw = result.text

        if not raw or raw == REFUSAL:
            # generate() returns REFUSAL for an empty completion and for
            # an exception it could not classify. Neither is JSON, and
            # letting it fall through would report a provider problem as
            # "the model returned malformed JSON".
            print(
                f"[paper-intelligence] empty generation "
                f"paper={resolved_paper_id} chunks={len(selected)} "
                f"finish_reason={result.finish_reason}"
            )
            return {
                "status": "error",
                "code": GENERATION_FAILED_CODE,
                "message": GENERATION_FAILED_MESSAGE,
            }

        # -- 6-7. strict parse + validation against THIS paper ----------
        # Raises IntelligenceValidationError on anything that must not be
        # persisted. Nothing below this line runs if it does.
        intelligence = validate_intelligence(
            raw,
            allowed_evidence=allowed_evidence,
            total_pages=total_pages,
        )

        # -- 8. persist, only now ---------------------------------------
        stored = save_intelligence(
            owner_id,
            resolved_paper_id,
            intelligence,
            model=GROQ_MODEL,
            generated_at=generated_at,
        )

        # -- 9. return the persisted object -----------------------------
        return {
            "status": "success",
            "paper_id": resolved_paper_id,
            "paper": paper_title,
            "intelligence": stored.intelligence.model_dump(mode="json"),
            "generated_at": stored.generated_at.isoformat(),
            "model": stored.model,
            "schema_version": stored.schema_version,
            # True when a newer generation won the race and this result
            # was discarded. The object returned is the stored one either
            # way, so the client always renders what is actually current.
            "superseded": stored.superseded,
            "chunks_used": len(selected),
            "pages_covered": len({page for page, _ in allowed_evidence}),
        }

    except IntelligenceValidationError as e:
        # The rejection path, and the one that matters most: a model
        # response that failed validation is NOT persisted, NOT repaired
        # and NOT returned. The message is authored in
        # intelligence_schema.py — never the model's text.
        print(
            f"[paper-intelligence] rejected generation code={e.code} "
            f"paper={paper_id} allowlist={len(allowed_evidence)} "
            f"total_pages={total_pages} response_chars={len(raw)}"
        )
        return {"status": "error", "code": e.code, "message": e.message}

    except PaperNotOwned:
        # The paper disappeared, or changed hands, between resolution and
        # persistence. Same neutral response as any other miss.
        return {"status": "error", "message": NOT_FOUND_MESSAGE}

    except UsageLimitError:
        # Not an application failure: it must reach the handler in
        # app/main.py as a normalized 429/503 rather than being flattened
        # into this endpoint's 200 error shape.
        raise

    except Exception as e:
        print(f"Paper Intelligence Error: {str(e)}")

        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        return internal_error_payload()
