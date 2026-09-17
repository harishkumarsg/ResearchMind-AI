from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
import json
import os
import re
import time

from groq import Groq
from dotenv import load_dotenv
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.limits import AI_GENERATION, UsageLimitError
from app.core.providers import (
    ProviderTimeout,
    classify_provider_error,
    groq_client_options,
    groq_stream_deadline_seconds,
)
from app.core.quota import charge as charge_quota
from app.core.usage_guard import precheck_ai_generation
from app.rag.embedder import encode_query
from app.rag.vector_store import COLLECTION_NAME, client
from app.rag.reranker import rerank_results
from app.services.chat_store import (
    coerce_paper_id,
    load_chat_state,
    persist_assistant_turn,
    persist_user_turn,
)

load_dotenv()
# Explicit timeouts and a bounded retry count; see app/core/providers.py.
_groq_client = Groq(api_key=os.environ["GROQ_API_KEY"], **groq_client_options())
GROQ_MODEL = "openai/gpt-oss-120b"

router = APIRouter()

SEARCH_LIMIT = 8
TOP_CHUNKS = 4
MAX_CONTEXT = 4000
EVIDENCE_SEPARATOR = "\n\n"

# Matches the page locators the model is instructed to emit. Deliberately
# bracket-tolerant: models frequently substitute full-width CJK brackets
# (【 】) or parentheses for the ASCII form asked for in the prompt, and a
# strict r"\[Page (\d+)\]" would then match nothing and mark every piece of
# evidence uncited. A bracket of SOME kind is still required — matching a
# bare "Page 6" would false-positive on passage text the answer quotes.
CITED_PAGE_PATTERN = re.compile(r"[\[\(【]\s*Page\s*(\d+)\s*[\]\)】]", re.IGNORECASE)

FOLLOW_UP_WORDS = [
    "it", "they", "them", "this", "that", "these", "those",
    "its", "their", "algorithm", "hardware", "method", "approach",
    "system", "work", "works", "used", "use", "implementation",
    "architecture", "dataset", "results", "accuracy", "performance",
    "advantages", "limitations", "future scope", "future work",
]

# The one configured refusal string. Defined once and interpolated into
# the prompt below so the text the model is told to emit and the text the
# citation-suppression check compares against can never drift apart.
NO_ANSWER_RESPONSE = "I could not find that information in the indexed papers."

SYSTEM_PROMPT = f"""
You are ResearchMind AI.

You are an academic research assistant.

RULES:

1. Use ONLY the provided context.
2. Never use external knowledge.
3. Never hallucinate facts.
4. If information is unavailable, respond exactly:

{NO_ANSWER_RESPONSE}

5. Keep answers concise.
6. Use bullet points when appropriate.
7. Limit answers to 3-8 sentences unless explicitly asked for details.
8. Return only the answer.

CITATIONS:

9. Every passage in the context is preceded by its page label, e.g. [Page 61].
10. Cite ONLY those [Page N] labels, exactly as they appear in the context.
11. Never invent or infer a page number that is not present in the context.
12. Never cite a section number or section title as a location.
13. Passage text may itself mention things like "Section 4" or "see Section 7.3".
    Those are the paper's own internal cross-references, pointing at material
    you have NOT been given. Never repeat them as citations.
"""


def generate_sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _evidence_block(hit):
    """The exact passage text given to the model, or None for an empty chunk.

    Page-labelled so the model has a real, checkable locator. Without this
    it can only echo section numbers it finds in the prose, which point at
    material never retrieved.
    """
    text = (hit.payload.get("text") or "").strip()
    if not text:
        return None
    return f"[Page {hit.payload.get('page', '')}]\n{text}"


def _paper_key(hit):
    """Which paper a chunk belongs to. paper_id is unique; the title is only
    a fallback for a payload without one, and two papers sharing a title
    would then be treated as one."""
    payload = hit.payload or {}
    return payload.get("paper_id") or payload.get("paper") or ""


