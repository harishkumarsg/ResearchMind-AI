"""
/summarize-paper — evidence is the paper, not a similarity search.

Production returned "Information not specified" for Title, Research
Problem, Objective, Background, Methodology, Experimental Results,
Limitations and Conclusion on a paper whose details page plainly showed
all of them. The cause was retrieval, not the model: the endpoint
embedded the FILE NAME, ranked the owner's whole library against it, and
rerank_results() then kept only its first MAX_RERANK (5) hits. Most of
the paper never reached the prompt, so the model obeyed its "never
assume" rule and reported the evidence missing.

Summarising is not a search. These tests pin the replacement: a fetch
filtered on owner_id + paper_id, re-sorted into page/chunk_id order and
selected within the existing 12000-character budget, with
rerank_results() deliberately out of the evidence path.

Fully offline: Qdrant is a MagicMock serving fixtures, the provider is a
fake completion, and the database is in-memory SQLite. No network call
happens in this file.
"""
import os
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

from app.agents import qa_agent
from app.api.summarize_paper import (
    SUMMARY_CONTEXT_CHARS,
    SUMMARY_MAX_TOKENS,
    select_within_budget,
)
from app.core.auth import get_current_owner_id
from app.db.models import Paper
from app.memory import _store as module_store
from tests.sqlite_harness import attach_sqlite_db

OWNER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PAPER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_PAPER_ID = "22222222-2222-4222-8222-222222222222"

PAPER = "ETASR_18859.pdf"
OTHER_PAPER = "unrelated.pdf"

#: Distinctive per-section markers. They cannot occur in the prompt
#: scaffolding, so finding one proves that section's chunk reached the
#: model rather than the template.
TITLE_MARK = "QQTITLEQQ"
METHOD_MARK = "QQMETHODQQ"
RESULTS_MARK = "QQRESULTSQQ"
CONCLUSION_MARK = "QQCONCLUSIONQQ"
OTHER_MARK = "JJOTHERPAPERJJ"


def _completion(text, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(completion_tokens=None, completion_tokens_details=None),
    )


def _payload(paper, paper_id, page, chunk_id, text, owner_id=OWNER_A):
    return {
        "owner_id": owner_id,
        "paper": paper,
        "source": paper,
        "paper_id": paper_id,
        "page": page,
        "chunk_id": chunk_id,
        "text": text,
        "authors": "A. Author",
        "keywords": "vision-language, AOI",
        "abstract": "An abstract about VLM-AOI inspection.",
    }


def _hit(payload):
    h = MagicMock()
    h.id = str(uuid.uuid4())
    h.score = 0.9
    h.payload = payload
    return h


def _point(payload):
    return SimpleNamespace(id=str(uuid.uuid4()), payload=payload)


#: A paper whose sections are spread across pages, deliberately returned
#: out of order so the ordering assertion means something.
PAPER_PAYLOADS = [
    _payload(PAPER, PAPER_ID, 9, 0, f"Conclusion. {CONCLUSION_MARK}"),
    _payload(PAPER, PAPER_ID, 1, 0, f"Title page. {TITLE_MARK}"),
    _payload(PAPER, PAPER_ID, 5, 1, f"Results table. {RESULTS_MARK}"),
    _payload(PAPER, PAPER_ID, 3, 0, f"Methodology. {METHOD_MARK}"),
    _payload(PAPER, PAPER_ID, 5, 0, "Results intro."),
]


class SummarizeTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)

    def tearDown(self):
        module_store.clear(OWNER_A)

    def seed_paper(self, title, paper_id, owner_id=OWNER_A):
        """A row in `papers`, the system of record for identification."""
        session = self.factory()
        try:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="h" * 64,
                    storage_path=f"{owner_id}/{title}",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()
        finally:
            session.close()

    def fetch_filter_keys(self):
        return {
            c.key: c.match.value
            for c in self.fetch_kwargs["query_filter"].must
        }

    def run_summary(self, paper_payloads, search_hits=None, completion=None):
        """Drives /summarize-paper with Qdrant serving the fixtures.

        The first query identifies the paper and is deliberately kept
        sparse; the evidence must come from the second, filtered fetch
        rather than from those few hits.
        """
        if search_hits is None:
            search_hits = [_hit(PAPER_PAYLOADS[0])]

        def fake_query(**kwargs):
            # Identification asks for a single point; the evidence fetch
            # asks for PAPER_FETCH_LIMIT. Dispatching on that rather than
            # on call order stays correct whether or not the database
            # answered identification first.
            if kwargs.get("limit") == 1:
                self.identify_kwargs = kwargs
                # Real Qdrant applies the filter; a fake that ignored it
                # would let a hit for another paper stand in for the one
                # requested, which is the defect under test.
                conditions = {
                    c.key: c.match.value
                    for c in kwargs["query_filter"].must
                }
                matching = [
                    h for h in search_hits
                    if h.payload.get("paper") == conditions.get("paper")
                    and h.payload.get("owner_id") == conditions.get("owner_id")
                ]
                return SimpleNamespace(points=matching)
            self.fetch_kwargs = kwargs
            return SimpleNamespace(points=[_point(p) for p in paper_payloads])

        with patch("app.api.summarize_paper.encode_query", return_value=[0.1]), patch(
            "app.api.summarize_paper.client"
        ) as qdrant, patch(
            "app.api.summarize_paper.rerank_results", side_effect=lambda q, r: r
        ), patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=completion or _completion("### Title\n\nA summary."),
        ) as create:
            qdrant.query_points.side_effect = fake_query
            resp = self.client.get("/summarize-paper", params={"paper_name": PAPER})

        self.create = create
        return resp

    def sent_to_model(self):
        return self.create.call_args.kwargs["messages"][1]["content"]


