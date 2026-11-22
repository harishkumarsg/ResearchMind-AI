"""
Search results and AI answers use only vectors of live papers.

Qdrant can hold vectors whose paper is not usable — an index run whose
claim was taken over and whose cleanup failed, a deletion that stopped
partway, a paper still uploading or indexing, one that failed — and
retrieval filtered on owner_id alone. Now every hit is checked against
Postgres (app/services/retrieval_guard.py): it is used only if its
paper_id names one of the verified owner's papers with status 'indexed'.

Each test drives the real /search or /ask-stream route against a SQLite
database, with Voyage, Qdrant, the reranker and Groq replaced. The
Qdrant double returns whatever hits a test gives it, as if they had all
passed the owner filter — exactly the case the database check exists for.
"""
import json
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.api.paper_intelligence import NOT_INDEXED_MESSAGE  # noqa: E402
from app.api.paper_relationship import MISSING_INTELLIGENCE_CODE, NOT_GENERATED_CODE  # noqa: E402
from app.core.auth import get_current_owner_id  # noqa: E402
from app.core.providers import internal_error_payload  # noqa: E402
from app.db.models import Paper, Report  # noqa: E402
from app.memory import _store as module_store  # noqa: E402
from app.services import retrieval_guard  # noqa: E402
from app.services.storage import build_storage_path  # noqa: E402
from tests.sqlite_harness import attach_sqlite_db  # noqa: E402

OWNER_A = "aaaaaaaa-7777-4777-8777-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-7777-4777-8777-bbbbbbbbbbbb"

NOT_LIVE = ("uploading", "indexing", "failed", "deleting")
# What a database driver can put in its message; none of it may reach a client.
DATABASE_ERROR_TEXT = "connection to server at db.internal.example (10.0.0.5), port 5432 failed: password authentication failed for user researchmind_app"


def _hit(paper_id, title, text, page=1, point=None):
    return SimpleNamespace(
        id=point or str(uuid.uuid4()),
        score=0.5,
        payload={
            "owner_id": OWNER_A,
            "paper_id": paper_id,
            "paper": title,
            "source": f"{title}.pdf",
            "page": page,
            "chunk_id": 0,
            "text": text,
        },
    )


