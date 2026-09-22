"""
Phase 2B step 4 — GET /paper-intelligence, the read path.

Reading a stored analysis must be cheap and boring: a database lookup,
owner-scoped, with no provider anywhere near it. The tests that matter
most are the ones proving what the endpoint does NOT do —

  * it never generates, so opening a paper cannot bill a generation;
  * it never calls Groq, Voyage, Qdrant or Storage;
  * it returns the same 404 for "no such paper" and "someone else's
    paper", so the response cannot be used to probe another account.

Fully offline: the database is per-test in-memory SQLite through the
existing harness, and every provider client is left unpatched precisely
so that touching one would fail loudly.
"""
import ast
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app.db.session as session_module
from app.core.auth import get_current_owner_id
from app.db.models import Paper, PaperIntelligenceRow
from app.services.intelligence_schema import SECTION_NAMES, validate_intelligence
from app.services.paper_intelligence_store import save_intelligence
from tests.sqlite_harness import attach_sqlite_db

import app.api.paper_intelligence as pipeline

MODULE_PATH = os.path.join(BACKEND_DIR, "app", "api", "paper_intelligence.py")

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"
PAPER_A2 = "33333333-3333-3333-3333-333333333333"
UNKNOWN_PAPER = "99999999-9999-9999-9999-999999999999"

TEST_MODEL = "test-model/step4"
GENERATED_AT = datetime(2026, 9, 22, 11, 30, 0, tzinfo=timezone.utc)

ALLOWED = {
    (1, 0): "We address defect detection in PCB inspection.",
    (3, 0): "METHODOLOGY. A two-stage hybrid framework is proposed.",
    (3, 1): "Stage two performs zero-shot classification.",
    (7, 0): "LIMITATIONS. The approach depends on prompt quality.",
}
TOTAL_PAGES = 9


def _section(status="answered", summary="A summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def _validated(**overrides):
    payload = {name: _section() for name in SECTION_NAMES}
    payload.update(overrides)
    return validate_intelligence(
        payload, allowed_evidence=ALLOWED, total_pages=TOTAL_PAGES
    )


class ReadTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self._seed_paper(OWNER_A, PAPER_A, "Paper A")
        self._seed_paper(OWNER_A, PAPER_A2, "Paper A second")
        self._seed_paper(OWNER_B, PAPER_B, "Paper B")

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

    def _store(self, owner_id, paper_id, **kw):
        return save_intelligence(
            owner_id,
            paper_id,
            kw.pop("intelligence", None) or _validated(),
            model=kw.pop("model", TEST_MODEL),
            generated_at=kw.pop("generated_at", GENERATED_AT),
        )

    def get(self, paper_id=PAPER_A, **params):
        return self.client.get(
            "/paper-intelligence", params={"paper_id": paper_id, **params}
        )

    def rows(self):
        with self.factory() as session:
            return session.query(PaperIntelligenceRow).all()


# ----------------------------------------------------------------------
# 1, 6, 7. The stored object comes back intact
# ----------------------------------------------------------------------
class TestReadsStoredIntelligence(ReadTestCase):

    def test_owner_with_stored_intelligence_gets_200(self):
        self._store(OWNER_A, PAPER_A)

        r = self.get()

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["paper_id"], PAPER_A)
        self.assertEqual(body["paper"], "Paper A")
        self.assertEqual(body["model"], TEST_MODEL)
        self.assertEqual(body["schema_version"], "1")
        self.assertFalse(body["superseded"])
        self.assertTrue(body["generated_at"].startswith("2026-09-22T11:30"))

    def test_response_carries_exactly_the_ten_sections(self):
        self._store(OWNER_A, PAPER_A)

        intelligence = self.get().json()["intelligence"]

        self.assertEqual(sorted(intelligence), sorted(SECTION_NAMES))
        self.assertEqual(len(intelligence), 10)

    def test_status_summary_and_evidence_shape_is_preserved(self):
        self._store(
            OWNER_A,
            PAPER_A,
            intelligence=_validated(
                key_results=_section(
                    summary="Precision rose to 0.94.",
                    evidence=[
                        {"page": 3, "chunk_id": 1},
                        {"page": 7, "chunk_id": 0, "quote": ALLOWED[(7, 0)]},
                    ],
                ),
                reproducibility=_section(status="not_specified", summary=None),
            ),
        )

        intelligence = self.get().json()["intelligence"]

        kr = intelligence["key_results"]
        self.assertEqual(kr["status"], "answered")
        self.assertEqual(kr["summary"], "Precision rose to 0.94.")
        self.assertEqual(
            [(e["page"], e["chunk_id"]) for e in kr["evidence"]], [(3, 1), (7, 0)]
        )
        self.assertIsNone(kr["evidence"][0]["quote"])
        self.assertEqual(kr["evidence"][1]["quote"], ALLOWED[(7, 0)])

        rp = intelligence["reproducibility"]
        self.assertEqual(rp["status"], "not_specified")
        self.assertIsNone(rp["summary"])
        self.assertEqual(rp["evidence"], [])

    def test_every_section_reports_a_known_status(self):
        self._store(OWNER_A, PAPER_A)

        intelligence = self.get().json()["intelligence"]

        for name, section in intelligence.items():
            with self.subTest(section=name):
                self.assertIn(section["status"], ("answered", "not_specified"))

    def test_reading_twice_returns_the_same_object(self):
        self._store(OWNER_A, PAPER_A)

        first = self.get().json()
        second = self.get().json()

        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)


