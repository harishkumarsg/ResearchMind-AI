"""
Paper Intelligence — the structured schema and its validator.

Pure and provider-independent: nothing here calls Groq, Voyage, Qdrant,
Postgres or the network, and nothing here knows what a request is. It
takes a model's JSON output plus an evidence allowlist the SERVER built,
and either returns a normalized object or raises. That separation is the
whole point — the language model proposes, this module disposes.

WHAT THE MODEL IS AND IS NOT TRUSTED FOR
----------------------------------------
Trusted: section prose, and REFERENCES to evidence.
Not trusted: anything identifying. owner_id, paper_id, paper title and
Qdrant point ids never appear in this schema, and `extra="forbid"` at
every level means a model that invents them is rejected rather than
quietly stripped. Ownership and paper identity are decided before this
module is ever called, by the caller that built the allowlist.

EVIDENCE IDENTITY
-----------------
`chunk_id` is the chunk's index WITHIN ITS PAGE (see index_document.py:
`chunk_index` over `create_chunks(page_text)`), so it is not unique on
its own — page 3 chunk 0 and page 7 chunk 0 both exist. Evidence is
therefore keyed on the pair (page, chunk_id), and the allowlist is keyed
the same way. Treating chunk_id as a global id would silently accept
evidence pointing at the wrong passage.

WHAT VALIDATION WILL AND WILL NOT DO
------------------------------------
It will REJECT: malformed JSON, unknown or missing sections, bad status,
incoherent status/evidence combinations, a page outside the paper, and a
(page, chunk_id) pair that was never supplied.

It will NOT repair a reference. A page or pair that does not check out
fails the whole object; it is never remapped to a nearby one, and
evidence is never invented to satisfy a section.

The single exception is `quote`, which is decorative: a quote that is not
a verbatim substring of its own chunk is DROPPED while the page/chunk
reference it came with is kept. A paraphrase is never substituted for
the model's text, and the reference — the part a reader clicks — is
still exactly what the model cited.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: The v1 sections, in the order they are generated and displayed.
#: Deliberately exactly ten: Context/Motivation, Baselines and Future
#: Work are the most interpretive and are deferred to a later
#: schema_version rather than competing for the same token budget.
SECTION_NAMES: Tuple[str, ...] = (
    "research_problem",
    "research_objective",
    "methodology",
    "dataset",
    "experimental_setup",
    "evaluation_metrics",
    "key_results",
    "contributions",
    "limitations",
    "reproducibility",
)

STATUS_ANSWERED = "answered"
STATUS_NOT_SPECIFIED = "not_specified"

#: Hard ceiling on a verbatim evidence span, in characters.
#:
#: Two jobs. It keeps the completion inside the proven
#: INTELLIGENCE_MAX_TOKENS budget (3000) now that spans are requested —
#: ten sections' worth of unbounded quotations is exactly what would push
#: a response past it. And it keeps a span a SPAN: a quote long enough to
#: be a paragraph stops being evidence for one claim and becomes a
#: reproduction of the source.
#:
#: Enforced by DROPPING an over-long quote, never by truncating it. A
#: truncated span would still be rendered as verbatim while no longer
#: being what the paper says.
MAX_QUOTE_CHARS = 200


@dataclass
class QuoteTally:
    """Why each evidence item ended up with, or without, a span.

    Exists because the three gates in `_normalized_section` set
    `quote = None` SILENTLY, which made two very different outcomes
    indistinguishable from outside: a model that offered no spans, and a
    model whose every span was refused. The first production span-aware
    generation returned nine evidence items and zero quotes, and nothing
    in the system could say which of those had happened.

    COUNTS ONLY. No quote text, no chunk text, no page or chunk id — this
    object is built to be safe to log, so it deliberately cannot carry
    anything that would leak paper content into a log line.

    `absent` and the three `dropped_*` fields are the distinction that
    matters: absent means the model omitted the field (rule 8e of the
    prompt says that is correct and expected), while a drop means it
    supplied one the server refused.

    Invariant: offered == kept + dropped, and offered + absent is the
    number of evidence items across all answered sections.
    """

    #: Evidence items where the model supplied a non-null quote.
    offered: int = 0
    #: …of those, the ones that passed all three gates.
    kept: int = 0
    #: Evidence items with no quote field at all, or an explicit null.
    absent: int = 0
    #: Refused: empty or whitespace-only.
    dropped_blank: int = 0
    #: Refused: longer than MAX_QUOTE_CHARS.
    dropped_too_long: int = 0
    #: Refused: not an exact substring of the chunk the item cites.
    dropped_not_verbatim: int = 0

    @property
    def dropped(self) -> int:
        return self.dropped_blank + self.dropped_too_long + self.dropped_not_verbatim

    def as_log_fields(self) -> str:
        """One space-separated key=value run, for an existing log line.

        Safe by construction: every value is an integer.
        """
        return (
            f"offered={self.offered} kept={self.kept} absent={self.absent} "
            f"blank={self.dropped_blank} too_long={self.dropped_too_long} "
            f"not_verbatim={self.dropped_not_verbatim}"
        )

#: Evidence allowlist: (page, chunk_id) -> the exact chunk text supplied
#: to the model. The caller builds this from chunks already filtered by
#: owner_id and paper_id, which is why cross-owner and cross-paper
#: evidence cannot pass validation: such a pair is simply not a key.
EvidenceAllowlist = Mapping[Tuple[int, int], str]


class IntelligenceValidationError(Exception):
    """A model response that must not be persisted.

    Carries a stable `code` for the caller to branch on and a message
    written HERE. The offending model text is deliberately not
    interpolated: this message is destined for an HTTP response, and a
    rejected generation is exactly the case where echoing model output
    back to a user is least safe.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Evidence(BaseModel):
    """One reference into the supplied context.

    `quote` is optional and advisory. `page` and `chunk_id` are the
    load-bearing pair and are checked against the allowlist.
    """

    model_config = ConfigDict(extra="forbid", strict=False)

    # strict=True on the REFERENCE fields specifically, while the rest of
    # the schema stays lax. Pydantic's default coercion accepts "3", 3.0
    # and — the case that decided this — True, which it turns into page
    # 1. A boolean silently becoming a plausible page number is exactly
    # the fabrication this validator exists to stop: if (1, 0) happens to
    # be in the allowlist, that evidence would validate and a reader
    # would be shown "Page 1" for a claim the model never located.
    #
    # Coercion is harmless for prose; for a citation it is a repair, and
    # references are the one thing this module never repairs.
    #
    # ge=1 because pages are 1-based in every payload the indexer writes.
    page: int = Field(ge=1, strict=True)
    # ge=0 because chunk_index starts at zero within each page.
    chunk_id: int = Field(ge=0, strict=True)
    quote: Optional[str] = None