class RetrievalTestCase(unittest.TestCase):

    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self.papers = {}  # label -> paper id
        self.papers["live"] = self.seed(OWNER_A, "indexed", "Live paper")
        self.papers["live_two"] = self.seed(OWNER_A, "indexed", "Second live paper")
        for status in NOT_LIVE:
            self.papers[status] = self.seed(OWNER_A, status, f"{status.title()} paper")
        self.papers["other_owner"] = self.seed(OWNER_B, "indexed", "Someone else's paper")
        self.papers["missing"] = str(uuid.uuid4())  # vectors with no row at all

        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(module_store.clear, OWNER_A)
        self.client = TestClient(app, raise_server_exceptions=False)

    def seed(self, owner_id, status, title):
        paper_id = str(uuid.uuid4())
        with self.factory() as db:
            db.add(Paper(
                id=uuid.UUID(paper_id), owner_id=uuid.UUID(owner_id), title=title,
                content_hash=uuid.uuid4().hex * 2, storage_path=build_storage_path(owner_id, paper_id),
                file_size_bytes=1024, status=status,
            ))
            db.commit()
        return paper_id

    def hit(self, label, page=1):
        return _hit(self.papers[label], f"{label} title", f"text of {label}, page {page}", page=page)

    def every_kind_of_hit(self):
        """One hit per paper state, the live ones placed between others."""
        order = ("missing", "live", "deleting", "uploading", "other_owner", "live_two", "indexing", "failed")
        return [self.hit(label) for label in order]

    def count_paper_lookups(self):
        """Counts, from here on, the evidence check's batched SELECTs: on
        papers, by status, for a list of ids. (/ask-stream's chat history
        also reads one paper row by id; that is not this check.)"""
        engine = self.factory.kw["bind"]
        counter = {"papers": 0}

        def before(conn, cursor, statement, parameters, context, executemany):
            if "FROM papers" in statement and "papers.status" in statement and " IN (" in statement:
                counter["papers"] += 1

        event.listen(engine, "before_cursor_execute", before)
        self.addCleanup(event.remove, engine, "before_cursor_execute", before)
        return counter


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------
class TestSearchUsesOnlyLivePapers(RetrievalTestCase):

    def search(self, hits):
        with patch("app.api.search.encode_query", return_value=[0.1] * 4), \
             patch("app.api.search.client") as qdrant, \
             patch("app.api.search.rerank_results", side_effect=lambda q, r: list(r)):
            qdrant.query_points.return_value.points = hits
            return self.client.get("/search", params={"query": "anything"})

    def test_only_the_owners_indexed_papers_are_returned_in_their_ranked_order(self):
        body = self.search(self.every_kind_of_hit()).json()

        self.assertEqual(body["status"], "success", body)
        self.assertEqual([r["paper_id"] for r in body["results"]],
                         [self.papers["live"], self.papers["live_two"]])
        self.assertEqual(body["total_results"], 2)
        self.assertEqual(body["vector_results"], 2)

    def test_each_paper_state_is_excluded_on_its_own(self):
        for label in NOT_LIVE + ("missing", "other_owner"):
            with self.subTest(paper=label):
                body = self.search([self.hit(label)]).json()
                self.assertEqual(body["status"], "success", body)
                self.assertEqual(body["results"], [])
                self.assertEqual(body["total_results"], 0)

    def test_a_hit_with_no_or_a_malformed_paper_id_is_excluded(self):
        for paper_id in (None, "", "not-a-uuid", 12345):
            with self.subTest(paper_id=paper_id):
                hit = self.hit("live")
                hit.payload["paper_id"] = paper_id
                self.assertEqual(self.search([hit]).json()["results"], [])

    def test_the_result_payload_is_unchanged_for_a_live_paper(self):
        result = self.search([self.hit("live", page=7)]).json()["results"][0]

        self.assertEqual(result["paper_id"], self.papers["live"])
        self.assertEqual(result["paper"], "live title")
        self.assertEqual(result["page"], 7)
        self.assertEqual(result["text"], "text of live, page 7")
        self.assertEqual(result["score"], 0.5)

    def test_all_hits_are_checked_in_one_query_not_one_per_hit(self):
        hits = [self.hit(label, page=p) for p in range(1, 7) for label in ("live", "live_two", "missing", "failed")]
        lookups = self.count_paper_lookups()

        body = self.search(hits).json()

        self.assertEqual(lookups["papers"], 1)
        self.assertEqual({r["paper_id"] for r in body["results"]}, {self.papers["live"], self.papers["live_two"]})

    def test_more_distinct_papers_than_one_batch_are_checked_in_bounded_batches(self):
        hits = self.every_kind_of_hit()  # 8 distinct paper ids
        lookups = self.count_paper_lookups()

        with patch.object(retrieval_guard, "LOOKUP_BATCH", 3):
            body = self.search(hits).json()

        self.assertEqual(lookups["papers"], 3)  # ceil(8 / 3)
        self.assertEqual([r["paper_id"] for r in body["results"]],
                         [self.papers["live"], self.papers["live_two"]])

    def test_a_database_failure_is_an_error_not_an_empty_result(self):
        with patch.object(retrieval_guard, "session_scope",
                          side_effect=OperationalError("SELECT", {}, Exception(DATABASE_ERROR_TEXT))):
            resp = self.search(self.every_kind_of_hit())

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertNotIn("db.internal.example", resp.text)
        self.assertNotIn("researchmind_app", resp.text)


