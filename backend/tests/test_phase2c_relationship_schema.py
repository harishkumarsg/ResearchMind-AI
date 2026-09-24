"""
Phase 2C step 2C-1 — the cross-paper relationship schema and validator.

This module is the gate between a model's opinion about two papers and
durable storage, so the tests are mostly about what must be REFUSED.

Four properties carry the most weight:

  * A/B evidence isolation. (page, chunk_id) is meaningless without
    knowing whose paper it is — page 3 chunk 0 exists in nearly every
    paper — so a citation valid in B must NOT rescue a citation made for
    A. A validator that merged the two namespaces would mix two papers'
    evidence while looking perfectly well-formed.

  * An asserted relationship costs evidence from BOTH sides. aligned,
    divergent and complementary each claim something about two papers;
    one-sided evidence would read as a cross-paper finding while resting
    on one paper.

  * not_comparable carries nothing. Evidence there would imply the
    comparison the model just declined to make.

  * The deterministic verdicts are NOT repeated here. comparable /
    a_only / b_only / neither belong to the offline engine; this schema
    must not offer a second, competing way to express them.

Pure and offline: no provider, no Qdrant, no database, no network.
"""
import ast
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services.intelligence_schema import SECTION_NAMES
from app.services.relationship_schema import (
    GROUNDED_RELATIONS,
    RELATION_ALIGNED,
    RELATION_COMPLEMENTARY,
    RELATION_DIVERGENT,
    RELATION_NOT_COMPARABLE,
    RELATIONS,
    PaperRelationship,
    RelationshipValidationError,
    build_relationship_allowlists,
    parse_relationship_json,
    validate_relationship,
)

MODULE_PATH = os.path.join(
    BACKEND_DIR, "app", "services", "relationship_schema.py"
)

#: Paper A's supplied chunks. Note (3, 0) is in BOTH papers — that
#: overlap is the whole point of the isolation tests.
ALLOWED_A = {
    (1, 0): "Paper A: we address defect detection in PCB inspection.",
    (3, 0): "Paper A: a two-stage hybrid framework is proposed.",
    (7, 0): "Paper A: the approach depends on prompt quality.",
}

#: Paper B's supplied chunks. (5, 2) exists only here.
ALLOWED_B = {
    (2, 0): "Paper B: we study surface defect segmentation.",
    (3, 0): "Paper B: an end-to-end transformer is used.",
    (5, 2): "Paper B: evaluated on 4,000 held-out images.",
}

#: Only in A.
A_ONLY = (1, 0)
#: Only in B.
B_ONLY = (5, 2)
#: In neither.
NOWHERE = (9, 9)

COMPARABLE = ("methodology", "key_results")


def cite(pair):
    page, chunk_id = pair
    return {"page": page, "chunk_id": chunk_id}


def section(
    relation=RELATION_ALIGNED,
    statement="Both papers describe a staged pipeline.",
    cites_a=None,
    cites_b=None,
):
    if relation in GROUNDED_RELATIONS:
        if cites_a is None:
            cites_a = [cite((3, 0))]
        if cites_b is None:
            cites_b = [cite((3, 0))]
    return {
        "relation": relation,
        "statement": statement,
        "cites_a": cites_a if cites_a is not None else [],
        "cites_b": cites_b if cites_b is not None else [],
    }


def payload(**overrides):
    """A complete, valid object covering exactly the comparable set."""
    sections = {name: section() for name in COMPARABLE}
    sections.update(overrides)
    return {"sections": sections}


def validate(raw, comparable=COMPARABLE, allowed_a=None, allowed_b=None):
    return validate_relationship(
        raw,
        comparable_sections=comparable,
        allowed_evidence_a=ALLOWED_A if allowed_a is None else allowed_a,
        allowed_evidence_b=ALLOWED_B if allowed_b is None else allowed_b,
    )


class RelationshipTestCase(unittest.TestCase):
    def assertRejected(self, raw, code=None, **kwargs):
        with self.assertRaises(RelationshipValidationError) as ctx:
            validate(raw, **kwargs)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        return ctx.exception


