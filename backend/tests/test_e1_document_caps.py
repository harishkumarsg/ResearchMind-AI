"""
E1 mechanism D: per-paper page and chunk caps.

The property that matters is not the error message but the ordering: an
oversized document must be rejected BEFORE create_embeddings() is called,
so it costs nothing at the provider and writes nothing to Qdrant.

These caps are input validation, not a per-user policy, so they stay
active even when QUOTA_ENFORCEMENT is off.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.api.index_document import index_one_paper

OWNER = uuid.UUID("dddddddd-4444-4444-8444-dddddddddddd")


def _paper(title="paper.pdf"):
    paper = MagicMock()
    paper.id = uuid.uuid4()
    paper.owner_id = OWNER
    paper.title = title
    return paper


def _pages(count):
    return [{"page": i + 1, "text": "lorem ipsum " * 40} for i in range(count)]


def _chunks(count):
    return [{"text": f"chunk {i}", "page": 1, "chunk_id": i} for i in range(count)]


class DocumentCapTestCase(unittest.TestCase):
    """Everything below index_one_paper is mocked: no Storage read, no
    Voyage call, no Qdrant write."""

    def setUp(self):
        self.fetch = patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

        self.embeddings = patch("app.api.index_document.create_embeddings")
        self.create_embeddings = self.embeddings.start()
        self.create_embeddings.return_value = [[0.1] * 1024]
        self.addCleanup(self.embeddings.stop)

        self.qdrant = patch("app.api.index_document.client")
        self.client = self.qdrant.start()
        self.addCleanup(self.qdrant.stop)

        self.collection = patch("app.api.index_document.create_collection")
        self.collection.start()
        self.addCleanup(self.collection.stop)

        self.clear_points = patch("app.api.index_document._clear_existing_points_for_paper")
        self.clear_points.start()
        self.addCleanup(self.clear_points.stop)


class TestPageCap(DocumentCapTestCase):
    def test_a_paper_over_the_page_cap_never_reaches_the_embedder(self):
        meter = MagicMock()
        with patch.dict("os.environ", {"MAX_PAGES_PER_PAPER": "40"}, clear=False):
            with patch("app.api.index_document.extract_pdf_pages", return_value=_pages(41)):
                with self.assertRaises(ValueError) as ctx:
                    index_one_paper(_paper(), before_provider_work=meter)

        self.assertIn("41 pages", str(ctx.exception))
        self.assertIn("limit 40", str(ctx.exception))
        self.create_embeddings.assert_not_called()
        self.client.upsert.assert_not_called()
        # Quota model B charges through this hook, so an oversized paper
        # costs the user nothing.
        meter.assert_not_called()

    def test_a_paper_at_the_cap_is_accepted_and_metered_once(self):
        meter = MagicMock()
        with patch.dict("os.environ", {"MAX_PAGES_PER_PAPER": "40"}, clear=False):
            with patch(
                "app.api.index_document.extract_pdf_pages", return_value=_pages(40)
            ), patch(
                "app.api.index_document._build_chunks_for_paper", return_value=_chunks(3)
            ):
                self.create_embeddings.return_value = [[0.1] * 1024] * 3
                written = index_one_paper(_paper(), before_provider_work=meter)

        self.assertEqual(written, 3)
        self.create_embeddings.assert_called_once()
        meter.assert_called_once()

    def test_the_cap_is_configurable(self):
        with patch.dict("os.environ", {"MAX_PAGES_PER_PAPER": "5"}, clear=False):
            with patch("app.api.index_document.extract_pdf_pages", return_value=_pages(6)):
                with self.assertRaises(ValueError) as ctx:
                    index_one_paper(_paper())

        self.assertIn("limit 5", str(ctx.exception))
        self.create_embeddings.assert_not_called()


class TestChunkCap(DocumentCapTestCase):
    def test_a_paper_over_the_chunk_cap_never_reaches_the_embedder(self):
        meter = MagicMock()
        with patch.dict(
            "os.environ",
            {"MAX_PAGES_PER_PAPER": "40", "MAX_CHUNKS_PER_PAPER": "150"},
            clear=False,
        ):
            with patch(
                "app.api.index_document.extract_pdf_pages", return_value=_pages(10)
            ), patch(
                "app.api.index_document._build_chunks_for_paper", return_value=_chunks(151)
            ):
                with self.assertRaises(ValueError) as ctx:
                    index_one_paper(_paper(), before_provider_work=meter)

        self.assertIn("151 passages", str(ctx.exception))
        self.assertIn("limit 150", str(ctx.exception))
        self.create_embeddings.assert_not_called()
        self.client.upsert.assert_not_called()
        meter.assert_not_called()

    def test_a_paper_at_the_chunk_cap_is_accepted(self):
        with patch.dict("os.environ", {"MAX_CHUNKS_PER_PAPER": "4"}, clear=False):
            with patch(
                "app.api.index_document.extract_pdf_pages", return_value=_pages(2)
            ), patch(
                "app.api.index_document._build_chunks_for_paper", return_value=_chunks(4)
            ):
                self.create_embeddings.return_value = [[0.1] * 1024] * 4
                written = index_one_paper(_paper())

        self.assertEqual(written, 4)


class TestCapsAreNotAUserPolicy(DocumentCapTestCase):
    def test_caps_apply_even_with_quota_enforcement_off(self):
        with patch.dict(
            "os.environ",
            {"QUOTA_ENFORCEMENT": "off", "MAX_PAGES_PER_PAPER": "10"},
            clear=False,
        ):
            with patch("app.api.index_document.extract_pdf_pages", return_value=_pages(11)):
                with self.assertRaises(ValueError):
                    index_one_paper(_paper())

        self.create_embeddings.assert_not_called()

    def test_the_existing_empty_pdf_guard_still_fires_first(self):
        meter = MagicMock()
        with patch("app.api.index_document.extract_pdf_pages", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                index_one_paper(_paper(), before_provider_work=meter)

        self.assertIn("Unable to extract any pages", str(ctx.exception))
        self.create_embeddings.assert_not_called()
        meter.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
