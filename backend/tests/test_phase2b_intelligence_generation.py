"""
Phase 2B step 3 — the Paper Intelligence generation pipeline.

The subject is the gate, not the prose. A language model writing a good
summary is not what these tests are about; what they pin is that NOTHING
reaches `paper_intelligence` unless it survived validate_intelligence()
against an allowlist built from the exact paper being analysed.

Three properties carry the weight:

  * the allowlist is built from the chunks ACTUALLY SUPPLIED to the
    model, so a citation of a chunk the budget dropped is fabrication
    and is refused;
  * every rejection persists nothing at all — a failed generation must
    leave the table exactly as it found it;
  * the paper is identified by exact owner-scoped id, never by
    similarity, never by title substring.

Fully offline: Qdrant, Groq and the database are all fakes. No provider
call, no network, no production data.
"""
import ast
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app.db.session as session_module
from app.agents.qa_agent import REFUSAL, GenerationResult
from app.core.auth import get_current_owner_id
from app.core.providers import PROVIDER_TIMEOUT, ProviderTimeout
from app.db.models import Paper, PaperIntelligenceRow
from app.services.intelligence_schema import (
    SECTION_NAMES,
    IntelligenceValidationError,
)
from app.services.paper_intelligence_store import save_intelligence
from tests.sqlite_harness import attach_sqlite_db

import app.api.paper_intelligence as pipeline

MODULE_PATH = os.path.join(
    BACKEND_DIR, "app", "api", "paper_intelligence.py"
)

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"
UNKNOWN_PAPER = "99999999-9999-9999-9999-999999999999"

TOTAL_PAGES = 9

#: What the indexer would have written for paper A. (3, 0) and (7, 0)
#: share a chunk_id on different pages — the pair identity this feature
#: depends on.
CHUNKS_A = [
    (1, 0, "We address defect detection in PCB inspection."),
    (3, 0, "METHODOLOGY. A two-stage hybrid framework is proposed."),
    (3, 1, "Stage two performs zero-shot classification."),
    (7, 0, "LIMITATIONS. The approach depends on prompt quality."),
]


def _point(page, chunk_id, text, paper_id=PAPER_A, owner_id=OWNER_A):
    point = MagicMock()
    point.id = uuid.uuid4().hex
    point.payload = {
        "text": text,
        "page": page,
        "chunk_id": chunk_id,
        "total_pages": TOTAL_PAGES,
        "paper_id": paper_id,
        "owner_id": owner_id,
        "paper": "Paper A",
        "source": "a.pdf",
    }
    return point


def _points_for(chunks=CHUNKS_A, **kw):
    return [_point(p, c, t, **kw) for p, c, t in chunks]