# ----------------------------------------------------------------------
# 2. Nothing generated yet
# ----------------------------------------------------------------------
class TestNotGenerated(ReadTestCase):

    def test_owned_paper_without_intelligence_is_a_clean_empty_state(self):
        r = self.get()

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "not_generated")
        self.assertEqual(body["paper_id"], PAPER_A)
        self.assertEqual(body["paper"], "Paper A")
        self.assertIn("message", body)
        self.assertNotIn("intelligence", body)

    def test_not_generated_is_not_an_error_status(self):
        """The UI must be able to tell 'nothing yet' from 'it broke'."""
        self.assertNotEqual(self.get().json()["status"], "error")

    def test_reading_does_not_generate(self):
        self.get()
        self.get()

        self.assertEqual(self.rows(), [])

    def test_one_paper_having_intelligence_does_not_leak_to_another(self):
        self._store(OWNER_A, PAPER_A)

        other = self.get(paper_id=PAPER_A2).json()

        self.assertEqual(other["status"], "not_generated")
        self.assertEqual(other["paper_id"], PAPER_A2)


# ----------------------------------------------------------------------
# 3, 4, 11. Ownership and isolation
# ----------------------------------------------------------------------
class TestOwnershipAndIsolation(ReadTestCase):

    def test_nonexistent_paper_is_404(self):
        r = self.get(paper_id=UNKNOWN_PAPER)

        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["message"], pipeline.NOT_FOUND_MESSAGE)

    def test_another_owners_paper_is_404(self):
        self._store(OWNER_B, PAPER_B)

        r = self.get(paper_id=PAPER_B)

        self.assertEqual(r.status_code, 404)
        self.assertNotIn("intelligence", r.json())

    def test_foreign_and_unknown_papers_are_indistinguishable(self):
        self._store(OWNER_B, PAPER_B)

        foreign = self.get(paper_id=PAPER_B)
        unknown = self.get(paper_id=UNKNOWN_PAPER)

        self.assertEqual(foreign.status_code, unknown.status_code)
        self.assertEqual(foreign.json(), unknown.json())

    def test_malformed_paper_id_is_404_not_a_crash(self):
        for bad in ("not-a-uuid", "", "1; drop table papers"):
            with self.subTest(paper_id=bad):
                r = self.get(paper_id=bad)
                self.assertIn(r.status_code, (404, 422))

    def test_each_owner_sees_only_their_own(self):
        self._store(OWNER_A, PAPER_A, model="owner-a-model")
        self._store(OWNER_B, PAPER_B, model="owner-b-model")

        self.assertEqual(self.get().json()["model"], "owner-a-model")

        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_B
        self.assertEqual(self.get(paper_id=PAPER_B).json()["model"], "owner-b-model")
        self.assertEqual(self.get(paper_id=PAPER_A).status_code, 404)

    def test_client_supplied_owner_id_cannot_override_the_jwt(self):
        self._store(OWNER_B, PAPER_B)

        seen = []
        real_new_session = session_module._new_session

        def spy(owner_id):
            seen.append(owner_id)
            return real_new_session(owner_id)

        with patch.object(session_module, "_new_session", side_effect=spy):
            r = self.get(paper_id=PAPER_B, owner_id=OWNER_B, user_id=OWNER_B)

        self.assertEqual(r.status_code, 404)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {OWNER_A})

    def test_the_route_declares_no_owner_parameter(self):
        """Structural: a client cannot name an owner because the handler
        has no such parameter to bind."""
        import inspect

        params = inspect.signature(pipeline.read_paper_intelligence).parameters
        self.assertEqual(sorted(params), ["owner_id", "paper_id", "response"])
        # owner_id is a dependency default, not a bindable query parameter.
        self.assertTrue(hasattr(params["owner_id"].default, "dependency"))

    def test_owner_id_is_never_returned_to_the_client(self):
        self._store(OWNER_A, PAPER_A)

        rendered = json.dumps(self.get().json())

        self.assertNotIn(OWNER_A, rendered)
        self.assertNotIn("owner_id", rendered)


# ----------------------------------------------------------------------
# 5. Authentication
# ----------------------------------------------------------------------
class TestAuthenticationRequired(unittest.TestCase):
    """No dependency override here: the real auth dependency runs."""

    def setUp(self):
        self.factory = attach_sqlite_db(self)
        from fastapi.testclient import TestClient
        from app.main import app

        app.dependency_overrides.clear()
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_unauthenticated_read_is_rejected(self):
        r = self.client.get("/paper-intelligence", params={"paper_id": PAPER_A})

        self.assertEqual(r.status_code, 401)

    def test_a_bogus_bearer_token_is_rejected(self):
        r = self.client.get(
            "/paper-intelligence",
            params={"paper_id": PAPER_A},
            headers={"Authorization": "Bearer not-a-real-token"},
        )

        self.assertEqual(r.status_code, 401)

    def test_rejection_leaks_no_intelligence(self):
        r = self.client.get("/paper-intelligence", params={"paper_id": PAPER_A})

        self.assertNotIn("intelligence", r.text)