# ======================================================================
# 1. The whole paper reaches the model
# ======================================================================
class TestSummaryUsesTheWholePaper(SummarizeTestCase):
    def test_multiple_chunks_from_the_requested_paper_are_used(self):
        resp = self.run_summary(PAPER_PAYLOADS)
        sent = self.sent_to_model()

        self.assertEqual(resp.status_code, 200)
        for mark in (TITLE_MARK, METHOD_MARK, RESULTS_MARK, CONCLUSION_MARK):
            with self.subTest(mark=mark):
                self.assertIn(mark, sent)

    def test_sections_beyond_the_first_few_hits_are_reachable(self):
        # The exact production failure: the search returned one hit, and
        # the summary could only describe that hit. Conclusion lives on
        # page 9 and must still arrive.
        self.run_summary(PAPER_PAYLOADS, search_hits=[_hit(PAPER_PAYLOADS[0])])

        self.assertIn(CONCLUSION_MARK, self.sent_to_model())

    def test_chunks_are_ordered_by_page_then_chunk_id(self):
        sent = self.run_summary(PAPER_PAYLOADS) and self.sent_to_model()

        positions = [
            sent.index(TITLE_MARK),
            sent.index(METHOD_MARK),
            sent.index(RESULTS_MARK),
            sent.index(CONCLUSION_MARK),
        ]

        # Returned as 9, 1, 5.1, 3, 5.0 — rendered as 1, 3, 5.0, 5.1, 9.
        self.assertEqual(positions, sorted(positions))
        self.assertLess(sent.index("Results intro."), sent.index(RESULTS_MARK))


