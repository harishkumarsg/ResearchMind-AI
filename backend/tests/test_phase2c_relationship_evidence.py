"""
Phase 2C step 2C-2 — evidence rehydration.

This layer turns stored `(page, chunk_id)` references back into the exact
paper text they name. It decides nothing about meaning, so the tests are
almost entirely about identity and isolation:

  * the owner and paper filters are not optional. `chunk_id` is an index
    WITHIN ITS PAGE, so page 3 chunk 0 exists in essentially every paper
    and in every account. A retrieval that forgot either filter would
    return a real, well-formed passage from the wrong document — a
    failure that looks like success;

  * nothing is ever substituted. A reference that cannot be resolved
    exactly comes back with no text and a reason. Not a neighbouring
    chunk, not another chunk on the same page, not the first result;

  * two points claiming one identity is an integrity failure, not a tie
    to be broken;

  * the returned text is byte-for-byte the matched chunk's payload text.
    Model prose has no route into it.

Qdrant is mocked throughout. No real Qdrant, no Voyage, no Groq, no
Storage, no database.
"""
import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services.intelligence_schema import SECTION_NAMES

import app.services.paper_relationship_evidence as evidence_service
from app.services.paper_relationship_evidence import (
    DUPLICATE_IDENTITY,
    EVIDENCE_CONTEXT_CHARS,
    FOUND,
    MALFORMED_PAYLOAD,
    MISSING_CHUNK,
    OVER_BUDGET,
    build_chunk_index,
    references_from_intelligence,
    rehydrate_paper_evidence,
)

MODULE_PATH = os.path.join(
    BACKEND_DIR, "app", "services", "paper_relationship_evidence.py"
)

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"


def point(page, chunk_id, text, *, owner_id=OWNER_A, paper_id=PAPER_A, **extra):
    """A Qdrant point as index_document.py writes them."""
    p = MagicMock()
    p.id = "point-uuid-regenerated-every-index-run"
    payload = {
        "text": text,
        "source": "paper.pdf",
        "paper": "A Paper",
        "paper_id": paper_id,
        "owner_id": owner_id,
        "authors": "Someone",
        "abstract": "AN ABSTRACT THAT IS NOT CHUNK TEXT",
        "keywords": "defects",
        "page": page,
        "total_pages": 9,
        "chunk_id": chunk_id,
        "chunk_count": 3,
    }
    payload.update(extra)
    p.payload = payload
    return p


class EvidenceTestCase(unittest.TestCase):
    """Fake Qdrant that honours the owner_id + paper_id filter itself.

    Honouring the filter is the point: a mock that returned everything
    regardless would make the isolation tests pass no matter what the
    production filter did.
    """

    def setUp(self):
        self.all_points = []
        self.scroll_calls = []

        def scroll(**kwargs):
            self.scroll_calls.append(kwargs)
            conditions = {
                c.key: c.match.value for c in kwargs["scroll_filter"].must
            }
            matched = [
                p
                for p in self.all_points
                # A point with no payload carries none of the filtered
                # fields, so a real filter could not match it either.
                if isinstance(getattr(p, "payload", None), dict)
                and all(p.payload.get(k) == v for k, v in conditions.items())
            ]
            return matched, None

        self.qdrant = MagicMock()
        self.qdrant.scroll.side_effect = scroll
        patcher = patch.object(evidence_service, "client", self.qdrant)
        patcher.start()
        self.addCleanup(patcher.stop)

    def given(self, *points):
        self.all_points = list(points)

    def resolve(self, references, owner_id=OWNER_A, paper_id=PAPER_A, **kw):
        return rehydrate_paper_evidence(owner_id, paper_id, references, **kw)

    def one(self, result, page, chunk_id):
        for item in result.items:
            if (item.page, item.chunk_id) == (page, chunk_id):
                return item
        raise AssertionError(f"no result for ({page}, {chunk_id})")


