"""
Reference-list text is kept out of the index (app/services/reference_filter.py).

The rules were measured by the retrieval eval as the "no-refs" variant
(eval/check_questions.classify_page). Production applies citation density
only inside a reference section: the run of citation-dense pages directly
after a References heading. These tests pin each case:

  * a heading mid-page keeps the body above it;
  * a heading near the top drops the whole page;
  * citation-dense continuation pages after the heading are dropped;
  * the first page that is not citation-dense ends the section, and every
    later page is kept (appendices);
  * a citation-dense page before the heading (related work) is kept;
  * a paper with no heading loses nothing;
and that the indexer applies the filter, and that the ported rules stay
identical to the eval's.

Offline: synthetic page text only, no PDF, provider or database.
"""
import os
import sys
import unittest
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from app.api.index_document import _build_chunks_for_paper
from app.db.models import Paper
from app.services import reference_filter
from app.services.reference_filter import (
    BODY,
    MIXED,
    REF_MIN_BODY,
    REF_RATIO,
    REFERENCE,
    citation_ratio,
    classify_pages,
    drop_reference_text,
)

PROSE_LINE = (
    "The proposed detector localises anomalies by comparing patch features "
    "against a memory bank of normal samples."
)


def prose(lines=8):
    """Body text with no citation markers, about 110 characters a line."""
    return "\n".join(PROSE_LINE for _ in range(lines))


def citations(start=1, count=10):
    """A citation-dense block: numbered entries with venues and years."""
    return "\n".join(
        f"[{n}] A. Author, B. Author, et al. A study of defects. In CVPR, pp. 1-9, 2023."
        for n in range(start, start + count)
    )


def related_work():
    """A related-work page: prose, but nearly every line carries a citation."""
    return "\n".join(
        f"Zhang et al. (2023) extend PatchCore [{n}] with CLIP features (CVPR 2023)."
        for n in range(1, 12)
    )


def kinds(texts):
    return [kind for kind, _ in classify_pages(texts)]


class TestFixturesAreWhatTheyClaim(unittest.TestCase):
    def test_prose_is_not_citation_dense(self):
        self.assertLess(citation_ratio(prose()), REF_RATIO)

    def test_citations_and_related_work_are_citation_dense(self):
        self.assertGreaterEqual(citation_ratio(citations()), REF_RATIO)
        self.assertGreaterEqual(citation_ratio(related_work()), REF_RATIO)

    def test_body_above_a_mid_page_heading_is_long_enough(self):
        self.assertGreaterEqual(len(prose(8)), REF_MIN_BODY)
        self.assertLess(len(prose(2)), REF_MIN_BODY)


class TestHeading(unittest.TestCase):
    def test_heading_mid_page_keeps_the_body_above_it(self):
        page = prose(8) + "\nReferences\n" + citations()

        [(kind, kept)] = classify_pages([page])

        self.assertEqual(kind, MIXED)
        self.assertEqual(kept, prose(8) + "\n")
        self.assertNotIn("[1]", kept)

    def test_heading_near_the_top_drops_the_page(self):
        page = prose(2) + "\nReferences\n" + citations()

        self.assertEqual(classify_pages([page]), [(REFERENCE, "")])

    def test_heading_on_the_first_line_drops_the_page(self):
        self.assertEqual(kinds(["References\n" + citations()]), [REFERENCE])

    def test_bibliography_heading_in_any_case(self):
        self.assertEqual(kinds(["BIBLIOGRAPHY\n" + citations()]), [REFERENCE])

    def test_the_word_in_a_sentence_is_not_a_heading(self):
        page = prose(4) + "\nSee the references for details.\n" + prose(4)
        self.assertEqual(kinds([page]), [BODY])


class TestReferenceSection(unittest.TestCase):
    def test_continuation_reference_pages_are_dropped(self):
        pages = [
            prose(),
            prose(8) + "\nReferences\n" + citations(1, 5),
            citations(6, 12),
            citations(18, 12),
        ]

        self.assertEqual(kinds(pages), [BODY, MIXED, REFERENCE, REFERENCE])

    def test_appendix_after_references_is_kept(self):
        # As in Reason-IAD: references, then an appendix of many pages.
        appendix = "Appendix A\n" + prose()
        pages = [
            prose(),
            "References\n" + citations(1, 12),
            citations(13, 12),
            appendix,
            prose(),
            prose(),
        ]

        result = classify_pages(pages)

        self.assertEqual([k for k, _ in result], [BODY, REFERENCE, REFERENCE, BODY, BODY, BODY])
        self.assertEqual(result[3], (BODY, appendix))

    def test_a_citation_dense_page_after_the_section_ended_is_kept(self):
        # Once a page that is not citation-dense ends the section, density
        # is never applied again: an appendix table of prior results stays.
        pages = ["References\n" + citations(), prose(), related_work()]

        self.assertEqual(kinds(pages), [REFERENCE, BODY, BODY])

    def test_a_second_heading_starts_a_new_section(self):
        # Supplementary material with its own reference list.
        pages = [
            "References\n" + citations(),
            prose(),
            prose(8) + "\nReferences\n" + citations(),
            citations(),
            prose(),
        ]

        self.assertEqual(kinds(pages), [REFERENCE, BODY, MIXED, REFERENCE, BODY])