def _section(status="answered", summary="A summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def _model_payload(**overrides):
    """A complete, valid model response, with named sections replaced."""
    payload = {name: _section() for name in SECTION_NAMES}
    payload.update(overrides)
    return payload


def _model_json(**overrides):
    return json.dumps(_model_payload(**overrides))


class PipelineTestCase(unittest.TestCase):
    """Drives the real endpoint with fake Qdrant, fake Groq, real DB."""

    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self._seed_paper(OWNER_A, PAPER_A, "Paper A")
        self._seed_paper(OWNER_B, PAPER_B, "Paper B")

        # --- fake Qdrant ------------------------------------------------
        self.qdrant = MagicMock()
        self.scroll_calls = []

        def scroll(**kwargs):
            self.scroll_calls.append(kwargs)
            return list(self.points), None

        self.qdrant.scroll.side_effect = scroll
        self.points = _points_for()
        patcher = patch.object(pipeline, "client", self.qdrant)
        patcher.start()
        self.addCleanup(patcher.stop)

        # --- fake Groq --------------------------------------------------
        self.generate_calls = []

        def fake_generate(question, context, **kwargs):
            self.generate_calls.append({"context": context, **kwargs})
            return GenerationResult(text=self.model_text, finish_reason="stop")

        self.model_text = _model_json()
        self.generate = MagicMock(side_effect=fake_generate)
        gpatch = patch.object(pipeline, "generate", self.generate)
        gpatch.start()
        self.addCleanup(gpatch.stop)

        # --- app + auth -------------------------------------------------
        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _seed_paper(self, owner_id, paper_id, title):
        with self.factory() as session:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash="h" * 64,
                    storage_path=f"{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()

    def post(self, paper_id=PAPER_A, **params):
        return self.client.post(
            "/paper-intelligence", params={"paper_id": paper_id, **params}
        )

    def rows(self):
        """Every stored row, read with NO owner filter."""
        with self.factory() as session:
            return session.query(PaperIntelligenceRow).all()

    def assertNothingPersisted(self):
        self.assertEqual(self.rows(), [], "a rejected generation persisted a row")


# ----------------------------------------------------------------------
# 1-3. The happy path
# ----------------------------------------------------------------------
class TestSuccessfulGeneration(PipelineTestCase):

    def test_successful_structured_generation(self):
        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["paper_id"], PAPER_A)
        self.assertEqual(body["paper"], "Paper A")
        self.assertEqual(body["schema_version"], "1")
        self.assertFalse(body["superseded"])

    def test_response_carries_exactly_the_ten_sections(self):
        intelligence = self.post().json()["intelligence"]

        self.assertEqual(sorted(intelligence), sorted(SECTION_NAMES))
        self.assertEqual(len(intelligence), 10)

    def test_valid_evidence_passes_through_unchanged(self):
        self.model_text = _model_json(
            methodology=_section(
                summary="A two-stage framework.",
                evidence=[{"page": 3, "chunk_id": 1}, {"page": 7, "chunk_id": 0}],
            )
        )

        section = self.post().json()["intelligence"]["methodology"]

        self.assertEqual(section["status"], "answered")
        self.assertEqual(
            [(e["page"], e["chunk_id"]) for e in section["evidence"]],
            [(3, 1), (7, 0)],
        )

    def test_context_carries_the_citation_headers_the_model_must_use(self):
        self.post()

        context = self.generate_calls[0]["context"]
        for page, chunk_id, text in CHUNKS_A:
            self.assertIn(f"[page={page} chunk={chunk_id}]", context)
            self.assertIn(text, context)

    def test_context_is_in_reading_order(self):
        self.points = _points_for(list(reversed(CHUNKS_A)))

        context = self.post() and self.generate_calls[0]["context"]

        positions = [
            context.index(f"[page={p} chunk={c}]") for p, c, _ in CHUNKS_A
        ]
        self.assertEqual(positions, sorted(positions))

    def test_caller_budget_is_not_re_truncated_by_the_agent(self):
        """max_context_chars=None: the double cut is what once discarded
        Compare's entire second paper."""
        self.post()

        self.assertIsNone(self.generate_calls[0]["max_context_chars"])
        self.assertEqual(
            self.generate_calls[0]["max_tokens"], pipeline.INTELLIGENCE_MAX_TOKENS
        )