# ----------------------------------------------------------------------
# 1-4. The four valid relations
# ----------------------------------------------------------------------
class TestValidRelationships(RelationshipTestCase):

    def test_aligned_is_accepted(self):
        result = validate(payload(methodology=section(RELATION_ALIGNED)))
        self.assertEqual(result.sections["methodology"].relation, RELATION_ALIGNED)

    def test_divergent_is_accepted(self):
        result = validate(payload(methodology=section(RELATION_DIVERGENT)))
        self.assertEqual(result.sections["methodology"].relation, RELATION_DIVERGENT)

    def test_complementary_is_accepted(self):
        result = validate(payload(methodology=section(RELATION_COMPLEMENTARY)))
        self.assertEqual(
            result.sections["methodology"].relation, RELATION_COMPLEMENTARY
        )

    def test_not_comparable_with_no_citations_is_accepted(self):
        result = validate(
            payload(
                methodology=section(
                    RELATION_NOT_COMPARABLE,
                    statement="The papers measure different things.",
                    cites_a=[],
                    cites_b=[],
                )
            )
        )
        section_result = result.sections["methodology"]
        self.assertEqual(section_result.relation, RELATION_NOT_COMPARABLE)
        self.assertEqual(section_result.cites_a, [])
        self.assertEqual(section_result.cites_b, [])

    def test_the_vocabulary_is_exactly_four_neutral_values(self):
        self.assertEqual(
            set(RELATIONS),
            {"aligned", "divergent", "complementary", "not_comparable"},
        )
        for banned in ("better", "worse", "superior", "inferior", "winner"):
            self.assertNotIn(banned, RELATIONS)

    def test_deterministic_verdicts_are_not_repeated_here(self):
        """comparable / a_only / b_only / neither belong to the offline
        engine. A second way to say them would compete with it."""
        for verdict in ("comparable", "a_only", "b_only", "neither",
                        "insufficient_evidence"):
            self.assertNotIn(verdict, RELATIONS)

    def test_citations_survive_in_order(self):
        result = validate(
            payload(
                methodology=section(
                    cites_a=[cite((7, 0)), cite((1, 0)), cite((3, 0))],
                    cites_b=[cite((5, 2)), cite((2, 0))],
                )
            )
        )
        got = result.sections["methodology"]
        self.assertEqual([(e.page, e.chunk_id) for e in got.cites_a],
                         [(7, 0), (1, 0), (3, 0)])
        self.assertEqual([(e.page, e.chunk_id) for e in got.cites_b],
                         [(5, 2), (2, 0)])

    def test_statement_is_trimmed_but_otherwise_untouched(self):
        result = validate(
            payload(methodology=section(statement="  A careful reading.  "))
        )
        self.assertEqual(result.sections["methodology"].statement, "A careful reading.")


# ----------------------------------------------------------------------
# 5-14. Relation / citation coherence
# ----------------------------------------------------------------------
class TestRelationCitationCoherence(RelationshipTestCase):

    def test_empty_statement_is_rejected(self):
        self.assertRejected(payload(methodology=section(statement="")),
                            "invalid_structure")

    def test_whitespace_only_statement_is_rejected(self):
        self.assertRejected(payload(methodology=section(statement="   \n\t ")),
                            "invalid_structure")

    def test_grounded_relation_without_paper_a_evidence_is_rejected(self):
        for relation in GROUNDED_RELATIONS:
            with self.subTest(relation=relation):
                self.assertRejected(
                    payload(methodology=section(relation, cites_a=[])),
                    "invalid_structure",
                )

    def test_grounded_relation_without_paper_b_evidence_is_rejected(self):
        for relation in GROUNDED_RELATIONS:
            with self.subTest(relation=relation):
                self.assertRejected(
                    payload(methodology=section(relation, cites_b=[])),
                    "invalid_structure",
                )

    def test_not_comparable_with_paper_a_evidence_is_rejected(self):
        self.assertRejected(
            payload(
                methodology=section(
                    RELATION_NOT_COMPARABLE, cites_a=[cite((3, 0))], cites_b=[]
                )
            ),
            "invalid_structure",
        )

    def test_not_comparable_with_paper_b_evidence_is_rejected(self):
        self.assertRejected(
            payload(
                methodology=section(
                    RELATION_NOT_COMPARABLE, cites_a=[], cites_b=[cite((3, 0))]
                )
            ),
            "invalid_structure",
        )

    def test_an_unknown_relation_is_rejected(self):
        """Supplied WITH valid evidence on both sides, so the only rule
        that can reject these is the vocabulary itself.

        Mutation testing caught this: with empty citations the cases were
        rejected by the missing-evidence rule instead, and the test
        passed even with the vocabulary check removed entirely."""
        both = dict(cites_a=[cite((3, 0))], cites_b=[cite((3, 0))])

        for bad in ("better", "superior", "winner", "agree", "", "ALIGNED",
                    "Aligned", "aligned ", "score"):
            with self.subTest(relation=bad):
                self.assertRejected(
                    payload(methodology=section(bad, **both)), "invalid_structure"
                )

    def test_a_non_string_relation_is_rejected(self):
        both = dict(cites_a=[cite((3, 0))], cites_b=[cite((3, 0))])
        for bad in (None, 1, True, ["aligned"], {"relation": "aligned"}):
            with self.subTest(relation=bad):
                self.assertRejected(
                    payload(methodology=section(bad, **both)), "invalid_structure"
                )