class TestOutsideAReferenceSection(unittest.TestCase):
    def test_citation_dense_related_work_before_the_heading_is_kept(self):
        pages = [prose(), related_work(), prose(), "References\n" + citations()]

        result = classify_pages(pages)

        self.assertEqual([k for k, _ in result], [BODY, BODY, BODY, REFERENCE])
        self.assertEqual(result[1], (BODY, related_work()))

    def test_no_heading_drops_nothing(self):
        pages = [prose(), related_work(), citations(), citations()]

        self.assertEqual(kinds(pages), [BODY, BODY, BODY, BODY])


class TestDropReferenceText(unittest.TestCase):
    def pages(self):
        return [
            {"page": 1, "text": prose()},
            {"page": 2, "text": prose(8) + "\nReferences\n" + citations(1, 5)},
            {"page": 3, "text": citations(6, 12)},
            {"page": 4, "text": "Appendix A\n" + prose()},
        ]

    def test_page_entries_and_numbers_are_kept(self):
        out = drop_reference_text(self.pages())
        self.assertEqual([p["page"] for p in out], [1, 2, 3, 4])

    def test_body_pages_are_returned_untouched(self):
        pages = self.pages()
        out = drop_reference_text(pages)
        self.assertIs(out[0], pages[0])
        self.assertIs(out[3], pages[3])

    def test_mixed_page_keeps_its_body_and_a_reference_page_is_emptied(self):
        out = drop_reference_text(self.pages())
        self.assertEqual(out[1]["text"], prose(8) + "\n")
        self.assertEqual(out[2]["text"], "")

    def test_input_is_not_modified(self):
        pages = self.pages()
        before = [dict(p) for p in pages]
        drop_reference_text(pages)
        self.assertEqual(pages, before)

    def test_page_text_is_classified_after_cleaning(self):
        # The indexer cleans each page before chunking; the heading must
        # still be found when the extracted text uses CRLF and padding.
        page = {"page": 1, "text": prose(8).replace("\n", "\r\n") + "\r\n  References  \r\n" + citations()}
        [out] = drop_reference_text([page])
        self.assertNotIn("[1]", out["text"])
        self.assertGreaterEqual(len(out["text"]), REF_MIN_BODY)


class TestIndexerUsesTheFilter(unittest.TestCase):
    def paper(self):
        return Paper(
            id=uuid.UUID("00000000-0000-4000-8000-0000000000a1"),
            owner_id=uuid.UUID("00000000-0000-4000-8000-0000000000b1"),
            title="paper.pdf",
            content_hash="0" * 64,
            storage_path="x/y/original.pdf",
            file_size_bytes=0,
            status="uploaded",
        )

    def pages(self):
        return [
            {"page": 1, "text": prose(12)},
            {"page": 2, "text": prose(8) + "\nReferences\n" + citations(1, 5)},
            {"page": 3, "text": citations(6, 12)},
            {"page": 4, "text": "Appendix A\n" + prose(12)},
        ]

    def test_no_chunk_holds_reference_text(self):
        chunks = _build_chunks_for_paper(self.paper(), self.pages())

        self.assertEqual(sorted({c["page"] for c in chunks}), [1, 2, 4])
        self.assertFalse(any("et al." in c["text"] for c in chunks))
        self.assertTrue(all(c["total_pages"] == 4 for c in chunks))

    def test_drop_references_false_is_the_previous_indexer(self):
        chunks = _build_chunks_for_paper(self.paper(), self.pages(), drop_references=False)

        self.assertEqual(sorted({c["page"] for c in chunks}), [1, 2, 3, 4])
        self.assertTrue(any("et al." in c["text"] for c in chunks))


class TestPortedRulesMatchTheEval(unittest.TestCase):
    """The eval measured check_questions.classify_page; production must use
    the same heading, density and length rules."""

    @classmethod
    def setUpClass(cls):
        eval_dir = os.path.join(BACKEND_DIR, "eval")
        if eval_dir not in sys.path:
            sys.path.insert(0, eval_dir)
        import check_questions

        cls.cq = check_questions

    def test_constants_and_patterns_are_identical(self):
        self.assertEqual(reference_filter.REF_RATIO, self.cq.REF_RATIO)
        self.assertEqual(reference_filter.REF_MIN_BODY, self.cq.REF_MIN_BODY)
        self.assertEqual(reference_filter._CITATION_LINE.pattern, self.cq._CITATION_LINE.pattern)
        self.assertEqual(reference_filter._CITATION_LINE.flags, self.cq._CITATION_LINE.flags)
        self.assertEqual(reference_filter._REF_HEADING.pattern, self.cq._REF_HEADING.pattern)
        self.assertEqual(reference_filter._REF_HEADING.flags, self.cq._REF_HEADING.flags)

    def test_same_result_wherever_every_dense_page_follows_a_heading(self):
        pages = [
            prose(),
            prose(8) + "\nReferences\n" + citations(1, 5),
            citations(6, 12),
            prose(),
        ]
        self.assertEqual(classify_pages(pages), [self.cq.classify_page(p) for p in pages])

    def test_the_only_difference_is_density_outside_a_section(self):
        self.assertEqual(self.cq.classify_page(related_work())[0], REFERENCE)
        self.assertEqual(kinds([related_work()]), [BODY])


if __name__ == "__main__":
    unittest.main(verbosity=2)