# ----------------------------------------------------------------------
# 4-9. Evidence the model must not get away with
# ----------------------------------------------------------------------
class TestFabricatedEvidenceIsRejected(PipelineTestCase):

    def assertRejected(self, code=None):
        body = self.post().json()
        self.assertEqual(body["status"], "error")
        if code:
            self.assertEqual(body["code"], code)
        self.assertNothingPersisted()
        return body

    def test_nonexistent_page_is_rejected(self):
        self.model_text = _model_json(
            key_results=_section(evidence=[{"page": 99, "chunk_id": 0}])
        )
        self.assertRejected("evidence_out_of_range")

    def test_page_inside_the_paper_but_never_supplied_is_rejected(self):
        """Page 4 exists in a 9-page paper but was not among the chunks
        given to the model, so citing it is fabrication."""
        self.model_text = _model_json(
            key_results=_section(evidence=[{"page": 4, "chunk_id": 0}])
        )
        self.assertRejected("evidence_not_supplied")

    def test_nonexistent_chunk_id_is_rejected(self):
        self.model_text = _model_json(
            dataset=_section(evidence=[{"page": 3, "chunk_id": 47}])
        )
        self.assertRejected("evidence_not_supplied")

    def test_chunk_id_valid_on_another_page_is_rejected(self):
        """(1, 0) and (3, 1) both exist; (1, 1) does not. A validator
        keyed on chunk_id alone would accept this."""
        self.model_text = _model_json(
            dataset=_section(evidence=[{"page": 1, "chunk_id": 1}])
        )
        self.assertRejected("evidence_not_supplied")

    def test_evidence_from_another_paper_is_rejected(self):
        """Paper B's chunks are not in paper A's allowlist, so a citation
        of one cannot validate — whatever the model saw."""
        self.model_text = _model_json(
            contributions=_section(evidence=[{"page": 2, "chunk_id": 0}])
        )
        self.assertRejected("evidence_not_supplied")

    def test_evidence_from_another_owner_is_rejected(self):
        """Same shape, and blocked by the same mechanism: the allowlist
        is built only from chunks the owner+paper filter returned."""
        self.model_text = _model_json(
            limitations=_section(evidence=[{"page": 5, "chunk_id": 0}])
        )
        self.assertRejected("evidence_not_supplied")

    def test_a_fake_qdrant_point_id_cannot_substitute_for_page_and_chunk(self):
        self.model_text = _model_json(
            dataset=_section(
                evidence=[
                    {
                        "page": 3,
                        "chunk_id": 0,
                        "point_id": "b4f1c0de-0000-4000-8000-000000000000",
                    }
                ]
            )
        )
        self.assertRejected("invalid_structure")

    def test_a_point_id_instead_of_page_and_chunk_is_rejected(self):
        self.model_text = _model_json(
            dataset=_section(
                evidence=[{"id": "b4f1c0de-0000-4000-8000-000000000000"}]
            )
        )
        self.assertRejected("invalid_structure")

    def test_no_qdrant_point_id_reaches_the_stored_json(self):
        self.post()

        blob = json.dumps(self.rows()[0].intelligence).lower()
        for forbidden in ("point_id", "qdrant", "vector_id"):
            self.assertNotIn(forbidden, blob)

    def test_a_chunk_the_budget_dropped_cannot_be_cited(self):
        """THE allowlist property, exercised end to end.

        At this budget the stride keeps (1,0) and (3,1) and drops (3,0)
        and (7,0). The model never saw the dropped blocks, so citing one
        is fabrication and must be refused — even though the chunk is
        real, belongs to this paper and was retrieved from Qdrant.

        Asserted through the ENDPOINT on purpose: a unit test over
        select_context() + build_allowlist() passes just as happily when
        the endpoint wires the allowlist to the wrong list.
        """
        with patch.object(pipeline, "INTELLIGENCE_CONTEXT_CHARS", 150):
            # Default payload cites (3, 0), which this budget drops.
            body = self.post().json()

            self.assertEqual(body["status"], "error")
            self.assertEqual(body["code"], "evidence_not_supplied")
            self.assertNothingPersisted()

            context = self.generate_calls[0]["context"]
            self.assertNotIn("[page=3 chunk=0]", context)
            self.assertIn("[page=3 chunk=1]", context)

    def test_a_chunk_the_budget_kept_can_still_be_cited(self):
        """The other half of the same property: narrowing the allowlist
        must not break citation of what WAS supplied."""
        with patch.object(pipeline, "INTELLIGENCE_CONTEXT_CHARS", 150):
            self.model_text = _model_json(
                **{
                    name: _section(evidence=[{"page": 3, "chunk_id": 1}])
                    for name in SECTION_NAMES
                }
            )

            body = self.post().json()

            self.assertEqual(body["status"], "success")
            self.assertEqual(len(self.rows()), 1)

    def test_an_invalid_quote_does_not_create_a_false_citation(self):
        """A non-verbatim quote is DROPPED; the reference it came with is
        kept, because the reference checked out. The paraphrase is never
        shown as if the paper contained it."""
        self.model_text = _model_json(
            methodology=_section(
                evidence=[
                    {
                        "page": 3,
                        "chunk_id": 0,
                        "quote": "The authors clearly prove their method is best.",
                    }
                ]
            )
        )

        section = self.post().json()["intelligence"]["methodology"]

        self.assertEqual(section["evidence"][0]["page"], 3)
        self.assertEqual(section["evidence"][0]["chunk_id"], 0)
        self.assertIsNone(section["evidence"][0]["quote"])

    def test_a_verbatim_quote_survives(self):
        self.model_text = _model_json(
            methodology=_section(
                evidence=[{"page": 3, "chunk_id": 0, "quote": CHUNKS_A[1][2]}]
            )
        )

        section = self.post().json()["intelligence"]["methodology"]
        self.assertEqual(section["evidence"][0]["quote"], CHUNKS_A[1][2])