# ---------------------------------------------------------------------------
# /ask-stream
# ---------------------------------------------------------------------------
class TestAnswersUseOnlyLivePapers(RetrievalTestCase):

    def ask(self, hits, paper_id=""):
        """Returns (events, prompt or None if Groq was never called)."""
        params = {"question": "what do these papers find?"}
        if paper_id:
            params["paper_id"] = paper_id
        with patch("app.api.ask_stream.encode_query", return_value=[0.1] * 4), \
             patch("app.api.ask_stream.client") as qdrant, \
             patch("app.api.ask_stream.rerank_results", side_effect=lambda q, r: list(r)), \
             patch("app.api.ask_stream._groq_client") as groq:
            qdrant.query_points.return_value.points = hits
            groq.chat.completions.create.return_value = iter([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Grounded answer [Page 1]."))])
            ])
            resp = self.client.get("/ask-stream", params=params)
            called = groq.chat.completions.create.call_args
        events = [
            json.loads(frame.strip()[len("data:"):])
            for frame in resp.text.split("\n\n") if frame.strip().startswith("data:")
        ]
        prompt = called.kwargs["messages"][1]["content"] if called else None
        return events, prompt

    def done(self, events):
        return next(e for e in events if e.get("type") == "done")

    def test_only_live_papers_reach_the_prompt_and_the_citations(self):
        events, prompt = self.ask(self.every_kind_of_hit())

        cited = {c["paper_id"] for c in self.done(events)["citations"]}
        self.assertTrue(cited)
        self.assertLessEqual(cited, {self.papers["live"], self.papers["live_two"]})
        for label in NOT_LIVE + ("missing", "other_owner"):
            self.assertNotIn(f"text of {label}", prompt, f"{label} reached the model")
        self.assertIn("text of live", prompt)

    def test_when_no_hit_is_live_nothing_is_generated(self):
        hits = [self.hit(label) for label in NOT_LIVE + ("missing", "other_owner")]

        events, prompt = self.ask(hits)

        self.assertIsNone(prompt, "the model was called with unverified evidence")
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("No relevant papers found", errors[0]["text"])

    def test_a_question_scoped_to_a_paper_that_is_not_indexed_gets_no_evidence(self):
        for status in ("indexing", "failed", "uploading"):
            with self.subTest(status=status):
                events, prompt = self.ask([self.hit(status)], paper_id=self.papers[status])

                self.assertIsNone(prompt)
                self.assertTrue(any(e.get("type") == "error" for e in events))

    def test_a_database_failure_is_a_safe_error_and_nothing_is_generated(self):
        with patch.object(retrieval_guard, "session_scope",
                          side_effect=OperationalError("SELECT", {}, Exception(DATABASE_ERROR_TEXT))):
            events, prompt = self.ask(self.every_kind_of_hit())

        self.assertIsNone(prompt)
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(errors, [{
            "type": "error",
            "code": internal_error_payload()["code"],
            "text": internal_error_payload()["message"],
        }])
        self.assertNotIn("db.internal.example", json.dumps(events))
        self.assertFalse(any(e.get("type") == "done" for e in events))

    def test_all_hits_are_checked_in_one_query(self):
        lookups = self.count_paper_lookups()

        self.ask(self.every_kind_of_hit())

        # The chat history read and turn writes are separate statements on
        # other tables; exactly one SELECT on papers decides the evidence.
        self.assertEqual(lookups["papers"], 1)


def _database_down():
    return patch.object(
        retrieval_guard, "session_scope",
        side_effect=OperationalError("SELECT", {}, Exception(DATABASE_ERROR_TEXT)),
    )


# ---------------------------------------------------------------------------
# /research
# ---------------------------------------------------------------------------
class TestResearchUsesOnlyLivePapers(RetrievalTestCase):

    def research(self, hits):
        with patch("app.api.research.encode_query", return_value=[0.1] * 4), \
             patch("app.api.research.client") as qdrant, \
             patch("app.api.research.rerank_results", side_effect=lambda q, r: list(r)), \
             patch("app.api.research.research_agent", return_value="# Report") as agent:
            qdrant.query_points.return_value.points = hits
            resp = self.client.get("/research", params={"query": "what is known?"})
        return resp, agent

    def test_only_live_papers_reach_the_report_and_its_stored_citations(self):
        resp, agent = self.research(self.every_kind_of_hit())

        body = resp.json()
        self.assertEqual(body["status"], "success", body)
        context = agent.call_args.kwargs["context"]
        for label in NOT_LIVE + ("missing", "other_owner"):
            self.assertNotIn(f"text of {label},", context, f"{label} reached the report")
        self.assertIn("text of live,", context)
        self.assertIn("text of live_two,", context)
        # Ranked order of the live hits is kept.
        self.assertEqual([c["paper"] for c in body["citations"]], ["live title", "live_two title"])
        with self.factory() as db:
            self.assertEqual(db.query(Report).one().citations, body["citations"])

    def test_a_database_failure_is_an_error_and_no_report_is_written(self):
        with _database_down():
            resp, agent = self.research(self.every_kind_of_hit())

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertNotIn("db.internal.example", resp.text)
        agent.assert_not_called()
        with self.factory() as db:
            self.assertEqual(db.query(Report).count(), 0)