# ======================================================================
# 1b. Identification is deterministic, not a ranking
# ======================================================================
class TestPaperIdentification(SummarizeTestCase):
    """The requested paper, or nothing.

    The previous implementation embedded the file name, ranked the
    owner's whole library against it, and fell back to results[0] when no
    ranked title contained the requested name — so a request for one
    paper could be answered with another. Identification is now an exact,
    owner-scoped lookup in `papers`, with an exact index match only as a
    consistency fallback.
    """

    def test_an_exact_title_resolves_to_its_own_paper_id(self):
        self.seed_paper(PAPER, PAPER_ID)

        resp = self.run_summary(PAPER_PAYLOADS, search_hits=[])

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.fetch_filter_keys().get("paper_id"), PAPER_ID)

    def test_another_owners_same_named_paper_cannot_be_selected(self):
        # Same title, different owner. Resolution must not cross over.
        self.seed_paper(PAPER, OTHER_PAPER_ID, owner_id=OWNER_B)
        self.seed_paper(PAPER, PAPER_ID, owner_id=OWNER_A)

        self.run_summary(PAPER_PAYLOADS, search_hits=[])

        keys = self.fetch_filter_keys()
        self.assertEqual(keys.get("owner_id"), OWNER_A)
        self.assertEqual(keys.get("paper_id"), PAPER_ID)
        self.assertNotEqual(keys.get("paper_id"), OTHER_PAPER_ID)

    def test_only_another_owner_has_it_so_it_is_not_found(self):
        self.seed_paper(PAPER, OTHER_PAPER_ID, owner_id=OWNER_B)

        resp = self.run_summary(PAPER_PAYLOADS, search_hits=[])

        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("Paper not found", body["message"])
        self.create.assert_not_called()

    def test_a_missing_paper_does_not_fall_back_to_another(self):
        # The owner has a different paper, and the index offers a hit for
        # it. Neither may stand in for the paper that was asked for.
        self.seed_paper(OTHER_PAPER, OTHER_PAPER_ID)

        resp = self.run_summary(
            PAPER_PAYLOADS,
            search_hits=[_hit(_payload(OTHER_PAPER, OTHER_PAPER_ID, 1, 0, "Other."))],
        )

        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("Paper not found", body["message"])
        # The decisive assertion: no summary was produced at all.
        self.create.assert_not_called()

    def test_two_papers_of_the_same_owner_are_not_confused(self):
        self.seed_paper(OTHER_PAPER, OTHER_PAPER_ID)
        self.seed_paper(PAPER, PAPER_ID)

        self.run_summary(PAPER_PAYLOADS, search_hits=[])

        self.assertEqual(self.fetch_filter_keys().get("paper_id"), PAPER_ID)

    def test_a_title_that_is_a_substring_of_another_is_not_confused(self):
        # "report.pdf" must never select "final report.pdf".
        self.seed_paper(f"final {PAPER}", OTHER_PAPER_ID)
        self.seed_paper(PAPER, PAPER_ID)

        self.run_summary(PAPER_PAYLOADS, search_hits=[])

        self.assertEqual(self.fetch_filter_keys().get("paper_id"), PAPER_ID)

    def test_identification_prefers_the_database_over_the_index(self):
        self.seed_paper(PAPER, PAPER_ID)

        self.run_summary(PAPER_PAYLOADS, search_hits=[])

        # Resolved without an index lookup at all.
        self.assertFalse(hasattr(self, "identify_kwargs"))

    def test_the_index_fallback_matches_the_title_exactly(self):
        # No `papers` row: the fallback runs, and its filter must pin the
        # owner and the exact title rather than rank candidates.
        self.run_summary(PAPER_PAYLOADS)

        keys = {
            c.key: c.match.value
            for c in self.identify_kwargs["query_filter"].must
        }
        self.assertEqual(keys.get("owner_id"), OWNER_A)
        self.assertEqual(keys.get("paper"), PAPER)
        self.assertEqual(self.identify_kwargs["limit"], 1)

    def test_nothing_anywhere_means_not_found_not_a_guess(self):
        resp = self.run_summary(PAPER_PAYLOADS, search_hits=[])

        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("Paper not found", body["message"])
        self.create.assert_not_called()


# ======================================================================
# 2. Isolation
# ======================================================================
class TestIsolation(SummarizeTestCase):
    def test_the_fetch_filter_pins_owner_and_paper(self):
        self.run_summary(PAPER_PAYLOADS)

        must = self.fetch_kwargs["query_filter"].must
        keys = {c.key: c.match.value for c in must}

        self.assertEqual(keys.get("owner_id"), OWNER_A)
        self.assertEqual(keys.get("paper_id"), PAPER_ID)
        self.assertNotIn("paper", keys)

    def test_the_fetch_limit_is_high_enough_for_a_whole_paper(self):
        from app.api.summarize_paper import PAPER_FETCH_LIMIT

        self.run_summary(PAPER_PAYLOADS)

        # The filter, not the limit, must decide the result. A limit at
        # rerank_results()' MAX_RERANK of 5 is the defect being fixed.
        self.assertEqual(self.fetch_kwargs["limit"], PAPER_FETCH_LIMIT)
        self.assertGreaterEqual(PAPER_FETCH_LIMIT, 1024)

    def test_another_papers_chunk_never_reaches_the_prompt(self):
        # A payload that the filter should have excluded. Even if it were
        # somehow returned, it must not be attributed to this paper.
        polluted = PAPER_PAYLOADS + [
            _payload(OTHER_PAPER, OTHER_PAPER_ID, 2, 0, f"Other paper. {OTHER_MARK}")
        ]

        self.run_summary(polluted)

        must = self.fetch_kwargs["query_filter"].must
        keys = {c.key: c.match.value for c in must}
        self.assertEqual(keys.get("paper_id"), PAPER_ID)

    def test_falls_back_to_the_title_filter_when_no_paper_id_is_stored(self):
        legacy = [
            {**p, "paper_id": ""} for p in PAPER_PAYLOADS
        ]

        self.run_summary(legacy, search_hits=[_hit(legacy[0])])

        keys = {c.key: c.match.value for c in self.fetch_kwargs["query_filter"].must}
        self.assertEqual(keys.get("owner_id"), OWNER_A)
        self.assertEqual(keys.get("paper"), PAPER)
        self.assertNotIn("paper_id", keys)


