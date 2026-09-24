"""
Cross-paper relationships — the structured schema and its validator.

Pure and provider-independent, exactly like intelligence_schema.py:
nothing here calls Groq, Voyage, Qdrant, Postgres or the network, and
nothing here knows what a request is. It takes a model's JSON output
plus TWO evidence allowlists the SERVER built — one per paper — and
either returns a normalized object or raises.

WHAT THIS LAYER IS FOR, AND WHAT IT IS NOT
------------------------------------------
The deterministic engine (frontend: lib/paper-relationships.ts) already
answers "does each paper have a grounded claim for this section?" with
certainty, and its verdicts — comparable / a_only / b_only / neither —
are facts about stored data. Those verdicts are NOT repeated here.

This layer covers the different, genuinely interpretive question: GIVEN
that both papers made a grounded claim, how do those claims relate? That
is a semantic judgement, so it is the model's, and it is labelled as an
interpretation everywhere it surfaces.

Crucially, "both papers have a grounded claim" is not "the papers
agree". Two papers can both be well-grounded and still disagree, which
is why `aligned` has to be earned from the evidence rather than inferred
from the deterministic verdict.

WHAT THE MODEL IS AND IS NOT TRUSTED FOR
----------------------------------------
Trusted: the relation, the statement, and REFERENCES into the evidence
it was given. Not trusted: anything identifying. owner_id, paper ids,
paper titles and Qdrant point ids never appear in this schema, and
`extra="forbid"` at every level means a model that invents them is
rejected rather than quietly stripped.

Which paper is "A" and which is "B" is decided by the server before this
module is ever called; the model only ever sees those two labels.

EVIDENCE OWNERSHIP IS ABSOLUTE
------------------------------
`cites_a` is checked against Paper A's allowlist and nothing else;
`cites_b` against Paper B's. A pair that exists in B but not A is NOT
accepted for A — it is a rejection, never a remap. The two namespaces
are never merged, because (page, chunk_id) is meaningless without
knowing which paper it belongs to: page 3 chunk 0 exists in almost every
paper.

WHAT VALIDATION WILL AND WILL NOT DO
------------------------------------
It will REJECT: malformed JSON, duplicate JSON keys, unknown or missing
sections, a section that was not comparable, an unknown relation, an
empty statement, an incoherent relation/citation combination, and any
citation that was not supplied to the model for that specific paper.

It will NOT judge whether "aligned" was the right call. That is the
interpretation this layer exists to carry, not to second-guess, and
lexical or semantic similarity checking is deliberately absent.

It repairs nothing. A citation that does not check out fails its object;
it is never remapped to a nearby page or to the other paper.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The canonical ten and the fence-stripping policy are REUSED rather than
# restated. A second copy of either would be free to drift, and the two
# parsers must tolerate exactly the same fences.
from app.services.intelligence_schema import (  # noqa: F401  (EvidenceAllowlist re-exported)
    SECTION_NAMES,
    EvidenceAllowlist,
    _strip_one_json_fence,
)

#: The controlled vocabulary. Deliberately four values, all neutral:
#: nothing here ranks, scores or picks a winner, and there is no ordering
#: between them.
RELATION_ALIGNED = "aligned"
RELATION_DIVERGENT = "divergent"
RELATION_COMPLEMENTARY = "complementary"
RELATION_NOT_COMPARABLE = "not_comparable"

RELATIONS: Tuple[str, ...] = (
    RELATION_ALIGNED,
    RELATION_DIVERGENT,
    RELATION_COMPLEMENTARY,
    RELATION_NOT_COMPARABLE,
)

#: The three that assert a relationship between two claims, and therefore
#: must be paid for with evidence from BOTH papers.
GROUNDED_RELATIONS: Tuple[str, ...] = (
    RELATION_ALIGNED,
    RELATION_DIVERGENT,
    RELATION_COMPLEMENTARY,
)

#: Deliberately NOT a relation value. The deterministic engine already
#: reports a_only / b_only / neither; duplicating "insufficient_evidence"
#: here would create a second, competing source of truth for the same
#: fact. A section the model genuinely cannot relate is `not_comparable`.


class RelationshipValidationError(Exception):
    """A model response that must not be persisted.

    Carries a stable `code` for the caller to branch on and a message
    written HERE. The offending model text is deliberately not
    interpolated: this message is destined for an HTTP response, and a
    rejected generation is exactly the case where echoing model output
    back to a user is least safe.

    A separate type from IntelligenceValidationError on purpose. The two
    validators guard different endpoints with different failure codes,
    and a caller that handles one must not silently swallow the other.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RelationshipEvidence(BaseModel):
    """One reference into ONE paper's supplied context.

    Identity is the (page, chunk_id) PAIR, matching the rest of the
    system. Which paper it belongs to is carried by the field it sits in
    (`cites_a` or `cites_b`), never by anything the model writes.
    """

    model_config = ConfigDict(extra="forbid", strict=False)

    # strict=True on both reference fields, for the reason Phase 2B found
    # empirically: Pydantic's default coercion turns True into 1 and
    # False into 0, so a boolean would silently become "page 1" — a
    # nonsense value wearing a plausible disguise. It also rejects "3"
    # and 3.0. Coercion is harmless for prose; for a citation it is a
    # repair, and references are the one thing this module never repairs.
    page: int = Field(ge=1, strict=True)
    chunk_id: int = Field(ge=0, strict=True)


