"""
Phase 3.2 — verified verbatim evidence spans.

`Evidence.quote` was always a field on the schema; until now the prompt
forbade it, so it was always null. Phase 3.2 asks the model for it, which
means the span becomes rendered text and therefore has to be earned.

The whole safety argument rests on one property: a quote is accepted ONLY
if it is an exact substring of the one chunk its (page, chunk_id) names.
So the tests that matter are the ones proving a span cannot arrive any
other way —

  * not from a different supplied chunk of the same paper;
  * not from another paper, even when both papers hold (3, 0);
  * not by being paraphrased, reworded or re-spaced;
  * not by being truncated to fit the length cap;
  * and not as an empty or whitespace-only string, which is subtler than
    it looks: "" and " " ARE substrings of virtually every chunk, so the
    substring test alone would admit them.

A failed span is always DROPPED to None while its (page, chunk_id)
survives, because the reference itself already checked out. Nothing here
repairs, remaps or shortens a quote.

Fully offline: pure validator calls plus the in-memory SQLite harness. No
provider, no network, no production database.
"""
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.db.models import Paper
from app.services.intelligence_schema import (
    MAX_QUOTE_CHARS,
    SECTION_NAMES,
    IntelligenceValidationError,
    validate_intelligence,
)
from app.services.paper_intelligence_store import (
    PAPER_INTELLIGENCE_SCHEMA_VERSION,
    get_intelligence,
    save_intelligence,
)
from tests.sqlite_harness import attach_sqlite_db

OWNER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"

TOTAL_PAGES = 9

#: Paper A's supplied chunks. (3, 0) and (7, 0) are distinct blocks — the
#: pair that proves a span cannot drift between supplied chunks.
ALLOWED_A = {
    (3, 0): "METHODOLOGY. A two-stage hybrid framework is proposed for defect detection.",
    (7, 0): "LIMITATIONS. The approach depends on prompt quality and careful tuning.",
}

#: Paper B holds the SAME (3, 0) identity with different text. This is the
#: collision that makes a merged allowlist dangerous.
ALLOWED_B = {
    (3, 0): "METHOD. A single-stage transformer detector is trained end to end.",
}

A_3_0 = ALLOWED_A[(3, 0)]
A_7_0 = ALLOWED_A[(7, 0)]
B_3_0 = ALLOWED_B[(3, 0)]


def _section(status="answered", summary="A grounded summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def _payload(**overrides):
    body = {name: _section() for name in SECTION_NAMES}
    body.update(overrides)
    return body


def _solo(section_name, evidence):
    """One answered section, the other nine not_specified.

    Needed whenever a test supplies its OWN allowlist: the default payload
    has all ten sections citing (3, 0), which would fail
    evidence_not_supplied against an allowlist keyed on anything else —
    and the failure would be about the other nine sections, not about the
    span under test.
    """
    body = {name: _section(status="not_specified", summary=None, evidence=[])
            for name in SECTION_NAMES}
    body[section_name] = _section(evidence=evidence)
    return body


def _validate(payload, allowed=None, total_pages=TOTAL_PAGES):
    return validate_intelligence(
        payload,
        allowed_evidence=ALLOWED_A if allowed is None else allowed,
        total_pages=total_pages,
    )


def _one_evidence(result, section="methodology"):
    return getattr(result, section).evidence[0]


# ----------------------------------------------------------------------
# 1 — a valid span survives
# ----------------------------------------------------------------------
class TestValidSpanSurvives(unittest.TestCase):
    def test_a_verbatim_span_is_kept_exactly(self):
        span = "A two-stage hybrid framework is proposed"
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": span}]))
        )
        item = _one_evidence(result)
        self.assertEqual(item.quote, span)
        # Character-for-character, not merely similar.
        self.assertIn(item.quote, A_3_0)

    def test_the_whole_chunk_is_a_valid_span_when_short_enough(self):
        self.assertLessEqual(len(A_3_0), MAX_QUOTE_CHARS)
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": A_3_0}]))
        )
        self.assertEqual(_one_evidence(result).quote, A_3_0)

    def test_a_span_at_exactly_the_cap_is_kept(self):
        chunk = "x" * MAX_QUOTE_CHARS
        allowed = {(1, 0): chunk}
        result = _validate(
            _solo("methodology", [{"page": 1, "chunk_id": 0, "quote": chunk}]),
            allowed=allowed,
        )
        item = _one_evidence(result)
        self.assertEqual(len(item.quote), MAX_QUOTE_CHARS)

    def test_the_reference_survives_whether_or_not_the_span_does(self):
        for quote in (A_3_0, "a paraphrase that appears nowhere"):
            with self.subTest(quote=quote[:20]):
                result = _validate(
                    _payload(methodology=_section(
                        evidence=[{"page": 3, "chunk_id": 0, "quote": quote}]))
                )
                item = _one_evidence(result)
                self.assertEqual((item.page, item.chunk_id), (3, 0))

    def test_an_omitted_quote_stays_none(self):
        result = _validate(_payload())
        self.assertIsNone(_one_evidence(result).quote)

    def test_an_explicit_null_quote_stays_none(self):
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": None}]))
        )
        self.assertIsNone(_one_evidence(result).quote)


