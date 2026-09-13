"""
Evidence/citation integrity tests for /ask-stream (audit findings A1-A6).

The invariant these tests exist to enforce, IN CODE rather than by prompt
instruction:

    EXACT CHUNKS/PASSAGES SUPPLIED TO THE LLM
    =
    EXACT CHUNKS/PASSAGES REPRESENTED BY SOURCES

Each test reads the ACTUAL prompt handed to Groq (captured from the mocked
client) and compares it against the ACTUAL citations emitted on the SSE
"done" event — so a regression in either direction fails here.

Fully mocked: no real Voyage, Qdrant, or Groq call is made anywhere in
this file.
"""
import json
import os
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

from app.api.ask_stream import MAX_CONTEXT, NO_ANSWER_RESPONSE, SYSTEM_PROMPT
from tests.sqlite_harness import attach_sqlite_db
from app.core.auth import get_current_owner_id
from app.memory import _store as module_store

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _hit(point_id, page, chunk_id, text, paper="Paper A"):
    """A Qdrant hit double with a REAL unique .id, as the fix relies on."""
    h = MagicMock()
    h.id = point_id
    h.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "paper_id": "paper-uuid-1",
        "page": page,
        "chunk_id": chunk_id,
        "text": text,
    }
    return h


def _groq_stream(answer_text):
    if not answer_text:
        return iter([])
    return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])])


def _parse_supplied_passages(prompt):
    """Recover the EXACT ordered passages the model was given, by parsing
    the real prompt rather than trusting what the code claims it sent."""
    body = prompt.split("Current Retrieved Context:\n", 1)[1]
    body = body.split("\n\n----------------------------------------", 1)[0]
    if not body.strip():
        return []
    return [
        (page, text.strip())
        for page, text in re.findall(r"\[Page ([^\]]*)\]\n(.*?)(?=\n\n\[Page |\Z)", body, re.S)
    ]


class AskStreamEvidenceTestCase(unittest.TestCase):

    def setUp(self):
        from app.main import app
        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.client = TestClient(app, raise_server_exceptions=False)
        # ask_stream.py reads history/topic from the database and writes
        # a row per turn. A private in-memory one keeps this suite
        # offline without stubbing the behaviour it exercises.
        attach_sqlite_db(self)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        module_store.clear(USER_A)

    def _run(self, hits, answer="Some grounded answer.", question="what is this about?"):
        """Drives the real endpoint with mocked Voyage/Qdrant/Groq and
        returns (supplied_passages, citations, prompt)."""
        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 1024), \
             patch("app.api.ask_stream.client") as mock_qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: r), \
             patch("app.api.ask_stream._groq_client") as mock_groq:

            mock_qdrant.query_points.return_value.points = hits
            mock_groq.chat.completions.create.return_value = _groq_stream(answer)

            resp = self.client.get("/ask-stream", params={"question": question})

            events = []
            for frame in resp.text.split("\n\n"):
                frame = frame.strip()
                if frame.startswith("data:"):
                    events.append(json.loads(frame[len("data:"):].strip()))

            create_kwargs = mock_groq.chat.completions.create.call_args.kwargs
            prompt = create_kwargs["messages"][1]["content"]

        done = [e for e in events if e.get("type") == "done"]
        citations = done[0]["citations"] if done else None
        return _parse_supplied_passages(prompt), citations, prompt


class TestA1PointIdDeduplication(AskStreamEvidenceTestCase):

    def test_chunks_sharing_page_local_chunk_id_are_all_kept(self):
        """chunk_id is the index WITHIN a page, so it repeats across pages.
        Three distinct passages that all happen to be chunk_id=0 must all
        survive — the old dedupe silently discarded two of them."""
        hits = [
            _hit("id-1", 6, 0, "Passage from page six."),
            _hit("id-2", 61, 0, "Passage from page sixty-one."),
            _hit("id-3", 67, 0, "Passage from page sixty-seven."),
        ]
        supplied, citations, _ = self._run(hits)

        self.assertEqual(len(supplied), 3)
        self.assertEqual(len(citations), 3)
        self.assertEqual([p for p, _ in supplied], ["6", "61", "67"])

    def test_identical_point_id_is_deduped(self):
        hits = [
            _hit("same-id", 6, 0, "Duplicated passage."),
            _hit("same-id", 6, 0, "Duplicated passage."),
            _hit("other-id", 9, 1, "A different passage."),
        ]
        supplied, citations, _ = self._run(hits)

        self.assertEqual(len(supplied), 2)
        self.assertEqual(len(citations), 2)


