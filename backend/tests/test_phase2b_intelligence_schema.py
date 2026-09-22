"""
Phase 2B step 1 — the Paper Intelligence schema and its validator.

This module is the gate between a language model's output and durable
storage, so the tests are mostly about what must be REFUSED.

Three properties carry the most weight:

  * evidence identity is the PAIR (page, chunk_id). chunk_id is the
    chunk's index within its page, so page 3 chunk 0 and page 7 chunk 0
    both exist. A validator keyed on chunk_id alone would accept
    evidence pointing at the wrong passage, and that bug would be
    invisible — the page shown to the reader would look plausible.

  * references are never repaired. A page or pair that does not check
    out fails the object outright; it is never remapped, and evidence is
    never invented to prop up a section.

  * the model is not trusted for identity. owner_id, paper_id, title and
    point ids are not in the schema, and extra="forbid" rejects a
    response that tries to supply them.

Pure and offline: no provider, no Qdrant, no database, no network.
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import json

from app.services.intelligence_schema import (
    SECTION_NAMES,
    Evidence,
    IntelligenceValidationError,
    PaperIntelligence,
    Section,
    build_allowlist,
    parse_intelligence_json,
    validate_intelligence,
)

TOTAL_PAGES = 9

#: The allowlist the SERVER would build. Note (3, 0) and (7, 0) share a
#: chunk_id on different pages — the case a global-chunk_id design gets
#: wrong.
ALLOWED = {
    (1, 0): "We address defect detection in PCB inspection.",
    (3, 0): "METHODOLOGY. A two-stage hybrid framework is proposed.",
    (3, 1): "Stage two performs zero-shot classification.",
    (7, 0): "LIMITATIONS. The approach depends on prompt quality.",
}

OTHER_PAPER_CHUNK = (4, 0)  # deliberately absent from ALLOWED


def section(status="answered", summary="A summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def full_payload(**overrides):
    """A complete, valid object, with named sections overridden."""
    payload = {name: section() for name in SECTION_NAMES}
    payload.update(overrides)
    return payload


def validate(payload, allowed=None, total_pages=TOTAL_PAGES):
    return validate_intelligence(
        payload,
        allowed_evidence=ALLOWED if allowed is None else allowed,
        total_pages=total_pages,
    )


class SchemaTestCase(unittest.TestCase):
    def assertRejected(self, payload, code=None, **kwargs):
        with self.assertRaises(IntelligenceValidationError) as ctx:
            validate(payload, **kwargs)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        return ctx.exception


# ======================================================================
# 1. Accepting what is valid
# ======================================================================
class TestValidObjects(SchemaTestCase):
    def test_a_complete_object_is_accepted(self):
        result = validate(full_payload())

        self.assertIsInstance(result, PaperIntelligence)
        self.assertEqual(len(result.sections()), 10)
        self.assertEqual(tuple(result.sections()), SECTION_NAMES)

    def test_sections_may_be_not_specified(self):
        result = validate(
            full_payload(
                dataset=section(status="not_specified", summary=None, evidence=[]),
                reproducibility=section(status="not_specified", summary=None, evidence=[]),
            )
        )

        self.assertEqual(result.dataset.status, "not_specified")
        self.assertIsNone(result.dataset.summary)
        self.assertEqual(result.dataset.evidence, [])
        self.assertEqual(result.methodology.status, "answered")

    def test_every_section_may_be_not_specified(self):
        # A paper the model genuinely cannot characterise is a valid
        # result, not an error.
        payload = {
            name: section(status="not_specified", summary=None, evidence=[])
            for name in SECTION_NAMES
        }

        result = validate(payload)

        self.assertTrue(all(s.status == "not_specified" for s in result.sections().values()))

    def test_the_same_chunk_id_on_different_pages_is_distinct(self):
        # (3, 0) and (7, 0) are different evidence. Both are allowed, and
        # neither may stand in for the other.
        result = validate(
            full_payload(
                methodology=section(evidence=[{"page": 3, "chunk_id": 0}]),
                limitations=section(evidence=[{"page": 7, "chunk_id": 0}]),
            )
        )

        self.assertEqual(result.methodology.evidence[0].page, 3)
        self.assertEqual(result.limitations.evidence[0].page, 7)
        self.assertEqual(result.methodology.evidence[0].chunk_id, 0)
        self.assertEqual(result.limitations.evidence[0].chunk_id, 0)

    def test_multiple_evidence_items_are_kept_in_order(self):
        result = validate(
            full_payload(
                methodology=section(
                    evidence=[
                        {"page": 3, "chunk_id": 1},
                        {"page": 1, "chunk_id": 0},
                        {"page": 3, "chunk_id": 0},
                    ]
                )
            )
        )

        self.assertEqual(
            [(e.page, e.chunk_id) for e in result.methodology.evidence],
            [(3, 1), (1, 0), (3, 0)],
        )


# ======================================================================
# 2. Structure
# ======================================================================
class TestStructure(SchemaTestCase):
    def test_an_unknown_section_is_rejected(self):
        payload = full_payload()
        payload["future_work"] = section()

        self.assertRejected(payload, "invalid_structure")

    def test_a_missing_section_is_rejected(self):
        # Absent and "explicitly not specified" are different claims;
        # only the model can make the second one.
        payload = full_payload()
        del payload["limitations"]

        self.assertRejected(payload, "invalid_structure")

    def test_an_unknown_field_inside_a_section_is_rejected(self):
        payload = full_payload()
        payload["methodology"]["confidence"] = 0.9

        self.assertRejected(payload, "invalid_structure")

    def test_an_unknown_field_inside_evidence_is_rejected(self):
        payload = full_payload(
            methodology=section(
                evidence=[{"page": 3, "chunk_id": 0, "point_id": "abc-123"}]
            )
        )

        self.assertRejected(payload, "invalid_structure")

    def test_identity_fields_from_the_model_are_rejected(self):
        # The model must not be able to assert whose paper this is.
        for field, value in (
            ("owner_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            ("paper_id", "11111111-1111-4111-8111-111111111111"),
            ("paper", "some_other_paper.pdf"),
        ):
            with self.subTest(field=field):
                payload = full_payload()
                payload[field] = value
                self.assertRejected(payload, "invalid_structure")

    def test_an_invalid_status_is_rejected(self):
        for bad in ("Answered", "unknown", "partial", "", None):
            with self.subTest(status=bad):
                self.assertRejected(
                    full_payload(methodology=section(status=bad)), "invalid_structure"
                )


# ======================================================================
# 3. Status / evidence coherence
# ======================================================================
class TestCoherence(SchemaTestCase):
    def test_answered_without_evidence_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[])), "invalid_structure"
        )

    def test_answered_without_a_summary_is_rejected(self):
        for empty in (None, "", "   "):
            with self.subTest(summary=empty):
                self.assertRejected(
                    full_payload(methodology=section(summary=empty)), "invalid_structure"
                )

    def test_not_specified_with_evidence_is_rejected(self):
        self.assertRejected(
            full_payload(
                dataset=section(
                    status="not_specified",
                    summary=None,
                    evidence=[{"page": 3, "chunk_id": 0}],
                )
            ),
            "invalid_structure",
        )

    def test_not_specified_prose_is_normalized_to_null(self):
        # Whatever wording the model chose for "absent", the stored form
        # is canonical so the UI never pattern-matches on English.
        result = validate(
            full_payload(
                dataset=section(
                    status="not_specified",
                    summary="Not specified in the paper.",
                    evidence=[],
                )
            )
        )

        self.assertIsNone(result.dataset.summary)


# ======================================================================
# 4. Page and evidence references
# ======================================================================
class TestEvidenceReferences(SchemaTestCase):
    def test_page_zero_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 0, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_negative_page_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": -3, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_negative_chunk_id_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": -1}])),
            "invalid_structure",
        )

    def test_a_page_beyond_the_paper_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 99, "chunk_id": 0}])),
            "evidence_out_of_range",
        )

    def test_a_page_not_supplied_is_rejected(self):
        # Page 4 is inside the paper but was never given to the model.
        self.assertRejected(
            full_payload(
                methodology=section(
                    evidence=[{"page": OTHER_PAPER_CHUNK[0], "chunk_id": OTHER_PAPER_CHUNK[1]}]
                )
            ),
            "evidence_not_supplied",
        )

    def test_an_unsupplied_chunk_on_a_supplied_page_is_rejected(self):
        # Page 3 was supplied; chunk 9 of it was not.
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": 9}])),
            "evidence_not_supplied",
        )

    def test_evidence_from_another_paper_cannot_enter(self):
        # The allowlist is built from chunks already filtered by owner
        # and paper, so another paper's pair is simply not a key. This is
        # the structural reason cross-paper and cross-owner evidence
        # cannot be validated, not a rule applied afterwards.
        #
        # The page is deliberately INSIDE this paper (4 <= TOTAL_PAGES),
        # so the range guard cannot fire first and mask the check under
        # test. Only membership of the allowlist decides it.
        foreign_page, foreign_chunk = OTHER_PAPER_CHUNK
        self.assertLessEqual(foreign_page, TOTAL_PAGES)
        self.assertNotIn(OTHER_PAPER_CHUNK, ALLOWED)

        cites_foreign = full_payload(
            methodology=section(
                evidence=[{"page": foreign_page, "chunk_id": foreign_chunk}]
            )
        )

        self.assertRejected(cites_foreign, "evidence_not_supplied", allowed=ALLOWED)

        # ...and the very same object validates once the server actually
        # supplied that chunk. Nothing about the model output changed.
        supplied = {**ALLOWED, OTHER_PAPER_CHUNK: "Text the server did supply."}
        ok = validate(cites_foreign, allowed=supplied)

        self.assertEqual(
            (ok.methodology.evidence[0].page, ok.methodology.evidence[0].chunk_id),
            OTHER_PAPER_CHUNK,
        )

    def test_a_bad_reference_fails_the_object_rather_than_being_repaired(self):
        payload = full_payload(
            methodology=section(
                evidence=[{"page": 3, "chunk_id": 0}, {"page": 3, "chunk_id": 9}]
            )
        )

        # The valid first item does not rescue the invalid second one,
        # and the second is not remapped to a nearby chunk.
        self.assertRejected(payload, "evidence_not_supplied")


# ======================================================================
# 4b. Reference types are strict
# ======================================================================
class TestReferenceTypeStrictness(SchemaTestCase):
    """page and chunk_id are integers, not "integers after coercion".

    Pydantic's default lax mode accepts "3", 3.0 and True for an int
    field. The first two are merely sloppy; True is dangerous, because it
    coerces to 1 — a NONSENSE value silently becoming a PLAUSIBLE page.
    If (1, 0) is in the allowlist that evidence validates, and a reader
    is shown "Page 1" for a claim the model never located there.

    These fields are therefore strict while the rest of the schema stays
    lax: coercion is fine for prose, but a citation is a reference, and
    references are never repaired.
    """

    def test_a_numeric_string_page_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": "3", "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_numeric_string_chunk_id_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": "0"}])),
            "invalid_structure",
        )

    def test_a_float_page_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3.0, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_float_chunk_id_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": 0.0}])),
            "invalid_structure",
        )

    def test_a_fractional_page_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": 3.7, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_boolean_page_is_rejected_rather_than_becoming_page_one(self):
        # The case that decided the design. (1, 0) IS in the allowlist,
        # so lax coercion would have accepted this and displayed "Page 1".
        self.assertIn((1, 0), ALLOWED)

        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": True, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_a_whitespace_padded_page_is_rejected(self):
        self.assertRejected(
            full_payload(methodology=section(evidence=[{"page": " 3 ", "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_null_and_non_numeric_references_are_rejected(self):
        for bad in (None, "abc", [], {}):
            with self.subTest(page=bad):
                self.assertRejected(
                    full_payload(
                        methodology=section(evidence=[{"page": bad, "chunk_id": 0}])
                    ),
                    "invalid_structure",
                )

    def test_plain_integers_are_still_accepted(self):
        result = validate(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": 1}]))
        )

        self.assertEqual(result.methodology.evidence[0].page, 3)
        self.assertEqual(result.methodology.evidence[0].chunk_id, 1)

    def test_prose_fields_remain_lenient(self):
        # Strictness is deliberately scoped to the reference fields; a
        # summary is prose and does not carry the same risk.
        result = validate(full_payload(methodology=section(summary="  spaced  ")))
        self.assertEqual(result.methodology.summary, "spaced")


# ======================================================================
# 5. Quotes
# ======================================================================
class TestQuotes(SchemaTestCase):
    def test_a_verbatim_quote_is_kept(self):
        result = validate(
            full_payload(
                methodology=section(
                    evidence=[
                        {
                            "page": 3,
                            "chunk_id": 0,
                            "quote": "A two-stage hybrid framework is proposed.",
                        }
                    ]
                )
            )
        )

        self.assertEqual(
            result.methodology.evidence[0].quote,
            "A two-stage hybrid framework is proposed.",
        )

    def test_a_paraphrased_quote_is_dropped_but_the_reference_survives(self):
        result = validate(
            full_payload(
                methodology=section(
                    evidence=[
                        {
                            "page": 3,
                            "chunk_id": 0,
                            "quote": "The authors propose a two-stage framework.",
                        }
                    ]
                )
            )
        )

        item = result.methodology.evidence[0]
        self.assertIsNone(item.quote)
        # The clickable part — the reference — is untouched.
        self.assertEqual((item.page, item.chunk_id), (3, 0))

    def test_a_quote_from_a_different_supplied_chunk_is_dropped(self):
        # Verbatim text, but not from the chunk it was attached to.
        result = validate(
            full_payload(
                methodology=section(
                    evidence=[
                        {
                            "page": 3,
                            "chunk_id": 0,
                            "quote": "The approach depends on prompt quality.",
                        }
                    ]
                )
            )
        )

        self.assertIsNone(result.methodology.evidence[0].quote)

    def test_absent_quotes_stay_absent(self):
        result = validate(full_payload())
        self.assertIsNone(result.methodology.evidence[0].quote)


# ======================================================================
# 6. Parsing
# ======================================================================
class TestParsing(SchemaTestCase):
    def test_strict_json_is_accepted(self):
        self.assertEqual(parse_intelligence_json('{"a": 1}'), {"a": 1})

    def test_one_fenced_block_is_accepted(self):
        self.assertEqual(parse_intelligence_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_intelligence_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_malformed_json_is_rejected(self):
        for bad in ("", "   ", "not json", "{unclosed", '{"a": }', None, 42):
            with self.subTest(raw=bad):
                with self.assertRaises(IntelligenceValidationError) as ctx:
                    parse_intelligence_json(bad)
                self.assertEqual(ctx.exception.code, "malformed_json")

    def test_a_json_array_is_rejected(self):
        with self.assertRaises(IntelligenceValidationError) as ctx:
            parse_intelligence_json("[1, 2, 3]")
        self.assertEqual(ctx.exception.code, "malformed_json")

    def test_prose_around_the_object_is_not_salvaged(self):
        # Salvaging this would hide that the model ignored its
        # instructions.
        with self.assertRaises(IntelligenceValidationError):
            parse_intelligence_json('Here is the result:\n{"a": 1}\nHope that helps!')

    def test_a_raw_json_string_validates_end_to_end(self):
        result = validate(json.dumps(full_payload()))
        self.assertEqual(result.methodology.status, "answered")

    def test_the_error_message_does_not_echo_model_text(self):
        secret = "SENSITIVE-MODEL-TEXT-12345"
        with self.assertRaises(IntelligenceValidationError) as ctx:
            parse_intelligence_json(secret)

        self.assertNotIn(secret, ctx.exception.message)
        self.assertNotIn(secret, str(ctx.exception))


# ======================================================================
# 7. Determinism and purity
# ======================================================================
class TestDeterminismAndPurity(SchemaTestCase):
    def test_the_same_input_always_produces_the_same_output(self):
        payload = full_payload()

        first = validate(payload)
        second = validate(payload)

        self.assertEqual(first.model_dump(), second.model_dump())

    def test_the_input_is_not_mutated(self):
        payload = full_payload(
            dataset=section(status="not_specified", summary="Not specified.", evidence=[]),
            methodology=section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": "a paraphrase"}]
            ),
        )
        before = json.dumps(payload, sort_keys=True)

        validate(payload)

        self.assertEqual(json.dumps(payload, sort_keys=True), before)

    def test_evidence_is_never_invented(self):
        result = validate(
            full_payload(methodology=section(evidence=[{"page": 3, "chunk_id": 0}]))
        )

        self.assertEqual(len(result.methodology.evidence), 1)
        for name, sec in result.sections().items():
            with self.subTest(section=name):
                if sec.status == "not_specified":
                    self.assertEqual(sec.evidence, [])

    def test_a_paper_with_no_pages_is_rejected(self):
        self.assertRejected(full_payload(), "invalid_structure", total_pages=0)

    def test_validation_is_provider_independent(self):
        """The module must not IMPORT provider, database or network code.

        Checked against the parsed import statements rather than the
        source text: the docstring legitimately names Groq, Voyage and
        Qdrant while explaining what it does not touch, and a substring
        search cannot tell documentation from a dependency. Deleting
        accurate documentation to satisfy a grep would be the wrong
        trade.
        """
        import ast

        import app.services.intelligence_schema as mod

        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())

        imported: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        # Exactly what a pure schema needs, and nothing that can reach out.
        self.assertEqual(imported, {"__future__", "json", "typing", "pydantic"})

        for forbidden in (
            "groq", "voyageai", "qdrant_client", "requests", "httpx",
            "sqlalchemy", "fastapi", "app", "supabase", "os", "socket",
        ):
            self.assertNotIn(forbidden, imported)


# ======================================================================
# 8. The allowlist builder
# ======================================================================
class TestBuildAllowlist(SchemaTestCase):
    def test_it_keys_on_the_page_and_chunk_pair(self):
        chunks = [
            {"page": 3, "chunk_id": 0, "text": "alpha"},
            {"page": 7, "chunk_id": 0, "text": "beta"},
        ]

        allowlist, highest = build_allowlist(chunks)

        self.assertEqual(allowlist[(3, 0)], "alpha")
        self.assertEqual(allowlist[(7, 0)], "beta")
        self.assertEqual(highest, 7)

    def test_unusable_payloads_are_skipped_not_guessed(self):
        chunks = [
            {"page": 3, "chunk_id": 0, "text": "good"},
            {"page": "n/a", "chunk_id": 0, "text": "bad page"},
            {"chunk_id": 0, "text": "no page"},
            {"page": 0, "chunk_id": 0, "text": "page zero"},
        ]

        allowlist, highest = build_allowlist(chunks)

        self.assertEqual(list(allowlist), [(3, 0)])
        self.assertEqual(highest, 3)

    def test_an_empty_paper_yields_an_empty_allowlist(self):
        self.assertEqual(build_allowlist([]), ({}, 0))

    def test_it_round_trips_with_the_validator(self):
        chunks = [
            {"page": 2, "chunk_id": 0, "text": "Some methodology text."},
            {"page": 5, "chunk_id": 1, "text": "Some results text."},
        ]
        allowlist, highest = build_allowlist(chunks)

        # Every other section is not_specified, because this allowlist
        # contains only the two pairs above. full_payload()'s default
        # answered sections cite (3, 0), which this server never supplied
        # — and the validator would rightly reject them.
        payload = {
            name: section(status="not_specified", summary=None, evidence=[])
            for name in SECTION_NAMES
        }
        payload["methodology"] = section(evidence=[{"page": 2, "chunk_id": 0}])
        payload["key_results"] = section(
            evidence=[{"page": 5, "chunk_id": 1, "quote": "Some results text."}]
        )

        result = validate(payload, allowed=allowlist, total_pages=highest)

        self.assertEqual(result.methodology.evidence[0].page, 2)
        self.assertEqual(result.key_results.evidence[0].quote, "Some results text.")
        self.assertEqual(result.dataset.status, "not_specified")


if __name__ == "__main__":
    unittest.main()