# ----------------------------------------------------------------------
# 2, 3, 4 — a span may only come from the chunk it cites
# ----------------------------------------------------------------------
class TestSpanProvenance(unittest.TestCase):
    def test_a_span_copied_from_a_different_supplied_chunk_is_dropped(self):
        """(7, 0) is supplied, so its text is genuinely available to the
        model — but it is not the chunk this entry cites."""
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": A_7_0}]))
        )
        item = _one_evidence(result)
        self.assertIsNone(item.quote)
        self.assertEqual((item.page, item.chunk_id), (3, 0))

    def test_a_span_from_another_paper_is_dropped(self):
        """Paper B's (3, 0) has the same identity and different text.
        Validated against Paper A's allowlist it must not survive."""
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": B_3_0}])),
            allowed=ALLOWED_A,
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_a_span_not_present_in_the_cited_chunk_is_dropped(self):
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0,
                           "quote": "The authors prove their method is best."}]))
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_a_reworded_span_is_dropped(self):
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0,
                           "quote": "a two stage hybrid framework is proposed"}]))
        )
        # Case and punctuation differ, so it is not verbatim.
        self.assertIsNone(_one_evidence(result).quote)

    def test_a_respaced_span_is_dropped(self):
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0,
                           "quote": "A  two-stage  hybrid  framework"}]))
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_a_span_stitched_from_two_chunks_is_dropped(self):
        stitched = A_3_0[:30] + A_7_0[:30]
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": stitched}]))
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_the_span_is_checked_against_one_key_not_the_whole_allowlist(self):
        """A span valid for (7, 0) must fail under (3, 0) and succeed under
        (7, 0) — proving the check is keyed, not a scan."""
        span = "depends on prompt quality"

        under_wrong_key = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": span}]))
        )
        under_right_key = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 7, "chunk_id": 0, "quote": span}]))
        )

        self.assertIsNone(_one_evidence(under_wrong_key).quote)
        self.assertEqual(_one_evidence(under_right_key).quote, span)


