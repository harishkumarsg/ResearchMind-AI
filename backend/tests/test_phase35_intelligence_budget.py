"""
Phase 3.5 follow-up — the intelligence completion budget.

Phase 3.2 added optional verbatim spans to the prompt and deliberately held
INTELLIGENCE_MAX_TOKENS at 3000. The number's own justification was that the
prompt suppressed the `quote` field; rule 8 was inverted to permit it and the
number stayed. Production settled the argument: ETASR_18859 returned
finish_reason=length with reasoning_tokens=1716, leaving 1284 of 3000 for a
JSON body that needs more. The run was discarded AFTER a quota unit was spent.

What these tests pin:

  * the budget is 5000, and the endpoint passes the SYMBOLIC constant rather
    than a literal, so the two can never drift;
  * /paper-relationship stays at 3000 — it emits no `quote` field and is
    genuinely smaller, so it is not implicated;
  * truncation is still handled correctly, which is the gap that let this
    reach production: the intelligence suite's fake generator hard-codes
    finish_reason="stop", so IncompleteGeneration was never exercised here
    even though the sibling relationship endpoint has exactly this test;
  * a truncated run persists NOTHING, charges EXACTLY ONE unit, and never
    reaches QuoteTally — the tally instruments validation decisions, and a
    truncated completion never gets that far;
  * a full-size quote-bearing response still reaches validation and the
    tally, with every gate unchanged.

Fully offline: Qdrant, Groq and the database are all fakes. No provider call,
no network, no production data.
"""
import ast
import hashlib
import io
import json
import os
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.agents.qa_agent import GenerationResult
from app.core.auth import get_current_owner_id
from app.core.providers import GENERATION_INCOMPLETE, IncompleteGeneration
from app.db.models import Paper, PaperIntelligenceRow
from app.services.intelligence_schema import MAX_QUOTE_CHARS, SECTION_NAMES
from tests.sqlite_harness import attach_sqlite_db

import app.api.paper_intelligence as pipeline

MODULE_PATH = os.path.join(BACKEND_DIR, "app", "api", "paper_intelligence.py")

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PAPER_A = "11111111-1111-1111-1111-111111111111"
TOTAL_PAGES = 9

#: Long enough that a MAX_QUOTE_CHARS span is a genuine substring — the
#: full-size case below quotes 200 characters out of each of these.
CHUNK_3_0 = (
    "METHODOLOGY. A two-stage hybrid framework is proposed for printed circuit "
    "board defect detection, combining a lightweight region proposal stage with "
    "a zero-shot classification stage so that unseen defect categories do not "
    "require retraining, and the whole pipeline remains deployable on the "
    "inspection line without a dedicated accelerator."
)
CHUNK_7_0 = (
    "LIMITATIONS. The approach depends on prompt quality and careful tuning of "
    "the proposal threshold, and the authors note that performance degrades on "
    "very low contrast solder defects where the proposal stage misses the "
    "region entirely, so recall rather than precision is the binding constraint "
    "in the reported failure cases."
)
CHUNKS = [(3, 0, CHUNK_3_0), (7, 0, CHUNK_7_0)]

SPAN_3_0 = CHUNK_3_0[:MAX_QUOTE_CHARS]
SPAN_7_0 = CHUNK_7_0[:MAX_QUOTE_CHARS]


def _point(page, chunk_id, text):
    point = MagicMock()
    point.id = uuid.uuid4().hex
    point.payload = {
        "text": text,
        "page": page,
        "chunk_id": chunk_id,
        "total_pages": TOTAL_PAGES,
        "paper_id": PAPER_A,
        "owner_id": OWNER_A,
        "paper": "Paper A",
        "source": "a.pdf",
    }
    return point


def _full_size_payload():
    """Ten answered sections, two quoted entries each: 20 offered spans, the
    worst case the raised budget has to cover."""
    section = {
        "status": "answered",
        "summary": "A grounded summary of this section, kept to a sentence.",
        "evidence": [
            {"page": 3, "chunk_id": 0, "quote": SPAN_3_0},
            {"page": 7, "chunk_id": 0, "quote": SPAN_7_0},
        ],
    }
    return {name: dict(section) for name in SECTION_NAMES}