# ---------------------------------------------------------------------------
# /compare-papers
# ---------------------------------------------------------------------------
class TestCompareUsesOnlyLivePapers(RetrievalTestCase):

    def compare(self, hits, paper1="Alpha.pdf", paper2="Beta.pdf"):
        with patch("app.api.compare_papers.encode_query", return_value=[0.1] * 4), \
             patch("app.api.compare_papers.client") as qdrant, \
             patch("app.api.compare_papers.rerank_results", side_effect=lambda q, r: list(r)), \
             patch("app.api.compare_papers.generate_answer", return_value="A comparison.") as generate:
            qdrant.query_points.return_value.points = hits
            resp = self.client.get("/compare-papers", params={"paper1": paper1, "paper2": paper2})
        return resp, generate

    def titled_hits(self):
        """Each title is matched by substring, so dead vectors that share a
        live paper's title are the case that matters; they rank first."""
        return [
            _hit(self.papers["missing"], "Alpha.pdf", "DEAD-ALPHA orphaned"),
            _hit(self.papers["deleting"], "Alpha.pdf", "DEAD-ALPHA being removed"),
            _hit(self.papers["live"], "Alpha.pdf", "LIVE-ALPHA text"),
            _hit(self.papers["failed"], "Beta.pdf", "DEAD-BETA failed"),
            _hit(self.papers["other_owner"], "Beta.pdf", "DEAD-BETA someone else's"),
            _hit(self.papers["live_two"], "Beta.pdf", "LIVE-BETA text"),
        ]

    def test_only_live_papers_are_compared(self):
        resp, generate = self.compare(self.titled_hits())

        self.assertEqual(resp.json()["status"], "success", resp.json())
        sent = str(generate.call_args)
        self.assertIn("LIVE-ALPHA", sent)
        self.assertIn("LIVE-BETA", sent)
        self.assertNotIn("DEAD-", sent)

    def test_a_paper_with_only_dead_vectors_is_not_found(self):
        hits = [h for h in self.titled_hits() if h.payload["text"] != "LIVE-BETA text"]

        resp, generate = self.compare(hits)

        self.assertEqual(resp.json(), {"status": "error", "message": "Paper not found: Beta.pdf"})
        generate.assert_not_called()

    def test_a_database_failure_is_an_error_not_a_comparison(self):
        with _database_down():
            resp, generate = self.compare(self.titled_hits())

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertNotIn("db.internal.example", resp.text)
        generate.assert_not_called()


# ---------------------------------------------------------------------------
# /paper-details
# ---------------------------------------------------------------------------
class TestPaperDetailsUsesOnlyLivePapers(RetrievalTestCase):

    def details(self, hits, name="Report.pdf"):
        with patch("app.api.paper_details.encode_query", return_value=[0.1] * 4), \
             patch("app.api.paper_details.client") as qdrant:
            qdrant.query_points.return_value.points = hits
            return self.client.get("/paper-details", params={"paper_name": name})

    def test_a_dead_incarnation_with_the_same_title_never_answers(self):
        """The same file uploaded again after a delete is a new paper with
        the old title. Leftover vectors of the old one rank first; the
        answer must still be the live paper's id and text."""
        hits = [
            _hit(self.papers["missing"], "Report.pdf", "DEAD orphaned text"),
            _hit(self.papers["deleting"], "Report.pdf", "DEAD being removed"),
            _hit(self.papers["live"], "Report.pdf", "LIVE text"),
        ]

        body = self.details(hits).json()

        self.assertEqual(body["status"], "success", body)
        self.assertEqual(body["paper_id"], self.papers["live"])
        self.assertEqual(body["total_chunks"], 1)
        self.assertEqual(body["preview"], "LIVE text")

    def test_a_paper_that_is_not_live_is_not_found(self):
        for label in NOT_LIVE + ("missing", "other_owner"):
            with self.subTest(paper=label):
                body = self.details([_hit(self.papers[label], "Report.pdf", "text")]).json()
                self.assertEqual(body, {"status": "error", "message": "Paper not found: Report.pdf"})

    def test_a_hit_with_a_malformed_paper_id_is_not_found(self):
        body = self.details([_hit("not-a-uuid", "Report.pdf", "text")]).json()
        self.assertEqual(body["status"], "error")

    def test_a_database_failure_is_an_error(self):
        with _database_down():
            resp = self.details([_hit(self.papers["live"], "Report.pdf", "LIVE text")])

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertNotIn("db.internal.example", resp.text)