class SectionRelationship(BaseModel):
    """How the two papers' claims for ONE canonical section relate."""

    model_config = ConfigDict(extra="forbid", strict=False)

    relation: str
    #: AI interpretation, never a deterministic fact. The field is named
    #: `statement` rather than `finding` or `conclusion` so that nothing
    #: downstream is tempted to present it as established.
    statement: str
    cites_a: List[RelationshipEvidence] = Field(default_factory=list)
    cites_b: List[RelationshipEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _relation_and_citations_agree(self) -> "SectionRelationship":
        if self.relation not in RELATIONS:
            raise ValueError(f"relation must be one of {RELATIONS}")

        if not (self.statement or "").strip():
            raise ValueError("a relationship requires a non-empty statement")

        if self.relation == RELATION_NOT_COMPARABLE:
            # Nothing was related, so nothing may be cited. Evidence here
            # would imply a comparison the model just said it could not
            # make.
            if self.cites_a or self.cites_b:
                raise ValueError("not_comparable must carry no citations")
            return self

        # An asserted relationship with only one side behind it is the
        # exact failure this whole layer exists to prevent: it would read
        # as a cross-paper finding while resting on one paper.
        if not self.cites_a:
            raise ValueError(f"{self.relation} requires evidence from Paper A")
        if not self.cites_b:
            raise ValueError(f"{self.relation} requires evidence from Paper B")

        return self


class PaperRelationship(BaseModel):
    """The relationships for the sections that were comparable.

    Not all ten. The deterministic engine decides which sections have a
    grounded claim on BOTH sides, and only those are eligible — asking
    the model about a section where one paper says nothing would be
    asking it to invent the missing half. The keys are still drawn from
    SECTION_NAMES, so an arbitrary section name can never appear.
    """

    model_config = ConfigDict(extra="forbid", strict=False)

    sections: Dict[str, SectionRelationship]


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def _reject_duplicate_keys(pairs: Sequence[Tuple[str, object]]) -> dict:
    """object_pairs_hook that refuses a repeated key at any depth.

    json.loads() silently keeps the LAST value for a duplicated key, so
    `{"methodology": <valid>, "methodology": <invalid>}` would parse
    cleanly and quietly discard one of them. For a document that decides
    what gets persisted, "silently discarded" is the wrong failure: a
    response that cannot be read unambiguously is not a response.
    """
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key}")
        seen.add(key)
    return dict(pairs)


