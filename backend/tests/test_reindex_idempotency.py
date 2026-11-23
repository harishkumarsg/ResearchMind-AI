"""
Re-indexing a paper leaves exactly one copy of its vectors.

index_one_paper() clears a paper's existing points immediately before it
upserts the fresh ones, and point ids are random, so the clear is the ONLY
thing standing between a retry and duplicate vectors. Until now every
test patched _clear_existing_points_for_paper out (test_e1_document_caps
does), so nothing checked either half of the guarantee:

  - the cleanup filter is exactly {owner_id, paper_id} — broad enough to
    remove every old copy, narrow enough to touch nothing else;
  - after any number of runs the paper has exactly N vectors.

Here the REAL index_one_paper, _build_chunks_for_paper, chunker and
_clear_existing_points_for_paper run against an in-memory Qdrant that
evaluates the delete filter itself. Only the Storage read, PDF parsing
and the Voyage embedder are stubbed — no network, no provider spend.
"""
import os
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: E402

from app.api.index_document import index_one_paper  # noqa: E402

OWNER_A = uuid.UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
OWNER_B = uuid.UUID("bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb")
PAPER_ID = uuid.UUID("cccccccc-1111-5111-8111-cccccccccccc")
OTHER_PAPER_ID = uuid.UUID("dddddddd-1111-5111-8111-dddddddddddd")


def _pages():
    """Three pages of distinct prose, long enough to pass the indexer's
    length checks and to split into several chunks per page."""
    pages = []
    for page in range(1, 4):
        sentences = " ".join(
            f"Page {page} sentence {i} describes a distinct finding about retrieval quality."
            for i in range(40)
        )
        pages.append({"page": page, "text": sentences})
    return pages


class FakeQdrant:
    """In-memory points store that evaluates delete filters itself.

    Deliberately strict: it understands only the filter shape the indexer
    is supposed to send (a `must` list of exact-match field conditions)
    and fails loudly on anything else, so a change to the cleanup's shape
    cannot pass by being misread.
    """

    def __init__(self):
        self.points = {}  # point id -> payload
        self.delete_filters = []

    def delete(self, collection_name, points_selector, **kwargs):
        assert isinstance(points_selector, Filter), f"unexpected selector: {points_selector!r}"
        assert not points_selector.should and not points_selector.must_not, points_selector
        for condition in points_selector.must:
            assert isinstance(condition, FieldCondition), condition
            assert isinstance(condition.match, MatchValue), condition
        self.delete_filters.append(points_selector)
        for point_id, payload in list(self.points.items()):
            if all(payload.get(c.key) == c.match.value for c in points_selector.must):
                del self.points[point_id]

    def upsert(self, collection_name, points, **kwargs):
        for point in points:
            self.points[str(point.id)] = dict(point.payload)

    def ids_for(self, owner_id, paper_id):
        return {
            point_id
            for point_id, payload in self.points.items()
            if payload.get("owner_id") == str(owner_id) and payload.get("paper_id") == str(paper_id)
        }


class ReindexTestCase(unittest.TestCase):

    def setUp(self):
        self.qdrant = FakeQdrant()
        self.paper = SimpleNamespace(id=PAPER_ID, owner_id=OWNER_A, title="retrieval-study.pdf")

        for target, kwargs in (
            ("app.api.index_document.client", {"new": self.qdrant}),
            ("app.api.index_document.create_collection", {}),
            ("app.api.index_document.fetch_pdf", {"return_value": b"%PDF-1.4 reindex"}),
            ("app.api.index_document.extract_pdf_pages", {"return_value": _pages()}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        embedder = patch(
            "app.api.index_document.create_embeddings",
            side_effect=lambda texts: [[0.0] * 1024 for _ in texts],
        )
        self.create_embeddings = embedder.start()
        self.addCleanup(embedder.stop)

    def seed(self, owner_id, paper_id, count):
        ids = set()
        for _ in range(count):
            point_id = str(uuid.uuid4())
            self.qdrant.points[point_id] = {"owner_id": str(owner_id), "paper_id": str(paper_id)}
            ids.add(point_id)
        return ids


class TestReindexIdempotency(ReindexTestCase):

    def test_the_cleanup_filter_is_exactly_this_owner_and_this_paper(self):
        index_one_paper(self.paper)

        self.assertEqual(len(self.qdrant.delete_filters), 1)
        conditions = {(c.key, c.match.value) for c in self.qdrant.delete_filters[0].must}
        self.assertEqual(
            conditions, {("owner_id", str(OWNER_A)), ("paper_id", str(PAPER_ID))}
        )

    def test_repeated_indexing_leaves_exactly_n_vectors(self):
        first = index_one_paper(self.paper)
        self.assertGreater(first, 1, "the fixture should produce several chunks")

        for run in range(2, 5):
            with self.subTest(run=run):
                written = index_one_paper(self.paper)
                self.assertEqual(written, first)
                self.assertEqual(len(self.qdrant.ids_for(OWNER_A, PAPER_ID)), first)
        self.assertEqual(len(self.qdrant.points), first)

    def test_reindex_after_a_failed_run_left_stale_points_still_ends_at_n(self):
        """A crash after upsert (or a timeout Qdrant applied anyway) can
        leave an old copy behind; the retry must replace it, not add."""
        stale = self.seed(OWNER_A, PAPER_ID, 7)

        written = index_one_paper(self.paper)

        current = self.qdrant.ids_for(OWNER_A, PAPER_ID)
        self.assertEqual(len(current), written)
        self.assertFalse(current & stale, "stale points survived the re-index")

    def test_other_papers_and_other_owners_are_untouched(self):
        same_owner_other_paper = self.seed(OWNER_A, OTHER_PAPER_ID, 3)
        # Defensive: even a point carrying the SAME paper_id under another
        # owner must survive — the owner condition is not redundant.
        other_owner_same_paper = self.seed(OWNER_B, PAPER_ID, 2)
        other_owner_other_paper = self.seed(OWNER_B, OTHER_PAPER_ID, 4)
        foreign = same_owner_other_paper | other_owner_same_paper | other_owner_other_paper

        written = index_one_paper(self.paper)
        index_one_paper(self.paper)

        self.assertTrue(foreign <= set(self.qdrant.points), "a foreign point was deleted")
        self.assertEqual(len(self.qdrant.points), len(foreign) + written)

    def test_every_written_point_carries_the_keys_the_cleanup_matches_on(self):
        """If the payload keys ever drifted from the filter keys, the
        cleanup would silently match nothing and every retry would add a
        full duplicate copy."""
        written = index_one_paper(self.paper)

        payloads = [p for p in self.qdrant.points.values()]
        self.assertEqual(len(payloads), written)
        for payload in payloads:
            self.assertEqual(payload["owner_id"], str(OWNER_A))
            self.assertEqual(payload["paper_id"], str(PAPER_ID))


if __name__ == "__main__":
    unittest.main(verbosity=2)