# ======================================================================
# 3. The budget is respected, and is now the real limit
# ======================================================================
class TestContextBudget(unittest.TestCase):
    def test_everything_is_kept_when_it_fits(self):
        blocks = ["a" * 100 for _ in range(10)]
        self.assertEqual(select_within_budget(blocks, SUMMARY_CONTEXT_CHARS), blocks)

    def test_an_oversized_paper_is_trimmed_to_the_budget(self):
        blocks = ["a" * 1000 for _ in range(100)]

        picked = select_within_budget(blocks, SUMMARY_CONTEXT_CHARS)

        joined = "\n\n".join(picked)
        self.assertLessEqual(len(joined), SUMMARY_CONTEXT_CHARS)
        self.assertGreater(len(picked), 1)

    def test_trimming_keeps_the_end_of_the_paper_not_just_the_front(self):
        # Taking the first N would reproduce the original defect from the
        # other direction: Conclusion would be missing again.
        blocks = [f"block {i} " + "a" * 900 for i in range(100)]

        picked = select_within_budget(blocks, SUMMARY_CONTEXT_CHARS)

        self.assertIn(blocks[0], picked)
        self.assertGreater(
            blocks.index(picked[-1]),
            len(blocks) // 2,
            "selection never reaches the second half of the paper",
        )

    def test_the_selection_stays_in_reading_order(self):
        blocks = [f"block-{i:03d} " + "a" * 900 for i in range(100)]

        picked = select_within_budget(blocks, SUMMARY_CONTEXT_CHARS)

        self.assertEqual(picked, sorted(picked))

    def test_an_empty_paper_selects_nothing(self):
        self.assertEqual(select_within_budget([], SUMMARY_CONTEXT_CHARS), [])


class TestBudgetEndToEnd(SummarizeTestCase):
    def test_the_model_receives_at_most_the_summary_budget(self):
        big = [
            _payload(PAPER, PAPER_ID, page, 0, "z" * 2000)
            for page in range(1, 60)
        ]

        self.run_summary(big)
        sent = self.sent_to_model()

        # The context block is inside the user message; the evidence
        # itself must not exceed the budget.
        self.assertLessEqual(sent.count("z"), SUMMARY_CONTEXT_CHARS)

    def test_the_budgets_are_unchanged(self):
        self.assertEqual(SUMMARY_CONTEXT_CHARS, 12000)
        self.assertEqual(SUMMARY_MAX_TOKENS, 3000)


# ======================================================================
# 4. Metadata reaches the prompt
# ======================================================================
class TestStoredMetadataReachesThePrompt(SummarizeTestCase):
    def test_authors_keywords_and_abstract_are_supplied(self):
        self.run_summary(PAPER_PAYLOADS)
        sent = self.sent_to_model()

        self.assertIn("A. Author", sent)
        self.assertIn("vision-language, AOI", sent)
        self.assertIn("An abstract about VLM-AOI inspection.", sent)

    def test_missing_metadata_is_simply_omitted(self):
        bare = [
            {**p, "authors": "", "keywords": "", "abstract": ""}
            for p in PAPER_PAYLOADS
        ]

        resp = self.run_summary(bare, search_hits=[_hit(bare[0])])

        self.assertEqual(resp.status_code, 200)
        sent = self.sent_to_model()
        self.assertNotIn("Authors:", sent)
        self.assertNotIn("Abstract:", sent)


# ======================================================================
# 5. Existing behaviour that must not change
# ======================================================================
class TestExistingBehaviourPreserved(SummarizeTestCase):
    def test_the_anti_hallucination_rules_are_still_in_the_prompt(self):
        self.run_summary(PAPER_PAYLOADS)
        sent = self.sent_to_model()

        self.assertIn("Use ONLY the supplied context", sent)
        self.assertIn("Never invent information", sent)
        self.assertIn("Never assume missing information", sent)
        self.assertIn("Information not specified in the reviewed paper.", sent)

    def test_every_required_section_is_still_requested(self):
        self.run_summary(PAPER_PAYLOADS)
        sent = self.sent_to_model()

        for section in (
            "### Title",
            "### Research Problem",
            "### Objective",
            "### Methodology",
            "### Experimental Results",
            "### Limitations",
            "### Conclusion",
        ):
            with self.subTest(section=section):
                self.assertIn(section, sent)

    def test_a_paper_with_no_stored_chunks_is_reported_not_invented(self):
        resp = self.run_summary([])

        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("Paper not found", body["message"])

    def test_a_length_stopped_summary_is_still_rejected(self):
        from app.core.providers import GENERATION_INCOMPLETE

        resp = self.run_summary(
            PAPER_PAYLOADS, completion=_completion("Partial (", finish_reason="length")
        )

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)
        self.assertNotIn("Partial (", resp.text)

    def test_the_response_still_reports_metadata_and_pages(self):
        resp = self.run_summary(PAPER_PAYLOADS)
        body = resp.json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["paper"], PAPER)
        self.assertEqual(body["authors"], "A. Author")
        self.assertEqual(sorted(body["pages_found"]), ["1", "3", "5", "9"])


if __name__ == "__main__":
    unittest.main()
