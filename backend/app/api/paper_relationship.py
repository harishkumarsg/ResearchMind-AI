"""
GET/POST /paper-relationship — how TWO of one owner's papers relate.

The generation pipeline, in the order it must happen:

    verified owner
  → both papers resolved exactly, owner-scoped (from `papers`)
  → canonical pair (paper_a_id < paper_b_id)
  → BOTH papers' stored paper_intelligence, or stop
  → a fresh stored relationship short-circuits here, with no provider call
  → deterministic comparable sections (both sides grounded)
  → rehydrate ONLY the exact cited chunks, per paper, owner+paper filtered
  → context and TWO evidence allowlists built from exactly what survived
  → one Groq generation
  → strict JSON parse
  → validate_relationship() against those two allowlists
  → re-check the source stamps did not move during generation
  → save_relationship(), and only then

WHY THE PAPERS ARE NEVER NAMED IN THE PROMPT
--------------------------------------------
The model sees "PAPER A" and "PAPER B" and their evidence text. It does
not see either title, either id, or either paper's `summary` prose. That
is deliberate and it is the defence against the failure this feature is
most prone to: a model shown two similar titles will confidently report
that two papers agree without any evidence saying so. It cannot compare
titles it was never given.

For the same reason the input to rehydration is the bare `(page,
chunk_id)` references, never the PaperIntelligence object — see
services/paper_relationship_evidence.py, which cannot return model prose
because it never receives any.

WHY THE ALLOWLISTS ARE BUILT FROM THE SURVIVING CHUNKS
-----------------------------------------------------
Not from what the stored intelligence cited. A reference can outlive the
chunk it points at, and a section can be dropped because one side's
evidence no longer rehydrates. Any chunk the model did not actually see
is removed from the allowlist as well as from the context, because an
allowlist wider than the context is precisely the gap a plausible-looking
invented citation passes through. Same rule as
api/paper_intelligence.py, one allowlist per paper instead of one.

THE TWO ALLOWLISTS ARE NEVER MERGED
-----------------------------------
`cites_a` is checked against Paper A's allowlist and nothing else. Page 3
chunk 0 exists in almost every paper, so a merged namespace would let a
citation of B's page 3 silently validate as evidence for A —
misattributed evidence that looks perfectly well-formed.

WHAT IS NOT TRUSTED
-------------------
owner_id comes only from the verified JWT, via the usage-guard
dependencies. There is no owner_id parameter on either route, so a client
cannot supply one. Both paper ids ARE client-supplied and are resolved
against `papers` before being used anywhere — including in the Qdrant
filter, which is built from the RESOLVED ids. The model is trusted for
the relation, the statement, and references into the context it was
given, and for nothing else; relationship_schema.py decides what
survives.

STATUS CODES
------------
This endpoint returns real HTTP status codes for application outcomes —
404, 409, 422 — which is a deliberate departure from
api/paper_intelligence.py's 200-with-an-error-body style. It has no
frontend yet (Step 2C-6), so there is no existing client contract to
preserve, and a stale-conflict that arrived as 200 would be easy for a
future caller to treat as success.

Provider failures keep the ESTABLISHED shape instead: 200 with the
neutral `code` from classify_provider_error(). That contract is already
recognised by the frontend's provider-error handling across five
endpoints, and a new endpoint inventing a second one would split it.
"""
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from fastapi import APIRouter, Depends, Response, status

from app.agents.qa_agent import GROQ_MODEL, REFUSAL, generate
from app.api.paper_file import resolve_owned_paper
from app.core.limits import UsageLimitError
from app.core.providers import classify_provider_error, internal_error_payload
from app.core.usage_guard import (
    charge_ai_unit,
    guard_cheap_read,
    precheck_ai_generation,
)
from app.db.session import session_scope
from app.services.intelligence_schema import SECTION_NAMES, STATUS_ANSWERED
from app.services.paper_intelligence_store import PaperNotOwned, get_intelligence
from app.services.paper_relationship_evidence import (
    EVIDENCE_CONTEXT_CHARS,
    PaperEvidence,
    ResolvedEvidence,
    references_from_intelligence,
    rehydrate_paper_evidence,
)
from app.services.paper_relationship_store import (
    canonical_pair,
    get_relationship,
    relationship_is_stale,
    save_relationship,
)
from app.services.relationship_schema import (
    RELATIONS,
    RelationshipValidationError,
    validate_relationship,
)

router = APIRouter()