class Section(BaseModel):
    """One of the ten sections."""

    model_config = ConfigDict(extra="forbid", strict=False)

    status: str
    summary: Optional[str] = None
    evidence: List[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_and_evidence_agree(self) -> "Section":
        if self.status not in (STATUS_ANSWERED, STATUS_NOT_SPECIFIED):
            raise ValueError(
                f"status must be {STATUS_ANSWERED!r} or {STATUS_NOT_SPECIFIED!r}"
            )

        if self.status == STATUS_ANSWERED:
            # An answered section with nothing behind it is the exact
            # failure this whole feature exists to prevent.
            if not (self.summary or "").strip():
                raise ValueError("an answered section requires a non-empty summary")
            if not self.evidence:
                raise ValueError("an answered section requires at least one evidence item")
        else:
            if self.evidence:
                raise ValueError("a not_specified section must carry no evidence")

        return self


class PaperIntelligence(BaseModel):
    """All ten sections. Every one is required.

    A model that omits a section is rejected rather than defaulted:
    "absent" and "explicitly not specified" are different claims, and
    only the model can make the second one.
    """

    model_config = ConfigDict(extra="forbid", strict=False)

    research_problem: Section
    research_objective: Section
    methodology: Section
    dataset: Section
    experimental_setup: Section
    evaluation_metrics: Section
    key_results: Section
    contributions: Section
    limitations: Section
    reproducibility: Section

    def sections(self) -> Dict[str, Section]:
        """The sections in SECTION_NAMES order."""
        return {name: getattr(self, name) for name in SECTION_NAMES}


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
_FENCE = "```"


def _strip_one_json_fence(text: str) -> str:
    """Remove a single surrounding ``` or ```json fence, if present.

    Narrow by design. Models asked for JSON sometimes wrap it in one
    fence, and tolerating exactly that is cheap. What this must NOT
    become is a scanner that digs JSON out of arbitrary prose: prose
    around the object means the model ignored its instructions, and
    salvaging it would hide that.
    """
    stripped = text.strip()

    if not stripped.startswith(_FENCE) or not stripped.endswith(_FENCE):
        return stripped

    inner = stripped[len(_FENCE) : -len(_FENCE)]

    # An opening fence may carry a language tag on its own first line.
    newline = inner.find("\n")
    if newline != -1 and inner[:newline].strip().lower() in ("", "json"):
        inner = inner[newline + 1 :]

    return inner.strip()


def parse_intelligence_json(raw: str) -> dict:
    """Strict JSON, optionally inside one fence, and it must be an object.

    Raises IntelligenceValidationError rather than json's own exception
    so callers have one failure type to handle.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise IntelligenceValidationError(
            "malformed_json", "The model returned no parseable content."
        )

    try:
        parsed = json.loads(_strip_one_json_fence(raw))
    except (json.JSONDecodeError, ValueError):
        raise IntelligenceValidationError(
            "malformed_json", "The model response was not valid JSON."
        ) from None

    if not isinstance(parsed, dict):
        raise IntelligenceValidationError(
            "malformed_json", "The model response was not a JSON object."
        )

    return parsed


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def _check_structure(payload: dict) -> PaperIntelligence:
    try:
        return PaperIntelligence.model_validate(payload)
    except ValidationError:
        # Pydantic's report can quote the offending values; it belongs in
        # a server log, not in the message handed back to a caller.
        raise IntelligenceValidationError(
            "invalid_structure",
            "The model response did not match the required structure.",
        ) from None


def _normalized_section(
    section: Section,
    allowed_evidence: EvidenceAllowlist,
    total_pages: int,
    tally: "QuoteTally",
) -> Section:
    if section.status == STATUS_NOT_SPECIFIED:
        # Canonical form: a section with nothing to say carries no
        # summary text, whatever wording the model chose for "absent".
        return Section(status=STATUS_NOT_SPECIFIED, summary=None, evidence=[])

    checked: List[Evidence] = []

    for item in section.evidence:
        if item.page > total_pages:
            raise IntelligenceValidationError(
                "evidence_out_of_range",
                "The model cited a page that does not exist in this paper.",
            )

        key = (item.page, item.chunk_id)
        if key not in allowed_evidence:
            # Covers three cases at once: a page never supplied, a
            # chunk never supplied, and — because the allowlist was
            # built from owner- and paper-filtered chunks — any
            # reference to another paper or another owner's data.
            raise IntelligenceValidationError(
                "evidence_not_supplied",
                "The model cited evidence that was not supplied to it.",
            )

        # Three deterministic gates, cheapest first. A quote that fails any
        # of them is DROPPED to None — never rewritten, never trimmed to
        # fit, and never matched against a different chunk. The reference
        # itself survives, because (page, chunk_id) already checked out
        # against the allowlist; only the span is discarded.
        #
        # No similarity anywhere: the test is `in`, an exact substring of
        # the one chunk this reference names.
        quote = item.quote
        if quote is None:
            # The model omitted the field. Per the prompt's rule 8e that is
            # a correct answer, and counting it separately is the whole
            # point of the tally: it is NOT a rejection.
            tally.absent += 1
        else:
            tally.offered += 1
            if not quote.strip():
                # An empty or whitespace-only span. This gate is not
                # theoretical: "" and " " ARE substrings of essentially
                # every chunk, so without it they pass the substring test
                # below and get stored as a quote. Rendered, that is an
                # empty span presented as though it were supporting text.
                tally.dropped_blank += 1
                quote = None
            elif len(quote) > MAX_QUOTE_CHARS:
                # Over the cap. Dropped rather than truncated: half a
                # sentence is not what the paper says, and a silently
                # shortened span would still be rendered as verbatim.
                tally.dropped_too_long += 1
                quote = None
            elif quote not in allowed_evidence[key]:
                # Not present in the exact chunk this reference names.
                # Deliberately keyed on `key` alone — never a scan across
                # the allowlist, which would let a span from another
                # supplied chunk (or, with a merged allowlist, another
                # paper) validate under the wrong reference.
                tally.dropped_not_verbatim += 1
                quote = None
            else:
                tally.kept += 1

        checked.append(Evidence(page=item.page, chunk_id=item.chunk_id, quote=quote))

    return Section(
        status=STATUS_ANSWERED,
        summary=(section.summary or "").strip(),
        evidence=checked,
    )


def validate_intelligence(
    raw: "str | dict",
    *,
    allowed_evidence: EvidenceAllowlist,
    total_pages: int,
    tally: "Optional[QuoteTally]" = None,
) -> PaperIntelligence:
    """Parse, validate and normalize one model response.

    `allowed_evidence` and `total_pages` come from the SERVER — the
    chunks actually supplied to the model, and the paper's own page
    count — never from the response being checked.

    Returns a new object; the input is not mutated. Deterministic: the
    same input and allowlist always produce the same output, with
    evidence left in the order the model gave it.

    Raises IntelligenceValidationError on anything that must not be
    persisted.
    """
    # An OUT-parameter, deliberately optional: the return type stays
    # PaperIntelligence so every existing caller — eight test sites and the
    # endpoint — is unaffected. A caller that wants the counts passes one in;
    # a caller that does not gets a throwaway.
    if tally is None:
        tally = QuoteTally()

    if total_pages < 1:
        raise IntelligenceValidationError(
            "invalid_structure", "The paper has no indexed pages to cite."
        )

    payload = parse_intelligence_json(raw) if isinstance(raw, str) else raw

    if not isinstance(payload, dict):
        raise IntelligenceValidationError(
            "malformed_json", "The model response was not a JSON object."
        )

    structured = _check_structure(payload)

    return PaperIntelligence(
        **{
            name: _normalized_section(
                section, allowed_evidence, total_pages, tally
            )
            for name, section in structured.sections().items()
        }
    )


def build_allowlist(chunks: "List[dict]") -> Tuple[Dict[Tuple[int, int], str], int]:
    """(allowlist, highest page) from the chunk payloads supplied.

    A convenience for the caller that will assemble the prompt context,
    kept here so the allowlist is always built the same way it is
    checked. Payloads whose page or chunk_id is not an integer are
    skipped rather than guessed at — an unusable reference should fail
    validation later, not be quietly admitted now.
    """
    allowlist: Dict[Tuple[int, int], str] = {}
    highest_page = 0

    for payload in chunks:
        try:
            page = int(payload["page"])
            chunk_id = int(payload["chunk_id"])
        except (KeyError, TypeError, ValueError):
            continue

        if page < 1 or chunk_id < 0:
            continue

        allowlist[(page, chunk_id)] = payload.get("text", "") or ""
        highest_page = max(highest_page, page)

    return allowlist, highest_page