class TestA2ContextBudget(AskStreamEvidenceTestCase):

    def test_over_budget_chunk_is_in_neither_context_nor_citations(self):
        oversized = "X" * (MAX_CONTEXT + 500)
        hits = [
            _hit("id-big", 10, 0, oversized),
            _hit("id-small", 11, 0, "A short passage that fits."),
        ]
        supplied, citations, prompt = self._run(hits)

        self.assertNotIn(oversized, prompt)
        self.assertEqual([p for p, _ in supplied], ["11"])
        self.assertEqual([c["page"] for c in citations], [11])

    def test_budget_skips_oversized_but_still_considers_later_chunks(self):
        """Required behaviour: skip-and-continue, NOT break. An oversized
        candidate must not block lower-ranked candidates that still fit."""
        first = "A" * 2000
        oversized = "B" * (MAX_CONTEXT + 100)
        later = "C" * 500
        hits = [
            _hit("id-1", 1, 0, first),
            _hit("id-2", 2, 0, oversized),
            _hit("id-3", 3, 0, later),
        ]
        supplied, citations, prompt = self._run(hits)

        self.assertEqual([p for p, _ in supplied], ["1", "3"])
        self.assertEqual([c["page"] for c in citations], [1, 3])
        self.assertNotIn(oversized, prompt)

    def test_total_supplied_context_never_exceeds_the_budget(self):
        hits = [_hit(f"id-{i}", i, 0, "Z" * 1500) for i in range(1, 5)]
        _, _, prompt = self._run(hits)

        body = prompt.split("Current Retrieved Context:\n", 1)[1]
        body = body.split("\n\n----------------------------------------", 1)[0]
        self.assertLessEqual(len(body), MAX_CONTEXT)


class TestA3SamePageChunks(AskStreamEvidenceTestCase):

    def test_two_chunks_from_the_same_page_produce_two_citations(self):
        """Two distinct passages from one page are two pieces of evidence.
        The old (paper, page) citation dedupe under-reported this."""
        hits = [
            _hit("id-1", 61, 0, "First passage on page sixty-one."),
            _hit("id-2", 61, 1, "Second, different passage on page sixty-one."),
        ]
        supplied, citations, prompt = self._run(hits)

        self.assertEqual(len(supplied), 2)
        self.assertEqual(len(citations), 2)
        self.assertEqual([c["page"] for c in citations], [61, 61])
        self.assertIn("First passage on page sixty-one.", prompt)
        self.assertIn("Second, different passage on page sixty-one.", prompt)


class TestA4NoCarriedOverEvidence(AskStreamEvidenceTestCase):

    def test_previous_turn_evidence_is_never_supplied_to_a_later_prompt(self):
        self._run([_hit("id-1", 5, 0, "ALPHA_UNIQUE_EVIDENCE")], question="first question")
        _, _, prompt2 = self._run(
            [_hit("id-2", 9, 0, "BETA_UNIQUE_EVIDENCE")], question="second question about it"
        )

        self.assertIn("BETA_UNIQUE_EVIDENCE", prompt2)
        self.assertNotIn("ALPHA_UNIQUE_EVIDENCE", prompt2)

    def test_prompt_no_longer_contains_a_previous_topic_context_section(self):
        _, _, prompt = self._run([_hit("id-1", 5, 0, "Some evidence.")])
        self.assertNotIn("Previous Topic Context", prompt)


