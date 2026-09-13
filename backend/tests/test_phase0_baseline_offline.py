"""
Phase 0 baseline tests — OFFLINE ONLY.

No network calls of any kind. Exercises the existing, unmodified RAG utility
modules (pdf_loader, text_cleaner, chunker, metadata_extractor) against the
3 PDFs already present in backend/uploads/papers/, purely to establish what
today's actual behavior is before any Phase 1+ code changes land.

These are characterization tests: some assertions document known-flawed
current behavior (see metadata_extractor test) rather than asserting
correctness. That is intentional for a baseline.
"""
import glob
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services.pdf_loader import extract_pdf_pages
from app.services.text_cleaner import clean_text
from app.rag.chunker import create_chunks
from app.services.metadata_extractor import (
    extract_authors,
    extract_abstract,
    extract_keywords,
)

UPLOAD_DIR = os.path.join(BACKEND_DIR, "uploads", "papers")


class TestPdfExtractionBaseline(unittest.TestCase):
    """Confirms PyMuPDF-based extraction still works, unmodified, on the
    real papers currently in the library."""

    @classmethod
    def setUpClass(cls):
        cls.pdf_paths = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*.pdf")))

    def test_upload_dir_has_the_expected_three_pdfs(self):
        self.assertEqual(
            len(self.pdf_paths),
            3,
            f"Expected 3 PDFs in {UPLOAD_DIR}, found {len(self.pdf_paths)}: "
            f"{[os.path.basename(p) for p in self.pdf_paths]}",
        )

    def test_each_pdf_extracts_nonempty_pages(self):
        for path in self.pdf_paths:
            with self.subTest(pdf=os.path.basename(path)):
                pages = extract_pdf_pages(path)
                self.assertIsInstance(pages, list)
                self.assertGreater(
                    len(pages), 0, f"No pages extracted from {path}"
                )
                total_chars = sum(len(p.get("text", "")) for p in pages)
                self.assertGreater(
                    total_chars,
                    1000,
                    f"Suspiciously little text extracted from {path} "
                    f"({total_chars} chars) — possible extraction regression",
                )
                # Every page dict has the shape index_document.py expects
                for page in pages:
                    self.assertIn("page", page)
                    self.assertIn("text", page)


class TestTextCleanerBaseline(unittest.TestCase):
    """Documents clean_text()'s exact current normalization behavior."""

    def test_normalizes_whitespace_page_numbers_and_blank_lines(self):
        raw = "Line one.\t\tLine   one continued.\r\nPage 12\r\n\n\n\nLine two."
        cleaned = clean_text(raw)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("Page 12", cleaned)
        self.assertNotIn("\n\n\n", cleaned)
        self.assertNotIn("\t", cleaned)


class TestChunkerBaseline(unittest.TestCase):
    """Baseline for chunker.py's CURRENT per-call chunking behavior
    (chunk_size=1500, chunk_overlap=300). Phase 6 is expected to change
    this from per-page to whole-document chunking — this test documents
    what "before" looks like so that change can be diffed against it."""

    def test_chunk_size_and_overlap_bounds(self):
        # Build a long, distinguishable synthetic text (~6000 chars)
        sentence = "Sentence number {n} of the synthetic baseline text. "
        text = "".join(sentence.format(n=i) for i in range(200))
        self.assertGreater(len(text), 5000)

        chunks = create_chunks(text)

        self.assertIsInstance(chunks, list)
        self.assertGreater(len(chunks), 1, "Expected multiple chunks from ~6000 chars")

        for chunk in chunks:
            # RecursiveCharacterTextSplitter can slightly exceed chunk_size
            # when no clean separator is found near the boundary; 1600 gives
            # reasonable slack while still catching a real regression.
            self.assertLessEqual(
                len(chunk), 1600, "Chunk exceeds expected size bound (1500 + slack)"
            )

        # Overlap check: consecutive chunks should share a trailing/leading
        # substring given chunk_overlap=300.
        if len(chunks) >= 2:
            tail_of_first = chunks[0][-100:]
            self.assertIn(
                tail_of_first[-30:],
                chunks[1],
                "Expected overlap between consecutive chunks not found",
            )

    def test_short_text_produces_single_chunk(self):
        text = "A short paragraph well under the 1500-character chunk size."
        chunks = create_chunks(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)


class TestMetadataExtractorBaseline(unittest.TestCase):
    """Characterizes (does NOT validate) current metadata extraction
    behavior against one real paper. This is the M6 finding from the
    earlier review made concrete and measurable."""

    @classmethod
    def setUpClass(cls):
        pdf_paths = sorted(glob.glob(os.path.join(UPLOAD_DIR, "*.pdf")))
        if not pdf_paths:
            raise unittest.SkipTest("No PDFs available to characterize")
        pages = extract_pdf_pages(pdf_paths[0])
        full_text = clean_text("\n".join(p.get("text", "") for p in pages))
        cls.pdf_name = os.path.basename(pdf_paths[0])
        cls.full_text = full_text

    def test_functions_run_without_exception_and_return_strings(self):
        authors = extract_authors(self.full_text)
        abstract = extract_abstract(self.full_text)
        keywords = extract_keywords(self.full_text)

        self.assertIsInstance(authors, str)
        self.assertIsInstance(abstract, str)
        self.assertIsInstance(keywords, str)

        # Recorded, not asserted as "correct" — this is the baseline snapshot.
        # Sanitized to ASCII for the print only (Windows console codepage
        # cannot render every character PyMuPDF extracts, e.g. ligatures);
        # the underlying string values are untouched.
        def _safe(s):
            return s.encode("ascii", "backslashreplace").decode("ascii")

        print(f"\n[baseline] paper={self.pdf_name!r}")
        print(f"[baseline] extract_authors  -> {_safe(authors)!r}")
        print(f"[baseline] extract_abstract -> {_safe(abstract[:120])!r}...")
        print(f"[baseline] extract_keywords -> {_safe(keywords)!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
