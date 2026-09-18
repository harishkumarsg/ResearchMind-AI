"""
Phase B — caller-owned context and generation budgets.

Phase A made the shared primitive budgetable but deliberately left every
caller on the old 4000-character default, so Report, Compare and
Summarize still lost most of the context they had just built. Phase B
gives each caller its own budget and its own completion size.

The load-bearing test here is
TestCompareReachesBothPapers.test_paper_two_evidence_reaches_the_model.
It was written before the fix and failed against the Phase A tree, where
the 4000-character cut landed inside Paper 1 and Paper 2 contributed
literally nothing to the comparison.

Nothing here reaches a real provider, a real vector store or a real
database: completions are fake objects, Qdrant is a MagicMock and
persistence runs on in-memory SQLite.
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
from app.core.auth import get_current_owner_id
from app.memory import _store as module_store
from tests.sqlite_harness import attach_sqlite_db

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PAPER_ID = "11111111-1111-4111-8111-111111111111"

PAPER_1 = "alpha.pdf"
PAPER_2 = "beta.pdf"

#: Markers must not occur in the prompt scaffolding, the banners or each
#: other, or an assertion could pass on the template rather than on the
#: evidence. Doubled uncommon letters cannot appear in the templates.
P1_MARK = "QQPAPERONEQQ"
P2_MARK = "JJPAPERTWOJJ"

#: Filler for the two papers, distinct so each paper's share of the final
#: context can be measured by counting characters.
P1_FILL = "W"
P2_FILL = "Y"


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


def _hit(paper, text, page=1, chunk_id=0):
    h = MagicMock()
    h.id = str(uuid.uuid4())
    h.score = 0.9
    h.payload = {
        "paper": paper,
        "source": paper,
        "paper_id": PAPER_ID,
        "page": page,
        "chunk_id": chunk_id,
        "text": text,
        "authors": "A. Author",
        "keywords": "k",
        "abstract": "abstract",
    }
    return h


# ======================================================================
# 1. Compare reaches both papers
# ======================================================================
class CompareTestCase(unittest.TestCase):
    """Drives /compare-papers with both papers present in the index."""

    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)
        self.captured = {}

    def tearDown(self):
        module_store.clear(USER_A)

    def run_compare(self, p1_text, p2_text, completion=None):
        """Compare two papers and capture what the caller handed the
        primitive, without stubbing the primitive out: the spy delegates,
        so the context the model receives is still produced by the real
        truncation path."""
        import app.api.compare_papers as compare_mod

        real = compare_mod.generate_answer

        def spy(prompt, context, **kwargs):
            self.captured = {"prompt": prompt, "context": context, "kwargs": kwargs}
            return real(prompt, context, **kwargs)

        hits = [_hit(PAPER_1, p1_text), _hit(PAPER_2, p2_text)]

        with patch("app.api.compare_papers.encode_query", return_value=[0.1]), patch(
            "app.api.compare_papers.client"
        ) as qdrant, patch(
            "app.api.compare_papers.rerank_results", side_effect=lambda q, r: r
        ), patch(
            "app.api.compare_papers.generate_answer", side_effect=spy
        ), patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=completion or _completion("A comparison."),
        ) as create:
            qdrant.query_points.return_value.points = hits
            resp = self.client.get(
                "/compare-papers", params={"paper1": PAPER_1, "paper2": PAPER_2}
            )

        self.create = create
        return resp

    def sent_to_model(self):
        """The user message the model actually received."""
        return self.create.call_args.kwargs["messages"][1]["content"]


class TestCompareReachesBothPapers(CompareTestCase):
    def test_paper_one_evidence_reaches_the_model(self):
        self.run_compare(P1_MARK + P1_FILL * 6000, P2_MARK + P2_FILL * 500)
        self.assertIn(P1_MARK, self.sent_to_model())

    def test_paper_two_evidence_reaches_the_model(self):
        # THE Phase B regression test. Paper 1 alone exceeds the old
        # 4000-character cut, so on the Phase A tree the cut lands inside
        # Paper 1 and this marker never reaches the model at all.
        self.run_compare(P1_MARK + P1_FILL * 6000, P2_MARK + P2_FILL * 500)
        self.assertIn(
            P2_MARK,
            self.sent_to_model(),
            "Paper 2 contributed no evidence: the comparison saw only Paper 1",
        )

    def test_an_oversized_paper_one_cannot_starve_paper_two(self):
        # Both papers oversized. Paper 2 must still receive a meaningful
        # share rather than whatever happens to be left over.
        self.run_compare(P1_MARK + P1_FILL * 20000, P2_MARK + P2_FILL * 20000)
        sent = self.sent_to_model()

        self.assertIn(P2_MARK, sent)
        self.assertGreater(
            sent.count(P2_FILL),
            7000,
            "Paper 2 was trimmed far below its guaranteed floor",
        )

    def test_the_assembled_context_stays_within_the_total_budget(self):
        from app.api.compare_papers import COMPARE_TOTAL_CONTEXT_CHARS

        self.run_compare(P1_MARK + P1_FILL * 20000, P2_MARK + P2_FILL * 20000)

        # Measured on the assembled string, banners included: budgeting the
        # two papers to half each and then adding banners is exactly the
        # overflow that re-arms the tail cut on Paper 2.
        self.assertLessEqual(len(self.captured["context"]), COMPARE_TOTAL_CONTEXT_CHARS)

    def test_both_paper_names_remain_in_the_prompt(self):
        self.run_compare(P1_MARK + P1_FILL * 6000, P2_MARK + P2_FILL * 500)
        prompt = self.captured["prompt"]

        self.assertIn(PAPER_1, prompt)
        self.assertIn(PAPER_2, prompt)

    def test_the_caller_passes_its_own_budgets(self):
        from app.api.compare_papers import (
            COMPARE_MAX_TOKENS,
            COMPARE_TOTAL_CONTEXT_CHARS,
        )

        self.run_compare(P1_MARK, P2_MARK)

        self.assertEqual(
            self.captured["kwargs"]["max_context_chars"], COMPARE_TOTAL_CONTEXT_CHARS
        )
        self.assertEqual(self.captured["kwargs"]["max_tokens"], COMPARE_MAX_TOKENS)
        self.assertEqual(COMPARE_TOTAL_CONTEXT_CHARS, 16000)
        self.assertEqual(COMPARE_MAX_TOKENS, 6000)

    def test_a_length_stopped_comparison_is_still_rejected(self):
        from app.core.providers import GENERATION_INCOMPLETE

        resp = self.run_compare(
            P1_MARK, P2_MARK, completion=_completion("| Category |", finish_reason="length")
        )

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)
        self.assertNotIn("could not find", resp.text)


# ======================================================================
# 2. The allocator on its own
# ======================================================================
class TestCompareAllocation(unittest.TestCase):
    def test_two_short_papers_are_left_untouched(self):
        from app.api.compare_papers import allocate_paper_budgets

        self.assertEqual(allocate_paper_budgets(10, 20), (10, 20))

    def test_two_oversized_papers_split_evenly(self):
        from app.api.compare_papers import allocate_paper_budgets

        b1, b2 = allocate_paper_budgets(999999, 999999)
        self.assertEqual(b1, b2)

    def test_a_short_paper_gives_its_slack_to_the_other(self):
        from app.api.compare_papers import allocate_paper_budgets

        b1, b2 = allocate_paper_budgets(100, 999999)
        _, half = allocate_paper_budgets(999999, 999999)

        self.assertEqual(b1, 100)
        # Paper 2 gets more than a bare half because Paper 1 did not use
        # its share: slack may be given, never taken.
        self.assertGreater(b2, half)

    def test_assembly_order_confers_no_advantage(self):
        from app.api.compare_papers import allocate_paper_budgets

        forward = allocate_paper_budgets(999999, 100)
        backward = allocate_paper_budgets(100, 999999)

        self.assertEqual(forward, tuple(reversed(backward)))

    def test_the_envelope_is_accounted_for_inside_the_budget(self):
        from app.api.compare_papers import (
            COMPARE_TOTAL_CONTEXT_CHARS,
            _CONTEXT_TEMPLATE,
            allocate_paper_budgets,
        )

        b1, b2 = allocate_paper_budgets(999999, 999999)
        assembled = _CONTEXT_TEMPLATE.format(paper1="x" * b1, paper2="y" * b2)

        self.assertLessEqual(len(assembled), COMPARE_TOTAL_CONTEXT_CHARS)


# ======================================================================
# 3. Report
# ======================================================================
class ResearchTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)
        self.captured = {}

    def tearDown(self):
        module_store.clear(USER_A)

    def count(self, model):
        with self.factory() as db:
            return db.query(model).count()

    def run_research(self, completion, hits=None):
        real = None
        import app.agents.research_agent as agent_mod

        real = agent_mod.generate_answer

        def spy(prompt, context, **kwargs):
            self.captured = {"prompt": prompt, "context": context, "kwargs": kwargs}
            return real(prompt, context, **kwargs)

        with patch("app.api.research.encode_query", return_value=[0.1]), patch(
            "app.api.research.client"
        ) as qdrant, patch(
            "app.api.research.rerank_results", side_effect=lambda q, r: r
        ), patch(
            "app.agents.research_agent.generate_answer", side_effect=spy
        ), patch.object(
            qa_agent._groq_client.chat.completions, "create", return_value=completion
        ) as create:
            qdrant.query_points.return_value.points = hits or [
                _hit(PAPER_1, "passage text")
            ]
            resp = self.client.get("/research", params={"query": "vision models"})

        self.create = create
        return resp


class TestReportBudgets(ResearchTestCase):
    def test_a_complete_report_is_generated_and_saved(self):
        from app.db.models import Report

        resp = self.run_research(_completion("### Executive Summary Complete report."))

        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(self.count(Report), 1)

    def test_a_length_stopped_report_is_still_rejected(self):
        from app.core.providers import GENERATION_INCOMPLETE
        from app.db.models import Report

        resp = self.run_research(
            _completion(
                "### Executive Summary The paper introduces (", finish_reason="length"
            )
        )

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)
        self.assertEqual(self.count(Report), 0)
        self.assertNotIn("Executive Summary", resp.text)

    def test_the_caller_passes_its_own_budgets(self):
        from app.api.research import REPORT_CONTEXT_CHARS, REPORT_MAX_TOKENS

        self.run_research(_completion("Report."))

        self.assertEqual(
            self.captured["kwargs"]["max_context_chars"], REPORT_CONTEXT_CHARS
        )
        self.assertEqual(self.captured["kwargs"]["max_tokens"], REPORT_MAX_TOKENS)
        self.assertEqual(REPORT_CONTEXT_CHARS, 12000)
        self.assertEqual(REPORT_MAX_TOKENS, 5000)

    def test_the_context_budget_no_longer_cuts_at_four_thousand(self):
        # The Phase A behaviour: 12000 characters of assembled context
        # arrived at the model as 4000.
        big = _hit(PAPER_1, P1_MARK + P1_FILL * 9000)
        self.run_research(_completion("Report."), hits=[big])

        sent = self.create.call_args.kwargs["messages"][1]["content"]
        self.assertGreater(sent.count(P1_FILL), 4000)

    def test_the_model_receives_at_most_the_report_budget(self):
        from app.api.research import REPORT_CONTEXT_CHARS

        big = _hit(PAPER_1, P1_FILL * 40000)
        self.run_research(_completion("Report."), hits=[big])

        self.assertLessEqual(len(self.captured["context"]), REPORT_CONTEXT_CHARS)


class TestReportCitations(ResearchTestCase):
    def test_citations_are_returned_for_a_complete_report(self):
        resp = self.run_research(_completion("Report."))
        citations = resp.json()["citations"]

        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["paper"], PAPER_1)

    def test_citations_can_still_outrun_the_context_budget(self):
        """PRE-EXISTING LIMITATION, pinned rather than fixed.

        Citations are collected per chunk as the context is assembled, but
        the assembled string is then cut to MAX_CONTEXT_LENGTH. A chunk
        past that boundary therefore contributes a citation while its text
        never reaches the model. Phase B narrows the gap -- the second,
        4000-character cut is gone -- but does not close it, and changing
        citation semantics is out of scope here.
        """
        hits = [
            _hit(PAPER_1, P1_FILL * 11000, page=1, chunk_id=0),
            _hit("gamma.pdf", P2_FILL * 5000, page=2, chunk_id=1),
        ]
        resp = self.run_research(_completion("Report."), hits=hits)

        papers = {c["paper"] for c in resp.json()["citations"]}
        self.assertIn("gamma.pdf", papers)
        # ...while the second paper's text was cut from the context.
        self.assertLess(self.captured["context"].count(P2_FILL), 5000)


# ======================================================================
# 4. Summarize
# ======================================================================
class SummarizeTestCase(unittest.TestCase):
    def setUp(self):
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: USER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.factory = attach_sqlite_db(self)
        self.captured = {}

    def tearDown(self):
        module_store.clear(USER_A)

    def run_summarize(self, completion, text="passage text"):
        import app.api.summarize_paper as sum_mod

        real = sum_mod.generate_answer

        def spy(prompt, context, **kwargs):
            self.captured = {"prompt": prompt, "context": context, "kwargs": kwargs}
            return real(prompt, context, **kwargs)

        with patch("app.api.summarize_paper.encode_query", return_value=[0.1]), patch(
            "app.api.summarize_paper.client"
        ) as qdrant, patch(
            "app.api.summarize_paper.rerank_results", side_effect=lambda q, r: r
        ), patch(
            "app.api.summarize_paper.generate_answer", side_effect=spy
        ), patch.object(
            qa_agent._groq_client.chat.completions, "create", return_value=completion
        ) as create:
            qdrant.query_points.return_value.points = [_hit(PAPER_1, text)]
            resp = self.client.get("/summarize-paper", params={"paper_name": PAPER_1})

        self.create = create
        return resp


class TestSummarizeBudgets(SummarizeTestCase):
    def test_a_complete_summary_is_generated(self):
        resp = self.run_summarize(_completion("A complete summary."))
        self.assertEqual(resp.json()["status"], "success")

    def test_a_length_stopped_summary_is_still_rejected(self):
        from app.core.providers import GENERATION_INCOMPLETE

        resp = self.run_summarize(
            _completion("Partial summary (", finish_reason="length")
        )

        self.assertEqual(resp.json()["code"], GENERATION_INCOMPLETE)
        self.assertNotIn("Partial summary", resp.text)
        self.assertNotIn("could not find", resp.text)

    def test_the_caller_passes_its_own_budgets(self):
        from app.api.summarize_paper import SUMMARY_CONTEXT_CHARS, SUMMARY_MAX_TOKENS

        self.run_summarize(_completion("Summary."))

        self.assertEqual(
            self.captured["kwargs"]["max_context_chars"], SUMMARY_CONTEXT_CHARS
        )
        self.assertEqual(self.captured["kwargs"]["max_tokens"], SUMMARY_MAX_TOKENS)
        self.assertEqual(SUMMARY_CONTEXT_CHARS, 12000)
        self.assertEqual(SUMMARY_MAX_TOKENS, 3000)

    def test_the_context_budget_no_longer_cuts_at_four_thousand(self):
        self.run_summarize(_completion("Summary."), text=P1_MARK + P1_FILL * 9000)

        sent = self.create.call_args.kwargs["messages"][1]["content"]
        self.assertGreater(sent.count(P1_FILL), 4000)


# ======================================================================
# 5. What Phase B must NOT move
# ======================================================================
class TestSharedDefaultsUnchanged(unittest.TestCase):
    """Phase B gives callers budgets; it must not move the defaults Phase A
    pinned, or every untouched caller changes behaviour silently."""

    def test_the_context_default_is_still_four_thousand(self):
        from app.agents.qa_agent import DEFAULT_MAX_CONTEXT_CHARS

        self.assertEqual(DEFAULT_MAX_CONTEXT_CHARS, 4000)

    def test_the_completion_default_is_still_five_hundred_and_twelve(self):
        from app.agents.qa_agent import DEFAULT_MAX_TOKENS

        self.assertEqual(DEFAULT_MAX_TOKENS, 512)

    def test_a_caller_that_states_nothing_still_gets_the_old_ceiling(self):
        from app.agents.qa_agent import generate

        with patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=_completion("answer"),
        ) as create:
            generate("q", "context")

        self.assertEqual(create.call_args.kwargs["max_tokens"], 512)

    def test_a_caller_budget_is_forwarded(self):
        from app.agents.qa_agent import generate_answer

        with patch.object(
            qa_agent._groq_client.chat.completions,
            "create",
            return_value=_completion("answer"),
        ) as create:
            generate_answer("q", "context", max_tokens=4242)

        self.assertEqual(create.call_args.kwargs["max_tokens"], 4242)


class TestAskUnchangedByPhaseB(unittest.TestCase):
    def test_ask_keeps_its_own_four_thousand_character_budget(self):
        from app.api.ask_stream import MAX_CONTEXT

        self.assertEqual(MAX_CONTEXT, 4000)

    def test_ask_still_does_not_use_the_shared_primitive(self):
        from app.api import ask_stream

        self.assertFalse(hasattr(ask_stream, "generate_answer"))
        self.assertFalse(hasattr(ask_stream, "generate"))


if __name__ == "__main__":
    unittest.main()