class TestA5PageLabelledEvidence(AskStreamEvidenceTestCase):

    def test_every_supplied_passage_carries_its_page_label(self):
        hits = [
            _hit("id-1", 61, 0, "Passage one."),
            _hit("id-2", 67, 0, "Passage two."),
        ]
        _, _, prompt = self._run(hits)

        self.assertIn("[Page 61]\nPassage one.", prompt)
        self.assertIn("[Page 67]\nPassage two.", prompt)

    def test_system_prompt_forbids_section_and_invented_citations(self):
        lowered = SYSTEM_PROMPT.lower()
        self.assertIn("[page n]", lowered)
        self.assertIn("never invent", lowered)
        self.assertIn("section", lowered)
        self.assertIn(NO_ANSWER_RESPONSE, SYSTEM_PROMPT)


class TestA6RefusalSuppressesSources(AskStreamEvidenceTestCase):

    def test_refusal_answer_emits_no_citations(self):
        hits = [
            _hit("id-1", 6, 0, "Loosely related passage."),
            _hit("id-2", 61, 0, "Another loosely related passage."),
        ]
        supplied, citations, _ = self._run(hits, answer=NO_ANSWER_RESPONSE)

        # The passages were still supplied to the model...
        self.assertEqual(len(supplied), 2)
        # ...but must not be advertised as Sources supporting the refusal.
        self.assertEqual(citations, [])

    def test_normal_answer_still_emits_citations(self):
        hits = [_hit("id-1", 6, 0, "Directly relevant passage.")]
        _, citations, _ = self._run(hits, answer="A real, grounded answer.")

        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["page"], 6)


class TestCitedFlag(AskStreamEvidenceTestCase):
    """The `cited` flag distinguishes evidence the answer actually used
    from evidence that was supplied but went unused. It must never REMOVE
    anything: the disclosed set stays exactly the supplied set."""

    def test_cited_page_is_flagged_cited(self):
        hits = [_hit("id-1", 6, 0, "Relevant passage.")]
        _, citations, _ = self._run(hits, answer="The objective is X [Page 6].")

        self.assertEqual(len(citations), 1)
        self.assertTrue(citations[0]["cited"])

    def test_retrieved_but_uncited_page_is_flagged_uncited_and_still_returned(self):
        hits = [_hit("id-1", 61, 0, "Passage the answer never used.")]
        _, citations, _ = self._run(hits, answer="An answer citing nothing at all.")

        # Still disclosed — never filtered out.
        self.assertEqual(len(citations), 1)
        self.assertFalse(citations[0]["cited"])

    def test_cited_and_uncited_evidence_coexist(self):
        hits = [
            _hit("id-1", 6, 0, "Used passage."),
            _hit("id-2", 61, 0, "Unused passage."),
            _hit("id-3", 7, 0, "Also used passage."),
        ]
        _, citations, _ = self._run(hits, answer="Objective is X [Page 6] and Y [Page 7].")

        by_page = {c["page"]: c["cited"] for c in citations}
        self.assertEqual(len(citations), 3)
        self.assertEqual(by_page, {6: True, 61: False, 7: True})

    def test_same_page_chunks_are_both_marked_cited_documented_caveat(self):
        """Matching is page-level by design, so two chunks sharing a cited
        page are both marked cited. Documented, not accidental."""
        hits = [
            _hit("id-1", 61, 0, "First passage on page 61."),
            _hit("id-2", 61, 1, "Second passage on page 61."),
        ]
        _, citations, _ = self._run(hits, answer="As shown [Page 61].")

        self.assertEqual(len(citations), 2)
        self.assertTrue(all(c["cited"] for c in citations))

    def test_refusal_yields_no_citations_at_all(self):
        hits = [
            _hit("id-1", 6, 0, "Loosely related."),
            _hit("id-2", 61, 0, "Also loosely related."),
        ]
        _, citations, _ = self._run(hits, answer=NO_ANSWER_RESPONSE)

        # Neither Sources nor "Also retrieved" can render from this.
        self.assertEqual(citations, [])

    def test_full_width_cjk_brackets_are_matched(self):
        """Models frequently emit 【Page 6】 instead of [Page 6]. A strict
        ASCII-only matcher would mark everything uncited and empty the
        Sources section entirely."""
        hits = [_hit("id-1", 6, 0, "Relevant passage.")]
        _, citations, _ = self._run(hits, answer="The objective is X 【Page 6】.")

        self.assertTrue(citations[0]["cited"])

    def test_parenthesised_and_loosely_spaced_forms_are_matched(self):
        hits = [
            _hit("id-1", 6, 0, "One."),
            _hit("id-2", 7, 0, "Two."),
        ]
        _, citations, _ = self._run(hits, answer="See (Page 6) and [ Page  7 ].")

        self.assertTrue(all(c["cited"] for c in citations))

    def test_bare_page_mention_without_brackets_is_not_treated_as_a_citation(self):
        """Guards against false positives from prose the answer quotes."""
        hits = [_hit("id-1", 6, 0, "Some passage.")]
        _, citations, _ = self._run(hits, answer="The text on Page 6 discusses many things.")

        self.assertFalse(citations[0]["cited"])

    def test_flag_does_not_change_the_supplied_evidence_set(self):
        """The A2/A3 invariant must survive the addition of `cited`."""
        hits = [
            _hit("id-1", 6, 0, "Alpha."),
            _hit("id-2", 61, 0, "Beta."),
        ]
        supplied, citations, prompt = self._run(hits, answer="Only this one [Page 6].")

        self.assertEqual(len(supplied), 2)
        self.assertEqual(len(citations), len(supplied))
        self.assertEqual(prompt.count("[Page "), len(citations))
        for (page, _text), citation in zip(supplied, citations):
            self.assertEqual(str(citation["page"]), page)