# ----------------------------------------------------------------------
# 5, 6 — the length cap and the empty-span gate
# ----------------------------------------------------------------------
class TestSpanLengthAndEmptiness(unittest.TestCase):
    def test_a_span_over_the_cap_is_dropped_not_truncated(self):
        chunk = "y" * (MAX_QUOTE_CHARS + 50)
        allowed = {(1, 0): chunk}
        over = "y" * (MAX_QUOTE_CHARS + 1)

        result = _validate(
            _solo("methodology", [{"page": 1, "chunk_id": 0, "quote": over}]),
            allowed=allowed,
        )
        item = _one_evidence(result)
        # Dropped entirely. A truncated span would still render as
        # verbatim while no longer being what the paper says.
        self.assertIsNone(item.quote)

    def test_an_over_cap_span_is_dropped_even_though_it_IS_a_substring(self):
        """The cap is independent of verbatimness: this span is genuinely
        present in the chunk and is still refused."""
        chunk = "z" * (MAX_QUOTE_CHARS + 10)
        over = "z" * (MAX_QUOTE_CHARS + 5)
        self.assertIn(over, chunk)

        result = _validate(
            _solo("methodology", [{"page": 1, "chunk_id": 0, "quote": over}]),
            allowed={(1, 0): chunk},
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_the_cap_is_exactly_200(self):
        self.assertEqual(MAX_QUOTE_CHARS, 200)

    def test_an_empty_span_is_dropped(self):
        """"" is a substring of every string, so the substring test alone
        would admit it."""
        self.assertIn("", A_3_0)
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": ""}]))
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_a_whitespace_only_span_is_dropped(self):
        """A single space IS present in the chunk, so this is the case the
        substring test cannot catch on its own."""
        self.assertIn(" ", A_3_0)
        for blank in (" ", "  ", "\t", "\n", " \t\n "):
            with self.subTest(blank=repr(blank)):
                result = _validate(
                    _payload(methodology=_section(
                        evidence=[{"page": 3, "chunk_id": 0, "quote": blank}]))
                )
                self.assertIsNone(_one_evidence(result).quote)


# ----------------------------------------------------------------------
# 7, 8, 11, 13 — the reference rules are untouched by spans
# ----------------------------------------------------------------------
class TestReferenceRulesUnchanged(unittest.TestCase):
    def test_a_malformed_page_is_still_rejected(self):
        for bad in ("3", 3.0, None, [3]):
            with self.subTest(bad=bad):
                with self.assertRaises(IntelligenceValidationError):
                    _validate(_payload(methodology=_section(
                        evidence=[{"page": bad, "chunk_id": 0, "quote": A_3_0}])))

    def test_a_boolean_page_or_chunk_is_still_rejected(self):
        for field in ("page", "chunk_id"):
            with self.subTest(field=field):
                ev = {"page": 3, "chunk_id": 0, "quote": A_3_0}
                ev[field] = True
                with self.assertRaises(IntelligenceValidationError):
                    _validate(_payload(methodology=_section(evidence=[ev])))

    def test_an_unsupplied_reference_is_still_rejected_even_with_a_real_span(self):
        """A perfect span cannot buy a reference that was never supplied."""
        with self.assertRaises(IntelligenceValidationError) as caught:
            _validate(_payload(methodology=_section(
                evidence=[{"page": 5, "chunk_id": 9, "quote": A_3_0}])))
        self.assertEqual(caught.exception.code, "evidence_not_supplied")

    def test_a_page_beyond_the_paper_is_still_rejected(self):
        with self.assertRaises(IntelligenceValidationError) as caught:
            _validate(_payload(methodology=_section(
                evidence=[{"page": TOTAL_PAGES + 1, "chunk_id": 0, "quote": A_3_0}])))
        self.assertEqual(caught.exception.code, "evidence_out_of_range")

    def test_extra_fields_are_still_forbidden(self):
        with self.assertRaises(IntelligenceValidationError):
            _validate(_payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": A_3_0,
                           "confidence": 0.9}])))

    def test_a_span_cannot_expand_the_allowlist(self):
        """The set of accepted (page, chunk_id) keys is identical whether
        spans are supplied or not."""
        with_spans = _validate(_payload(
            methodology=_section(evidence=[{"page": 3, "chunk_id": 0, "quote": A_3_0}]),
            limitations=_section(evidence=[{"page": 7, "chunk_id": 0, "quote": A_7_0}]),
        ))
        without = _validate(_payload(
            methodology=_section(evidence=[{"page": 3, "chunk_id": 0}]),
            limitations=_section(evidence=[{"page": 7, "chunk_id": 0}]),
        ))
        keys = lambda r: sorted(
            (e.page, e.chunk_id)
            for name in SECTION_NAMES
            for e in getattr(r, name).evidence
        )
        self.assertEqual(keys(with_spans), keys(without))

    def test_a_not_specified_section_carries_no_span(self):
        result = _validate(_payload(
            methodology=_section(status="not_specified", summary=None, evidence=[])))
        self.assertEqual(result.methodology.evidence, [])