# ----------------------------------------------------------------------
# 1, 9. Exact retrieval
# ----------------------------------------------------------------------
class TestExactRetrieval(EvidenceTestCase):

    def test_an_exact_page_and_chunk_is_retrieved(self):
        self.given(point(3, 0, "METHODOLOGY. A two-stage framework."))

        result = self.resolve({"methodology": [(3, 0)]})

        item = self.one(result, 3, 0)
        self.assertEqual(item.status, FOUND)
        self.assertEqual(item.text, "METHODOLOGY. A two-stage framework.")

    def test_returned_text_is_byte_for_byte_the_payload_text(self):
        exact = "  Leading and trailing spaces, \n a newline, and \"quotes\".  "
        self.given(point(3, 0, exact))

        self.assertEqual(self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0).text, exact)

    def test_the_same_chunk_id_on_a_different_page_is_a_different_chunk(self):
        self.given(
            point(3, 0, "page three chunk zero"),
            point(7, 0, "page seven chunk zero"),
        )

        result = self.resolve({"methodology": [(3, 0), (7, 0)]})

        self.assertEqual(self.one(result, 3, 0).text, "page three chunk zero")
        self.assertEqual(self.one(result, 7, 0).text, "page seven chunk zero")

    def test_a_different_chunk_on_the_requested_page_is_not_substituted(self):
        """(3, 1) is requested; only (3, 0) exists. Choosing it would be
        a plausible-looking wrong passage."""
        self.given(point(3, 0, "the wrong chunk on the right page"))

        item = self.one(self.resolve({"methodology": [(3, 1)]}), 3, 1)

        self.assertEqual(item.status, MISSING_CHUNK)
        self.assertIsNone(item.text)

    def test_only_the_requested_references_are_returned(self):
        self.given(point(1, 0, "one"), point(3, 0, "three"), point(7, 0, "seven"))

        result = self.resolve({"methodology": [(3, 0)]})

        self.assertEqual([(i.page, i.chunk_id) for i in result.items], [(3, 0)])


# ----------------------------------------------------------------------
# 2-5. Owner and paper isolation
# ----------------------------------------------------------------------
class TestOwnerAndPaperIsolation(EvidenceTestCase):

    def test_the_qdrant_filter_always_carries_owner_and_paper(self):
        self.given(point(3, 0, "text"))

        self.resolve({"methodology": [(3, 0)]})

        self.assertEqual(len(self.scroll_calls), 1)
        conditions = {
            c.key: c.match.value for c in self.scroll_calls[0]["scroll_filter"].must
        }
        self.assertEqual(conditions, {"owner_id": OWNER_A, "paper_id": PAPER_A})

    def test_an_absent_owner_id_raises_rather_than_widening_the_query(self):
        for bad in ("", None, 0):
            with self.subTest(owner_id=bad):
                with self.assertRaises(ValueError):
                    self.resolve({"methodology": [(3, 0)]}, owner_id=bad)
        self.qdrant.scroll.assert_not_called()

    def test_an_absent_paper_id_raises_rather_than_widening_the_query(self):
        for bad in ("", None, 0):
            with self.subTest(paper_id=bad):
                with self.assertRaises(ValueError):
                    self.resolve({"methodology": [(3, 0)]}, paper_id=bad)
        self.qdrant.scroll.assert_not_called()

    def test_another_owners_identical_page_and_chunk_is_not_returned(self):
        """Owner B holds (3, 0) of the SAME paper id. Owner A must not
        see it — this is the collision that makes the owner filter
        load-bearing rather than decorative."""
        self.given(point(3, 0, "OWNER B SECRET TEXT", owner_id=OWNER_B))

        item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)

        self.assertEqual(item.status, MISSING_CHUNK)
        self.assertIsNone(item.text)

    def test_another_papers_identical_page_and_chunk_is_not_returned(self):
        self.given(point(3, 0, "PAPER B TEXT", paper_id=PAPER_B))

        item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)

        self.assertEqual(item.status, MISSING_CHUNK)
        self.assertIsNone(item.text)

    def test_each_paper_resolves_only_its_own_text(self):
        self.given(
            point(3, 0, "A methodology", paper_id=PAPER_A),
            point(3, 0, "B methodology", paper_id=PAPER_B),
        )

        a = self.resolve({"methodology": [(3, 0)]}, paper_id=PAPER_A)
        b = self.resolve({"methodology": [(3, 0)]}, paper_id=PAPER_B)

        self.assertEqual(self.one(a, 3, 0).text, "A methodology")
        self.assertEqual(self.one(b, 3, 0).text, "B methodology")

    def test_a_foreign_owner_cannot_reach_a_paper_they_do_not_hold(self):
        self.given(point(3, 0, "A's text", owner_id=OWNER_A))

        result = self.resolve({"methodology": [(3, 0)]}, owner_id=OWNER_B)

        self.assertEqual(self.one(result, 3, 0).status, MISSING_CHUNK)
        self.assertNotIn("A's text", str(result))


