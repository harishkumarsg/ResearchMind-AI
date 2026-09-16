"""
Evidence-budget breadth for /ask-stream (E2b).

E2 stopped a new question being narrowed to a single paper, but a second
defect cancelled it out in production: evidence was chosen by one greedy
pass under MAX_CONTEXT, so a few long chunks from the top paper spent the
whole budget before another paper was reached. For "What is the research
about?" the three ETASR chunks used 3417 of 4000 characters and the EIAD
chunk (696) was skipped.

_select_evidence now takes the best fitting chunk of each paper first, then
fills the rest in rank order, then restores rank order. These tests pin that
behaviour, pin that single-paper selection is unchanged against a verbatim
copy of the old loop, and pin every invariant the old loop guaranteed.

Unit tests call _select_evidence directly. Endpoint tests drive /ask-stream
with Voyage, Qdrant and Groq mocked — no real provider call is made.
"""
import json
import os
import random
import re
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from fastapi.testclient import TestClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.api.ask_stream import (
    EVIDENCE_SEPARATOR,
    MAX_CONTEXT,
    NO_ANSWER_RESPONSE,
    _select_evidence,
)
from app.core.auth import get_current_owner_id
from app.memory import _store as module_store
from app.services.chat_store import set_current_paper
from tests.sqlite_harness import attach_sqlite_db

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

ETASR_ID = "11111111-1111-4111-8111-111111111111"
EIAD_ID = "22222222-2222-4222-8222-222222222222"
THIRD_ID = "33333333-3333-4333-8333-333333333333"

#: "[Page N]\n" for a single-digit page — the fixed overhead of every block.
LABEL = len("[Page 1]\n")


def hit(point_id, page, text, paper="ETASR_18859.pdf", paper_id=ETASR_ID):
    h = MagicMock()
    h.id = point_id
    h.payload = {
        "paper": paper,
        "source": paper,
        "paper_id": paper_id,
        "page": page,
        "chunk_id": 0,
        "text": text,
    }
    return h


def eiad(point_id, page, text):
    return hit(point_id, page, text, paper="2503.14162v2.pdf", paper_id=EIAD_ID)


def third(point_id, page, text):
    return hit(point_id, page, text, paper="Third.pdf", paper_id=THIRD_ID)


def ids(selected):
    return [h.id for h, _ in selected]


def joined_length(selected):
    return len(EVIDENCE_SEPARATOR.join(block for _, block in selected))


def reference_old_loop(filtered):
    """A verbatim copy of the single-pass loop this change replaced, kept as
    the oracle for proving single-paper selection is unchanged."""
    selected = []
    seen_ids = set()
    used_chars = 0
    for h in filtered:
        if h.id in seen_ids:
            continue
        text = (h.payload.get("text") or "").strip()
        if not text:
            continue
        block = f"[Page {h.payload.get('page', '')}]\n{text}"
        cost = len(block) + (len(EVIDENCE_SEPARATOR) if selected else 0)
        if used_chars + cost > MAX_CONTEXT:
            continue
        seen_ids.add(h.id)
        selected.append((h, block))
        used_chars += cost
    return selected


# ----------------------------------------------------------------------
# Unit tests: _select_evidence
# ----------------------------------------------------------------------
class TestConfiguration(unittest.TestCase):
    def test_max_context_is_still_4000(self):
        self.assertEqual(MAX_CONTEXT, 4000)


class TestProductionCase(unittest.TestCase):
    """The exact lengths observed in production for "What is the research
    about?" against ETASR_18859.pdf and 2503.14162v2.pdf."""

    def setUp(self):
        self.filtered = [
            hit("etasr-p1", 1, "e" * 1424),
            hit("etasr-p6-bib", 6, "b" * 495),
            hit("etasr-p6-future", 6, "f" * 1467),
            eiad("eiad-p1", 1, "i" * 685),
        ]

    def test_old_loop_reproduces_the_production_failure(self):
        old = reference_old_loop(self.filtered)
        self.assertEqual(ids(old), ["etasr-p1", "etasr-p6-bib", "etasr-p6-future"])
        self.assertEqual(joined_length(old), 3417)

    def test_eiad_survives_and_the_long_etasr_chunk_is_skipped(self):
        selected = _select_evidence(self.filtered)

        self.assertIn("etasr-p1", ids(selected))
        self.assertIn("etasr-p6-bib", ids(selected))
        self.assertIn("eiad-p1", ids(selected))
        self.assertNotIn("etasr-p6-future", ids(selected))

    def test_rank_order_and_exact_context_length(self):
        selected = _select_evidence(self.filtered)

        self.assertEqual(ids(selected), ["etasr-p1", "etasr-p6-bib", "eiad-p1"])
        self.assertEqual(joined_length(selected), 2635)
        self.assertLessEqual(joined_length(selected), MAX_CONTEXT)

    def test_selected_zero_is_still_the_top_ranked_chunk(self):
        self.assertEqual(_select_evidence(self.filtered)[0][0].id, "etasr-p1")

    def test_block_format_is_unchanged(self):
        _, block = _select_evidence(self.filtered)[0]
        self.assertEqual(block, "[Page 1]\n" + "e" * 1424)