# ----------------------------------------------------------------------
# 10-14. Malformed and incoherent responses
# ----------------------------------------------------------------------
class TestMalformedResponsesAreRejected(PipelineTestCase):

    def assertRejected(self, code=None):
        body = self.post().json()
        self.assertEqual(body["status"], "error")
        if code:
            self.assertEqual(body["code"], code)
        self.assertNothingPersisted()
        return body

    def test_invalid_json_is_rejected(self):
        self.model_text = "{not valid json,,,"
        self.assertRejected("malformed_json")

    def test_arbitrary_prose_is_rejected(self):
        self.model_text = (
            "Certainly! Here is my analysis of the paper. The authors "
            "propose a two-stage framework that works very well."
        )
        self.assertRejected("malformed_json")

    def test_prose_wrapped_around_valid_json_is_rejected(self):
        """Salvaging JSON out of prose would hide that the model ignored
        its instructions."""
        self.model_text = f"Here you go!\n{_model_json()}\nHope that helps."
        self.assertRejected("malformed_json")

    def test_a_single_json_fence_is_tolerated(self):
        self.model_text = f"```json\n{_model_json()}\n```"

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(len(self.rows()), 1)

    def test_a_missing_section_is_rejected(self):
        payload = _model_payload()
        payload.pop("reproducibility")
        self.model_text = json.dumps(payload)
        self.assertRejected("invalid_structure")

    def test_an_unknown_section_is_rejected(self):
        self.model_text = json.dumps(
            dict(_model_payload(), future_work=_section())
        )
        self.assertRejected("invalid_structure")

    def test_answered_section_without_evidence_is_rejected(self):
        self.model_text = _model_json(
            key_results=_section(summary="Precision rose to 0.94.", evidence=[])
        )
        self.assertRejected("invalid_structure")

    def test_answered_section_without_summary_is_rejected(self):
        self.model_text = _model_json(key_results=_section(summary=""))
        self.assertRejected("invalid_structure")

    def test_not_specified_section_with_evidence_is_rejected(self):
        self.model_text = _model_json(
            dataset=_section(
                status="not_specified",
                summary=None,
                evidence=[{"page": 3, "chunk_id": 0}],
            )
        )
        self.assertRejected("invalid_structure")

    def test_not_specified_summary_is_canonicalised_to_null(self):
        self.model_text = _model_json(
            dataset=_section(
                status="not_specified",
                summary="The paper does not mention a dataset.",
                evidence=[],
            )
        )

        section = self.post().json()["intelligence"]["dataset"]

        self.assertEqual(section["status"], "not_specified")
        self.assertIsNone(section["summary"])
        self.assertEqual(section["evidence"], [])

    def test_an_unknown_status_is_rejected(self):
        self.model_text = _model_json(dataset=_section(status="probably"))
        self.assertRejected("invalid_structure")

    def test_an_empty_completion_is_a_generation_failure_not_bad_json(self):
        """generate() returns REFUSAL for an empty completion. Parsing it
        would report a provider problem as malformed model JSON."""
        self.model_text = REFUSAL

        body = self.post().json()

        self.assertEqual(body["code"], pipeline.GENERATION_FAILED_CODE)
        self.assertNothingPersisted()


