"""
Regression baseline for the PDF path indexing uses: extraction, cleaning,
chunking, and the page / chunk_id mapping written to every Qdrant point.

PyMuPDF and langchain-text-splitters are unpinned in requirements.txt, so
any rebuild can change what an uploaded paper turns into. These tests make
such a change visible before it reaches production.

Three layers, by how much they depend on the environment:

  - TestFixtureIntegrity and TestPipelineInvariants run everywhere. They
    check the committed fixtures and properties of the real pipeline that
    hold for any extractor version (every chunk is a slice of its cleaned
    page, chunk ids run 0..n-1 per page, short pages are skipped, each page
    marker lands on its own page).
  - TestGoldenComparisonHarness runs everywhere and proves the golden
    comparison is not vacuous.
  - TestGoldenBaseline compares exact lengths, hashes and offsets with
    values recorded in the target environment (tests/pdf_golden.py,
    TARGET_ENVIRONMENT). It skips on any other platform, and FAILS on the
    target platform when the library versions differ from the target or
    when no golden values have been recorded yet — a missing or drifted
    baseline is reported, never passed over.

Fully offline: the fixtures are local files and nothing below reaches
Storage, Voyage or Qdrant.
"""
import copy
import hashlib
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from tests import pdf_golden as golden  # noqa: E402

#: From app/rag/chunker.py and app/api/index_document.py. Restated here so a
#: change to either is a visible test change, not a silent baseline shift.
CHUNK_SIZE = 1500
MIN_INDEXED_PAGE_CHARS = 100