# ----------------------------------------------------------------------
# 17-27, 41-46. Structure and strict typing
# ----------------------------------------------------------------------
class TestStructureAndTyping(RelationshipTestCase):

    def test_extra_top_level_field_is_rejected(self):
        self.assertRejected(
            dict(payload(), model="gpt"), "invalid_structure"
        )

    def test_extra_section_field_is_rejected(self):
        bad = section()
        bad["confidence"] = 0.9
        self.assertRejected(payload(methodology=bad), "invalid_structure")

    def test_extra_citation_field_is_rejected(self):
        self.assertRejected(
            payload(
                methodology=section(
                    cites_a=[{"page": 3, "chunk_id": 0, "quote": "text"}]
                )
            ),
            "invalid_structure",
        )

    def test_page_zero_is_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 0, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_negative_page_is_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": -1, "chunk_id": 0}])),
            "invalid_structure",
        )

    def test_negative_chunk_id_is_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 3, "chunk_id": -1}])),
            "invalid_structure",
        )

    def test_boolean_page_is_rejected(self):
        """True must not become page 1. Phase 2B found this empirically:
        a boolean silently becoming a plausible page is fabrication
        wearing a disguise."""
        for value in (True, False):
            with self.subTest(page=value):
                self.assertRejected(
                    payload(
                        methodology=section(cites_a=[{"page": value, "chunk_id": 0}])
                    ),
                    "invalid_structure",
                )

    def test_boolean_chunk_id_is_rejected(self):
        for value in (True, False):
            with self.subTest(chunk_id=value):
                self.assertRejected(
                    payload(
                        methodology=section(cites_a=[{"page": 3, "chunk_id": value}])
                    ),
                    "invalid_structure",
                )

    def test_float_page_and_chunk_id_are_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 3.0, "chunk_id": 0}])),
            "invalid_structure",
        )
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 3, "chunk_id": 0.0}])),
            "invalid_structure",
        )

    def test_string_page_and_chunk_id_are_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": "3", "chunk_id": 0}])),
            "invalid_structure",
        )
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 3, "chunk_id": "0"}])),
            "invalid_structure",
        )

    def test_missing_citation_field_is_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[{"page": 3}])), "invalid_structure"
        )

    def test_the_model_cannot_introduce_paper_ids(self):
        for field in ("paper_a_id", "paper_b_id", "paper_id", "paper", "title"):
            with self.subTest(field=field):
                bad = section()
                bad[field] = "2add54fc-f825-54e6-b6eb-69bf9ad664c7"
                self.assertRejected(payload(methodology=bad), "invalid_structure")

    def test_the_model_cannot_introduce_owner_ids(self):
        self.assertRejected(
            dict(payload(), owner_id="dddddddd-dddd-dddd-dddd-dddddddddddd"),
            "invalid_structure",
        )

    def test_the_model_cannot_introduce_qdrant_point_ids(self):
        for field in ("point_id", "id", "vector_id"):
            with self.subTest(field=field):
                self.assertRejected(
                    payload(
                        methodology=section(
                            cites_a=[{"page": 3, "chunk_id": 0, field: "abc"}]
                        )
                    ),
                    "invalid_structure",
                )

    def test_score_winner_and_confidence_fields_are_rejected(self):
        for field in ("score", "winner", "confidence", "rank", "probability"):
            with self.subTest(field=field):
                bad = section()
                bad[field] = 1
                self.assertRejected(payload(methodology=bad), "invalid_structure")

    def test_no_scoring_field_exists_in_the_schema(self):
        from app.services.relationship_schema import SectionRelationship

        self.assertEqual(
            sorted(SectionRelationship.model_fields),
            ["cites_a", "cites_b", "relation", "statement"],
        )
        self.assertEqual(sorted(PaperRelationship.model_fields), ["sections"])


