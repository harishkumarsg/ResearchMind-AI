"""
Evidence rehydration: turn stored evidence REFERENCES back into the exact
paper text they point at.

Retrieval only. This module decides nothing about how two papers relate —
no aligned, divergent, complementary or not_comparable, no similarity, no
ranking, no model call. It is handed a set of `(page, chunk_id)`
references that some earlier layer already chose, and its whole job is to
return the exact chunk text for each one, or say plainly that it could
not.

WHY REFERENCES IN, NOT INTELLIGENCE IN
--------------------------------------
The input is deliberately the bare references plus which section cited
them — never a PaperIntelligence object. A module that could see the
model's `summary` field could, through a one-line mistake, return that
prose as though it were source text. It cannot make that mistake because
it never receives it: the only text this module can return is text it
read from a matched Qdrant payload.

EVIDENCE IDENTITY
-----------------
`(paper_id, page, chunk_id)`, with `owner_id` supplied separately and
applied as a mandatory filter. Never a quote, never similarity, never a
title, never array position, and never a Qdrant point UUID — point ids
are regenerated on every index run (index_document.py assigns a fresh
uuid4 per chunk), so one used as an identifier would rot at the next
re-index.

`chunk_id` is the chunk's index WITHIN ITS PAGE, so it is not unique on
its own: page 3 chunk 0 and page 7 chunk 0 both exist, in essentially
every paper. That is why the pair is the key and why the owner and paper
filters are not optional.

WHAT "COULD NOT RESOLVE" MEANS
------------------------------
A reference can outlive the chunks it points at — a re-index after a text
extractor upgrade can move boundaries. So the result distinguishes found
from unresolved, with a reason, and an unresolved reference carries NO
text. Nothing is ever substituted: not a neighbouring chunk, not another
chunk on the same page, not the first search result. A wrong passage
presented confidently is worse than a missing one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.rag.vector_store import COLLECTION_NAME, client
from app.services.intelligence_schema import SECTION_NAMES

#: Points requested per scroll round trip. Matches the existing exact
#: paper-scoped reader in api/paper_intelligence.py.
SCROLL_PAGE_SIZE = 256

#: Hard ceiling on points examined for one paper. Comfortably above
#: MAX_CHUNKS_PER_PAPER (150), so the FILTER rather than this limit
#: decides the result; it exists so a pathological collection cannot make
#: one request unbounded.
PAPER_CHUNK_CAP = 1024

#: Per-paper source-text budget, in characters.
#:
#: Deliberately the SAME number the generation path already uses
#: (api/paper_intelligence.py INTELLIGENCE_CONTEXT_CHARS = 12000) rather
#: than a new one. That figure is proven in production against the
#: configured GROQ_TIMEOUT_SECONDS, and a second independent constant
#: would be free to drift away from it. Imported by value rather than
#: from the API module on purpose: this service must not depend on an
#: endpoint, and api/paper_intelligence.py imports a Groq client at
#: module scope.
EVIDENCE_CONTEXT_CHARS = 12000

# ----------------------------------------------------------------------
# Resolution outcomes
# ----------------------------------------------------------------------
#: The reference resolved to exactly one well-formed chunk.
FOUND = "found"

#: No chunk in this owner's copy of this paper carries that exact
#: (page, chunk_id). Usually a reference that outlived a re-index.
MISSING_CHUNK = "missing_chunk"

#: Two or more points claim the same (page, chunk_id). An integrity
#: failure, not a tie to be broken: picking one would silently choose
#: between two candidate passages.
DUPLICATE_IDENTITY = "duplicate_chunk_identity"

#: The chunk was found but its payload has no usable text.
MALFORMED_PAYLOAD = "malformed_payload"

#: The chunk resolved, but including it would exceed the per-paper
#: budget. Whole chunks only — a half chunk is not the evidence its
#: (page, chunk_id) names.
OVER_BUDGET = "over_budget"


@dataclass(frozen=True)
class ResolvedEvidence:
    """One reference, resolved or explained.

    `text` is present only when `status == FOUND`, and is then byte-for-byte
    the matched chunk's payload text.
    """

    paper_id: str
    page: int
    chunk_id: int
    #: Which canonical sections cited this chunk, in canonical order. A
    #: chunk cited by two sections appears once, carrying both — see
    #: `_deduplicate_references`.
    sections: Tuple[str, ...]
    status: str
    text: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.status == FOUND


@dataclass(frozen=True)
class PaperEvidence:
    """Every reference requested for ONE paper, in deterministic order."""

    paper_id: str
    items: Tuple[ResolvedEvidence, ...]
    #: Characters of source text actually included.
    used_chars: int
    budget_chars: int

    @property
    def found(self) -> Tuple[ResolvedEvidence, ...]:
        return tuple(item for item in self.items if item.found)

    @property
    def unresolved(self) -> Tuple[ResolvedEvidence, ...]:
        return tuple(item for item in self.items if not item.found)


# ----------------------------------------------------------------------
# Reference normalisation
# ----------------------------------------------------------------------
def _usable_pair(page: object, chunk_id: object) -> Optional[Tuple[int, int]]:
    """(page, chunk_id) if both are usable integers, else None.

    Booleans are excluded explicitly. In Python `True` IS an int, so
    without this a stray `True` would become page 1 — a nonsense value
    wearing a plausible disguise, which is exactly the fabrication the
    Phase 2B validator was hardened against.
    """
    if isinstance(page, bool) or isinstance(chunk_id, bool):
        return None
    if not isinstance(page, int) or not isinstance(chunk_id, int):
        return None
    if page < 1 or chunk_id < 0:
        return None
    return page, chunk_id


def _deduplicate_references(
    references: Mapping[str, Iterable[Tuple[int, int]]],
) -> Dict[Tuple[int, int], Tuple[str, ...]]:
    """(page, chunk_id) -> the canonical sections citing it.

    DEDUPLICATED rather than rejected. One chunk frequently supports
    several sections, and fetching or sending the same passage twice buys
    nothing while spending budget twice; the provenance is preserved by
    collecting the section names instead of the duplicate. Unusable
    references and non-canonical section names are dropped here — they
    cannot be resolved, and guessing at them is the behaviour this layer
    exists to avoid.
    """
    citing: Dict[Tuple[int, int], Set[str]] = {}

    for section, pairs in (references or {}).items():
        if section not in SECTION_NAMES:
            continue
        for raw in pairs or ():
            if not isinstance(raw, (tuple, list)) or len(raw) != 2:
                continue
            pair = _usable_pair(raw[0], raw[1])
            if pair is None:
                continue
            citing.setdefault(pair, set()).add(section)

    return {
        pair: tuple(name for name in SECTION_NAMES if name in sections)
        for pair, sections in citing.items()
    }


# ----------------------------------------------------------------------
# Qdrant read
# ----------------------------------------------------------------------
def _owner_paper_filter(owner_id: str, paper_id: str) -> Filter:
    """The only filter this module ever builds.

    Both conditions are unconditional and there is no code path that
    produces a filter without them: an empty owner_id or paper_id raises
    rather than widening the query. A retrieval that forgot one would
    read another owner's or another paper's chunks, and `(page, chunk_id)`
    collides across papers constantly, so the mistake would surface as
    plausible-looking wrong evidence rather than as an error.
    """
    if not owner_id or not isinstance(owner_id, str):
        raise ValueError("owner_id is required for evidence retrieval")
    if not paper_id or not isinstance(paper_id, str):
        raise ValueError("paper_id is required for evidence retrieval")

    return Filter(
        must=[
            FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
            FieldCondition(key="paper_id", match=MatchValue(value=paper_id)),
        ]
    )


def build_chunk_index(owner_id: str, paper_id: str) -> Dict[Tuple[int, int], List[dict]]:
    """(page, chunk_id) -> every payload claiming that identity.

    A list, not a single payload, so duplicate identities are visible to
    the caller instead of being collapsed by a last-write-wins dict
    assignment. Scroll with an exact payload filter: no query vector, no
    similarity, no reranking, and therefore no Voyage call.
    """
    scroll_filter = _owner_paper_filter(owner_id, paper_id)

    index: Dict[Tuple[int, int], List[dict]] = {}
    seen = 0
    offset = None

    while seen < PAPER_CHUNK_CAP:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        for point in points:
            seen += 1
            payload = getattr(point, "payload", None)
            if not isinstance(payload, dict):
                continue
            # A point whose own page/chunk_id are unusable cannot be
            # keyed at all, so it is not indexed; a reference to it
            # resolves as missing rather than matching something else.
            pair = _usable_pair(payload.get("page"), payload.get("chunk_id"))
            if pair is None:
                continue
            index.setdefault(pair, []).append(payload)

        # offset is None once the filtered set is exhausted; the
        # empty-page check guards a server that returns an offset but no
        # points, which would otherwise spin.
        if offset is None or not points:
            break

    return index


# ----------------------------------------------------------------------
# Rehydration
# ----------------------------------------------------------------------
def _resolve_one(
    paper_id: str,
    pair: Tuple[int, int],
    sections: Tuple[str, ...],
    index: Mapping[Tuple[int, int], List[dict]],
) -> Tuple[ResolvedEvidence, Optional[str]]:
    """One reference against the index. Returns (result, text_if_found)."""
    page, chunk_id = pair
    matches = index.get(pair) or []

    def outcome(status: str, text: Optional[str] = None) -> ResolvedEvidence:
        return ResolvedEvidence(
            paper_id=paper_id,
            page=page,
            chunk_id=chunk_id,
            sections=sections,
            status=status,
            text=text,
        )

    if not matches:
        # No substitution. Not a neighbouring chunk, not another chunk on
        # the same page, not the first scrolled result.
        return outcome(MISSING_CHUNK), None

    if len(matches) > 1:
        # Two payloads claim one identity. Choosing either would be
        # choosing between two candidate passages on no basis at all.
        return outcome(DUPLICATE_IDENTITY), None

    # Text comes ONLY from the exact matched chunk's own payload text
    # field. No metadata field, no abstract, no summary.
    text = matches[0].get("text")
    if not isinstance(text, str) or not text.strip():
        return outcome(MALFORMED_PAYLOAD), None

    return outcome(FOUND, text), text


def rehydrate_paper_evidence(
    owner_id: str,
    paper_id: str,
    references: Mapping[str, Iterable[Tuple[int, int]]],
    *,
    budget_chars: int = EVIDENCE_CONTEXT_CHARS,
) -> PaperEvidence:
    """Resolve ONE paper's evidence references to their exact chunk text.

    `references` maps a canonical section name to the `(page, chunk_id)`
    pairs that section cited. Sections with no references contribute
    nothing, and an empty mapping performs NO Qdrant read at all — there
    is nothing to look up.

    Ordering is page ascending, then chunk_id ascending, for every result
    including the unresolved ones. Qdrant's own return order is never
    relied on.

    The budget admits WHOLE chunks only, in that order, and stops before
    exceeding `budget_chars`; anything not admitted is reported as
    OVER_BUDGET with no text. A chunk larger than the whole budget is
    therefore also excluded rather than truncated — half a chunk is not
    the evidence its (page, chunk_id) names. With the current chunker
    (chunk_size 1500) against a 12,000-character budget that cannot
    arise, so it is defensive rather than expected.

    Makes no provider call, writes nothing, and decides nothing about how
    the papers relate.
    """
    wanted = _deduplicate_references(references)

    if not wanted:
        # Nothing to resolve, so nothing is fetched. An ungrounded or
        # not_specified section reaches here as an absent or empty entry
        # and costs no round trip.
        return PaperEvidence(
            paper_id=paper_id, items=(), used_chars=0, budget_chars=budget_chars
        )

    index = build_chunk_index(owner_id, paper_id)

    items: List[ResolvedEvidence] = []
    used = 0

    # Deterministic: page ascending, then chunk_id ascending.
    for pair in sorted(wanted):
        resolved, text = _resolve_one(paper_id, pair, wanted[pair], index)

        if text is None:
            items.append(resolved)
            continue

        if used + len(text) > budget_chars:
            # Whole chunks only. Reported rather than trimmed, and the
            # loop continues so later (smaller) chunks can still be
            # admitted deterministically.
            items.append(
                ResolvedEvidence(
                    paper_id=resolved.paper_id,
                    page=resolved.page,
                    chunk_id=resolved.chunk_id,
                    sections=resolved.sections,
                    status=OVER_BUDGET,
                )
            )
            continue

        items.append(resolved)
        used += len(text)

    return PaperEvidence(
        paper_id=paper_id,
        items=tuple(items),
        used_chars=used,
        budget_chars=budget_chars,
    )


def references_from_intelligence(
    intelligence: Mapping[str, object],
    sections: Sequence[str],
) -> Dict[str, List[Tuple[int, int]]]:
    """The `(page, chunk_id)` pairs the named sections of one stored
    intelligence object cite.

    A convenience for the caller that holds a persisted intelligence
    payload and needs the reference map this module consumes. It reads
    ONLY `evidence[].page` and `evidence[].chunk_id` — never `summary`
    and never `quote` — so model prose has no route from an intelligence
    object into rehydrated source text.
    """
    out: Dict[str, List[Tuple[int, int]]] = {}

    for name in sections:
        if name not in SECTION_NAMES:
            continue
        section = intelligence.get(name) if hasattr(intelligence, "get") else None
        if not isinstance(section, Mapping):
            continue

        pairs: List[Tuple[int, int]] = []
        for item in section.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            pair = _usable_pair(item.get("page"), item.get("chunk_id"))
            if pair is not None:
                pairs.append(pair)

        if pairs:
            out[name] = pairs

    return out