# ----------------------------------------------------------------------
# 8, 9, 10. No provider, no Qdrant, no Storage
# ----------------------------------------------------------------------
class TestReadTouchesNoProvider(ReadTestCase):
    """The provider clients are deliberately NOT mocked in this class —
    they are booby-trapped, so any contact fails the test rather than
    being silently absorbed by a stub."""

    def test_read_makes_no_qdrant_call(self):
        self._store(OWNER_A, PAPER_A)

        trap = MagicMock()
        trap.scroll.side_effect = AssertionError("GET called Qdrant scroll")
        trap.query_points.side_effect = AssertionError("GET called Qdrant search")

        with patch.object(pipeline, "client", trap):
            body = self.get().json()

        self.assertEqual(body["status"], "success")
        trap.scroll.assert_not_called()
        trap.query_points.assert_not_called()

    def test_read_makes_no_groq_call(self):
        self._store(OWNER_A, PAPER_A)

        trap = MagicMock(side_effect=AssertionError("GET called the model"))

        with patch.object(pipeline, "generate", trap):
            body = self.get().json()

        self.assertEqual(body["status"], "success")
        trap.assert_not_called()

    def test_read_makes_no_storage_call(self):
        self._store(OWNER_A, PAPER_A)

        from app.services import storage

        with patch.object(
            storage, "fetch_pdf", side_effect=AssertionError("GET called Storage")
        ) as trap:
            self.get()

        trap.assert_not_called()

    def test_read_opens_no_socket(self):
        """The strongest form: the handler run with the socket
        constructor booby-trapped.

        Called directly rather than through TestClient on purpose —
        Starlette's test client spins up an asyncio event loop, and on
        Windows ProactorEventLoop builds its own self-pipe from a real
        socket. Going through it would trip the trap on the test
        harness's plumbing rather than on anything the read path did.
        SQLite is in-memory, so the handler itself needs no socket at
        all.
        """
        import socket

        from fastapi import Response as FastAPIResponse

        self._store(OWNER_A, PAPER_A)

        def explode(*args, **kwargs):
            raise AssertionError("the read path opened a network socket")

        with patch.object(socket, "socket", explode):
            body = pipeline.read_paper_intelligence(
                paper_id=PAPER_A,
                response=FastAPIResponse(),
                owner_id=OWNER_A,
            )

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["paper_id"], PAPER_A)

    def test_read_consumes_no_ai_generation_quota(self):
        """CHEAP_READ, not AI_GENERATION: opening a paper must never eat
        into the daily generation allowance."""
        from app.core import limits as limits_config
        from app.core.limits import enforcement_enabled
        from app.core.quota import peek

        self._store(OWNER_A, PAPER_A)

        with patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "READ_RATE_PER_MINUTE": "100",
                "AI_QUOTA_PER_DAY": "50",
            },
            clear=False,
        ):
            assert enforcement_enabled(), "enforcement must be on for this test"

            for _ in range(3):
                self.assertEqual(self.get().json()["status"], "success")

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_the_read_handler_never_calls_the_generation_path(self):
        """Structural, parsed not grepped: the GET handler's body must
        not mention generate(), save_intelligence() or the Qdrant
        fetch."""
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        handler = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "read_paper_intelligence"
        )
        called = {
            n.func.id
            for n in ast.walk(handler)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        } | {
            n.func.attr
            for n in ast.walk(handler)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

        for forbidden in (
            "generate",
            "save_intelligence",
            "fetch_paper_chunks",
            "charge_ai_unit",
            "validate_intelligence",
            "encode_query",
            "scroll",
        ):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called)

        self.assertIn("get_intelligence", called)
        self.assertIn("resolve_paper", called)


# ----------------------------------------------------------------------
# A stored row that no longer parses
# ----------------------------------------------------------------------
class TestUnreadableStoredRow(ReadTestCase):

    def test_a_corrupted_row_is_reported_not_repaired(self):
        self._store(OWNER_A, PAPER_A)

        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            broken = dict(row.intelligence)
            broken.pop("limitations")
            row.intelligence = broken
            session.commit()

        body = self.get().json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "invalid_structure")
        self.assertEqual(body["message"], pipeline.UNREADABLE_MESSAGE)

        # The row is left exactly as it was — no silent rewrite.
        with self.factory() as session:
            still = session.query(PaperIntelligenceRow).one()
            self.assertNotIn("limitations", still.intelligence)

    def test_a_corrupted_row_leaks_no_stored_text(self):
        self._store(
            OWNER_A,
            PAPER_A,
            intelligence=_validated(
                dataset=_section(summary="SECRET-MARKER-IN-STORED-TEXT.")
            ),
        )

        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            broken = dict(row.intelligence)
            broken.pop("limitations")
            row.intelligence = broken
            session.commit()

        self.assertNotIn("SECRET-MARKER", json.dumps(self.get().json()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