#: Per-paper source-text budget. The evidence service's own default, which
#: is itself the proven INTELLIGENCE_CONTEXT_CHARS (12000). Named here so
#: the budget this endpoint applies is visible at the call site, and
#: deliberately NOT a new number — a second constant would be free to
#: drift away from the figure already proven against GROQ_TIMEOUT_SECONDS.
RELATIONSHIP_CONTEXT_CHARS_PER_PAPER = EVIDENCE_CONTEXT_CHARS

#: Completion budget. Held at /paper-intelligence's proven 3000 rather
#: than raised: a relationship object covers only the comparable sections
#: and carries no `quote` field, so it is strictly smaller than the
#: ten-section object that number was proven against.
RELATIONSHIP_MAX_TOKENS = 3000

#: One authored message for every miss — unknown id, malformed id, and
#: another owner's id alike. Matching /paper-file and /paper-intelligence:
#: the wording must not let a caller probe whether some other user holds a
#: given id. Reaching any other message below REQUIRES owning both papers.
NOT_FOUND_MESSAGE = "Paper not found."

#: A pair of one paper. Safe to distinguish from NOT_FOUND because it is
#: only reachable once BOTH ids resolved to this owner's own papers.
SAME_PAPER_CODE = "same_paper"
SAME_PAPER_MESSAGE = "A paper cannot be compared with itself."

#: Both papers are this owner's, but no relationship has been generated
#: for the pair. Distinct from NOT_FOUND for the same reason: an attacker
#: probing a foreign pair never reaches this branch.
NOT_GENERATED_CODE = "relationship_not_generated"
NOT_GENERATED_MESSAGE = (
    "No relationship has been generated for these two papers yet."
)

#: One or both papers have no stored analysis, so there are no grounded
#: claims to relate. A deterministic, actionable state — analyse the
#: papers first — and never a reason to call the model.
MISSING_INTELLIGENCE_CODE = "source_intelligence_missing"
MISSING_INTELLIGENCE_MESSAGE = (
    "Both papers must be analysed before they can be related. "
    "Generate the analysis for each paper and try again."
)

#: Both papers are analysed, but no section has a grounded claim on BOTH
#: sides with evidence that still rehydrates. Asking the model anyway
#: would be asking it to invent the missing half.
NOT_COMPARABLE_CODE = "no_comparable_sections"
NOT_COMPARABLE_MESSAGE = (
    "These papers have no section with grounded evidence on both sides, "
    "so there is nothing to relate."
)

#: A source analysis was regenerated while this generation was running, so
#: the relationship just produced describes claims that no longer exist.
#: Reported rather than written: overwriting here would be exactly the
#: last-writer-wins behaviour the store's staleness guard exists to avoid.
STALE_SOURCE_CODE = "source_intelligence_changed"
STALE_SOURCE_MESSAGE = (
    "One of the papers was re-analysed while this comparison was running. "
    "Please try again."
)

#: The stored row exists but no longer parses — a schema_version drift or
#: a hand-edited row. Authored here; the stored text is never rendered.
UNREADABLE_MESSAGE = (
    "The stored relationship for these papers could not be read. "
    "Generating it again will replace it."
)

#: A generation that produced nothing usable. Authored here; the model's
#: own output is never rendered to a client.
GENERATION_FAILED_CODE = "relationship_generation_failed"
GENERATION_FAILED_MESSAGE = (
    "The comparison could not be completed. Please try again."
)