# ----------------------------------------------------------------------
# 28-32. A/B evidence isolation — the property that matters most
# ----------------------------------------------------------------------
class TestEvidenceOwnership(RelationshipTestCase):

    def test_paper_a_citation_is_accepted_from_paper_a_allowlist(self):
        result = validate(payload(methodology=section(cites_a=[cite(A_ONLY)])))
        self.assertEqual(
            (result.sections["methodology"].cites_a[0].page,
             result.sections["methodology"].cites_a[0].chunk_id),
            A_ONLY,
        )

    def test_paper_a_citation_is_rejected_when_only_paper_b_has_it(self):
        """(5, 2) exists in B and not in A. It must NOT rescue an A
        citation — that would silently attribute B's passage to A."""
        self.assertRejected(
            payload(methodology=section(cites_a=[cite(B_ONLY)])),
            "evidence_not_supplied",
        )

    def test_paper_b_citation_is_accepted_from_paper_b_allowlist(self):
        result = validate(payload(methodology=section(cites_b=[cite(B_ONLY)])))
        self.assertEqual(
            (result.sections["methodology"].cites_b[0].page,
             result.sections["methodology"].cites_b[0].chunk_id),
            B_ONLY,
        )

    def test_paper_b_citation_is_rejected_when_only_paper_a_has_it(self):
        self.assertRejected(
            payload(methodology=section(cites_b=[cite(A_ONLY)])),
            "evidence_not_supplied",
        )

    def test_a_citation_in_neither_allowlist_is_rejected(self):
        self.assertRejected(
            payload(methodology=section(cites_a=[cite(NOWHERE)])),
            "evidence_not_supplied",
        )
        self.assertRejected(
            payload(methodology=section(cites_b=[cite(NOWHERE)])),
            "evidence_not_supplied",
        )

    def test_a_shared_pair_validates_independently_on_each_side(self):
        """(3, 0) is in BOTH allowlists, and each side is checked against
        its own — the pair is not 'globally valid'."""
        result = validate(
            payload(methodology=section(cites_a=[cite((3, 0))], cites_b=[cite((3, 0))]))
        )
        self.assertEqual(len(result.sections["methodology"].cites_a), 1)
        self.assertEqual(len(result.sections["methodology"].cites_b), 1)

    def test_swapping_the_allowlists_changes_the_outcome(self):
        """The strongest statement of isolation: the same response that
        validates against (A, B) must FAIL against (B, A)."""
        good = payload(methodology=section(cites_a=[cite(A_ONLY)], cites_b=[cite(B_ONLY)]))

        validate(good)  # passes with the correct pairing

        with self.assertRaises(RelationshipValidationError):
            validate(good, allowed_a=ALLOWED_B, allowed_b=ALLOWED_A)

    def test_one_bad_citation_fails_the_whole_object(self):
        self.assertRejected(
            payload(
                methodology=section(cites_a=[cite((3, 0)), cite(NOWHERE)])
            ),
            "evidence_not_supplied",
        )

    def test_an_empty_allowlist_admits_nothing(self):
        self.assertRejected(
            payload(), "evidence_not_supplied", allowed_a={}
        )