class BudgetTestCase(unittest.TestCase):
    """Drives the real endpoint with fake Qdrant, fake Groq, real DB."""

    def setUp(self):
        self.factory = attach_sqlite_db(self)
        with self.factory() as session:
            session.add(
                Paper(
                    id=uuid.UUID(PAPER_A),
                    owner_id=uuid.UUID(OWNER_A),
                    title="Paper A",
                    content_hash="h" * 64,
                    storage_path=f"{OWNER_A}/{PAPER_A}/original.pdf",
                    file_size_bytes=1024,
                    status="indexed",
                )
            )
            session.commit()

        self.qdrant = MagicMock()
        self.points = [_point(p, c, t) for p, c, t in CHUNKS]
        self.qdrant.scroll.side_effect = lambda **kw: (list(self.points), None)
        qpatch = patch.object(pipeline, "client", self.qdrant)
        qpatch.start()
        self.addCleanup(qpatch.stop)

        # Fake Groq. `raises` lets a test make generate() fail the way
        # production did instead of returning text.
        self.raises = None
        self.model_text = json.dumps(_full_size_payload())
        self.generate_calls = []

        def fake_generate(question, context, **kwargs):
            self.generate_calls.append({"context": context, **kwargs})
            if self.raises is not None:
                raise self.raises
            return GenerationResult(text=self.model_text, finish_reason="stop")

        self.generate = MagicMock(side_effect=fake_generate)
        gpatch = patch.object(pipeline, "generate", self.generate)
        gpatch.start()
        self.addCleanup(gpatch.stop)

        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    def post(self):
        return self.client.post("/paper-intelligence", params={"paper_id": PAPER_A})

    def post_capturing_stdout(self):
        """The tally reaches the world only through a log line, so the log is
        where 'did QuoteTally run' is actually answered."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            response = self.post()
        return response, buffer.getvalue()

    def rows(self):
        with self.factory() as session:
            return session.query(PaperIntelligenceRow).all()


# ----------------------------------------------------------------------
# 1-3. The constants
# ----------------------------------------------------------------------
class TestBudgetConstants(BudgetTestCase):

    def test_the_intelligence_budget_is_exactly_5000(self):
        self.assertEqual(pipeline.INTELLIGENCE_MAX_TOKENS, 5000)

    def test_the_endpoint_passes_the_symbolic_constant_not_a_literal(self):
        """Two halves, and both are needed: the source must reference the
        NAME (so the call site cannot be bumped independently), and the value
        that actually arrives must equal the constant."""
        with open(MODULE_PATH, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("max_tokens=INTELLIGENCE_MAX_TOKENS", source)
        self.assertNotIn("max_tokens=5000", source)
        self.assertNotIn("max_tokens=3000", source)

        self.post()

        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(
            self.generate_calls[0]["max_tokens"], pipeline.INTELLIGENCE_MAX_TOKENS
        )

    def test_the_context_budget_and_quote_cap_did_not_move(self):
        self.assertEqual(pipeline.INTELLIGENCE_CONTEXT_CHARS, 12000)
        self.assertEqual(MAX_QUOTE_CHARS, 200)

    def test_the_relationship_budget_stays_at_3000(self):
        """Not implicated and deliberately untouched: a relationship object
        covers fewer sections and carries no `quote` field, so it is strictly
        smaller than the object that overflowed."""
        import app.api.paper_relationship as relationship

        self.assertEqual(relationship.RELATIONSHIP_MAX_TOKENS, 3000)

    def test_the_intelligence_budget_does_not_exceed_the_proven_report_budget(self):
        """5000 is REPORT_MAX_TOKENS, already proven against
        GROQ_TIMEOUT_SECONDS by the same model. A future raise past it has to
        argue with this assertion rather than slip through."""
        from app.api.research import REPORT_MAX_TOKENS

        self.assertEqual(pipeline.INTELLIGENCE_MAX_TOKENS, REPORT_MAX_TOKENS)


# ----------------------------------------------------------------------
# 4-7. Truncation: the path production actually took
# ----------------------------------------------------------------------
class TestTruncatedGeneration(BudgetTestCase):

    def test_a_truncated_generation_maps_to_generation_incomplete(self):
        self.raises = IncompleteGeneration("length")

        response = self.post()
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], GENERATION_INCOMPLETE)

    def test_the_truncation_message_is_authored_not_the_models_text(self):
        self.raises = IncompleteGeneration("length")

        message = self.post().json()["message"]

        self.assertEqual(message, "The answer could not be completed. Please try again.")
        self.assertNotIn(SPAN_3_0, message)
        self.assertNotIn(CHUNK_3_0, message)

    def test_a_truncated_generation_persists_nothing(self):
        self.raises = IncompleteGeneration("length")

        self.post()

        self.assertEqual(self.rows(), [], "a truncated generation persisted a row")

    def test_a_truncated_generation_does_not_overwrite_an_existing_row(self):
        """The production case exactly: a paper that already HAS an analysis
        must keep it when a regeneration truncates."""
        self.assertEqual(self.post().json()["status"], "success")
        before = self.rows()
        self.assertEqual(len(before), 1)
        stored_at, stored_json = before[0].generated_at, before[0].intelligence

        self.raises = IncompleteGeneration("length")
        self.assertEqual(self.post().json()["code"], GENERATION_INCOMPLETE)

        after = self.rows()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].generated_at, stored_at)
        self.assertEqual(after[0].intelligence, stored_json)

    def test_quotetally_does_not_execute_on_truncation(self):
        """The tally counts validation decisions. generate() raises before
        validate_intelligence() is reached, so no counter moves and the spans
        line must NOT appear."""
        self.raises = IncompleteGeneration("length")

        _, logged = self.post_capturing_stdout()

        self.assertNotIn("[paper-intelligence] spans", logged)
        self.assertNotIn("offered=", logged)
        self.assertNotIn("not_verbatim=", logged)

    def test_the_validator_is_never_called_on_truncation(self):
        self.raises = IncompleteGeneration("length")

        with patch.object(
            pipeline, "validate_intelligence", wraps=pipeline.validate_intelligence
        ) as validator:
            self.post()

        validator.assert_not_called()

    def test_truncation_charges_exactly_one_unit(self):
        """It charged one in production and that is correct: charge_ai_unit()
        sits immediately before the provider call, and the provider WAS
        called. Pinned so it stays deliberate rather than incidental."""
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
            self.raises = IncompleteGeneration("length")

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)
            self.assertEqual(self.post().json()["code"], GENERATION_INCOMPLETE)
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_truncation_does_not_retry_the_provider(self):
        self.raises = IncompleteGeneration("length")

        self.post()

        self.assertEqual(self.generate.call_count, 1)


# ----------------------------------------------------------------------
# 8-9. The full-size success the raised budget exists to allow
# ----------------------------------------------------------------------
class TestFullSizeSuccess(BudgetTestCase):

    def test_a_full_size_quote_bearing_response_validates_and_persists(self):
        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(len(body["intelligence"]), 10)
        self.assertEqual(len(self.rows()), 1)

    def test_every_offered_span_survives_and_reaches_the_tally(self):
        response, logged = self.post_capturing_stdout()
        intelligence = response.json()["intelligence"]

        quotes = [
            item["quote"]
            for section in intelligence.values()
            for item in section["evidence"]
            if item.get("quote")
        ]
        self.assertEqual(len(quotes), 20)

        self.assertIn("[paper-intelligence] spans", logged)
        self.assertIn("offered=20", logged)
        self.assertIn("kept=20", logged)
        self.assertIn("not_verbatim=0", logged)

    def test_a_maximum_length_span_is_kept(self):
        """Exactly MAX_QUOTE_CHARS is inside the cap, not over it."""
        intelligence = self.post().json()["intelligence"]
        quote = intelligence["methodology"]["evidence"][0]["quote"]

        self.assertEqual(len(quote), MAX_QUOTE_CHARS)
        self.assertEqual(quote, SPAN_3_0)
        self.assertIn(quote, CHUNK_3_0)

    def test_an_over_length_span_is_still_dropped(self):
        """The raised budget did not relax the cap."""
        payload = _full_size_payload()
        payload["methodology"]["evidence"] = [
            {"page": 3, "chunk_id": 0, "quote": CHUNK_3_0[: MAX_QUOTE_CHARS + 1]}
        ]
        self.model_text = json.dumps(payload)

        response, logged = self.post_capturing_stdout()
        item = response.json()["intelligence"]["methodology"]["evidence"][0]

        self.assertIsNone(item["quote"])
        self.assertEqual((item["page"], item["chunk_id"]), (3, 0))
        self.assertIn("too_long=1", logged)

    def test_a_non_verbatim_span_is_still_dropped(self):
        payload = _full_size_payload()
        payload["methodology"]["evidence"] = [
            {"page": 3, "chunk_id": 0, "quote": "a paraphrase of the methodology"}
        ]
        self.model_text = json.dumps(payload)

        response, logged = self.post_capturing_stdout()
        item = response.json()["intelligence"]["methodology"]["evidence"][0]

        self.assertIsNone(item["quote"])
        self.assertIn("not_verbatim=1", logged)

    def test_an_omitted_span_is_still_absent_not_dropped(self):
        payload = _full_size_payload()
        payload["methodology"]["evidence"] = [{"page": 3, "chunk_id": 0}]
        self.model_text = json.dumps(payload)

        _, logged = self.post_capturing_stdout()

        self.assertIn("absent=1", logged)

    def test_the_log_line_still_carries_no_quote_or_chunk_text(self):
        _, logged = self.post_capturing_stdout()

        spans_line = [
            line for line in logged.splitlines()
            if "[paper-intelligence] spans" in line
        ][0]
        for leak in (SPAN_3_0, SPAN_7_0, CHUNK_3_0, CHUNK_7_0, "METHODOLOGY"):
            self.assertNotIn(leak, spans_line)


# ----------------------------------------------------------------------
# 10. The prompt did not move
# ----------------------------------------------------------------------
class TestPromptUnchanged(unittest.TestCase):
    """The budget was raised INSTEAD of changing the prompt. If a future
    change touches the prompt, that is a decision to make deliberately — not
    something that should ride along with a budget edit."""

    #: sha256 of the AST source segment of INTELLIGENCE_SYSTEM_PROMPT as
    #: published in f8804b9. Update it only together with an intentional,
    #: reviewed prompt change.
    PUBLISHED_PROMPT_SHA256 = (
        "38dced12bf393576424f57e281f55facdc1e0dc22d9ea864d5daa26d18e77a92"
    )

    def setUp(self):
        with open(MODULE_PATH, encoding="utf-8") as handle:
            self.source = handle.read()
        for node in ast.walk(ast.parse(self.source)):
            if (
                isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "INTELLIGENCE_SYSTEM_PROMPT"
            ):
                self.prompt = ast.get_source_segment(self.source, node)
                break
        else:
            self.fail("INTELLIGENCE_SYSTEM_PROMPT not found")

    def test_the_prompt_is_byte_identical_to_the_published_version(self):
        digest = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.assertEqual(
            digest,
            self.PUBLISHED_PROMPT_SHA256,
            "The system prompt changed. The budget fix must not touch it — if "
            "this is an intentional prompt change, update "
            "PUBLISHED_PROMPT_SHA256 in the same commit.",
        )

    def test_rule_8_still_permits_an_optional_quote(self):
        self.assertIn('8. Each evidence entry MAY include a "quote"', self.prompt)
        self.assertNotIn('Do NOT include a "quote"', self.prompt)

    def test_the_quote_rules_still_demand_verbatim_single_block_spans(self):
        for phrase in ("VERBATIM", "Do not paraphrase", "Never combine", "OMIT"):
            self.assertIn(phrase, self.prompt)

    def test_the_stale_justification_was_removed_from_the_budget_comment(self):
        """The old comment claimed the prompt suppressed the quote field,
        which stopped being true in Phase 3.2. A wrong comment about the very
        number this change fixes would be the next reader's trap."""
        self.assertNotIn("suppresses the optional `quote` field", self.source)
        self.assertIn("reasoning_tokens=1716", self.source)


if __name__ == "__main__":
    unittest.main()