#: Machine-readable staleness reasons, for a caller that must distinguish
#: "the analysis moved" from "the analysis is gone". Both mean the stored
#: relationship no longer describes the papers; only the second means
#: there is nothing to regenerate from.
STALE_REASON_CHANGED = "source_intelligence_changed"
STALE_REASON_MISSING = "source_intelligence_missing"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    """A stored timestamp as an unambiguous UTC ISO-8601 string.

    Not a bare `.isoformat()`. SQLite reads timestamptz values back NAIVE
    while Postgres returns them aware, so isoformat() alone emits
    "...T10:00:00" through one driver and "...T10:00:00+00:00" through the
    other — the same instant, but a client parsing the first would have to
    guess the zone.

    Every value these rows hold is UTC, because the stores write nothing
    else, so a naive value is LABELLED rather than shifted. Same
    assumption and same reasoning as _as_utc() in
    paper_relationship_store.py.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# 1-2. Owner and papers
# ----------------------------------------------------------------------
def resolve_paper(owner_id: str, paper_id: str) -> Optional[Tuple[str, str]]:
    """This owner's paper with this exact id, as (paper_id, title).

    Delegates to /paper-file's resolver, the authoritative by-id lookup in
    this codebase: exact `papers.id` AND `papers.owner_id`, no substring
    match, no similarity, None for a malformed id. Same helper
    /paper-intelligence uses, so "which paper is this" has one answer
    across the feature.
    """
    with session_scope(owner_id) as db:
        paper = resolve_owned_paper(db, owner_id, paper_id)
        if paper is None:
            return None
        # Read out inside the session; the row expires on commit.
        return str(paper.id), paper.title


def resolve_pair(
    owner_id: str, paper_a_id: str, paper_b_id: str
) -> Tuple[Optional[dict], Optional[dict]]:
    """Resolve both ids, then order them canonically.

    Returns (resolved, error). Exactly one is not None.

    OWNERSHIP IS CHECKED BEFORE ANYTHING ELSE, including before the
    same-paper check, and the same-paper check uses the RESOLVED ids. Two
    consequences, both wanted:

      * the same foreign id supplied twice answers 404, never 422, so the
        response cannot be used to learn that some id exists;
      * two spellings of one owned id (a differently-cased UUID, say)
        are still caught as a self-pair, because they resolve equal.

    This mirrors the ordering paper_relationship_store.save_relationship
    settled on: a paper that is not the caller's must always surface as
    "not found", never as a different error decided by how the two ids
    happened to sort.
    """
    first = resolve_paper(owner_id, paper_a_id)
    if first is None:
        return None, {"http": status.HTTP_404_NOT_FOUND, "message": NOT_FOUND_MESSAGE}

    second = resolve_paper(owner_id, paper_b_id)
    if second is None:
        return None, {"http": status.HTTP_404_NOT_FOUND, "message": NOT_FOUND_MESSAGE}

    if first[0] == second[0]:
        return None, {
            "http": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "code": SAME_PAPER_CODE,
            "message": SAME_PAPER_MESSAGE,
        }

    # canonical_pair() is the single place pair ordering is decided, and
    # its uuid comparison is the same byte-wise order the CHECK constraint
    # in migration 0006 uses.
    left, right = canonical_pair(first[0], second[0])

    titles = {first[0]: first[1], second[0]: second[1]}

    return (
        {
            "paper_a_id": str(left),
            "paper_b_id": str(right),
            # Kept for logging and for a future UI that shows the pair in
            # the reader's own order. Never sent to the model.
            "paper_a_title": titles[str(left)],
            "paper_b_title": titles[str(right)],
        },
        None,
    )


# ----------------------------------------------------------------------
# 3. The deterministic gate — which sections are even comparable
# ----------------------------------------------------------------------
def _usable_reference(item: object) -> bool:
    """Does this evidence item carry the (page, chunk_id) pair?

    Booleans are excluded explicitly: in Python `True` IS an int, so
    without this a stray `True` would count as page 1 — a nonsense value
    wearing a plausible disguise.
    """
    if not isinstance(item, Mapping):
        return False
    page, chunk_id = item.get("page"), item.get("chunk_id")
    if isinstance(page, bool) or isinstance(chunk_id, bool):
        return False
    if not isinstance(page, int) or not isinstance(chunk_id, int):
        return False
    return page >= 1 and chunk_id >= 0


def is_grounded(section: object) -> bool:
    """Does this side hold a grounded claim for the section?

    The SAME rule as the frontend deterministic engine's `isGrounded`
    (lib/paper-relationships.ts), restated here because this is the gate
    that decides whether a generation happens at all and it must not
    depend on a client agreeing:

      * `not_specified` is not grounded. The paper explicitly says
        nothing, which is a correct answer but not something to compare.
      * claiming `answered` is not enough — the summary and the evidence
        both have to be there.
      * ONE unusable evidence item makes the whole section ungrounded,
        rather than partially trusted.

    Corrupt data therefore degrades toward "not comparable", never toward
    "comparable".
    """
    if not isinstance(section, Mapping):
        return False
    if section.get("status") != STATUS_ANSWERED:
        return False

    summary = section.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False

    evidence = section.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False

    return all(_usable_reference(item) for item in evidence)


def comparable_sections(
    intelligence_a: Mapping[str, object], intelligence_b: Mapping[str, object]
) -> Tuple[str, ...]:
    """The canonical sections where BOTH papers have a grounded claim.

    Deterministic and provider-free: a fact about stored data, computed
    the same way every time, in canonical section order. The model is
    never asked about a section where one paper said nothing, because the
    only way to answer would be to invent the missing half.

    This is the backend's own copy of the deterministic verdict
    `comparable`; the other three verdicts (a_only, b_only, neither) are
    not needed here — they all mean "do not generate".
    """
    return tuple(
        name
        for name in SECTION_NAMES
        if is_grounded(intelligence_a.get(name)) and is_grounded(intelligence_b.get(name))
    )


# ----------------------------------------------------------------------
# 4. Evidence — exact cited chunks only
# ----------------------------------------------------------------------
def rehydrate_for_sections(
    owner_id: str,
    paper_id: str,
    intelligence: Mapping[str, object],
    sections: Sequence[str],
) -> PaperEvidence:
    """One paper's cited chunks for the named sections, and nothing else.

    Exact `(owner_id, paper_id, page, chunk_id)` lookups through
    services/paper_relationship_evidence.py: a Qdrant scroll under a
    mandatory owner+paper filter, with no query vector. Therefore no
    embedding call, no Voyage, no reranker, and no semantic search — a
    reference that no longer resolves stays missing rather than being
    replaced by the nearest thing.
    """
    references = references_from_intelligence(intelligence, sections)
    return rehydrate_paper_evidence(
        owner_id,
        paper_id,
        references,
        budget_chars=RELATIONSHIP_CONTEXT_CHARS_PER_PAPER,
    )


def sections_with_evidence(evidence: PaperEvidence) -> Set[str]:
    """The sections this paper has at least one RESOLVED chunk for.

    Built from `found` only. A section whose every citation went missing
    at re-index has no surviving evidence, so it is not something the
    model can be asked to relate.
    """
    names: Set[str] = set()
    for item in evidence.found:
        names.update(item.sections)
    return names


def usable_evidence(
    evidence: PaperEvidence, sections: Sequence[str]
) -> List[ResolvedEvidence]:
    """Resolved chunks that support at least one of the FINAL sections.

    The narrowing that keeps the context and the allowlist identical. A
    chunk cited only by a section that got dropped is removed from both
    at once: the model must not see a section name it was not asked about
    (it would be rejected as unknown_section), and it must not be able to
    cite a block that is not in front of it.
    """
    wanted = set(sections)
    keep: List[ResolvedEvidence] = []

    for item in evidence.found:
        if wanted.intersection(item.sections):
            keep.append(item)

    return keep


def build_allowlist(items: Sequence[ResolvedEvidence]) -> Dict[Tuple[int, int], str]:
    """(page, chunk_id) -> chunk text, for ONE paper.

    Keyed on the pair that is this system's evidence identity. Built from
    the same list that renders the context, so the two cannot disagree.
    """
    return {(item.page, item.chunk_id): (item.text or "") for item in items}


def render_evidence_block(
    label: str, items: Sequence[ResolvedEvidence], sections: Sequence[str]
) -> str:
    """One paper's evidence, headed by the citations the model must use.

    The `[page=N chunk=M]` header is the whole contract, identical in form
    to /paper-intelligence's: the model is told to cite a block by copying
    the numbers it sees there. The `(cited for: ...)` line carries section
    provenance so the model knows which claim a passage supports; it is
    informational, and the prompt is explicit that citations come from the
    bracket header only.

    Section names are filtered to the final comparable set, so no name the
    model was not asked about appears anywhere in its input.
    """
    wanted = set(sections)
    blocks = [f"=== PAPER {label} EVIDENCE ==="]

    for item in items:
        cited = ", ".join(name for name in item.sections if name in wanted)
        blocks.append(
            f"[page={item.page} chunk={item.chunk_id}]\n"
            f"(cited for: {cited})\n"
            f"{item.text or ''}"
        )

    return "\n\n".join(blocks)


def build_context(
    items_a: Sequence[ResolvedEvidence],
    items_b: Sequence[ResolvedEvidence],
    sections: Sequence[str],
) -> str:
    """The whole model input: the section list, then both papers' evidence.

    Paper A's blocks and Paper B's blocks are in separate, labelled
    sections and are never interleaved, because which paper a `(page,
    chunk_id)` belongs to is carried entirely by which block it sits
    under.
    """
    wanted = "\n".join(f"  - {name}" for name in sections)

    return (
        f"SECTIONS TO RELATE:\n{wanted}\n\n"
        f"{render_evidence_block('A', items_a, sections)}\n\n"
        f"{render_evidence_block('B', items_b, sections)}"
    )


# ----------------------------------------------------------------------
# 5. The prompt
# ----------------------------------------------------------------------
_RELATION_LIST = ", ".join(f'"{name}"' for name in RELATIONS)

RELATIONSHIP_SYSTEM_PROMPT = f"""
You are ResearchMind AI, judging how the claims of TWO research papers relate.