# ----------------------------------------------------------------------
# 15-17. Nothing is persisted, nothing is called, when it should not be
# ----------------------------------------------------------------------
class TestFailuresPersistNothing(PipelineTestCase):

    def test_validation_failure_persists_nothing(self):
        self.model_text = _model_json(
            dataset=_section(evidence=[{"page": 4, "chunk_id": 0}])
        )

        self.assertEqual(self.post().json()["status"], "error")
        self.assertNothingPersisted()

    def test_validation_failure_does_not_replace_an_existing_row(self):
        self.post()
        self.assertEqual(len(self.rows()), 1)
        before = self.rows()[0].intelligence

        self.model_text = _model_json(
            dataset=_section(evidence=[{"page": 4, "chunk_id": 0}])
        )
        self.assertEqual(self.post().json()["status"], "error")

        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].intelligence, before)

    def test_provider_failure_persists_nothing(self):
        self.generate.side_effect = ProviderTimeout()

        body = self.post().json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], PROVIDER_TIMEOUT)
        self.assertNothingPersisted()

    def test_provider_failure_returns_no_raw_exception_text(self):
        self.generate.side_effect = RuntimeError(
            "groq: invalid api key sk-SECRET at https://api.groq.com"
        )

        body = self.post().json()

        self.assertEqual(body["status"], "error")
        rendered = json.dumps(body)
        for leaked in ("sk-SECRET", "groq", "api.groq.com", "RuntimeError"):
            self.assertNotIn(leaked, rendered)
        self.assertNothingPersisted()

    def test_raw_model_output_is_never_returned_to_the_client(self):
        self.model_text = "TOTALLY-UNPARSEABLE-MODEL-TEXT-MARKER"

        body = self.post().json()

        self.assertNotIn("MARKER", json.dumps(body))
        self.assertNothingPersisted()

    def test_an_unindexed_paper_makes_no_provider_call(self):
        self.points = []

        body = self.post().json()

        self.assertEqual(body["status"], "error")
        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_a_missing_paper_makes_no_provider_call(self):
        body = self.post(paper_id=UNKNOWN_PAPER).json()

        self.assertEqual(body["message"], pipeline.NOT_FOUND_MESSAGE)
        self.generate.assert_not_called()
        self.qdrant.scroll.assert_not_called()
        self.assertNothingPersisted()

    def test_a_deterministic_rejection_does_not_consume_daily_allowance(self):
        """charge_ai_unit() sits immediately before the provider call, not
        in the dependency, so an unknown paper — which costs nothing
        upstream — must not bill a unit."""
        from app.core import limits as limits_config
        from app.core.quota import peek

        with patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "AI_RATE_PER_MINUTE": "100",
                "AI_QUOTA_PER_DAY": "50",
            },
            clear=False,
        ):
            self.post(paper_id=UNKNOWN_PAPER)
            self.points = []
            self.post()

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)

            self.points = _points_for()
            self.assertEqual(self.post().json()["status"], "success")

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_quota_rejection_causes_zero_provider_calls(self):
        """Opt in to enforcement explicitly — tests/__init__.py turns it
        off for the wider suite, and a guard that silently no-ops would
        make this test prove nothing."""
        from app.core.limits import enforcement_enabled

        with patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "AI_RATE_PER_MINUTE": "100",
                "AI_QUOTA_PER_DAY": "1",
            },
            clear=False,
        ):
            assert enforcement_enabled(), "enforcement must be on for this test"

            first = self.post().json()
            self.assertEqual(first["status"], "success")
            self.assertEqual(self.generate.call_count, 1)

            second = self.post()

            self.assertEqual(second.status_code, 429)
            # The refusal happened in the dependency, before the handler
            # body, so no second generation was attempted.
            self.assertEqual(self.generate.call_count, 1)
            self.assertEqual(len(self.rows()), 1)


# ----------------------------------------------------------------------
# The error paths themselves must not fail
# ----------------------------------------------------------------------
class TestErrorHandlingIsItselfSafe(PipelineTestCase):
    """An exception handler that raises turns a clean rejection into an
    unhandled fault: FastAPI returns a bare 500 with no body, so the
    client gets neither the sanitized payload nor a usable error."""

    SANITIZED = {
        "status": "error",
        "code": "internal_error",
        "message": "Something went wrong on our side. Please try again.",
    }

    def test_an_early_unexpected_exception_is_sanitized(self):
        with patch.object(
            pipeline, "resolve_paper", side_effect=RuntimeError("boom-early")
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.SANITIZED)
        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_an_early_unexpected_exception_leaks_no_exception_text(self):
        with patch.object(
            pipeline,
            "resolve_paper",
            side_effect=RuntimeError("table papers does not exist at 10.1.2.3:5432"),
        ):
            body = self.post().json()

        rendered = json.dumps(body)
        for leaked in ("papers", "10.1.2.3", "5432", "RuntimeError", "does not exist"):
            self.assertNotIn(leaked, rendered)

    def test_an_unexpected_exception_during_retrieval_is_sanitized(self):
        self.qdrant.scroll.side_effect = RuntimeError("qdrant exploded")

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.SANITIZED)
        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_a_validation_error_raised_before_the_allowlist_exists_is_handled(self):
        """The actual regression.

        The rejection handler logs allowed_evidence, total_pages and raw.
        Those are assigned part-way through the try block, so an
        IntelligenceValidationError escaping BEFORE them once made the
        handler raise UnboundLocalError — and that secondary exception
        escaped the endpoint, so the caller received a bare 500 with no
        body rather than the sanitized rejection.

        build_allowlist() does not raise today; it is patched here to
        stand in for any future step that might, because the handler must
        not depend on an invariant nothing enforces.
        """
        with patch.object(
            pipeline,
            "build_allowlist",
            side_effect=IntelligenceValidationError(
                "invalid_structure", "Raised before the allowlist existed."
            ),
        ):
            response = self.post()

        # The point: a JSON body came back at all.
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "invalid_structure")

        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_a_validation_error_raised_before_the_page_count_exists_is_handled(self):
        with patch.object(
            pipeline,
            "resolve_total_pages",
            side_effect=IntelligenceValidationError(
                "invalid_structure", "Raised before total_pages existed."
            ),
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "error")
        self.assertNothingPersisted()

    def test_the_diagnostic_defaults_never_reach_the_client(self):
        """They exist for the server log line only."""
        with patch.object(
            pipeline,
            "build_allowlist",
            side_effect=IntelligenceValidationError("invalid_structure", "early"),
        ):
            body = self.post().json()

        self.assertEqual(set(body), {"status", "code", "message"})
        for internal in ("allowed_evidence", "total_pages", "raw", "allowlist"):
            self.assertNotIn(internal, json.dumps(body))

    def test_the_ordinary_rejection_path_still_logs_real_values(self):
        """The defaults must not shadow the real diagnostics on the path
        that actually rejects."""
        self.model_text = _model_json(
            dataset=_section(evidence=[{"page": 4, "chunk_id": 0}])
        )

        with patch("builtins.print") as printed:
            body = self.post().json()

        self.assertEqual(body["code"], "evidence_not_supplied")

        logged = " ".join(
            str(call.args[0]) for call in printed.call_args_list if call.args
        )
        self.assertIn("allowlist=4", logged)
        self.assertIn("total_pages=9", logged)
        self.assertNotIn("allowlist=0", logged)
        self.assertNotIn("response_chars=0", logged)