# ----------------------------------------------------------------------
# 6-8. Unresolved references
# ----------------------------------------------------------------------
class TestUnresolvedReferences(EvidenceTestCase):

    def test_a_missing_chunk_is_reported_not_substituted(self):
        self.given(point(1, 0, "some other chunk"))

        item = self.one(self.resolve({"methodology": [(9, 9)]}), 9, 9)

        self.assertEqual(item.status, MISSING_CHUNK)
        self.assertIsNone(item.text)

    def test_a_duplicate_identity_is_an_integrity_failure(self):
        """Two points claim (3, 0). Picking one would choose between two
        candidate passages on no basis at all."""
        self.given(point(3, 0, "first copy"), point(3, 0, "second copy"))

        item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)

        self.assertEqual(item.status, DUPLICATE_IDENTITY)
        self.assertIsNone(item.text)
        self.assertNotIn("first copy", str(item))
        self.assertNotIn("second copy", str(item))

    def test_a_payload_with_no_text_is_malformed_not_found(self):
        for bad in (None, "", "   ", 42, [], {}):
            with self.subTest(text=bad):
                self.given(point(3, 0, bad))
                item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)
                self.assertEqual(item.status, MALFORMED_PAYLOAD)
                self.assertIsNone(item.text)

    def test_a_point_with_an_unusable_identity_is_not_indexed(self):
        """It cannot be keyed, so a reference resolves as missing rather
        than matching it by accident."""
        for page, chunk_id in ((True, 0), (0, 0), (1, -1), ("3", 0), (3, "0"), (None, 0)):
            with self.subTest(page=page, chunk_id=chunk_id):
                self.given(point(page, chunk_id, "unreachable"))
                index = build_chunk_index(OWNER_A, PAPER_A)
                self.assertEqual(index, {})

    def test_a_point_with_no_payload_is_skipped(self):
        bare = MagicMock()
        bare.payload = None
        self.given(bare, point(3, 0, "real text"))

        self.assertEqual(self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0).text, "real text")

    def test_unresolved_references_still_appear_in_the_result(self):
        self.given(point(3, 0, "found"))

        result = self.resolve({"methodology": [(3, 0), (4, 0)]})

        self.assertEqual(len(result.items), 2)
        self.assertEqual(len(result.found), 1)
        self.assertEqual(len(result.unresolved), 1)