class TestFixtureIntegrity(unittest.TestCase):

    def test_the_manifest_lists_exactly_the_committed_pdfs(self):
        on_disk = sorted(p.name for p in golden.FIXTURE_DIR.glob("*.pdf"))
        self.assertEqual(on_disk, golden.fixture_names())

    def test_every_fixture_matches_its_recorded_sha256(self):
        manifest = golden.load_manifest()["fixtures"]
        for name, entry in manifest.items():
            with self.subTest(fixture=name):
                data = (golden.FIXTURE_DIR / name).read_bytes()
                self.assertEqual(len(data), entry["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])


class TestPipelineInvariants(unittest.TestCase):
    """Properties of the real pipeline that do not depend on the
    extractor's exact output, checked on every platform."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = golden.load_manifest()["fixtures"]
        cls.results = {name: golden.extract(name) for name in golden.fixture_names()}

    def test_page_count_matches_the_generator(self):
        for name, result in self.results.items():
            with self.subTest(fixture=name):
                self.assertEqual(len(result["pages"]), self.manifest[name]["pages"])

    def test_pages_are_numbered_from_one_in_order(self):
        for name, result in self.results.items():
            with self.subTest(fixture=name):
                numbers = [page["page"] for page in result["pages"]]
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_each_page_marker_is_extracted_on_its_own_page_only(self):
        for name, result in self.results.items():
            texts = {page["page"]: page["text"] for page in result["pages"]}
            for page, marker in self.manifest[name]["page_markers"].items():
                with self.subTest(fixture=name, marker=marker):
                    found_on = [number for number, text in texts.items() if marker in text]
                    self.assertEqual(found_on, [int(page)])

    def test_blank_pages_extract_no_text(self):
        for name, result in self.results.items():
            texts = {page["page"]: page["text"] for page in result["pages"]}
            for page in self.manifest[name].get("blank_pages", []):
                with self.subTest(fixture=name, page=page):
                    self.assertEqual(texts[page].strip(), "")

    def test_every_chunk_is_a_contiguous_slice_of_its_cleaned_page(self):
        for name, result in self.results.items():
            for chunk, start in zip(result["chunks"], result["offsets"]):
                with self.subTest(fixture=name, page=chunk["page"], chunk_id=chunk["chunk_id"]):
                    self.assertGreaterEqual(start, 0)
                    page_text = result["cleaned"][chunk["page"]]
                    self.assertEqual(page_text[start:start + len(chunk["text"])], chunk["text"])

    def test_chunk_ids_run_from_zero_within_each_page_and_match_chunk_count(self):
        for name, result in self.results.items():
            by_page = {}
            for chunk in result["chunks"]:
                by_page.setdefault(chunk["page"], []).append(chunk)
            with self.subTest(fixture=name):
                self.assertEqual(list(by_page), sorted(by_page), "chunks must follow page order")
            for page, chunks in by_page.items():
                with self.subTest(fixture=name, page=page):
                    self.assertEqual([c["chunk_id"] for c in chunks], list(range(len(chunks))))
                    self.assertEqual({c["chunk_count"] for c in chunks}, {len(chunks)})
                    self.assertEqual({c["total_pages"] for c in chunks}, {len(result["pages"])})

    def test_chunk_offsets_increase_within_each_page(self):
        for name, result in self.results.items():
            last = {}
            for chunk, start in zip(result["chunks"], result["offsets"]):
                with self.subTest(fixture=name, page=chunk["page"], chunk_id=chunk["chunk_id"]):
                    self.assertGreater(start, last.get(chunk["page"], -1))
                last[chunk["page"]] = start

    def test_no_chunk_exceeds_the_configured_chunk_size(self):
        for name, result in self.results.items():
            for chunk in result["chunks"]:
                with self.subTest(fixture=name, page=chunk["page"], chunk_id=chunk["chunk_id"]):
                    self.assertLessEqual(len(chunk["text"]), CHUNK_SIZE)

    def test_exactly_the_pages_under_the_minimum_length_are_skipped(self):
        for name, result in self.results.items():
            chunked = {chunk["page"] for chunk in result["chunks"]}
            expected = {
                number for number, text in result["cleaned"].items()
                if len(text.strip()) >= MIN_INDEXED_PAGE_CHARS
            }
            with self.subTest(fixture=name):
                self.assertEqual(chunked, expected)

    def test_the_short_appendix_page_is_skipped(self):
        # Built under 100 characters on purpose (generate_fixtures.py), so the
        # skip rule is exercised by a real page, not only by the set check.
        chunked = {chunk["page"] for chunk in self.results["single_column.pdf"]["chunks"]}
        self.assertNotIn(3, chunked)
        self.assertIn(1, chunked)
        self.assertIn(2, chunked)

    def test_a_marker_chunk_maps_back_to_the_page_it_came_from(self):
        for name, result in self.results.items():
            for page, marker in self.manifest[name]["page_markers"].items():
                carrying = [c["page"] for c in result["chunks"] if marker in c["text"]]
                if int(page) not in {c["page"] for c in result["chunks"]}:
                    continue  # a skipped page has no chunks to map
                with self.subTest(fixture=name, marker=marker):
                    self.assertTrue(carrying, f"{marker} is in no chunk")
                    self.assertEqual(set(carrying), {int(page)})

    def test_extraction_and_chunking_are_repeatable_in_process(self):
        for name in golden.fixture_names():
            with self.subTest(fixture=name):
                self.assertEqual(
                    golden.summarize(golden.extract(name)),
                    golden.summarize(self.results[name]),
                )


class TestGoldenComparisonHarness(unittest.TestCase):
    """The comparison must catch a one-field change, or the baseline test
    would pass whatever the pipeline produced."""

    @classmethod
    def setUpClass(cls):
        cls.observed = golden.observe_all()

    def test_identical_values_have_no_differences(self):
        self.assertEqual(golden.compare(self.observed, copy.deepcopy(self.observed)), [])

    def test_a_changed_chunk_hash_is_reported(self):
        expected = copy.deepcopy(self.observed)
        expected["single_column.pdf"]["chunks"][0]["sha256"] = "0" * 64
        differences = golden.compare(self.observed, expected)
        self.assertEqual(len(differences), 1)
        self.assertIn("single_column.pdf.chunks[0].sha256", differences[0])

    def test_a_changed_offset_or_page_mapping_is_reported(self):
        expected = copy.deepcopy(self.observed)
        expected["two_column.pdf"]["chunks"][-1]["start"] += 1
        expected["two_column.pdf"]["chunks"][-1]["page"] += 1
        self.assertEqual(len(golden.compare(self.observed, expected)), 2)

    def test_a_missing_chunk_is_reported(self):
        expected = copy.deepcopy(self.observed)
        expected["typography.pdf"]["chunks"].append(dict(expected["typography.pdf"]["chunks"][0]))
        differences = golden.compare(self.observed, expected)
        self.assertTrue(any("expected" in d and "items" in d for d in differences), differences)

    def test_environment_differences_are_reported(self):
        target = golden.TARGET_ENVIRONMENT
        drifted = dict(target, PyMuPDF="2.0.1")
        self.assertEqual(golden.environment_differences(target, target, golden.LIBRARY_KEYS), [])
        self.assertEqual(
            golden.environment_differences(drifted, target, golden.LIBRARY_KEYS),
            ["PyMuPDF: expected 2.0, found 2.0.1"],
        )
        self.assertEqual(
            golden.environment_differences(dict(target, PyMuPDF="2.0.0"), target, golden.LIBRARY_KEYS),
            [],
        )


class TestGoldenBaseline(unittest.TestCase):

    def test_pipeline_output_matches_the_golden_baseline(self):
        environment = golden.current_environment()
        target = golden.TARGET_ENVIRONMENT

        off_platform = golden.environment_differences(environment, target, golden.PLATFORM_KEYS)
        if off_platform:
            self.skipTest(
                "golden values apply only to the target platform ("
                + "; ".join(off_platform) + ")"
            )

        drift = golden.environment_differences(environment, target, golden.LIBRARY_KEYS)
        self.assertEqual(
            drift, [],
            "PDF library versions differ from the production baseline. requirements.txt "
            "does not pin them, so a Railway rebuild would pick up the same versions: "
            + "; ".join(drift),
        )

        if not golden.GOLDEN_PATH.exists():
            self.fail(
                f"No golden values recorded yet ({golden.GOLDEN_PATH.name}). This IS the target "
                "environment: review the values below and save them with "
                "`python -m tests.pdf_golden --write`.\n"
                + json.dumps(golden.golden_document(), indent=2, sort_keys=True)
            )

        recorded = json.loads(golden.GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            golden.environment_differences(
                environment, recorded["environment"], golden.PLATFORM_KEYS + golden.LIBRARY_KEYS
            ),
            [],
            "golden values were recorded in a different environment",
        )

        differences = golden.compare(golden.observe_all(), recorded["fixtures"])
        self.assertEqual(differences, [], "\n".join(differences[:40]))


if __name__ == "__main__":
    unittest.main()