Return ONLY a single JSON object. No explanation before or after it. At
most one ``` fence around the whole object is tolerated; anything else is
rejected.

The object must have EXACTLY one key, "sections", and no others:

{{
  "sections": {{
    "<section name>": {{
      "relation": one of {_RELATION_LIST},
      "statement": a string,
      "cites_a": [ {{"page": <integer>, "chunk_id": <integer>}} ],
      "cites_b": [ {{"page": <integer>, "chunk_id": <integer>}} ]
    }}
  }}
}}

WHAT THE FOUR RELATIONS MEAN:

- "aligned": evidence from BOTH papers supports compatible approaches or
  findings.
- "divergent": evidence from BOTH papers indicates materially different
  or conflicting approaches or findings.
- "complementary": evidence from BOTH papers addresses related aspects
  that add to each other, rather than directly agreeing or conflicting.
- "not_comparable": the supplied evidence is insufficient to establish
  how the two papers' claims relate.

RULES:

1. Use ONLY the supplied evidence blocks. Never use outside knowledge.
2. Every block begins with a header like [page=3 chunk=1]. To cite that
   block, use page 3 and chunk_id 1, as integers. The "(cited for: ...)"
   line is context only — never cite from it.
3. cites_a may ONLY reference blocks listed under PAPER A EVIDENCE.
   cites_b may ONLY reference blocks listed under PAPER B EVIDENCE.
   Never cite one paper's block as evidence for the other.
4. Cite ONLY blocks that actually appear above. Never invent a page
   number or a chunk id, and never adjust one to look plausible.
5. "aligned", "divergent" and "complementary" EACH require at least one
   citation in cites_a AND at least one in cites_b. A relationship
   resting on one paper is not a relationship between two.
6. "not_comparable" requires cites_a and cites_b to both be EMPTY.
7. Include EXACTLY the section names listed under SECTIONS TO RELATE —
   every one of them, and no others.
8. Keep each statement to 1-3 sentences, describing how the two papers'
   claims relate.
9. Add no field beyond relation, statement, cites_a and cites_b, and no
   key beyond "sections" at the top level. Do NOT include a "quote"
   field.
10. Do NOT infer a relationship because the papers share a topic, use
    similar wording, or sound similar. You are not shown either paper's
    title or summary, and you must not guess at them. If the supplied
    evidence does not establish how the claims relate, the correct answer
    is "not_comparable".
11. "not_comparable" is a correct, expected answer. Never force a
    relationship in order to avoid it.
"""

RELATIONSHIP_QUESTION = (
    "Produce the structured JSON relationship object for these two papers, "
    "following every rule above exactly."
)


# ----------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------
def _stale_reason(
    current_a: Optional[datetime], current_b: Optional[datetime]
) -> str:
    """Which kind of staleness this is.

    MISSING when a source analysis is gone entirely — there is nothing to
    regenerate from, so a caller must not offer a retry that would fail.
    CHANGED when both are present but one moved.
    """
    if current_a is None or current_b is None:
        return STALE_REASON_MISSING
    return STALE_REASON_CHANGED


def _relationship_payload(
    stored,
    *,
    stale: bool,
    stale_reason: Optional[str],
    extra: Optional[dict] = None,
) -> dict:
    """The one response shape both routes return on success.

    Both source stamps are reported alongside the relationship's own
    `generated_at`, because they answer different questions: the source
    stamps say which analyses were compared, `generated_at` says when the
    comparison ran. A caller cannot derive either from the other.
    """
    payload = {
        "status": "success",
        "paper_a_id": str(stored.paper_a_id),
        "paper_b_id": str(stored.paper_b_id),
        "relationship": stored.relationship.model_dump(mode="json"),
        "paper_a_generated_at": _iso(stored.paper_a_generated_at),
        "paper_b_generated_at": _iso(stored.paper_b_generated_at),
        "generated_at": _iso(stored.generated_at),
        "model": stored.model,
        "schema_version": stored.schema_version,
        # Explicit and machine-readable. The endpoint never regenerates on
        # the strength of this flag — reporting it is the whole behaviour.
        "stale": stale,
        "stale_reason": stale_reason,
        # True when a newer generation won the race and this result was
        # discarded. The object returned is the stored one either way.
        "superseded": stored.superseded,
    }

    if extra:
        payload.update(extra)

    return payload


def _source_stamps(
    owner_id: str, paper_a_id: str, paper_b_id: str
) -> Tuple[Optional[datetime], Optional[datetime], Optional[object], Optional[object]]:
    """Both papers' CURRENT stored intelligence, in canonical orientation.

    Returns (stamp_a, stamp_b, stored_a, stored_b); a stamp is None when
    that paper has no analysis. Owner-scoped by construction:
    get_intelligence filters on owner_id and paper_id together.
    """
    stored_a = get_intelligence(owner_id, paper_a_id)
    stored_b = get_intelligence(owner_id, paper_b_id)

    return (
        stored_a.generated_at if stored_a is not None else None,
        stored_b.generated_at if stored_b is not None else None,
        stored_a,
        stored_b,
    )


# ----------------------------------------------------------------------
# Read endpoint
# ----------------------------------------------------------------------
@router.get("/paper-relationship")
def read_paper_relationship(
    paper_a_id: str,
    paper_b_id: str,
    response: Response,
    owner_id: str = Depends(guard_cheap_read),
):
    """The stored relationship for this owner's pair.

    DATABASE ONLY. No Groq, no Voyage, no Qdrant, no Storage — reading a
    relationship that already exists must never cost a provider call, and
    a read path that could generate would turn every page load into a
    billable request. It NEVER generates, whatever the staleness says.

    CHEAP_READ rather than AI_GENERATION: a burst-guarded read with no
    daily allowance, exactly like GET /paper-intelligence.

    Outcomes:

      404 "Paper not found."   — either paper is unknown, malformed, or
            someone else's. One response for all three, so the wording
            cannot probe whether another user holds a given id.
      422 same_paper           — both ids resolved to one owned paper.
      404 relationship_not_generated — both papers ARE this owner's, but
            nothing has been generated for the pair. Safe to distinguish:
            reaching this branch requires owning both.
      200 success             — the stored object, with `stale` and
            `stale_reason` computed against the papers' current analyses.

    owner_id is the verified JWT `sub` from guard_cheap_read. A client
    cannot supply it: there is no owner_id parameter on this route.
    """
    try:
        resolved, error = resolve_pair(owner_id, paper_a_id, paper_b_id)

        if error is not None:
            response.status_code = error["http"]
            return {
                "status": "error",
                **{k: v for k, v in error.items() if k != "http"},
            }

        left, right = resolved["paper_a_id"], resolved["paper_b_id"]

        # Owner-scoped by construction: get_relationship filters on
        # owner_id and both paper ids, so another owner's row cannot match.
        stored = get_relationship(owner_id, left, right)

        if stored is None:
            response.status_code = status.HTTP_404_NOT_FOUND
            return {
                "status": "error",
                "code": NOT_GENERATED_CODE,
                "message": NOT_GENERATED_MESSAGE,
                "paper_a_id": left,
                "paper_b_id": right,
            }

        # Staleness is DETECTED, never acted on. If a source analysis has
        # moved or vanished, the stored relationship is reported as stale
        # and returned unchanged — silently regenerating here would make a
        # read billable, and inventing a relationship for a paper whose
        # analysis is gone is exactly what must not happen.
        current_a, current_b, _, _ = _source_stamps(owner_id, left, right)
        stale = relationship_is_stale(stored, current_a, current_b)

        return _relationship_payload(
            stored,
            stale=stale,
            stale_reason=_stale_reason(current_a, current_b) if stale else None,
        )

    except RelationshipValidationError as e:
        # The stored JSON no longer matches the schema. Reported as a
        # readable state rather than a 500, and the row is left alone —
        # repairing it here would be the silent rewrite the validator
        # exists to prevent.
        print(
            f"[paper-relationship] unreadable stored row code={e.code} "
            f"pair={paper_a_id}/{paper_b_id}"
        )
        return {"status": "error", "code": e.code, "message": UNREADABLE_MESSAGE}

    except PaperNotOwned:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"status": "error", "message": NOT_FOUND_MESSAGE}

    except UsageLimitError:
        raise

    except Exception as e:
        print(f"Paper Relationship Read Error: {str(e)}")

        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            return provider_failure.to_payload()

        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return internal_error_payload()


# ----------------------------------------------------------------------
# Generation endpoint
# ----------------------------------------------------------------------
@router.post("/paper-relationship")
def paper_relationship(
    paper_a_id: str,
    paper_b_id: str,
    response: Response,
    owner_id: str = Depends(precheck_ai_generation),
):
    """Generate and persist the relationship for this owner's pair.

    Charges AI_GENERATION exactly once, immediately before the single Groq
    call, and only on a path that actually reaches it. Every earlier
    return — unknown paper, self-pair, missing analysis, nothing
    comparable, a fresh relationship already stored — costs no unit. The
    precheck dependency has already applied the burst guard and confirmed
    the allowance without consuming it.
    """
    # Diagnostic-only, and bound BEFORE the try so the rejection handler
    # can always log, even if the error is raised earlier than expected.
    # These never reach the client; they appear only in the log line.
    allowed_a: Dict[Tuple[int, int], str] = {}
    allowed_b: Dict[Tuple[int, int], str] = {}
    sections: Tuple[str, ...] = ()
    raw = ""

    try:
        # -- 1-2. both papers, owner-scoped, then canonical order --------
        resolved, error = resolve_pair(owner_id, paper_a_id, paper_b_id)

        if error is not None:
            response.status_code = error["http"]
            return {
                "status": "error",
                **{k: v for k, v in error.items() if k != "http"},
            }

        left, right = resolved["paper_a_id"], resolved["paper_b_id"]

        # -- 3. BOTH papers' stored analyses, or stop --------------------
        stamp_a, stamp_b, stored_a, stored_b = _source_stamps(owner_id, left, right)

        if stored_a is None or stored_b is None:
            # Deterministic and non-provider: no Groq, no quota, no write.
            response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            return {
                "status": "error",
                "code": MISSING_INTELLIGENCE_CODE,
                "message": MISSING_INTELLIGENCE_MESSAGE,
                "paper_a_id": left,
                "paper_b_id": right,
                # Which side is missing is safe to report: both papers are
                # already known to be this owner's.
                "paper_a_analysed": stored_a is not None,
                "paper_b_analysed": stored_b is not None,
            }

        # -- 4. a fresh stored relationship short-circuits --------------
        existing = get_relationship(owner_id, left, right)

        if existing is not None and not relationship_is_stale(existing, stamp_a, stamp_b):
            # Already current for exactly these two analyses. Returning it
            # is not a cache optimisation — regenerating would bill a unit
            # and could produce a different interpretation of identical
            # inputs.
            return _relationship_payload(
                existing,
                stale=False,
                stale_reason=None,
                extra={"regenerated": False},
            )

        # -- 5. the deterministic gate ----------------------------------
        intelligence_a = stored_a.intelligence.model_dump(mode="json")
        intelligence_b = stored_b.intelligence.model_dump(mode="json")

        candidate_sections = comparable_sections(intelligence_a, intelligence_b)

        if not candidate_sections:
            response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            return {
                "status": "error",
                "code": NOT_COMPARABLE_CODE,
                "message": NOT_COMPARABLE_MESSAGE,
                "paper_a_id": left,
                "paper_b_id": right,
                "comparable_sections": [],
            }

        # -- 6. exact cited chunks only ---------------------------------
        evidence_a = rehydrate_for_sections(
            owner_id, left, intelligence_a, candidate_sections
        )
        evidence_b = rehydrate_for_sections(
            owner_id, right, intelligence_b, candidate_sections
        )

        # A section survives only if BOTH sides still have real text for
        # it. Every grounded relation requires evidence from both papers,
        # so a section with nothing left on one side could only be
        # answered not_comparable — or, worse, answered with a citation
        # the model had to invent.
        available_a = sections_with_evidence(evidence_a)
        available_b = sections_with_evidence(evidence_b)
        sections = tuple(
            name
            for name in candidate_sections
            if name in available_a and name in available_b
        )

        if not sections:
            response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            return {
                "status": "error",
                "code": NOT_COMPARABLE_CODE,
                "message": NOT_COMPARABLE_MESSAGE,
                "paper_a_id": left,
                "paper_b_id": right,
                "comparable_sections": [],
            }

        # -- 7. context and the TWO allowlists, from the SAME lists ------
        items_a = usable_evidence(evidence_a, sections)
        items_b = usable_evidence(evidence_b, sections)

        allowed_a = build_allowlist(items_a)
        allowed_b = build_allowlist(items_b)

        context = build_context(items_a, items_b, sections)

        # Stamped at RUN START, before the provider call, because
        # save_relationship() orders concurrent regenerations by this
        # value: a slow run that started earlier must not overwrite a
        # faster one that started later.
        generated_at = _utcnow()

        # -- 8. generate ------------------------------------------------
        # Last safe point: the next statement is the provider call.
        charge_ai_unit(owner_id)

        result = generate(
            RELATIONSHIP_QUESTION,
            context,
            system_prompt=RELATIONSHIP_SYSTEM_PROMPT,
            # None, not a number: the context above is already budgeted
            # per paper, and a second structure-blind cut here is what
            # truncated Compare's whole second paper once before.
            max_context_chars=None,
            max_tokens=RELATIONSHIP_MAX_TOKENS,
        )

        raw = result.text

        if not raw or raw == REFUSAL:
            # generate() returns REFUSAL for an empty completion and for an
            # exception it could not classify. Neither is JSON, and letting
            # it fall through would report a provider problem as "the model
            # returned malformed JSON".
            print(
                f"[paper-relationship] empty generation pair={left}/{right} "
                f"sections={len(sections)} finish_reason={result.finish_reason}"
            )
            return {
                "status": "error",
                "code": GENERATION_FAILED_CODE,
                "message": GENERATION_FAILED_MESSAGE,
            }

        # -- 9. strict parse + validation against THESE two allowlists ---
        # Raises RelationshipValidationError on anything that must not be
        # persisted. Nothing below this line runs if it does.
        relationship = validate_relationship(
            raw,
            comparable_sections=sections,
            allowed_evidence_a=allowed_a,
            allowed_evidence_b=allowed_b,
        )

        # -- 10. did the ground move while we were generating? -----------
        # Re-read the source stamps. If either analysis was regenerated
        # during the model call, the object just produced describes claims
        # that no longer exist, and writing it would persist an analysis of
        # superseded inputs. Refuse instead of overwriting: this is the
        # deliberate opposite of last-writer-wins.
        fresh_a, fresh_b, _, _ = _source_stamps(owner_id, left, right)

        if fresh_a != stamp_a or fresh_b != stamp_b:
            print(
                f"[paper-relationship] source moved during generation "
                f"pair={left}/{right}"
            )
            response.status_code = status.HTTP_409_CONFLICT
            return {
                "status": "error",
                "code": STALE_SOURCE_CODE,
                "message": STALE_SOURCE_MESSAGE,
                "paper_a_id": left,
                "paper_b_id": right,
            }

        # -- 11. persist, only now --------------------------------------
        # The pair is already canonical, so save_relationship never has to
        # reorient content. The two source stamps come from the stored
        # paper_intelligence rows this generation actually read — never
        # from the relationship JSON, which has no field for them.
        stored = save_relationship(
            owner_id,
            left,
            right,
            relationship,
            paper_a_generated_at=stamp_a,
            paper_b_generated_at=stamp_b,
            model=GROQ_MODEL,
            generated_at=generated_at,
        )

        return _relationship_payload(
            stored,
            stale=False,
            stale_reason=None,
            extra={
                "regenerated": True,
                "comparable_sections": list(sections),
                "evidence_used": {
                    "paper_a": len(items_a),
                    "paper_b": len(items_b),
                },
            },
        )

    except RelationshipValidationError as e:
        # The rejection path, and the one that matters most: a model
        # response that failed validation is NOT persisted, NOT repaired
        # and NOT returned. The message is authored in
        # relationship_schema.py — never the model's text.
        print(
            f"[paper-relationship] rejected generation code={e.code} "
            f"pair={paper_a_id}/{paper_b_id} sections={len(sections)} "
            f"allowlist_a={len(allowed_a)} allowlist_b={len(allowed_b)} "
            f"response_chars={len(raw)}"
        )
        return {"status": "error", "code": e.code, "message": e.message}

    except PaperNotOwned:
        # A paper disappeared, or changed hands, between resolution and
        # persistence. Same neutral response as any other miss.
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"status": "error", "message": NOT_FOUND_MESSAGE}

    except UsageLimitError:
        # Not an application failure: it must reach the handler in
        # app/main.py as a normalized 429/503 rather than being flattened
        # into this endpoint's error shape.
        raise

    except Exception as e:
        print(f"Paper Relationship Error: {str(e)}")

        provider_failure = classify_provider_error(e)
        if provider_failure is not None:
            # Established contract: 200 with a neutral code. Nothing was
            # persisted — the provider call happens before validation and
            # before the write.
            return provider_failure.to_payload()

        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return internal_error_payload()