# ----------------------------------------------------------------------
# 10. Source text provenance
# ----------------------------------------------------------------------
class TestSourceTextProvenance(EvidenceTestCase):

    def test_text_comes_from_the_text_field_not_from_metadata(self):
        self.given(point(3, 0, "THE CHUNK TEXT"))

        item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)

        self.assertEqual(item.text, "THE CHUNK TEXT")
        # The payload also carries an abstract, authors and keywords.
        # None of them is source text for a chunk.
        self.assertNotIn("ABSTRACT", item.text)
        self.assertNotIn("Someone", item.text)
        self.assertNotIn("defects", item.text)

    def test_the_module_never_receives_a_summary_or_quote(self):
        """Structural, parsed not grepped: a module that cannot see model
        prose cannot return it as source text."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        # Every string literal used as a dict/mapping key lookup.
        looked_up = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }

        self.assertIn("text", looked_up)
        for forbidden in ("summary", "quote", "abstract", "statement"):
            self.assertNotIn(forbidden, looked_up)

    def test_references_from_intelligence_reads_only_page_and_chunk(self):
        intelligence = {
            "methodology": {
                "status": "answered",
                "summary": "A MODEL SUMMARY THAT MUST NOT BECOME SOURCE TEXT",
                "evidence": [
                    {"page": 3, "chunk_id": 0, "quote": "A QUOTE THAT MUST NOT TRAVEL"}
                ],
            }
        }

        refs = references_from_intelligence(intelligence, ["methodology"])

        self.assertEqual(refs, {"methodology": [(3, 0)]})
        rendered = str(refs)
        self.assertNotIn("SUMMARY", rendered)
        self.assertNotIn("QUOTE", rendered)


# ----------------------------------------------------------------------
# 11. Deterministic ordering
# ----------------------------------------------------------------------
class TestDeterministicOrdering(EvidenceTestCase):

    def test_results_are_ordered_by_page_then_chunk_id(self):
        self.given(
            point(7, 0, "g"), point(1, 0, "a"), point(3, 1, "c"), point(3, 0, "b")
        )

        result = self.resolve(
            {"methodology": [(7, 0), (3, 1), (1, 0), (3, 0)]}
        )

        self.assertEqual(
            [(i.page, i.chunk_id) for i in result.items],
            [(1, 0), (3, 0), (3, 1), (7, 0)],
        )

    def test_ordering_ignores_qdrant_return_order(self):
        """The fake returns points in insertion order; reversing it must
        not change the result."""
        forward = [point(1, 0, "a"), point(3, 0, "b"), point(7, 0, "c")]

        self.given(*forward)
        first = self.resolve({"methodology": [(1, 0), (3, 0), (7, 0)]})

        self.given(*reversed(forward))
        second = self.resolve({"methodology": [(1, 0), (3, 0), (7, 0)]})

        self.assertEqual(
            [(i.page, i.chunk_id, i.text) for i in first.items],
            [(i.page, i.chunk_id, i.text) for i in second.items],
        )

    def test_unresolved_references_are_ordered_alongside_found_ones(self):
        self.given(point(5, 0, "found"))

        result = self.resolve({"methodology": [(9, 0), (1, 0), (5, 0)]})

        self.assertEqual(
            [(i.page, i.chunk_id) for i in result.items], [(1, 0), (5, 0), (9, 0)]
        )

    def test_repeated_calls_are_identical(self):
        self.given(point(3, 0, "b"), point(1, 0, "a"))
        refs = {"methodology": [(3, 0), (1, 0)]}

        self.assertEqual(self.resolve(refs), self.resolve(refs))


# ----------------------------------------------------------------------
# 12, 13. Context budget
# ----------------------------------------------------------------------
class TestContextBudget(EvidenceTestCase):

    def test_the_default_budget_is_the_existing_proven_constant(self):
        self.assertEqual(EVIDENCE_CONTEXT_CHARS, 12000)

    def test_whole_chunks_only_and_the_budget_is_never_exceeded(self):
        self.given(*[point(p, 0, "x" * 400) for p in range(1, 11)])
        refs = {"methodology": [(p, 0) for p in range(1, 11)]}

        result = self.resolve(refs, budget_chars=1000)

        self.assertLessEqual(result.used_chars, 1000)
        # 2 whole chunks of 400 fit; the third would reach 1200.
        self.assertEqual(len(result.found), 2)
        for item in result.found:
            self.assertEqual(len(item.text), 400)

    def test_chunks_beyond_the_budget_are_reported_not_truncated(self):
        self.given(*[point(p, 0, "x" * 400) for p in range(1, 4)])

        result = self.resolve(
            {"methodology": [(1, 0), (2, 0), (3, 0)]}, budget_chars=500
        )

        self.assertEqual(self.one(result, 1, 0).status, FOUND)
        for page in (2, 3):
            item = self.one(result, page, 0)
            self.assertEqual(item.status, OVER_BUDGET)
            self.assertIsNone(item.text)

    def test_a_chunk_larger_than_the_whole_budget_is_excluded_not_cut(self):
        """Defensive: chunk_size is 1500 against a 12,000 budget, so this
        cannot arise in production. Half a chunk is not the evidence its
        (page, chunk_id) names, so it is excluded rather than trimmed."""
        self.given(point(3, 0, "y" * 5000))

        result = self.resolve({"methodology": [(3, 0)]}, budget_chars=100)

        item = self.one(result, 3, 0)
        self.assertEqual(item.status, OVER_BUDGET)
        self.assertIsNone(item.text)
        self.assertEqual(result.used_chars, 0)

    def test_a_later_smaller_chunk_can_still_be_admitted(self):
        self.given(point(1, 0, "x" * 900), point(2, 0, "y" * 50))

        result = self.resolve({"methodology": [(1, 0), (2, 0)]}, budget_chars=1000)

        self.assertEqual(self.one(result, 1, 0).status, FOUND)
        self.assertEqual(self.one(result, 2, 0).status, FOUND)
        self.assertEqual(result.used_chars, 950)

    def test_a_realistic_paper_fits_inside_the_default_budget(self):
        """The generation-time allowlist was itself capped at 12,000
        chars, so the citable set cannot exceed the budget by much."""
        self.given(*[point(p, c, "z" * 1500) for p in range(1, 5) for c in range(2)])
        refs = {"methodology": [(p, c) for p in range(1, 5) for c in range(2)]}

        result = self.resolve(refs)

        self.assertLessEqual(result.used_chars, EVIDENCE_CONTEXT_CHARS)
        self.assertEqual(len(result.found), 8)


# ----------------------------------------------------------------------
# 14, 23. Provenance and deduplication
# ----------------------------------------------------------------------
class TestProvenance(EvidenceTestCase):

    def test_each_result_carries_its_section_provenance(self):
        self.given(point(3, 0, "shared"), point(5, 0, "results only"))

        result = self.resolve(
            {"methodology": [(3, 0)], "key_results": [(5, 0)]}
        )

        self.assertEqual(self.one(result, 3, 0).sections, ("methodology",))
        self.assertEqual(self.one(result, 5, 0).sections, ("key_results",))

    def test_each_result_carries_its_paper_id(self):
        self.given(point(3, 0, "text"))

        self.assertEqual(self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0).paper_id,
                         PAPER_A)

    def test_a_chunk_cited_by_two_sections_appears_once_with_both(self):
        """DEDUPLICATED, not rejected: fetching and sending one passage
        twice buys nothing and spends the budget twice. Provenance is
        preserved by collecting the section names."""
        self.given(point(3, 0, "supports two sections"))

        result = self.resolve(
            {"methodology": [(3, 0)], "key_results": [(3, 0)]}
        )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(self.one(result, 3, 0).sections, ("methodology", "key_results"))
        self.assertEqual(result.used_chars, len("supports two sections"))

    def test_section_provenance_is_in_canonical_order(self):
        self.given(point(3, 0, "text"))

        result = self.resolve(
            {"limitations": [(3, 0)], "methodology": [(3, 0)], "dataset": [(3, 0)]}
        )

        # methodology < dataset < limitations in SECTION_NAMES.
        self.assertEqual(
            self.one(result, 3, 0).sections, ("methodology", "dataset", "limitations")
        )

    def test_a_duplicate_reference_within_one_section_is_deduplicated(self):
        self.given(point(3, 0, "once"))

        result = self.resolve({"methodology": [(3, 0), (3, 0), (3, 0)]})

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.used_chars, len("once"))

    def test_paper_a_and_paper_b_results_stay_separate(self):
        self.given(
            point(3, 0, "A text", paper_id=PAPER_A),
            point(3, 0, "B text", paper_id=PAPER_B),
        )

        a = self.resolve({"methodology": [(3, 0)]}, paper_id=PAPER_A)
        b = self.resolve({"methodology": [(3, 0)]}, paper_id=PAPER_B)

        self.assertEqual(a.paper_id, PAPER_A)
        self.assertEqual(b.paper_id, PAPER_B)
        self.assertEqual(self.one(a, 3, 0).text, "A text")
        self.assertEqual(self.one(b, 3, 0).text, "B text")
        self.assertNotIn("B text", str(a))
        self.assertNotIn("A text", str(b))


# ----------------------------------------------------------------------
# 21, 22. Nothing to fetch means no fetch
# ----------------------------------------------------------------------
class TestNoUnnecessaryRetrieval(EvidenceTestCase):

    def test_empty_references_perform_no_qdrant_read(self):
        self.given(point(3, 0, "text"))

        result = self.resolve({})

        self.qdrant.scroll.assert_not_called()
        self.assertEqual(result.items, ())
        self.assertEqual(result.used_chars, 0)

    def test_sections_with_no_evidence_perform_no_qdrant_read(self):
        """An ungrounded or not_specified section reaches here as an empty
        list and must cost nothing."""
        self.given(point(3, 0, "text"))

        result = self.resolve({"methodology": [], "dataset": []})

        self.qdrant.scroll.assert_not_called()
        self.assertEqual(result.items, ())

    def test_a_non_canonical_section_name_is_ignored(self):
        self.given(point(3, 0, "text"))

        result = self.resolve({"future_work": [(3, 0)]})

        self.qdrant.scroll.assert_not_called()
        self.assertEqual(result.items, ())

    def test_unusable_references_are_dropped_before_any_read(self):
        self.given(point(3, 0, "text"))

        result = self.resolve(
            {"methodology": [(True, 0), (0, 0), (1, -1), ("3", 0), (None, None)]}
        )

        self.qdrant.scroll.assert_not_called()
        self.assertEqual(result.items, ())

    def test_one_read_serves_every_reference_for_a_paper(self):
        """No N+1: the paper is scrolled once and matched in memory."""
        self.given(*[point(p, 0, f"page {p}") for p in range(1, 6)])

        self.resolve({"methodology": [(p, 0) for p in range(1, 6)]})

        self.assertEqual(len(self.scroll_calls), 1)


# ----------------------------------------------------------------------
# 16-20. Purity
# ----------------------------------------------------------------------
class TestNoProviderOrWrites(EvidenceTestCase):

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

    def test_no_provider_or_database_library_is_imported(self):
        forbidden = {
            "groq", "voyageai", "openai", "supabase", "httpx", "requests",
            "urllib", "socket", "sqlalchemy", "fastapi", "boto3",
        }
        self.assertEqual(self._imported_roots() & forbidden, set())

    def test_no_embedder_or_reranker_is_imported(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("app.rag.embedder", modules)
        self.assertNotIn("app.rag.reranker", modules)

    def test_only_scroll_is_called_on_the_qdrant_client(self):
        self.given(point(3, 0, "text"))

        self.resolve({"methodology": [(3, 0)]})

        # No search, no query_points: there is no embedding to search with.
        self.qdrant.query_points.assert_not_called()
        self.qdrant.search.assert_not_called()
        # And nothing is written.
        self.qdrant.upsert.assert_not_called()
        self.qdrant.delete.assert_not_called()
        self.qdrant.create_collection.assert_not_called()

    def test_the_module_calls_no_write_operation_at_source_level(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in ("upsert", "delete", "commit", "execute", "query_points",
                          "search", "encode_query", "create_embeddings", "generate"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called)

    def test_rehydration_makes_no_request(self):
        """Every network client is left unpatched except Qdrant, which is
        a fake — so touching anything else would raise."""
        self.given(point(3, 0, "text"))

        result = self.resolve({"methodology": [(3, 0)]})

        self.assertEqual(self.one(result, 3, 0).status, FOUND)
        self.assertEqual(len(self.scroll_calls), 1)

    def test_the_result_is_immutable(self):
        self.given(point(3, 0, "text"))

        item = self.one(self.resolve({"methodology": [(3, 0)]}), 3, 0)

        with self.assertRaises(Exception):
            item.text = "rewritten"


# ----------------------------------------------------------------------
# references_from_intelligence
# ----------------------------------------------------------------------
class TestReferencesFromIntelligence(unittest.TestCase):

    def _intelligence(self, evidence):
        return {
            name: {"status": "answered", "summary": f"{name} summary", "evidence": evidence}
            for name in SECTION_NAMES
        }

    def test_it_returns_pairs_for_the_named_sections_only(self):
        intelligence = self._intelligence([{"page": 3, "chunk_id": 0}])

        refs = references_from_intelligence(intelligence, ["methodology"])

        self.assertEqual(refs, {"methodology": [(3, 0)]})

    def test_it_preserves_reference_order_within_a_section(self):
        intelligence = self._intelligence(
            [{"page": 7, "chunk_id": 0}, {"page": 1, "chunk_id": 2}]
        )

        refs = references_from_intelligence(intelligence, ["methodology"])

        self.assertEqual(refs["methodology"], [(7, 0), (1, 2)])

    def test_it_skips_unusable_references(self):
        intelligence = self._intelligence(
            [
                {"page": 3, "chunk_id": 0},
                {"page": True, "chunk_id": 0},
                {"page": 0, "chunk_id": 0},
                {"page": 1, "chunk_id": -1},
                {"page": "3", "chunk_id": 0},
                {"chunk_id": 0},
            ]
        )

        refs = references_from_intelligence(intelligence, ["methodology"])

        self.assertEqual(refs["methodology"], [(3, 0)])

    def test_a_section_with_no_usable_evidence_is_omitted(self):
        intelligence = self._intelligence([])

        self.assertEqual(references_from_intelligence(intelligence, ["methodology"]), {})

    def test_a_non_canonical_section_is_ignored(self):
        intelligence = self._intelligence([{"page": 3, "chunk_id": 0}])

        self.assertEqual(references_from_intelligence(intelligence, ["future_work"]), {})

    def test_an_empty_intelligence_yields_nothing(self):
        self.assertEqual(references_from_intelligence({}, list(SECTION_NAMES)), {})


# ----------------------------------------------------------------------
# K. No AI inference in this layer
# ----------------------------------------------------------------------
class TestNoRelationshipInference(unittest.TestCase):

    def test_the_module_decides_no_relation(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()

        # The relation vocabulary belongs to relationship_schema.py and to
        # the model. Retrieval must not express an opinion.
        tree = ast.parse(source)
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for relation in ("aligned", "divergent", "complementary", "not_comparable"):
            self.assertNotIn(relation, literals)

    def test_the_module_does_not_import_the_relationship_schema(self):
        """Decoupled on purpose: rehydration takes bare references, so it
        is independently useful and independently committable."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("app.services.relationship_schema", modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
