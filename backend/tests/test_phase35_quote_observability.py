"""
Phase 3.5 — quote observability.

The three gates in `_normalized_section` set `quote = None` silently, which
made two very different outcomes indistinguishable from outside: a model
that offered no spans, and a model whose every span the server refused.
The first production span-aware generation returned nine evidence items
and zero quotes, and nothing recorded which of those had happened.

`QuoteTally` closes that. The properties these tests pin:

  * every counter reflects an ACTUAL validation decision — a counter that
    merely echoed the input would be worse than no counter at all, so each
    drop reason is asserted against the specific input that triggers it;
  * `absent` is kept distinct from the three `dropped_*` reasons, because
    an omitted quote is correct per the prompt's rule 8e while a refused
    one is not;
  * the tally is COUNTS ONLY and safe to log — no quote text, no chunk
    text, no ids;
  * adding the tally changed no validation decision. The same inputs
    produce the same objects as before.

Offline and pure: validator calls only. No provider, no database, no
network.
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services.intelligence_schema import (
    MAX_QUOTE_CHARS,
    SECTION_NAMES,
    IntelligenceValidationError,
    QuoteTally,
    validate_intelligence,
)

TOTAL_PAGES = 9

CHUNK_3_0 = "METHODOLOGY. A two-stage hybrid framework is proposed for defect detection."
CHUNK_7_0 = "LIMITATIONS. The approach depends on prompt quality and careful tuning."
ALLOWED = {(3, 0): CHUNK_3_0, (7, 0): CHUNK_7_0}

VERBATIM_3_0 = "A two-stage hybrid framework is proposed"
VERBATIM_7_0 = "depends on prompt quality"


def _answered(evidence):
    return {"status": "answered", "summary": "A grounded summary.", "evidence": evidence}


def _blank_section():
    return {"status": "not_specified", "summary": None, "evidence": []}


def _payload(**sections):
    """Only the named sections are answered; the rest are not_specified, so a
    tally reflects exactly the evidence a test supplied."""
    body = {name: _blank_section() for name in SECTION_NAMES}
    body.update(sections)
    return body


def _validate(payload, allowed=None, tally=None):
    return validate_intelligence(
        payload,
        allowed_evidence=ALLOWED if allowed is None else allowed,
        total_pages=TOTAL_PAGES,
        tally=tally,
    )


def _tally_for(evidence, allowed=None):
    """Validate one answered section and return its tally."""
    t = QuoteTally()
    _validate(_payload(methodology=_answered(evidence)), allowed=allowed, tally=t)
    return t


# ----------------------------------------------------------------------
# offered / kept
# ----------------------------------------------------------------------
class TestOfferedAndKept(unittest.TestCase):
    def test_a_verbatim_span_counts_as_offered_and_kept(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}])
        self.assertEqual((t.offered, t.kept, t.absent, t.dropped), (1, 1, 0, 0))

    def test_several_verbatim_spans_all_count(self):
        t = _tally_for([
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
            {"page": 7, "chunk_id": 0, "quote": VERBATIM_7_0},
        ])
        self.assertEqual((t.offered, t.kept, t.dropped), (2, 2, 0))

    def test_kept_counts_only_spans_that_actually_survived(self):
        """One good, one refused: offered 2, kept 1. A counter that echoed
        the input would report kept 2."""
        t = _tally_for([
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
            {"page": 7, "chunk_id": 0, "quote": "a paraphrase of the limitation"},
        ])
        self.assertEqual((t.offered, t.kept, t.dropped_not_verbatim), (2, 1, 1))

    def test_kept_matches_the_validated_object(self):
        """The counter and the returned object must agree — the counter is
        derived from the same branch, not from a second pass."""
        t = QuoteTally()
        result = _validate(
            _payload(methodology=_answered([
                {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
                {"page": 7, "chunk_id": 0, "quote": "not present anywhere"},
            ])),
            tally=t,
        )
        surviving = [e.quote for e in result.methodology.evidence if e.quote is not None]
        self.assertEqual(len(surviving), t.kept)
        self.assertEqual(surviving, [VERBATIM_3_0])


# ----------------------------------------------------------------------
# absent — the distinction the whole change exists for
# ----------------------------------------------------------------------
class TestAbsentIsNotADrop(unittest.TestCase):
    def test_an_omitted_quote_counts_as_absent_not_dropped(self):
        t = _tally_for([{"page": 3, "chunk_id": 0}])
        self.assertEqual((t.absent, t.offered, t.dropped), (1, 0, 0))

    def test_an_explicit_null_quote_counts_as_absent(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": None}])
        self.assertEqual((t.absent, t.offered, t.dropped), (1, 0, 0))

    def test_the_production_shape_is_now_distinguishable(self):
        """Nine evidence items, zero quotes. The two hypotheses that were
        indistinguishable in production now produce different tallies."""
        nine_absent = [{"page": 3, "chunk_id": 0} for _ in range(9)]
        nine_refused = [
            {"page": 3, "chunk_id": 0, "quote": "a paraphrase, not verbatim"}
            for _ in range(9)
        ]

        a = _tally_for(nine_absent)
        b = _tally_for(nine_refused)

        # Same observable outcome: no spans stored.
        self.assertEqual((a.kept, b.kept), (0, 0))
        # Different, and now visible, cause.
        self.assertEqual((a.absent, a.offered), (9, 0))
        self.assertEqual((b.absent, b.offered, b.dropped_not_verbatim), (0, 9, 9))
        self.assertNotEqual(a.as_log_fields(), b.as_log_fields())

    def test_a_not_specified_section_contributes_nothing(self):
        t = QuoteTally()
        _validate(_payload(), tally=t)
        self.assertEqual(
            (t.offered, t.kept, t.absent, t.dropped), (0, 0, 0, 0)
        )


# ----------------------------------------------------------------------
# each drop reason, against the input that actually triggers it
# ----------------------------------------------------------------------
class TestDropReasons(unittest.TestCase):
    def test_blank_counts_as_blank_only(self):
        for blank in ("", " ", "   ", "\t", "\n"):
            with self.subTest(blank=repr(blank)):
                t = _tally_for([{"page": 3, "chunk_id": 0, "quote": blank}])
                self.assertEqual(
                    (t.offered, t.dropped_blank, t.dropped_too_long,
                     t.dropped_not_verbatim, t.kept),
                    (1, 1, 0, 0, 0),
                )

    def test_over_length_counts_as_too_long_only(self):
        chunk = "y" * (MAX_QUOTE_CHARS + 50)
        t = _tally_for(
            [{"page": 1, "chunk_id": 0, "quote": "y" * (MAX_QUOTE_CHARS + 1)}],
            allowed={(1, 0): chunk},
        )
        self.assertEqual(
            (t.offered, t.dropped_too_long, t.dropped_blank,
             t.dropped_not_verbatim, t.kept),
            (1, 1, 0, 0, 0),
        )

    def test_too_long_is_counted_even_when_the_span_IS_verbatim(self):
        """The cap fires before the substring test, so a genuinely present
        but over-long span must be attributed to length, not verbatimness."""
        chunk = "z" * (MAX_QUOTE_CHARS + 10)
        over = "z" * (MAX_QUOTE_CHARS + 5)
        self.assertIn(over, chunk)
        t = _tally_for([{"page": 1, "chunk_id": 0, "quote": over}],
                       allowed={(1, 0): chunk})
        self.assertEqual((t.dropped_too_long, t.dropped_not_verbatim), (1, 0))

    def test_non_verbatim_counts_as_not_verbatim_only(self):
        t = _tally_for([{"page": 3, "chunk_id": 0,
                         "quote": "The authors prove their method is best."}])
        self.assertEqual(
            (t.offered, t.dropped_not_verbatim, t.dropped_blank,
             t.dropped_too_long, t.kept),
            (1, 1, 0, 0, 0),
        )

    def test_a_span_from_another_supplied_chunk_counts_as_not_verbatim(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": VERBATIM_7_0}])
        self.assertEqual((t.dropped_not_verbatim, t.kept), (1, 0))

    def test_whitespace_padded_span_counts_as_not_verbatim_not_blank(self):
        """"  span  " is neither empty nor over-long; it fails the substring
        test. The tally must say so rather than guessing."""
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": "  " + VERBATIM_3_0 + "  "}])
        self.assertEqual(
            (t.dropped_blank, t.dropped_too_long, t.dropped_not_verbatim),
            (0, 0, 1),
        )

    def test_the_newline_normalisation_case_is_attributed_to_not_verbatim(self):
        """The realistic near-miss: stored chunks keep newlines, and a model
        that reproduces one as a space fails the exact test. The tally names
        the reason, which is what makes the cause diagnosable."""
        chunk = "a\nlightweight detector localises candidate regions"
        t = _tally_for(
            [{"page": 1, "chunk_id": 0, "quote": chunk.replace("\n", " ")}],
            allowed={(1, 0): chunk},
        )
        self.assertEqual((t.offered, t.dropped_not_verbatim, t.kept), (1, 1, 0))

    def test_mixed_reasons_are_counted_separately(self):
        chunk = "q" * (MAX_QUOTE_CHARS + 20)
        allowed = {(1, 0): chunk, (3, 0): CHUNK_3_0}
        t = _tally_for([
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},          # kept
            {"page": 3, "chunk_id": 0, "quote": "   "},                  # blank
            {"page": 1, "chunk_id": 0, "quote": "q" * (MAX_QUOTE_CHARS + 1)},  # long
            {"page": 3, "chunk_id": 0, "quote": "invented text"},        # not verbatim
            {"page": 3, "chunk_id": 0},                                  # absent
        ], allowed=allowed)

        self.assertEqual(t.offered, 4)
        self.assertEqual(t.kept, 1)
        self.assertEqual(t.absent, 1)
        self.assertEqual(t.dropped_blank, 1)
        self.assertEqual(t.dropped_too_long, 1)
        self.assertEqual(t.dropped_not_verbatim, 1)
        self.assertEqual(t.dropped, 3)


# ----------------------------------------------------------------------
# arithmetic invariants
# ----------------------------------------------------------------------
class TestInvariants(unittest.TestCase):
    def test_offered_equals_kept_plus_dropped(self):
        t = _tally_for([
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
            {"page": 3, "chunk_id": 0, "quote": ""},
            {"page": 3, "chunk_id": 0, "quote": "nope"},
            {"page": 3, "chunk_id": 0},
        ])
        self.assertEqual(t.offered, t.kept + t.dropped)

    def test_offered_plus_absent_equals_the_evidence_count(self):
        evidence = [
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
            {"page": 7, "chunk_id": 0},
            {"page": 7, "chunk_id": 0, "quote": "nope"},
        ]
        t = _tally_for(evidence)
        self.assertEqual(t.offered + t.absent, len(evidence))

    def test_a_tally_spans_every_answered_section(self):
        t = QuoteTally()
        _validate(
            _payload(
                methodology=_answered([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}]),
                limitations=_answered([{"page": 7, "chunk_id": 0, "quote": VERBATIM_7_0}]),
                dataset=_answered([{"page": 3, "chunk_id": 0}]),
            ),
            tally=t,
        )
        self.assertEqual((t.offered, t.kept, t.absent), (2, 2, 1))

    def test_a_fresh_tally_starts_at_zero(self):
        t = QuoteTally()
        self.assertEqual(
            (t.offered, t.kept, t.absent, t.dropped_blank,
             t.dropped_too_long, t.dropped_not_verbatim, t.dropped),
            (0, 0, 0, 0, 0, 0, 0),
        )


# ----------------------------------------------------------------------
# the tally is safe to log
# ----------------------------------------------------------------------
class TestLogSafety(unittest.TestCase):
    def test_log_fields_contain_only_integers(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}])
        fields = t.as_log_fields()
        for pair in fields.split():
            key, _, value = pair.partition("=")
            self.assertTrue(key)
            self.assertTrue(value.isdigit(), f"{pair} is not key=<integer>")

    def test_log_fields_leak_no_quote_or_chunk_text(self):
        t = _tally_for([
            {"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0},
            {"page": 7, "chunk_id": 0, "quote": "SECRET PARAPHRASE"},
        ])
        fields = t.as_log_fields()
        for leak in (VERBATIM_3_0, "SECRET PARAPHRASE", CHUNK_3_0, CHUNK_7_0,
                     "METHODOLOGY", "two-stage"):
            self.assertNotIn(leak, fields)

    def test_log_fields_leak_no_page_or_chunk_identity(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}])
        fields = t.as_log_fields()
        self.assertNotIn("page=", fields)
        self.assertNotIn("chunk_id=", fields)

    def test_the_repr_carries_no_text_either(self):
        t = _tally_for([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}])
        self.assertNotIn(VERBATIM_3_0, repr(t))

    def test_all_six_counters_appear_in_the_log_line(self):
        t = QuoteTally()
        for key in ("offered", "kept", "absent", "blank", "too_long", "not_verbatim"):
            self.assertIn(key + "=", t.as_log_fields())


# ----------------------------------------------------------------------
# the tally changed no validation decision
# ----------------------------------------------------------------------
class TestBehaviourUnchanged(unittest.TestCase):
    CASES = [
        ("kept", [{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}], VERBATIM_3_0),
        ("absent", [{"page": 3, "chunk_id": 0}], None),
        ("null", [{"page": 3, "chunk_id": 0, "quote": None}], None),
        ("blank", [{"page": 3, "chunk_id": 0, "quote": "  "}], None),
        ("not_verbatim", [{"page": 3, "chunk_id": 0, "quote": "nope"}], None),
    ]

    def test_the_stored_quote_is_the_same_with_and_without_a_tally(self):
        for label, evidence, expected in self.CASES:
            with self.subTest(case=label):
                without = _validate(_payload(methodology=_answered(evidence)))
                with_tally = _validate(
                    _payload(methodology=_answered(evidence)), tally=QuoteTally()
                )
                self.assertEqual(without.methodology.evidence[0].quote, expected)
                self.assertEqual(with_tally.methodology.evidence[0].quote, expected)

    def test_the_tally_argument_is_optional(self):
        """Every pre-existing caller omits it, so this must keep working."""
        result = _validate(_payload(
            methodology=_answered([{"page": 3, "chunk_id": 0, "quote": VERBATIM_3_0}])))
        self.assertEqual(result.methodology.evidence[0].quote, VERBATIM_3_0)

    def test_an_unsupplied_reference_still_raises_before_any_counting(self):
        t = QuoteTally()
        with self.assertRaises(IntelligenceValidationError) as caught:
            _validate(
                _payload(methodology=_answered(
                    [{"page": 5, "chunk_id": 9, "quote": VERBATIM_3_0}])),
                tally=t,
            )
        self.assertEqual(caught.exception.code, "evidence_not_supplied")
        # The reference is rejected before the quote gates run.
        self.assertEqual((t.offered, t.kept, t.absent), (0, 0, 0))

    def test_the_reference_survives_a_dropped_span_as_before(self):
        result = _validate(
            _payload(methodology=_answered(
                [{"page": 3, "chunk_id": 0, "quote": "not verbatim"}])),
            tally=QuoteTally(),
        )
        item = result.methodology.evidence[0]
        self.assertEqual((item.page, item.chunk_id), (3, 0))
        self.assertIsNone(item.quote)


# ----------------------------------------------------------------------
# the endpoint wires it up, and does NOT expose it
# ----------------------------------------------------------------------
class TestEndpointWiring(unittest.TestCase):
    def setUp(self):
        import app.api.paper_intelligence as pipeline
        self.pipeline = pipeline
        with open(pipeline.__file__, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_endpoint_passes_a_tally_to_the_validator(self):
        self.assertIn("tally=tally", self.source)

    def test_the_tally_is_bound_before_the_try_so_the_handler_can_log_it(self):
        self.assertLess(
            self.source.index("tally = QuoteTally()"),
            self.source.index("        # -- 2. exact, owner-scoped paper resolution"),
        )

    def test_both_log_lines_carry_the_counts(self):
        self.assertEqual(self.source.count("tally.as_log_fields()"), 2)

    def test_the_counters_are_not_in_any_response_body(self):
        """Explicitly out of the public API for now."""
        for banned in ('"offered"', '"kept"', '"absent"', '"quote_count"',
                       '"spans"', '"dropped_not_verbatim"'):
            self.assertNotIn(banned, self.source)

    def test_the_prompt_was_not_touched(self):
        self.assertIn('8. Each evidence entry MAY include a "quote"', self.source)
        self.assertNotIn('Do NOT include a "quote"', self.source)

    def test_the_budgets_were_not_touched(self):
        self.assertEqual(self.pipeline.INTELLIGENCE_MAX_TOKENS, 3000)
        self.assertEqual(self.pipeline.INTELLIGENCE_CONTEXT_CHARS, 12000)
        self.assertEqual(MAX_QUOTE_CHARS, 200)


if __name__ == "__main__":
    unittest.main()