# ----------------------------------------------------------------------
# 18-21. Identity and isolation
# ----------------------------------------------------------------------
class TestIdentityAndIsolation(PipelineTestCase):

    def test_another_owners_paper_is_not_found(self):
        body = self.post(paper_id=PAPER_B).json()

        self.assertEqual(body["message"], pipeline.NOT_FOUND_MESSAGE)
        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_unknown_and_foreign_papers_are_indistinguishable(self):
        foreign = self.post(paper_id=PAPER_B).json()
        unknown = self.post(paper_id=UNKNOWN_PAPER).json()

        self.assertEqual(foreign, unknown)

    def test_a_malformed_paper_id_is_a_miss_not_a_crash(self):
        for bad in ("not-a-uuid", "", "1; drop table papers"):
            with self.subTest(paper_id=bad):
                response = self.post(paper_id=bad)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["message"], pipeline.NOT_FOUND_MESSAGE
                )
        self.assertNothingPersisted()

    def test_qdrant_retrieval_is_owner_and_paper_scoped(self):
        self.post()

        self.assertEqual(len(self.scroll_calls), 1)
        conditions = self.scroll_calls[0]["scroll_filter"].must

        pairs = {(c.key, c.match.value) for c in conditions}
        self.assertEqual(pairs, {("owner_id", OWNER_A), ("paper_id", PAPER_A)})

    def test_qdrant_retrieval_requests_no_vectors(self):
        self.post()

        self.assertFalse(self.scroll_calls[0]["with_vectors"])
        self.assertTrue(self.scroll_calls[0]["with_payload"])

    def test_owner_id_comes_only_from_the_verified_jwt(self):
        """A client-supplied owner_id must reach neither the database
        session nor the Qdrant filter."""
        seen = []
        real_new_session = session_module._new_session

        def spy(owner_id):
            seen.append(owner_id)
            return real_new_session(owner_id)

        with patch.object(session_module, "_new_session", side_effect=spy):
            self.post(owner_id=OWNER_B, user_id=OWNER_B)

        self.assertTrue(seen)
        self.assertEqual(set(seen), {OWNER_A})

        conditions = self.scroll_calls[0]["scroll_filter"].must
        owner_terms = [c.match.value for c in conditions if c.key == "owner_id"]
        self.assertEqual(owner_terms, [OWNER_A])

    def test_the_resolved_paper_id_is_used_not_the_raw_input(self):
        """Uppercased and braced UUIDs parse to the same id; the filter
        must carry the canonical resolved form."""
        self.post(paper_id=PAPER_A.upper())

        conditions = self.scroll_calls[0]["scroll_filter"].must
        paper_terms = [c.match.value for c in conditions if c.key == "paper_id"]
        self.assertEqual(paper_terms, [PAPER_A])

    def test_the_stored_row_is_keyed_to_the_authenticated_owner(self):
        self.post()

        row = self.rows()[0]
        self.assertEqual(str(row.owner_id), OWNER_A)
        self.assertEqual(str(row.paper_id), PAPER_A)

    def test_pipeline_uses_no_similarity_search(self):
        """Exact resolution only. Parsed, not grepped: the module's own
        docstring discusses the search path it replaced."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        for forbidden in ("query_points", "encode_query", "rerank_results", "search"):
            self.assertNotIn(forbidden, called)

    def test_pipeline_imports_no_embedder_or_reranker(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }

        self.assertNotIn("app.rag.embedder", modules)
        self.assertNotIn("app.rag.reranker", modules)

    def test_no_voyage_embedding_call_is_made(self):
        self.post()
        # The fake Qdrant records everything; a query_points call would
        # have needed a vector, and none was requested.
        self.qdrant.query_points.assert_not_called()


# ----------------------------------------------------------------------
# 22-24. Persistence and concurrency
# ----------------------------------------------------------------------
class TestPersistenceAndConcurrency(PipelineTestCase):

    def test_success_persists_exactly_one_current_row(self):
        self.post()

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].schema_version, "1")
        self.assertEqual(rows[0].model, pipeline.GROQ_MODEL)

    def test_repeated_generation_keeps_exactly_one_row(self):
        for _ in range(3):
            self.assertEqual(self.post().json()["status"], "success")

        self.assertEqual(len(self.rows()), 1)

    def test_regeneration_replaces_the_stored_content(self):
        self.post()

        self.model_text = _model_json(
            key_results=_section(summary="A revised finding.")
        )
        body = self.post().json()

        self.assertEqual(
            body["intelligence"]["key_results"]["summary"], "A revised finding."
        )
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(
            self.rows()[0].intelligence["key_results"]["summary"],
            "A revised finding.",
        )

    def test_a_stale_generation_cannot_overwrite_a_newer_result(self):
        """The hazard: a user triggers a slow regeneration, triggers a
        second, the second lands first, and the first then reverts the
        paper to the older analysis."""
        newer = datetime.now(timezone.utc) + timedelta(hours=1)
        self.post()  # establishes the row

        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            row.generated_at = newer
            row.model = "the-newer-run"
            session.commit()

        # This request stamps generated_at = now, which is older.
        self.model_text = _model_json(
            key_results=_section(summary="Stale overwrite attempt.")
        )
        body = self.post().json()

        self.assertTrue(body["superseded"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].model, "the-newer-run")
        self.assertNotEqual(
            self.rows()[0].intelligence["key_results"]["summary"],
            "Stale overwrite attempt.",
        )

    def test_the_superseding_row_is_what_the_client_receives(self):
        """Even when its own result lost, the response is the CURRENT
        intelligence — never the discarded one."""
        self.post()

        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            row.generated_at = datetime.now(timezone.utc) + timedelta(hours=1)
            stored = dict(row.intelligence)
            stored["key_results"] = {
                "status": "answered",
                "summary": "The winning result.",
                "evidence": [{"page": 3, "chunk_id": 0, "quote": None}],
            }
            row.intelligence = stored
            session.commit()

        self.model_text = _model_json(
            key_results=_section(summary="The losing result.")
        )
        body = self.post().json()

        self.assertEqual(
            body["intelligence"]["key_results"]["summary"], "The winning result."
        )

    def test_a_newer_generation_does_overwrite_an_older_one(self):
        """The guard must not freeze the row: an ordinary regeneration
        still wins."""
        self.post()

        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            row.generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
            session.commit()

        self.model_text = _model_json(
            key_results=_section(summary="A legitimately newer result.")
        )
        body = self.post().json()

        self.assertFalse(body["superseded"])
        self.assertEqual(
            self.rows()[0].intelligence["key_results"]["summary"],
            "A legitimately newer result.",
        )

    def test_each_owner_keeps_its_own_row(self):
        self.post()

        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_B
        self.points = _points_for(paper_id=PAPER_B, owner_id=OWNER_B)
        self.post(paper_id=PAPER_B)

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {str(r.owner_id) for r in rows}, {OWNER_A, OWNER_B}
        )


# ----------------------------------------------------------------------
# The store cannot be reached with unvalidated input
# ----------------------------------------------------------------------
class TestStoreCannotBeReachedUnvalidated(PipelineTestCase):

    def test_save_intelligence_rejects_a_raw_dict(self):
        with self.assertRaises(TypeError):
            save_intelligence(
                OWNER_A, PAPER_A, _model_payload(), model="m"
            )
        self.assertNothingPersisted()

    def test_save_intelligence_rejects_a_json_string(self):
        with self.assertRaises(TypeError):
            save_intelligence(OWNER_A, PAPER_A, _model_json(), model="m")
        self.assertNothingPersisted()

    def test_the_pipeline_passes_only_validate_intelligence_output(self):
        """Structural proof: the single save_intelligence call site is
        handed the name bound by the validate_intelligence call, so there
        is no path from raw model text to the table that skips it."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        validated_names = {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "validate_intelligence"
        }
        self.assertTrue(validated_names, "validate_intelligence is never called")

        saves = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "save_intelligence"
        ]
        self.assertEqual(len(saves), 1, "there must be exactly one save call site")

        third_positional = saves[0].args[2]
        self.assertIsInstance(third_positional, ast.Name)
        self.assertIn(third_positional.id, validated_names)

    def test_validation_runs_before_persistence_in_source_order(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()

        tree = ast.parse(source)
        validate_line = min(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "validate_intelligence"
        )
        save_line = min(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "save_intelligence"
        )
        self.assertLess(validate_line, save_line)


# ----------------------------------------------------------------------
# Allowlist construction
# ----------------------------------------------------------------------
class TestAllowlistConstruction(unittest.TestCase):
    """Unit-level, no app: the allowlist is the security boundary."""

    def _chunks(self, spec=CHUNKS_A):
        return [_point(p, c, t).payload for p, c, t in spec]

    def test_allowlist_is_built_from_selected_chunks_not_all_retrieved(self):
        """A chunk the budget dropped was never shown to the model, so
        citing it is fabrication — the allowlist must not contain it."""
        chunks = self._chunks()
        selected = pipeline.select_context(chunks, budget=60)

        self.assertLess(len(selected), len(chunks))

        allowed, _ = pipeline.build_allowlist(selected)
        selected_keys = {(c["page"], c["chunk_id"]) for c in selected}

        self.assertEqual(set(allowed), selected_keys)

    def test_chunks_are_deduplicated_by_page_and_chunk_id(self):
        """Re-indexing without clearing leaves two points per chunk."""
        doubled = self._chunks() + self._chunks()

        unique = pipeline.ordered_unique_chunks(doubled)

        self.assertEqual(len(unique), len(CHUNKS_A))
        self.assertEqual(
            [(c["page"], c["chunk_id"]) for c in unique],
            sorted((p, c) for p, c, _ in CHUNKS_A),
        )

    def test_chunks_are_returned_in_reading_order(self):
        shuffled = self._chunks(list(reversed(CHUNKS_A)))

        ordered = pipeline.ordered_unique_chunks(shuffled)

        keys = [(c["page"], c["chunk_id"]) for c in ordered]
        self.assertEqual(keys, sorted(keys))

    def test_uncitable_chunks_are_dropped(self):
        bad = [
            {"text": "no page", "chunk_id": 0},
            {"text": "no chunk", "page": 1},
            {"text": "bad page", "page": "x", "chunk_id": 0},
            {"text": "zero page", "page": 0, "chunk_id": 0},
            {"text": "", "page": 2, "chunk_id": 0},
        ]

        self.assertEqual(pipeline.ordered_unique_chunks(bad), [])

    def test_selection_is_deterministic(self):
        chunks = self._chunks()
        first = pipeline.select_context(chunks, budget=90)
        second = pipeline.select_context(chunks, budget=90)

        self.assertEqual(
            [(c["page"], c["chunk_id"]) for c in first],
            [(c["page"], c["chunk_id"]) for c in second],
        )

    def test_total_pages_prefers_the_indexed_count(self):
        self.assertEqual(pipeline.resolve_total_pages(self._chunks(), 7), TOTAL_PAGES)

    def test_total_pages_never_falls_below_the_highest_supplied_page(self):
        chunks = [{"page": 12, "chunk_id": 0, "total_pages": 3}]
        self.assertEqual(pipeline.resolve_total_pages(chunks, 12), 12)

    def test_total_pages_falls_back_when_the_payload_lacks_it(self):
        chunks = [{"page": 4, "chunk_id": 0}]
        self.assertEqual(pipeline.resolve_total_pages(chunks, 4), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