class TestSinglePaperUnchanged(unittest.TestCase):
    def test_simple_single_paper_selection_is_identical(self):
        filtered = [hit("a", 1, "x" * 1000), hit("b", 2, "y" * 1000), hit("c", 3, "z" * 1000)]
        self.assertEqual(ids(_select_evidence(filtered)), ids(reference_old_loop(filtered)))

    def test_oversized_leading_chunk_is_skipped_identically(self):
        filtered = [hit("huge", 1, "x" * 4500), hit("ok", 2, "y" * 300), hit("ok2", 3, "z" * 300)]
        self.assertEqual(ids(_select_evidence(filtered)), ["ok", "ok2"])
        self.assertEqual(ids(_select_evidence(filtered)), ids(reference_old_loop(filtered)))

    def test_randomised_single_paper_equivalence(self):
        rng = random.Random(20260917)
        lengths = [0, 40, 400, 700, 1400, 1500, 2500, 3990, 4100]
        for _ in range(3000):
            filtered = []
            for i in range(4):
                point = rng.choice([f"p{i}", "dup"])
                filtered.append(hit(point, rng.randrange(1, 8), "x" * rng.choice(lengths)))
            self.assertEqual(
                ids(_select_evidence(filtered)),
                ids(reference_old_loop(filtered)),
                f"diverged for lengths {[len(h.payload['text']) for h in filtered]}",
            )


class TestMultiPaper(unittest.TestCase):
    def test_all_chunks_fit_naturally_so_nothing_changes(self):
        filtered = [hit("a1", 1, "a" * 300), eiad("b1", 1, "b" * 300), hit("a2", 2, "c" * 300)]

        selected = _select_evidence(filtered)

        self.assertEqual(ids(selected), ["a1", "b1", "a2"])
        self.assertEqual(ids(selected), ids(reference_old_loop(filtered)))

    def test_three_papers_each_keep_their_best_chunk_under_a_tight_budget(self):
        filtered = [
            hit("a1", 1, "a" * 1500),
            hit("a2", 2, "a" * 1500),
            eiad("b1", 1, "b" * 1000),
            third("c1", 1, "c" * 1000),
        ]

        selected = _select_evidence(filtered)

        # Old loop: a1 (1509) + a2 (1511) = 3020, so b1 would reach 4031 and
        # c1 likewise — BOTH other papers vanished behind one paper's chunks.
        self.assertEqual(ids(reference_old_loop(filtered)), ["a1", "a2"])
        # Breadth first: one chunk from each paper (1509 + 1011 + 1011 =
        # 3531), after which a2 (1511) cannot fit.
        self.assertEqual(ids(selected), ["a1", "b1", "c1"])
        self.assertEqual(joined_length(selected), 3531)
        self.assertLessEqual(joined_length(selected), MAX_CONTEXT)

    def test_a_paper_whose_best_chunk_cannot_fit_is_omitted_without_breaking_budget(self):
        filtered = [
            hit("a1", 1, "a" * 2500),
            eiad("b1", 1, "b" * 1200),
            third("c1", 1, "c" * 1000),
        ]

        selected = _select_evidence(filtered)

        self.assertEqual(ids(selected), ["a1", "b1"])
        self.assertLessEqual(joined_length(selected), MAX_CONTEXT)

    def test_lower_ranked_papers_are_only_eligible_if_already_in_filtered(self):
        # _select_evidence only ever sees the top TOP_CHUNKS it is given.
        filtered = [hit("a1", 1, "a" * 100), hit("a2", 2, "a" * 100)]
        selected = _select_evidence(filtered)
        self.assertEqual({h.payload["paper_id"] for h, _ in selected}, {ETASR_ID})

    def test_paper_key_falls_back_to_title_when_paper_id_is_missing(self):
        a = hit("a1", 1, "a" * 2000)
        a.payload["paper_id"] = None
        a2 = hit("a2", 2, "a" * 1800)
        a2.payload["paper_id"] = None
        b = eiad("b1", 1, "b" * 900)
        b.payload["paper_id"] = None

        selected = _select_evidence([a, a2, b])

        # Titles differ, so b still earns a breadth slot.
        self.assertIn("b1", ids(selected))