def parse_relationship_json(raw: str) -> dict:
    """Strict JSON, optionally inside one fence, and it must be an object.

    Shares intelligence_schema's fence policy exactly — one optional
    surrounding fence, no scanning JSON out of prose — and adds
    duplicate-key rejection on top.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RelationshipValidationError(
            "malformed_json", "The model returned no parseable content."
        )

    try:
        parsed = json.loads(
            _strip_one_json_fence(raw), object_pairs_hook=_reject_duplicate_keys
        )
    except (json.JSONDecodeError, ValueError):
        raise RelationshipValidationError(
            "malformed_json", "The model response was not valid JSON."
        ) from None

    if not isinstance(parsed, dict):
        raise RelationshipValidationError(
            "malformed_json", "The model response was not a JSON object."
        )

    return parsed


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def _check_structure(payload: dict) -> PaperRelationship:
    try:
        return PaperRelationship.model_validate(payload)
    except ValidationError:
        # Pydantic's report can quote the offending values; it belongs in
        # a server log, not in the message handed back to a caller.
        raise RelationshipValidationError(
            "invalid_structure",
            "The model response did not match the required structure.",
        ) from None


def _canonical_comparable(comparable_sections: Iterable[str]) -> Tuple[str, ...]:
    """The caller's comparable set, in canonical order.

    A bad value here is a programming error at the call site, not
    untrusted model output, so it raises ValueError rather than the
    caller-facing RelationshipValidationError.
    """
    requested = list(comparable_sections)

    unknown = [name for name in requested if name not in SECTION_NAMES]
    if unknown:
        raise ValueError(f"not canonical section names: {unknown}")

    if len(set(requested)) != len(requested):
        raise ValueError("comparable_sections contains duplicates")

    if not requested:
        # Nothing was comparable, so there was nothing to ask the model
        # about. Reaching here means the caller skipped the deterministic
        # gate that exists precisely to avoid a pointless generation.
        raise ValueError("comparable_sections is empty; no generation should have run")

    wanted = set(requested)
    return tuple(name for name in SECTION_NAMES if name in wanted)


def _checked_citations(
    citations: List[RelationshipEvidence],
    allowed: EvidenceAllowlist,
    paper_label: str,
) -> List[RelationshipEvidence]:
    checked: List[RelationshipEvidence] = []

    for item in citations:
        # Checked against THIS paper's allowlist only. The same pair
        # existing in the other paper is irrelevant and must never
        # rescue it.
        if (item.page, item.chunk_id) not in allowed:
            raise RelationshipValidationError(
                "evidence_not_supplied",
                f"The model cited evidence for {paper_label} that was not "
                "supplied to it.",
            )
        checked.append(RelationshipEvidence(page=item.page, chunk_id=item.chunk_id))

    return checked


def validate_relationship(
    raw: "str | dict",
    *,
    comparable_sections: Iterable[str],
    allowed_evidence_a: EvidenceAllowlist,
    allowed_evidence_b: EvidenceAllowlist,
) -> PaperRelationship:
    """Parse, validate and normalize one cross-paper relationship response.

    `comparable_sections` comes from the deterministic engine, and the
    two allowlists come from the SERVER — the exact chunks supplied to
    the model for each paper — never from the response being checked.

    Returns a new object whose sections are in canonical order; the input
    is not mutated. Deterministic: the same input and allowlists always
    produce the same output, with citations left in the order the model
    gave them.

    Raises RelationshipValidationError on anything that must not be
    persisted, and ValueError on a malformed call.
    """
    expected = _canonical_comparable(comparable_sections)

    payload = parse_relationship_json(raw) if isinstance(raw, str) else raw

    if not isinstance(payload, dict):
        raise RelationshipValidationError(
            "malformed_json", "The model response was not a JSON object."
        )

    structured = _check_structure(payload)

    supplied = set(structured.sections)
    wanted = set(expected)

    # Anything outside the comparable set — including a canonical section
    # that simply was not comparable — is a section the model was never
    # asked about and has no grounded basis for.
    unexpected = sorted(supplied - wanted)
    if unexpected:
        raise RelationshipValidationError(
            "unknown_section",
            "The model returned a section that was not part of this comparison.",
        )

    missing = sorted(wanted - supplied)
    if missing:
        raise RelationshipValidationError(
            "missing_section",
            "The model omitted a section that was part of this comparison.",
        )

    checked: Dict[str, SectionRelationship] = {}

    for name in expected:  # canonical order
        section = structured.sections[name]
        checked[name] = SectionRelationship(
            relation=section.relation,
            statement=section.statement.strip(),
            cites_a=_checked_citations(section.cites_a, allowed_evidence_a, "Paper A"),
            cites_b=_checked_citations(section.cites_b, allowed_evidence_b, "Paper B"),
        )

    return PaperRelationship(sections=checked)


def build_relationship_allowlists(
    intelligence_a: Mapping[str, object],
    intelligence_b: Mapping[str, object],
    comparable_sections: Iterable[str],
) -> Tuple[set, set]:
    """The (page, chunk_id) pairs each paper's comparable sections cite.

    A convenience for the caller that will rehydrate chunks in Step 2C-2:
    it says WHICH chunks need fetching, without fetching anything. The
    two sets are kept separate for the same reason the allowlists are —
    a pair is meaningless without knowing whose paper it came from.

    Deliberately returns sets of pairs rather than allowlists: the text
    is not available at this layer, and inventing a placeholder for it
    would be worse than returning only what is known.
    """
    wanted = set(_canonical_comparable(comparable_sections))

    def pairs_for(intelligence: Mapping[str, object]) -> set:
        found = set()
        for name in wanted:
            section = intelligence.get(name) if hasattr(intelligence, "get") else None
            if not isinstance(section, Mapping):
                continue
            for item in section.get("evidence") or []:
                if not isinstance(item, Mapping):
                    continue
                page, chunk_id = item.get("page"), item.get("chunk_id")
                # Booleans are ints in Python; excluded explicitly so a
                # stray True cannot become page 1 here either.
                if (
                    isinstance(page, int)
                    and not isinstance(page, bool)
                    and isinstance(chunk_id, int)
                    and not isinstance(chunk_id, bool)
                    and page >= 1
                    and chunk_id >= 0
                ):
                    found.add((page, chunk_id))
        return found

    return pairs_for(intelligence_a), pairs_for(intelligence_b)