# ---------------------------------------------------------------------------
# /summarize-paper
# ---------------------------------------------------------------------------
class TestSummarizeUsesOnlyLivePapers(RetrievalTestCase):

    def summarize(self, name, fallback_hits=(), chunks=()):
        """Qdrant serves `fallback_hits` to the title fallback (limit 1)
        and `chunks` to the whole-paper fetch."""
        calls = {}

        def query_points(**kwargs):
            if kwargs.get("limit") == 1:
                calls["identify"] = kwargs
                return SimpleNamespace(points=list(fallback_hits))
            calls["fetch"] = kwargs
            return SimpleNamespace(points=list(chunks))

        with patch("app.api.summarize_paper.encode_query", return_value=[0.1] * 4), \
             patch("app.api.summarize_paper.client") as qdrant, \
             patch("app.api.summarize_paper.rerank_results", side_effect=lambda q, r: list(r)), \
             patch("app.api.summarize_paper.generate_answer", return_value="A summary.") as generate:
            qdrant.query_points.side_effect = query_points
            resp = self.client.get("/summarize-paper", params={"paper_name": name})
        return resp, generate, calls

    def test_the_title_resolves_to_the_indexed_incarnation(self):
        """The old incarnation's row (being removed) was created first and
        shares the title; only the indexed one may be summarised."""
        self.seed(OWNER_A, "deleting", "Same.pdf")
        new = self.seed(OWNER_A, "indexed", "Same.pdf")

        resp, generate, calls = self.summarize("Same.pdf", chunks=[_hit(new, "Same.pdf", "LIVE summary text")])

        self.assertEqual(resp.json()["status"], "success", resp.json())
        fetch_filter = {c.key: c.match.value for c in calls["fetch"]["query_filter"].must}
        self.assertEqual(fetch_filter.get("paper_id"), new)
        self.assertNotIn("identify", calls, "resolved from the database, without the index fallback")

    def test_a_title_naming_only_a_paper_that_is_not_indexed_is_not_found(self):
        for status in NOT_LIVE:
            with self.subTest(status=status):
                title = f"{status}-only.pdf"
                paper = self.seed(OWNER_A, status, title)
                hit = _hit(paper, title, "text")

                resp, generate, calls = self.summarize(title, fallback_hits=[hit], chunks=[hit])

                self.assertEqual(resp.json()["status"], "error")
                self.assertIn("Paper not found", resp.json()["message"])
                # Rejected at identification: its chunks are never fetched.
                self.assertNotIn("fetch", calls)
                generate.assert_not_called()

    def test_the_title_fallback_never_returns_a_deleted_papers_vectors(self):
        orphan = str(uuid.uuid4())  # its row is gone; its vectors remain
        hit = _hit(orphan, "Ghost.pdf", "DEAD text")

        resp, generate, calls = self.summarize("Ghost.pdf", fallback_hits=[hit], chunks=[hit])

        self.assertIn("identify", calls, "the fallback ran")
        # ...and resolved nothing: the dead paper's chunks are never fetched.
        self.assertNotIn("fetch", calls)
        self.assertEqual(resp.json()["status"], "error")
        self.assertIn("Paper not found", resp.json()["message"])
        generate.assert_not_called()

    def test_another_papers_stray_chunk_never_reaches_the_summary(self):
        chunks = [
            _hit(self.papers["live"], "Live paper", "LIVE text"),
            _hit(self.papers["deleting"], "Live paper", "DEAD stray chunk"),
        ]

        resp, generate, _ = self.summarize("Live paper", chunks=chunks)

        self.assertEqual(resp.json()["status"], "success", resp.json())
        sent = str(generate.call_args)
        self.assertIn("LIVE text", sent)
        self.assertNotIn("DEAD stray chunk", sent)

    def test_a_database_failure_is_an_error_not_a_summary(self):
        with _database_down():
            resp, generate, _ = self.summarize("Live paper", chunks=[_hit(self.papers["live"], "Live paper", "LIVE text")])

        self.assertEqual(resp.json(), internal_error_payload())
        self.assertNotIn("db.internal.example", resp.text)
        generate.assert_not_called()


