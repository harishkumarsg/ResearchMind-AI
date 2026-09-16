"""
Multi-paper retrieval for /ask-stream (E2).

A new question must keep whatever the retriever ranked highest, across
every paper in the library. /ask-stream used to narrow a new question's
evidence to the single paper owning the top hit, so a library-wide
question could never mention a second paper even when that paper's chunk
had outranked other kept chunks. These tests pin the corrected behaviour
and, just as importantly, pin the follow-up lock that must NOT change.

No existing suite seeds hits from two different papers, which is exactly
why the defect survived; every fixture here uses at least two.

Fully mocked: no real Voyage, Qdrant or Groq call is made in this file.
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
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.api.ask_stream import MAX_CONTEXT, NO_ANSWER_RESPONSE
from app.core.auth import get_current_owner_id
from app.memory import _store as module_store
from app.services.chat_store import set_current_paper
from tests.sqlite_harness import attach_sqlite_db

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

PAPER_A_ID = "11111111-1111-4111-8111-111111111111"
PAPER_B_ID = "22222222-2222-4222-8222-222222222222"


def _hit(point_id, page, text, paper="Paper A", paper_id=PAPER_A_ID, chunk_id=0):
    h = MagicMock()
    h.id = point_id
    h.payload = {
        "paper": paper,
        "source": f"{paper}.pdf",
        "paper_id": paper_id,
        "page": page,
        "chunk_id": chunk_id,
        "text": text,
    }
    return h


def _groq_stream(answer_text):
    if not answer_text:
        return iter([])
    return iter(
        [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer_text))])]
    )


def _parse_supplied_passages(prompt):
    """The EXACT ordered passages the model received, parsed from the real
    prompt rather than trusting what the code claims it sent."""
    body = prompt.split("Current Retrieved Context:\n", 1)[1]
    body = body.split("\n\n----------------------------------------", 1)[0]
    if not body.strip():
        return []
    return [
        (page, text.strip())
        for page, text in re.findall(
            r"\[Page ([^\]]*)\]\n(.*?)(?=\n\n\[Page |\Z)", body, re.S
        )
    ]


class MultiPaperTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        module_store.clear(USER_A)

    def seed_paper_rows(self, *paper_ids, owner=USER_A):
        """chat_store verifies ownership before writing current_paper_id, so
        a session pointer only sticks for a paper this owner really has."""
        from app.db.models import Paper

        with self.factory() as db:
            for index, paper_id in enumerate(paper_ids):
                db.add(
                    Paper(
                        id=uuid.UUID(paper_id),
                        owner_id=uuid.UUID(owner),
                        title=f"Paper {index}",
                        content_hash=f"{index}" * 64,
                        storage_path=f"{owner}/{paper_id}/original.pdf",
                        file_size_bytes=1024,
                        status="indexed",
                    )
                )
            db.commit()

    def run_ask(self, hits, answer="Grounded answer.", question="what is the research about?", params=None):
        """Drives the real endpoint with Voyage/Qdrant/Groq mocked and
        returns (supplied_passages, citations, prompt, qdrant_mock)."""
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

    def papers_in(self, citations):
        return [c["paper"] for c in citations]


class TestNewQuestionKeepsEveryPaper(MultiPaperTestCase):
    """The regression this change exists to prevent."""

    def test_a_new_question_keeps_chunks_from_both_papers(self):
        hits = [
            _hit("a1", 1, "Paper A page one about defect classification."),
            _hit("a2", 6, "Paper A page six future work.", chunk_id=2),
            _hit("b1", 1, "Paper B page one about anomaly detection.", paper="Paper B", paper_id=PAPER_B_ID),
        ]

        supplied, citations, _, _ = self.run_ask(hits)

        # Previously the third hit was discarded purely for belonging to a
        # different paper than the top hit.
        self.assertEqual(len(supplied), 3)
        self.assertEqual(sorted(set(self.papers_in(citations))), ["Paper A", "Paper B"])

    def test_a_lower_ranked_paper_survives_even_when_outnumbered(self):
        hits = [
            _hit("a1", 1, "A one"),
            _hit("a2", 2, "A two", chunk_id=1),
            _hit("a3", 3, "A three", chunk_id=2),
            _hit("b1", 1, "B one", paper="Paper B", paper_id=PAPER_B_ID),
        ]

        supplied, citations, _, _ = self.run_ask(hits)

        self.assertEqual(len(supplied), 4)
        self.assertIn("Paper B", self.papers_in(citations))

    def test_retrieval_order_is_preserved_across_papers(self):
        hits = [
            _hit("b1", 1, "B first", paper="Paper B", paper_id=PAPER_B_ID),
            _hit("a1", 1, "A second"),
            _hit("b2", 2, "B third", paper="Paper B", paper_id=PAPER_B_ID, chunk_id=1),
        ]

        _, citations, _, _ = self.run_ask(hits)

        self.assertEqual(self.papers_in(citations), ["Paper B", "Paper A", "Paper B"])

    def test_a_single_paper_library_is_unaffected(self):
        hits = [_hit("a1", 1, "only paper"), _hit("a2", 2, "same paper", chunk_id=1)]

        supplied, citations, _, _ = self.run_ask(hits)

        self.assertEqual(len(supplied), 2)
        self.assertEqual(set(self.papers_in(citations)), {"Paper A"})


class TestFollowUpLockUnchanged(MultiPaperTestCase):
    """The follow-up lock must behave exactly as before."""

    def seed_current_paper(self, paper_id):
        self.seed_paper_rows(PAPER_A_ID, PAPER_B_ID)
        set_current_paper(USER_A, uuid.UUID(paper_id))

    def prime_history(self):
        # is_followup requires prior history, so take one ordinary turn first.
        self.run_ask([_hit("seed", 1, "seed turn")], question="what is the research about?")

    def test_followup_is_still_locked_to_current_paper_id(self):
        self.prime_history()
        self.seed_current_paper(PAPER_A_ID)

        hits = [
            _hit("a1", 1, "A locked"),
            _hit("b1", 1, "B excluded", paper="Paper B", paper_id=PAPER_B_ID),
        ]
        # "it" makes this a follow-up per FOLLOW_UP_WORDS.
        supplied, citations, _, _ = self.run_ask(hits, question="what algorithm does it use?")

        self.assertEqual(len(supplied), 1)
        self.assertEqual(self.papers_in(citations), ["Paper A"])

    def test_followup_lock_falls_back_when_it_matches_nothing(self):
        self.prime_history()
        self.seed_current_paper(PAPER_A_ID)

        # Locked paper is absent from this turn's hits entirely.
        hits = [
            _hit("b1", 1, "B one", paper="Paper B", paper_id=PAPER_B_ID),
            _hit("b2", 2, "B two", paper="Paper B", paper_id=PAPER_B_ID, chunk_id=1),
        ]
        supplied, citations, _, _ = self.run_ask(hits, question="what dataset does it use?")

        # Unchanged `if locked:` guard — an empty lock keeps the full set
        # rather than returning nothing.
        self.assertEqual(len(supplied), 2)
        self.assertEqual(set(self.papers_in(citations)), {"Paper B"})

    def test_a_new_question_is_not_locked_even_when_a_pointer_exists(self):
        self.prime_history()
        self.seed_current_paper(PAPER_A_ID)

        hits = [
            _hit("a1", 1, "A one"),
            _hit("b1", 1, "B one", paper="Paper B", paper_id=PAPER_B_ID),
        ]
        # No follow-up word, so this is a new question.
        supplied, citations, _, _ = self.run_ask(
            hits, question="summarise every document in my library"
        )

        self.assertEqual(len(supplied), 2)
        self.assertEqual(sorted(set(self.papers_in(citations))), ["Paper A", "Paper B"])

    def test_current_paper_pointer_follows_the_top_ranked_chunk(self):
        # Documented consequence: after a multi-paper answer the pointer is
        # the paper of selected[0], not "the papers the answer used".
        self.seed_paper_rows(PAPER_A_ID, PAPER_B_ID)
        hits = [
            _hit("b1", 1, "B top", paper="Paper B", paper_id=PAPER_B_ID),
            _hit("a1", 1, "A second"),
        ]
        self.run_ask(hits)

        from app.db.models import ChatSession

        with self.factory() as db:
            session = db.query(ChatSession).one()
        self.assertEqual(session.current_paper_id, uuid.UUID(PAPER_B_ID))


class TestOwnerIsolationUnchanged(MultiPaperTestCase):
    def test_qdrant_filter_still_carries_the_verified_owner(self):
        hits = [_hit("a1", 1, "A"), _hit("b1", 1, "B", paper="Paper B", paper_id=PAPER_B_ID)]

        _, _, _, mock_qdrant = self.run_ask(hits)

        query_filter = mock_qdrant.query_points.call_args.kwargs["query_filter"]
        self.assertIsInstance(query_filter, Filter)
        matching = [
            c
            for c in (query_filter.must or [])
            if isinstance(c, FieldCondition)
            and c.key == "owner_id"
            and isinstance(c.match, MatchValue)
            and c.match.value == USER_A
        ]
        self.assertTrue(matching, f"no verified owner_id condition: {query_filter!r}")

    def test_a_client_supplied_owner_id_is_ignored(self):
        hits = [_hit("a1", 1, "A")]

        _, _, _, mock_qdrant = self.run_ask(
            hits, params={"owner_id": USER_B, "user_id": USER_B}
        )

        query_filter = mock_qdrant.query_points.call_args.kwargs["query_filter"]
        values = [
            c.match.value
            for c in (query_filter.must or [])
            if isinstance(c, FieldCondition) and c.key == "owner_id"
        ]
        self.assertEqual(values, [USER_A])
        self.assertNotIn(USER_B, values)


class TestEvidenceAndCitationInvariants(MultiPaperTestCase):
    """The 1:1 projection must still hold once evidence spans papers."""

    def test_citations_are_a_strict_one_to_one_projection(self):
        hits = [
            _hit("a1", 1, "A one"),
            _hit("b1", 4, "B four", paper="Paper B", paper_id=PAPER_B_ID),
            _hit("a2", 7, "A seven", chunk_id=1),
        ]

        supplied, citations, _, _ = self.run_ask(hits)

        self.assertEqual(len(supplied), len(citations))
        self.assertEqual([p for p, _ in supplied], [str(c["page"]) for c in citations])
        self.assertEqual([t for _, t in supplied], ["A one", "B four", "A seven"])
        self.assertEqual(self.papers_in(citations), ["Paper A", "Paper B", "Paper A"])

    def test_duplicate_point_ids_are_still_deduped_across_papers(self):
        # Same id, different papers: the dedupe key is the Qdrant point id.
        hits = [
            _hit("same-id", 1, "first"),
            _hit("same-id", 2, "second", paper="Paper B", paper_id=PAPER_B_ID),
            _hit("other-id", 3, "third", paper="Paper B", paper_id=PAPER_B_ID),
        ]

        supplied, citations, _, _ = self.run_ask(hits)

        self.assertEqual(len(supplied), 2)
        self.assertEqual([t for _, t in supplied], ["first", "third"])
        self.assertEqual(len(citations), 2)

    def test_context_budget_still_applies_with_multi_paper_evidence(self):
        big = "x" * (MAX_CONTEXT - 50)
        hits = [
            _hit("a1", 1, big),
            _hit("b1", 1, "y" * 500, paper="Paper B", paper_id=PAPER_B_ID),
        ]

        supplied, citations, prompt, _ = self.run_ask(hits)

        # The oversized-second chunk does not fit and appears in neither
        # the context nor the citations.
        self.assertEqual(len(supplied), 1)
        self.assertEqual(len(citations), 1)
        body = prompt.split("Current Retrieved Context:\n", 1)[1]
        body = body.split("\n\n----------------------------------------", 1)[0]
        self.assertLessEqual(len(body), MAX_CONTEXT)

    def test_a_refusal_emits_no_citations_even_with_two_papers(self):
        hits = [
            _hit("a1", 1, "A one"),
            _hit("b1", 1, "B one", paper="Paper B", paper_id=PAPER_B_ID),
        ]

        supplied, citations, _, _ = self.run_ask(hits, answer=NO_ANSWER_RESPONSE)

        self.assertEqual(len(supplied), 2)
        self.assertEqual(citations, [])

    def test_page_number_shared_across_papers_marks_both_cited(self):
        # ACCEPTED CAVEAT for this iteration: cited_pages is a set of page
        # NUMBERS, so "[Page 1]" marks page 1 of every selected paper as
        # cited. It affects only the cited flag, never which evidence is
        # disclosed. Pinned here so the behaviour is visible, not silent.
        hits = [
            _hit("a1", 1, "A one"),
            _hit("b1", 1, "B one", paper="Paper B", paper_id=PAPER_B_ID),
        ]

        _, citations, _, _ = self.run_ask(hits, answer="Both agree [Page 1].")

        self.assertEqual([c["cited"] for c in citations], [True, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