# ----------------------------------------------------------------------
# 12 — cross-owner isolation
# ----------------------------------------------------------------------
class TestCrossOwnerIsolation(unittest.TestCase):
    def test_another_owners_chunk_text_cannot_become_a_span(self):
        """The allowlist is built from owner- and paper-filtered chunks, so
        another owner's text is simply not in it."""
        result = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": B_3_0}])),
            allowed=ALLOWED_A,
        )
        self.assertIsNone(_one_evidence(result).quote)

    def test_merging_the_two_allowlists_is_what_the_separation_prevents(self):
        """Stated as a test so the hazard is explicit: under a MERGED
        allowlist the foreign span would validate. Production never merges
        — build_allowlist is called per paper on owner+paper-filtered
        chunks."""
        merged = {**ALLOWED_A, (3, 0): ALLOWED_A[(3, 0)] + " " + B_3_0}
        leaked = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": B_3_0}])),
            allowed=merged,
        )
        self.assertEqual(_one_evidence(leaked).quote, B_3_0)

        isolated = _validate(
            _payload(methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": B_3_0}])),
            allowed=ALLOWED_A,
        )
        self.assertIsNone(_one_evidence(isolated).quote)


# ----------------------------------------------------------------------
# 9, 10 — schema_version
# ----------------------------------------------------------------------
class TestSchemaVersion(unittest.TestCase):
    def setUp(self):
        self.factory = attach_sqlite_db(self)
        with self.factory() as session:
            session.add(
                Paper(
                    id=uuid.UUID(PAPER_A),
                    owner_id=uuid.UUID(OWNER_A),
                    title="Paper A",
                    content_hash="h" * 64,
                    storage_path=f"{OWNER_A}/{PAPER_A}/original.pdf",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()

    def _validated(self, with_span=True):
        ev = {"page": 3, "chunk_id": 0}
        if with_span:
            ev["quote"] = A_3_0
        return _validate(_payload(methodology=_section(evidence=[ev])))

    def test_the_constant_is_now_2(self):
        self.assertEqual(PAPER_INTELLIGENCE_SCHEMA_VERSION, "2")

    def test_a_new_generation_records_version_2(self):
        stored = save_intelligence(
            OWNER_A, PAPER_A, self._validated(), model="test-model/3.2",
            generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(stored.schema_version, "2")

    def test_version_2_round_trips_with_its_span_intact(self):
        save_intelligence(
            OWNER_A, PAPER_A, self._validated(), model="test-model/3.2",
            generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        )
        read = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(read.schema_version, "2")
        self.assertEqual(read.intelligence.methodology.evidence[0].quote, A_3_0)

    def test_a_version_1_row_remains_readable(self):
        """Backward compatibility: the jsonb shape never changed, so a row
        written before spans existed still parses."""
        save_intelligence(
            OWNER_A, PAPER_A, self._validated(with_span=False),
            model="legacy-model", schema_version="1",
            generated_at=datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc),
        )
        read = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(read.schema_version, "1")
        self.assertIsNone(read.intelligence.methodology.evidence[0].quote)
        self.assertEqual(read.intelligence.methodology.evidence[0].page, 3)

    def test_a_version_1_row_with_a_span_also_reads(self):
        """Nothing rejects a span on a "1" row — the version is a marker,
        not a gate."""
        save_intelligence(
            OWNER_A, PAPER_A, self._validated(with_span=True),
            model="legacy-model", schema_version="1",
            generated_at=datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc),
        )
        read = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(read.schema_version, "1")
        self.assertEqual(read.intelligence.methodology.evidence[0].quote, A_3_0)

    def test_regenerating_a_version_1_row_upgrades_it_to_2(self):
        save_intelligence(
            OWNER_A, PAPER_A, self._validated(with_span=False),
            model="legacy-model", schema_version="1",
            generated_at=datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc),
        )
        save_intelligence(
            OWNER_A, PAPER_A, self._validated(with_span=True), model="test-model/3.2",
            generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        )
        read = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(read.schema_version, "2")
        self.assertEqual(read.intelligence.methodology.evidence[0].quote, A_3_0)
        # Still exactly one current row.
        with self.factory() as session:
            from app.db.models import PaperIntelligenceRow
            self.assertEqual(session.query(PaperIntelligenceRow).count(), 1)


# ----------------------------------------------------------------------
# The prompt actually asks for what the validator enforces
# ----------------------------------------------------------------------
class TestPromptAndValidatorAgree(unittest.TestCase):
    def setUp(self):
        import app.api.paper_intelligence as pipeline
        self.prompt = pipeline.INTELLIGENCE_SYSTEM_PROMPT

    def test_the_prompt_requests_an_optional_verbatim_span(self):
        for phrase in ("quote", "VERBATIM", "OMIT the field"):
            self.assertIn(phrase, self.prompt)

    def test_the_prompt_states_the_same_cap_the_validator_enforces(self):
        self.assertIn(str(MAX_QUOTE_CHARS), self.prompt)

    def test_the_prompt_no_longer_forbids_quotes(self):
        self.assertNotIn('Do NOT include a "quote"', self.prompt)

    def test_the_prompt_forbids_paraphrase_and_stitching(self):
        for phrase in ("paraphrase", "Never combine"):
            self.assertIn(phrase, self.prompt)

    def test_the_completion_budget_is_unchanged(self):
        import app.api.paper_intelligence as pipeline
        self.assertEqual(pipeline.INTELLIGENCE_MAX_TOKENS, 3000)
        self.assertEqual(pipeline.INTELLIGENCE_CONTEXT_CHARS, 12000)


if __name__ == "__main__":
    unittest.main()