# ----------------------------------------------------------------------
# 33-36. Canonical sections and comparable coverage
# ----------------------------------------------------------------------
class TestSectionCoverage(RelationshipTestCase):

    def test_an_unknown_section_name_is_rejected(self):
        self.assertRejected(
            payload(future_work=section()), "unknown_section"
        )

    def test_a_canonical_but_non_comparable_section_is_rejected(self):
        """`dataset` is canonical, but it was not comparable for this
        pair — the model was never given evidence for it."""
        self.assertRejected(payload(dataset=section()), "unknown_section")

    def test_a_missing_comparable_section_is_rejected(self):
        incomplete = payload()
        del incomplete["sections"]["key_results"]
        self.assertRejected(incomplete, "missing_section")

    def test_sections_come_back_in_canonical_order(self):
        comparable = ("key_results", "methodology", "research_problem")
        raw = {"sections": {name: section() for name in reversed(comparable)}}

        result = validate(raw, comparable=comparable)

        # methodology precedes key_results in SECTION_NAMES, and
        # research_problem precedes both.
        self.assertEqual(
            list(result.sections),
            ["research_problem", "methodology", "key_results"],
        )

    def test_only_the_comparable_sections_are_required(self):
        """Not all ten: a section where one paper said nothing has no
        grounded basis for a relationship."""
        result = validate(
            {"sections": {"methodology": section()}}, comparable=("methodology",)
        )
        self.assertEqual(list(result.sections), ["methodology"])
        self.assertLess(len(result.sections), len(SECTION_NAMES))

    def test_all_ten_may_be_comparable(self):
        result = validate(
            {"sections": {name: section() for name in SECTION_NAMES}},
            comparable=SECTION_NAMES,
        )
        self.assertEqual(list(result.sections), list(SECTION_NAMES))

    def test_a_non_canonical_comparable_set_is_a_call_site_error(self):
        with self.assertRaises(ValueError):
            validate(payload(), comparable=("methodology", "made_up"))

    def test_an_empty_comparable_set_is_a_call_site_error(self):
        """Nothing comparable means the model should never have run."""
        with self.assertRaises(ValueError):
            validate({"sections": {}}, comparable=())

    def test_a_duplicated_comparable_set_is_a_call_site_error(self):
        with self.assertRaises(ValueError):
            validate(payload(), comparable=("methodology", "methodology"))