# ---------------------------------------------------------------------------
# Paper-scoped endpoints: intelligence, relationships, evidence
# ---------------------------------------------------------------------------
class TestPaperScopedEndpointsRejectPapersThatAreNotIndexed(RetrievalTestCase):
    """Each resolves the paper by id in Postgres first; one that is not
    indexed is answered as a missing paper, and its chunks are never read."""

    NOT_FOUND = {"status": "error", "message": "Paper not found."}

    def test_intelligence_read_answers_404_for_a_paper_that_is_not_indexed(self):
        for label in NOT_LIVE + ("other_owner", "missing"):
            with self.subTest(paper=label):
                resp = self.client.get("/paper-intelligence", params={"paper_id": self.papers[label]})
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json(), self.NOT_FOUND)

        live = self.client.get("/paper-intelligence", params={"paper_id": self.papers["live"]})
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "not_generated")

    def test_intelligence_generation_never_reads_a_paper_that_is_not_indexed(self):
        with patch("app.api.paper_intelligence.client") as qdrant:
            qdrant.scroll.return_value = ([], None)
            for label in NOT_LIVE:
                with self.subTest(paper=label):
                    resp = self.client.post("/paper-intelligence", params={"paper_id": self.papers[label]})
                    self.assertEqual(resp.json(), self.NOT_FOUND)
            qdrant.scroll.assert_not_called()

            # An indexed paper is read; this one has no chunks in the double.
            live = self.client.post("/paper-intelligence", params={"paper_id": self.papers["live"]})
            self.assertEqual(live.json(), {"status": "error", "message": NOT_INDEXED_MESSAGE})
            qdrant.scroll.assert_called()

    def test_a_relationship_with_a_paper_that_is_not_indexed_is_404(self):
        with patch("app.services.paper_relationship_evidence.client") as qdrant:
            for label in NOT_LIVE:
                for method in ("get", "post"):
                    with self.subTest(paper=label, method=method):
                        resp = getattr(self.client, method)("/paper-relationship", params={
                            "paper_a_id": self.papers["live"], "paper_b_id": self.papers[label],
                        })
                        self.assertEqual(resp.status_code, 404)
                        self.assertEqual(resp.json(), self.NOT_FOUND)
            qdrant.scroll.assert_not_called()

        pair = {"paper_a_id": self.papers["live"], "paper_b_id": self.papers["live_two"]}
        self.assertEqual(self.client.get("/paper-relationship", params=pair).json().get("code"), NOT_GENERATED_CODE)
        self.assertEqual(self.client.post("/paper-relationship", params=pair).json().get("code"), MISSING_INTELLIGENCE_CODE)

    def test_evidence_answers_404_for_a_paper_that_is_not_indexed(self):
        for label in NOT_LIVE:
            with self.subTest(paper=label):
                resp = self.client.get("/paper-evidence", params={"paper_id": self.papers[label]})
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json(), self.NOT_FOUND)

        live = self.client.get("/paper-evidence", params={"paper_id": self.papers["live"]})
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "not_generated")


if __name__ == "__main__":
    unittest.main()