def _select_evidence(filtered):
    """Choose the exact passages given to the model, within MAX_CONTEXT.

    `filtered` is the ranked, already owner-filtered candidate list. Only
    those candidates are eligible: a paper can be represented only if one
    of its chunks already earned a place in the top TOP_CHUNKS on
    relevance, so nothing lower-ranked is ever pulled in to force breadth.

    Two passes over that same list, with the same fit check:

      1. Breadth — in rank order, take the best chunk of each distinct
         paper that fits. Without this, a few long chunks from the top
         paper can spend the whole budget before a second paper is
         reached, so a library-wide question answers from one paper.
      2. Depth — in rank order, fill the remaining budget with whatever
         else fits.

    The result is returned in original rank order, so the context, the
    citations and selected[0] (which becomes chat_sessions
    .current_paper_id) mean exactly what they meant before. With a single
    paper, pass 1 takes the first chunk that fits — the same chunk a
    single greedy pass takes first — and pass 2 then makes the same
    decisions, so single-paper selection is unchanged.
    """
    chosen = {}  # rank index -> (hit, block)
    seen_ids = set()
    represented = set()
    used_chars = 0

    def take(index, hit, block):
        nonlocal used_chars
        # The separator is charged to every block after the first, so the
        # running total equals the final joined length whatever order the
        # two passes happen to choose blocks in.
        cost = len(block) + (len(EVIDENCE_SEPARATOR) if chosen else 0)

        # Budget is applied BEFORE anything is committed, so a chunk that
        # does not fit is added to neither the context nor the citations.
        # It is skipped, and lower-ranked candidates are still considered —
        # a smaller one may fit.
        if used_chars + cost > MAX_CONTEXT:
            return False

        seen_ids.add(hit.id)
        chosen[index] = (hit, block)
        used_chars += cost
        return True

    # Dedupe on the Qdrant point id, which is genuinely unique.
    # payload["chunk_id"] is only the chunk's index WITHIN its page, so it
    # repeats on every page (values 0-3 across the whole corpus) and using
    # it silently discarded distinct passages from different pages.

    # Pass 1 — breadth: at most one chunk per distinct paper.
    for index, hit in enumerate(filtered):
        if hit.id in seen_ids:
            continue
        paper = _paper_key(hit)
        if paper in represented:
            continue
        block = _evidence_block(hit)
        if block is None:
            # An empty chunk never selects, so it never claims its paper's
            # breadth slot either; a later non-empty chunk still can.
            continue
        if take(index, hit, block):
            represented.add(paper)

    # Pass 2 — depth: fill whatever budget remains, still in rank order.
    for index, hit in enumerate(filtered):
        if index in chosen or hit.id in seen_ids:
            continue
        block = _evidence_block(hit)
        if block is None:
            continue
        take(index, hit, block)

    return [chosen[index] for index in sorted(chosen)]


