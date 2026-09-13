"""
Phase 2: confirms every point index_one_paper() builds actually carries
paper_id and owner_id in its payload — not just that indexing was
triggered for the right owner (already covered in
test_phase1_5_ownership.py), but that the data written to Qdrant is
correctly tagged.

Fully mocked: Storage fetch, PDF extraction, Voyage embeddings, and the
Qdrant client are all replaced with test doubles. No real PDF, no real
Voyage call, no real Qdrant call.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from voyageai.error import RateLimitError

from app.api.index_document import index_one_paper
from app.db.models import Paper


def _make_paper(owner_id, paper_id, title="Test Paper"):
    return Paper(
        id=paper_id,
        owner_id=owner_id,
        title=title,
        content_hash="a" * 64,
        storage_path=f"{owner_id}/{paper_id}/original.pdf",
        file_size_bytes=100,
        status="uploaded",
    )


class TestIndexedPointsCarryOwnership(unittest.TestCase):

    @patch("app.api.index_document.client")
    @patch("app.api.index_document.create_collection")
    @patch("app.api.index_document.create_embeddings")
    @patch("app.api.index_document.extract_pdf_pages")
    @patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
    def test_every_upserted_point_has_paper_id_and_owner_id(
        self, mock_fetch, mock_extract, mock_embed, mock_create_collection, mock_client
    ):
        owner_id = uuid.uuid4()
        paper_id = uuid.uuid4()
        paper = _make_paper(owner_id, paper_id)

        mock_extract.return_value = [
            {"page": 1, "text": "x" * 1200},
            {"page": 2, "text": "y" * 1200},
        ]
        # Two chunks worth of embeddings (one per page's single chunk)
        mock_embed.return_value = [[0.1] * 1024, [0.2] * 1024]

        points_written = index_one_paper(paper)

        mock_client.upsert.assert_called_once()
        upserted_points = mock_client.upsert.call_args.kwargs["points"]

        self.assertGreater(len(upserted_points), 0)
        self.assertEqual(points_written, len(upserted_points))

        for point in upserted_points:
            self.assertEqual(point.payload["owner_id"], str(owner_id))
            self.assertEqual(point.payload["paper_id"], str(paper_id))

    @patch("app.api.index_document.client")
    @patch("app.api.index_document.create_collection")
    @patch("app.api.index_document.create_embeddings")
    @patch("app.api.index_document.extract_pdf_pages")
    @patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
    def test_reindexing_clears_only_this_papers_own_points_first(
        self, mock_fetch, mock_extract, mock_embed, mock_create_collection, mock_client
    ):
        owner_id = uuid.uuid4()
        paper_id = uuid.uuid4()
        paper = _make_paper(owner_id, paper_id)

        mock_extract.return_value = [{"page": 1, "text": "z" * 1200}]
        mock_embed.return_value = [[0.3] * 1024]

        index_one_paper(paper)

        mock_client.delete.assert_called_once()
        delete_filter = mock_client.delete.call_args.kwargs["points_selector"]
        conditions = {c.key: c.match.value for c in delete_filter.must}
        self.assertEqual(conditions["paper_id"], str(paper_id))
        self.assertEqual(conditions["owner_id"], str(owner_id))


class TestIndexingRateLimitSafety(unittest.TestCase):
    """Regression coverage for the manual-testing finding: a Voyage
    RateLimitError partway through embedding a large paper must never
    reach Qdrant, never mark the paper indexed, and must leave a clean,
    safely-retryable state — exactly like any other embedding failure."""

    @patch("app.api.index_document.client")
    @patch("app.api.index_document.create_collection")
    @patch("app.api.index_document.create_embeddings")
    @patch("app.api.index_document.extract_pdf_pages")
    @patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
    def test_exhausted_rate_limit_retries_propagate_and_never_touch_qdrant(
        self, mock_fetch, mock_extract, mock_embed, mock_create_collection, mock_client
    ):
        owner_id = uuid.uuid4()
        paper_id = uuid.uuid4()
        paper = _make_paper(owner_id, paper_id)

        mock_extract.return_value = [{"page": 1, "text": "x" * 1200}]
        # Simulates create_embeddings() (embedder.py) having exhausted its
        # own internal rate-limit retries and given up.
        mock_embed.side_effect = RateLimitError("rate limited", None, 429, None, None)

        with self.assertRaises(RateLimitError):
            index_one_paper(paper)

        # Partial-index safety: a failure during embedding must never
        # reach Qdrant at all — not create_collection, not delete, not
        # upsert. Qdrant state (and the paper's indexed-or-not status,
        # set by index_document()'s caller) stays exactly as it was.
        mock_create_collection.assert_not_called()
        mock_client.delete.assert_not_called()
        mock_client.upsert.assert_not_called()

    @patch("app.api.index_document.client")
    @patch("app.api.index_document.create_collection")
    @patch("app.api.index_document.create_embeddings")
    @patch("app.api.index_document.extract_pdf_pages")
    @patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
    def test_successful_embedding_after_internal_retry_still_produces_correct_points(
        self, mock_fetch, mock_extract, mock_embed, mock_create_collection, mock_client
    ):
        """From index_one_paper()'s perspective, a rate limit that was
        retried and recovered INSIDE create_embeddings() is
        indistinguishable from one that never happened — it just gets a
        correct embedding list back."""
        owner_id = uuid.uuid4()
        paper_id = uuid.uuid4()
        paper = _make_paper(owner_id, paper_id)

        mock_extract.return_value = [{"page": 1, "text": "x" * 1200}]
        mock_embed.return_value = [[0.4] * 1024]

        points_written = index_one_paper(paper)

        self.assertEqual(points_written, 1)
        mock_client.upsert.assert_called_once()
        upserted_points = mock_client.upsert.call_args.kwargs["points"]
        self.assertEqual(upserted_points[0].payload["owner_id"], str(owner_id))
        self.assertEqual(upserted_points[0].payload["paper_id"], str(paper_id))
        self.assertEqual(len(upserted_points[0].vector), 1024)

    @patch("app.api.index_document.client")
    @patch("app.api.index_document.create_collection")
    @patch("app.api.index_document.create_embeddings")
    @patch("app.api.index_document.extract_pdf_pages")
    @patch("app.api.index_document.fetch_pdf", return_value=b"%PDF-1.4 fake")
    def test_retry_after_a_failed_attempt_still_clears_before_upserting_no_duplicates(
        self, mock_fetch, mock_extract, mock_embed, mock_create_collection, mock_client
    ):
        """First call fails (simulating exhausted rate-limit retries);
        second call (the paper's actual retry, e.g. via re-upload)
        succeeds. The retry must still clear this paper's own prior
        points before upserting fresh ones — never accumulate duplicates,
        and never touch another paper's/owner's points."""
        owner_id = uuid.uuid4()
        paper_id = uuid.uuid4()
        paper = _make_paper(owner_id, paper_id)

        mock_extract.return_value = [{"page": 1, "text": "x" * 1200}]

        mock_embed.side_effect = RateLimitError("rate limited", None, 429, None, None)
        with self.assertRaises(RateLimitError):
            index_one_paper(paper)
        mock_client.upsert.assert_not_called()

        mock_embed.side_effect = None
        mock_embed.return_value = [[0.7] * 1024]
        points_written = index_one_paper(paper)

        self.assertEqual(points_written, 1)
        mock_client.delete.assert_called_once()
        delete_filter = mock_client.delete.call_args.kwargs["points_selector"]
        conditions = {c.key: c.match.value for c in delete_filter.must}
        self.assertEqual(conditions["paper_id"], str(paper_id))
        self.assertEqual(conditions["owner_id"], str(owner_id))
        mock_client.upsert.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