# ----------------------------------------------------------------------
# 37-40. Strict JSON
# ----------------------------------------------------------------------
class TestStrictJson(RelationshipTestCase):

    def test_malformed_json_is_rejected(self):
        self.assertRejected("{not valid json,,,", "malformed_json")

    def test_empty_input_is_rejected(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertRejected(raw, "malformed_json")

    def test_arbitrary_prose_is_rejected(self):
        self.assertRejected(
            "Certainly! The two papers both use staged pipelines.",
            "malformed_json",
        )

    def test_prose_wrapped_around_valid_json_is_rejected(self):
        self.assertRejected(
            f"Here you go!\n{json.dumps(payload())}\nHope that helps.",
            "malformed_json",
        )

    def test_a_non_object_top_level_is_rejected(self):
        self.assertRejected(json.dumps([payload()]), "malformed_json")

    def test_a_single_json_fence_is_tolerated(self):
        result = validate(f"```json\n{json.dumps(payload())}\n```")
        self.assertEqual(sorted(result.sections), sorted(COMPARABLE))

    def test_a_bare_fence_is_tolerated(self):
        result = validate(f"```\n{json.dumps(payload())}\n```")
        self.assertEqual(sorted(result.sections), sorted(COMPARABLE))

    def test_duplicate_json_keys_are_rejected(self):
        """json.loads keeps the LAST value silently. A response that
        cannot be read unambiguously is not a response."""
        raw = (
            '{"sections": {"methodology": '
            + json.dumps(section())
            + ', "methodology": '
            + json.dumps(section(RELATION_DIVERGENT))
            + ', "key_results": '
            + json.dumps(section())
            + "}}"
        )
        self.assertRejected(raw, "malformed_json")

    def test_duplicate_keys_are_rejected_at_the_top_level_too(self):
        raw = '{"sections": {}, "sections": ' + json.dumps(payload()["sections"]) + "}"
        self.assertRejected(raw, "malformed_json")

    def test_duplicate_keys_are_rejected_inside_a_citation(self):
        raw = (
            '{"sections": {"methodology": {"relation": "aligned", '
            '"statement": "x", "cites_a": [{"page": 3, "page": 3, "chunk_id": 0}], '
            '"cites_b": [{"page": 3, "chunk_id": 0}]}, "key_results": '
            + json.dumps(section())
            + "}}"
        )
        self.assertRejected(raw, "malformed_json")

    def test_parse_helper_returns_a_plain_dict(self):
        parsed = parse_relationship_json(json.dumps(payload()))
        self.assertIsInstance(parsed, dict)
        self.assertIn("sections", parsed)

    def test_a_dict_may_be_passed_directly(self):
        result = validate(payload())
        self.assertEqual(sorted(result.sections), sorted(COMPARABLE))


# ----------------------------------------------------------------------
# Purity
# ----------------------------------------------------------------------
class TestProviderIndependence(unittest.TestCase):

    def _imported_roots(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots.add((node.module or "").split(".")[0])
        return roots

    def test_the_module_imports_nothing_beyond_its_stated_dependencies(self):
        """Parsed, not grepped: the module's own docstring names the
        providers it does not touch, and a text search would match that
        prose."""
        self.assertEqual(
            self._imported_roots(),
            {"__future__", "json", "typing", "pydantic", "app"},
        )

    def test_no_provider_or_transport_library_is_imported(self):
        forbidden = {
            "groq", "qdrant_client", "voyageai", "openai", "supabase",
            "httpx", "requests", "urllib", "socket", "sqlalchemy", "fastapi",
        }
        self.assertEqual(self._imported_roots() & forbidden, set())

    def test_validation_opens_no_socket(self):
        import socket
        from unittest.mock import patch

        def explode(*args, **kwargs):
            raise AssertionError("the validator opened a network socket")

        with patch.object(socket, "socket", explode):
            result = validate(json.dumps(payload()))

        self.assertEqual(sorted(result.sections), sorted(COMPARABLE))

    def test_the_only_app_import_is_the_intelligence_schema(self):
        """Reuse of SECTION_NAMES and the fence policy is deliberate; a
        second source of truth for either would be free to drift."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        app_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.")
        }
        self.assertEqual(app_modules, {"app.services.intelligence_schema"})


# ----------------------------------------------------------------------
# The rehydration helper
# ----------------------------------------------------------------------
class TestAllowlistPlanning(unittest.TestCase):
    """build_relationship_allowlists says WHICH chunks Step 2C-2 will
    need to fetch, without fetching anything."""

    def _intelligence(self, marker, evidence):
        return {
            name: {
                "status": "answered",
                "summary": f"{marker} {name}",
                "evidence": evidence,
            }
            for name in SECTION_NAMES
        }

    def test_it_returns_each_paper_s_pairs_separately(self):
        a = self._intelligence("A", [{"page": 1, "chunk_id": 0}])
        b = self._intelligence("B", [{"page": 5, "chunk_id": 2}])

        pairs_a, pairs_b = build_relationship_allowlists(a, b, ("methodology",))

        self.assertEqual(pairs_a, {(1, 0)})
        self.assertEqual(pairs_b, {(5, 2)})

    def test_it_only_considers_comparable_sections(self):
        a = self._intelligence("A", [{"page": 1, "chunk_id": 0}])
        a["dataset"]["evidence"] = [{"page": 42, "chunk_id": 7}]

        pairs_a, _ = build_relationship_allowlists(a, a, ("methodology",))

        self.assertEqual(pairs_a, {(1, 0)})
        self.assertNotIn((42, 7), pairs_a)

    def test_it_deduplicates_across_sections(self):
        a = self._intelligence("A", [{"page": 3, "chunk_id": 0}])

        pairs_a, _ = build_relationship_allowlists(
            a, a, ("methodology", "key_results", "limitations")
        )

        self.assertEqual(pairs_a, {(3, 0)})

    def test_it_skips_unusable_references(self):
        a = self._intelligence(
            "A",
            [
                {"page": 3, "chunk_id": 0},
                {"page": True, "chunk_id": 0},
                {"page": 0, "chunk_id": 0},
                {"page": 1, "chunk_id": -1},
                {"page": "3", "chunk_id": 0},
                {"chunk_id": 0},
            ],
        )

        pairs_a, _ = build_relationship_allowlists(a, a, ("methodology",))

        # A stray True must not become page 1 here either.
        self.assertEqual(pairs_a, {(3, 0)})

    def test_a_paper_with_no_intelligence_yields_nothing(self):
        a = self._intelligence("A", [{"page": 1, "chunk_id": 0}])

        pairs_a, pairs_b = build_relationship_allowlists(a, {}, ("methodology",))

        self.assertEqual(pairs_a, {(1, 0)})
        self.assertEqual(pairs_b, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