class TestEvidenceInvariant(AskStreamEvidenceTestCase):
    """The core invariant, asserted at passage identity level — not merely
    by comparing sets of page numbers, which cannot distinguish two
    different chunks that happen to share a page."""

    def test_supplied_passages_and_citations_are_exactly_one_to_one(self):
        # Deliberately adversarial: duplicate point id, two chunks sharing
        # a page, an oversized chunk, and a later chunk that still fits.
        oversized = "O" * (MAX_CONTEXT + 100)
        hits = [
            _hit("id-1", 61, 0, "UNIQUE_TEXT_ONE"),
            _hit("id-2", 61, 1, "UNIQUE_TEXT_TWO"),
            _hit("id-1", 61, 0, "UNIQUE_TEXT_ONE"),   # duplicate id -> must collapse
            _hit("id-3", 67, 0, oversized),           # must be excluded entirely
        ]
        expected_selected = [("61", "UNIQUE_TEXT_ONE"), ("61", "UNIQUE_TEXT_TWO")]

        supplied, citations, prompt = self._run(hits)

        # 1. Exactly the passages we expect were supplied, in order.
        self.assertEqual(supplied, expected_selected)

        # 2. Every supplied passage has exactly one citation, positionally
        #    matched (citations are a 1:1 projection of the same list).
        self.assertEqual(len(citations), len(supplied))
        for (page, _text), citation in zip(supplied, citations):
            self.assertEqual(str(citation["page"]), page)

        # 3. No citation refers to a passage that was not supplied.
        self.assertNotIn(67, [c["page"] for c in citations])

        # 4. No excluded passage leaked into the model's context.
        self.assertNotIn(oversized, prompt)

        # 5. No supplied passage is missing from Sources: the count of
        #    page labels in the real prompt equals the citation count.
        self.assertEqual(prompt.count("[Page "), len(citations))

    def test_invariant_holds_when_every_candidate_fits(self):
        hits = [
            _hit("id-1", 3, 0, "ALPHA"),
            _hit("id-2", 4, 0, "BETA"),
            _hit("id-3", 5, 0, "GAMMA"),
        ]
        supplied, citations, prompt = self._run(hits)

        self.assertEqual([t for _, t in supplied], ["ALPHA", "BETA", "GAMMA"])
        self.assertEqual([c["page"] for c in citations], [3, 4, 5])
        self.assertEqual(prompt.count("[Page "), len(citations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