class TestInvariants(unittest.TestCase):
    def test_duplicate_point_ids_are_selected_at_most_once(self):
        filtered = [hit("same", 1, "first"), eiad("same", 2, "second"), eiad("other", 3, "third")]

        selected = _select_evidence(filtered)

        self.assertEqual(ids(selected), ["same", "other"])
        self.assertEqual(len(set(ids(selected))), len(selected))

    def test_empty_chunks_are_never_selected(self):
        filtered = [hit("a1", 1, "a" * 100), eiad("empty", 1, "   "), eiad("b1", 2, "b" * 100)]

        selected = _select_evidence(filtered)

        self.assertNotIn("empty", ids(selected))
        self.assertTrue(all(block.strip() for _, block in selected))

    def test_an_empty_chunk_does_not_claim_its_papers_breadth_slot(self):
        filtered = [
            hit("a1", 1, "a" * 2000),
            hit("a2", 2, "a" * 1800),
            eiad("b-empty", 1, ""),
            eiad("b-real", 2, "b" * 150),
        ]

        selected = _select_evidence(filtered)

        # The empty EIAD chunk did not represent the paper, so the real one
        # still got the breadth slot ahead of a2.
        self.assertIn("b-real", ids(selected))
        self.assertNotIn("b-empty", ids(selected))

    def test_a_single_block_of_exactly_4000_chars_is_kept(self):
        filtered = [hit("exact", 1, "x" * (MAX_CONTEXT - LABEL))]
        selected = _select_evidence(filtered)
        self.assertEqual(ids(selected), ["exact"])
        self.assertEqual(joined_length(selected), 4000)

    def test_a_single_block_of_4001_chars_is_skipped(self):
        filtered = [hit("over", 1, "x" * (MAX_CONTEXT - LABEL + 1))]
        self.assertEqual(_select_evidence(filtered), [])

    def test_two_blocks_totalling_exactly_4000_are_both_kept(self):
        first = 2000 - LABEL
        second = 4000 - 2000 - len(EVIDENCE_SEPARATOR) - LABEL
        filtered = [hit("a", 1, "x" * first), eiad("b", 1, "y" * second)]

        selected = _select_evidence(filtered)

        self.assertEqual(ids(selected), ["a", "b"])
        self.assertEqual(joined_length(selected), 4000)

    def test_two_blocks_totalling_4001_keep_only_the_first(self):
        first = 2000 - LABEL
        second = 4000 - 2000 - len(EVIDENCE_SEPARATOR) - LABEL + 1
        filtered = [hit("a", 1, "x" * first), eiad("b", 1, "y" * second)]

        selected = _select_evidence(filtered)

        self.assertEqual(ids(selected), ["a"])
        self.assertLessEqual(joined_length(selected), MAX_CONTEXT)

    def test_empty_input_selects_nothing(self):
        self.assertEqual(_select_evidence([]), [])


# ----------------------------------------------------------------------
# Endpoint tests: /ask-stream end to end
# ----------------------------------------------------------------------
def _groq_stream(answer_text):
    if not answer_text:
        return iter([])
    return iter(
        [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])]
    )


def _parse_supplied_passages(prompt):
    body = prompt.split("Current Retrieved Context:\n", 1)[1]
    body = body.split("\n\n----------------------------------------", 1)[0]
    if not body.strip():
        return []
    return [
        (page, text.strip())
        for page, text in re.findall(r"\[Page ([^\]]*)\]\n(.*?)(?=\n\n\[Page |\Z)", body, re.S)
    ]


class EndpointTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        module_store.clear(USER_A)

    def seed_paper_rows(self, *paper_ids):
        from app.db.models import Paper

        with self.factory() as db:
            for index, paper_id in enumerate(paper_ids):
                db.add(
                    Paper(
                        id=uuid.UUID(paper_id),
                        owner_id=uuid.UUID(USER_A),
                        title=f"Paper {index}",
                        content_hash=f"{index}" * 64,
                        storage_path=f"{USER_A}/{paper_id}/original.pdf",
                        file_size_bytes=1024,
                        status="indexed",
                    )
                )
            db.commit()

    def run_ask(self, hits, answer="Grounded answer.", question="What is the research about?", params=None):
        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024), patch(
            "app.api.ask_stream.client"
        ) as mock_qdrant, patch(
            "app.api.ask_stream.rerank_results", side_effect=lambda q, r: r
        ), patch(
            "app.api.ask_stream._groq_client"
        ) as mock_groq:
            mock_qdrant.query_points.return_value.points = hits
            mock_groq.chat.completions.create.return_value = _groq_stream(answer)

            query = {"question": question}
            if params:
                query.update(params)
            resp = self.client.get("/ask-stream", params=query)

            events = []
            for frame in resp.text.split("\n\n"):
                frame = frame.strip()
                if frame.startswith("data:"):
                    events.append(json.loads(frame[len("data:") :].strip()))

            call = mock_groq.chat.completions.create.call_args
            prompt = call.kwargs["messages"][1]["content"] if call else ""

        done = [e for e in events if e.get("type") == "done"]
        citations = done[0]["citations"] if done else None
        return _parse_supplied_passages(prompt), citations, prompt, mock_qdrant

    def production_hits(self):
        return [
            hit("etasr-p1", 1, "e" * 1424),
            hit("etasr-p6-bib", 6, "b" * 495),
            hit("etasr-p6-future", 6, "f" * 1467),
            eiad("eiad-p1", 1, "i" * 685),
        ]


class TestEndpointProductionCase(EndpointTestCase):
    def test_both_papers_reach_the_model_and_the_citations(self):
        supplied, citations, _, _ = self.run_ask(self.production_hits())

        self.assertEqual(
            [(c["paper"], c["page"]) for c in citations],
            [("ETASR_18859.pdf", 1), ("ETASR_18859.pdf", 6), ("2503.14162v2.pdf", 1)],
        )
        self.assertEqual(len(supplied), 3)

    def test_citations_are_a_strict_projection_of_supplied_evidence(self):
        supplied, citations, _, _ = self.run_ask(self.production_hits())

        self.assertEqual(len(supplied), len(citations))
        self.assertEqual([p for p, _ in supplied], [str(c["page"]) for c in citations])
        self.assertEqual([len(t) for _, t in supplied], [1424, 495, 685])

    def test_supplied_context_is_within_budget(self):
        _, _, prompt, _ = self.run_ask(self.production_hits())

        body = prompt.split("Current Retrieved Context:\n", 1)[1]
        body = body.split("\n\n----------------------------------------", 1)[0]
        self.assertEqual(len(body), 2635)
        self.assertLessEqual(len(body), MAX_CONTEXT)

    def test_current_paper_id_follows_selected_zero(self):
        self.seed_paper_rows(ETASR_ID, EIAD_ID)
        self.run_ask(self.production_hits())

        from app.db.models import ChatSession

        with self.factory() as db:
            session = db.query(ChatSession).one()
        self.assertEqual(session.current_paper_id, uuid.UUID(ETASR_ID))


class TestEndpointUnchangedBehaviour(EndpointTestCase):
    def prime_history(self):
        self.run_ask([hit("seed", 1, "seed turn")], question="What is the research about?")

    def test_followup_lock_still_restricts_to_current_paper_id(self):
        self.prime_history()
        self.seed_paper_rows(ETASR_ID, EIAD_ID)
        set_current_paper(USER_A, uuid.UUID(EIAD_ID))

        hits = [hit("etasr", 1, "e" * 300), eiad("eiad", 1, "i" * 300)]
        supplied, citations, _, _ = self.run_ask(hits, question="what algorithm does it use?")

        self.assertEqual(len(supplied), 1)
        self.assertEqual([c["paper"] for c in citations], ["2503.14162v2.pdf"])

    def test_owner_filter_still_carries_only_the_verified_owner(self):
        _, _, _, mock_qdrant = self.run_ask(
            self.production_hits(), params={"owner_id": USER_B, "user_id": USER_B}
        )

        query_filter = mock_qdrant.query_points.call_args.kwargs["query_filter"]
        self.assertIsInstance(query_filter, Filter)
        values = [
            c.match.value
            for c in (query_filter.must or [])
            if isinstance(c, FieldCondition) and c.key == "owner_id" and isinstance(c.match, MatchValue)
        ]
        self.assertEqual(values, [USER_A])

    def test_refusal_still_returns_no_citations(self):
        supplied, citations, _, _ = self.run_ask(self.production_hits(), answer=NO_ANSWER_RESPONSE)

        self.assertEqual(len(supplied), 3)
        self.assertEqual(citations, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