@router.get("/ask-stream")
def ask_stream(question: str, owner_id: str = Depends(precheck_ai_generation)):

    def stream():

        try:
            # Durable conversation state, scoped to the authenticated
            # identity only — never a client-supplied session id — so this
            # user's conversation can never be read or overwritten by
            # another user.
            #
            # Read BEFORE this turn is persisted below, otherwise the
            # question being asked would show up in its own history.
            # Postgres is the sole source of truth for history, topic and
            # current paper, so this survives a restart.
            chat_state = load_chat_state(owner_id)

            question_lower = question.lower()

            # Follow-up detection
            is_followup = bool(chat_state.history) and any(
                word in question_lower for word in FOLLOW_UP_WORDS
            )

            search_query = question
            if is_followup and chat_state.current_topic:
                search_query = chat_state.current_topic + " " + question

            # Record the user's turn before any paid work happens, so a
            # database failure costs no Voyage embedding and no Groq
            # generation. The session opened here is committed and closed
            # before the stream below starts — nothing holds a pooled
            # connection across the LLM call.
            chat_session_id = persist_user_turn(owner_id, question)

            # Emit: searching
            yield generate_sse_event({"type": "status", "text": "Searching papers…"})

            # Charge the daily allowance HERE, not in the dependency: this
            # is the last moment before the first provider call, so a
            # database failure in load_chat_state/persist_user_turn above
            # costs the user nothing. The dependency already rejected the
            # ordinary "out of quota" case with a real HTTP 429; reaching
            # this point and failing means a concurrent request took the
            # last unit, and the response has already started streaming,
            # so the only honest channel left is an SSE error frame.
            try:
                charge_quota(owner_id, AI_GENERATION)
            except UsageLimitError as limit_error:
                yield generate_sse_event({
                    "type": "error",
                    "code": limit_error.code,
                    "text": limit_error.message,
                })
                return

            query_vector = encode_query(search_query)

            # owner_id filter is server-constructed from the verified JWT
            # identity above — never from any client-supplied value — and
            # is unconditionally merged into every query. There is no code
            # path here that lets a caller widen or omit this filter.
            owner_filter = Filter(
                must=[FieldCondition(key="owner_id", match=MatchValue(value=owner_id))]
            )

            results = client.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector,
                query_filter=owner_filter,
                limit=SEARCH_LIMIT,
            ).points

            if not results:
                yield generate_sse_event({
                    "type": "error",
                    "text": "No relevant papers found. Make sure you have indexed papers first."
                })
                return

            # Emit: reranking
            yield generate_sse_event({"type": "status", "text": "Reranking results…"})

            ranked = rerank_results(search_query, results)
            filtered = ranked[:TOP_CHUNKS]

            # Paper lock for follow-ups — and ONLY for follow-ups.
            #
            # Keyed on chat_sessions.current_paper_id — an id, not a
            # title, so two papers sharing a title cannot be conflated.
            # Both /ask-stream and /summarize-paper write that pointer,
            # so a summarise-then-follow-up locks to the summarised paper
            # and survives a restart.
            #
            # A NEW question keeps whatever the retriever ranked highest,
            # across every paper in the library. There used to be an
            # `elif` here that narrowed a new question's evidence to the
            # single paper owning the top hit; it meant a library-wide
            # question such as "what is the research about?" could never
            # mention a second paper, even when that paper's chunk had
            # been retrieved and ranked above other kept chunks. Retrieval
            # relevance now decides, not document identity.
            if is_followup and chat_state.current_paper_id is not None:
                locked = [
                    h for h in filtered
                    if coerce_paper_id(h.payload.get("paper_id"))
                    == chat_state.current_paper_id
                ]
                if locked:
                    filtered = locked

            # ----------------------------------------------------------
            # Evidence selection — the SINGLE source of truth.
            #
            # This one selection step (_select_evidence) produces
            # `selected`, and BOTH the context sent to the model and the
            # citations sent to the UI are derived from it. There is
            # deliberately no second, independent
            # citation-selection path: that divergence is exactly what
            # previously let Sources cards advertise passages the model
            # never saw, and let the model see passages no card showed.
            # ----------------------------------------------------------
            selected = _select_evidence(filtered)  # [(hit, block)] — the exact passages given to the LLM

            context = EVIDENCE_SEPARATOR.join(block for _, block in selected)

            # Already limited to the last HISTORY_MESSAGE_LIMIT messages
            # by the database query, in chronological order.
            history_text = "\n".join(chat_state.history)
            full_context = f"""Conversation History:\n{history_text}\n\nCurrent Retrieved Context:\n{context}"""

            user_prompt = f"CONTEXT\n\n{full_context}\n\n----------------------------------------\n\nQUESTION\n\n{question}\n\n----------------------------------------\n\nANSWER\n"

            # Emit: generating
            yield generate_sse_event({"type": "status", "text": "Generating answer…"})

            # Stream tokens from Groq
            answer_parts = []

            # End-to-end deadline, started before the request is made so the
            # time spent establishing the stream counts too. It is checked as
            # each chunk arrives, which is event-driven rather than polling:
            # a stream that keeps trickling in slowly still stops here, while
            # a single stalled read is separately bounded by the client's
            # read timeout. Worst case is therefore the deadline plus one
            # read timeout.
            stream_started = time.monotonic()
            stream_deadline = groq_stream_deadline_seconds()

            stream_response = _groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=512,
                stream=True,
            )

            try:
                for chunk in stream_response:
                    if time.monotonic() - stream_started > stream_deadline:
                        raise ProviderTimeout()
                    token = chunk.choices[0].delta.content
                    if token:
                        answer_parts.append(token)
                        yield generate_sse_event({"type": "token", "text": token})
            finally:
                # Always release the upstream connection: on the deadline,
                # on a read timeout, and if the client disconnects mid-answer
                # (which closes this generator). Harmless after a normal end.
                close_stream = getattr(stream_response, "close", None)
                if callable(close_stream):
                    close_stream()

            full_answer = "".join(answer_parts).strip()

            # Which pages did the answer actually cite? This only marks
            # evidence as used or unused — nothing is filtered out, so the
            # disclosed set stays exactly the set supplied to the model.
            #
            # Caveat, by design: matching is page-level, so when two
            # selected chunks share a cited page BOTH are marked cited.
            # Distinguishing them would require chunk-level identifiers in
            # the answer, which the model is not asked to produce.
            cited_pages = {int(n) for n in CITED_PAGE_PATTERN.findall(full_answer)}

            # Citations are a strict 1:1 projection of `selected` — the
            # exact passages supplied to the model above, in the same order.
            # Deliberately NOT deduped by (paper, page): two distinct
            # passages from one page are two pieces of evidence, and
            # collapsing them would under-report what the answer used.
            citations = []
            for h, _ in selected:
                page = h.payload.get("page", "")
                try:
                    is_cited = int(page) in cited_pages
                except (TypeError, ValueError):
                    is_cited = False
                citations.append(
                    {
                        "paper": h.payload.get("paper", ""),
                        "source": h.payload.get("source", ""),
                        "page": page,
                        "paper_id": h.payload.get("paper_id", ""),
                        "cited": is_cited,
                    }
                )

            # A refusal means the retrieved passages did NOT support an
            # answer. Presenting them as "Sources" would imply they did.
            if full_answer == NO_ANSWER_RESPONSE:
                citations = []

            # Record the assistant's turn and advance the session
            # pointers. A second short-lived transaction, opened only now
            # that the stream has finished.
            #
            # current_topic mirrors the in-memory rule below: a follow-up
            # keeps the existing topic, so None is passed to leave it be.
            # current_paper_id comes from the same `selected` entry whose
            # evidence the answer was built from.
            #
            # This runs BEFORE the in-memory updates so that a failure
            # leaves both stores agreeing that the turn did not happen,
            # rather than memory claiming a turn Postgres never got.
            persist_assistant_turn(
                chat_session_id,
                owner_id,
                full_answer,
                current_topic=None if is_followup else question,
                current_paper_id=(
                    coerce_paper_id(selected[0][0].payload.get("paper_id"))
                    if selected else None
                ),
            )

            # No in-process mirror of the conversation is kept any more.
            # History, current topic and current paper are all read from
            # and written to chat_sessions/chat_messages above, so a
            # second copy in UserMemory could only drift out of sync.

            # Emit: done with citations
            yield generate_sse_event({
                "type": "done",
                "citations": citations,
                "sources": citations,
                "is_followup": is_followup,
            })

        except Exception as e:
            # A provider failure — including the stream deadline above —
            # reaches the client as a neutral message with a code. The
            # assistant turn is never persisted on this path: persistence
            # only happens after the stream completes normally.
            provider_failure = classify_provider_error(e)
            if provider_failure is not None:
                yield generate_sse_event({
                    "type": "error",
                    "code": provider_failure.code,
                    "text": provider_failure.message,
                })
            else:
                yield generate_sse_event({"type": "error", "text": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
