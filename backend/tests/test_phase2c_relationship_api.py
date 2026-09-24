"""
Phase 2C step 2C-5 — GET/POST /paper-relationship.

The tests that matter most here are the ones proving what the endpoints
do NOT do:

  * GET never generates, never writes, and never touches a provider, so
    opening a comparison cannot bill a unit;
  * POST calls Groq at most once, and only after every deterministic
    rejection has been passed;
  * a relationship that failed validation is never persisted;
  * Paper A's evidence and Paper B's evidence are never interchangeable,
    even when both papers contain the same (page, chunk_id);
  * a foreign or unknown paper produces the same 404 whichever way it is
    probed, so the response cannot be used to learn what another account
    holds.

Fully offline. The database is a per-test in-memory SQLite through the
existing harness, so the REAL quota counters, the REAL stores and the
REAL session_scope all run. Only three boundaries are mocked: the Qdrant
client (with a fake that honours the owner+paper filter itself, because a
fake that returned everything would make the isolation tests prove
nothing), the Groq generation call, and Voyage/reranker — the last two
purely so that touching them fails loudly.
"""
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

from app.core.auth import get_current_owner_id
from app.core.providers import (
    GENERATION_INCOMPLETE,
    INTERNAL_ERROR,
    PROVIDER_TIMEOUT,
    IncompleteGeneration,
    ProviderTimeout,
)
from app.db.models import Paper, PaperRelationshipRow
from app.services.intelligence_schema import SECTION_NAMES, validate_intelligence
from app.services.paper_intelligence_store import save_intelligence
from app.services.paper_relationship_store import get_relationship, save_relationship
from app.services.relationship_schema import validate_relationship
from tests.sqlite_harness import attach_sqlite_db

import app.api.paper_relationship as pipeline
import app.services.paper_relationship_evidence as evidence_service

OWNER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

#: Deliberately ordered so PAPER_1 < PAPER_2 < PAPER_3 as uuids, which
#: makes the canonical pair predictable in every assertion below.
PAPER_1 = "11111111-1111-1111-1111-111111111111"
PAPER_2 = "22222222-2222-2222-2222-222222222222"
PAPER_3 = "33333333-3333-3333-3333-333333333333"
FOREIGN = "ffffffff-ffff-ffff-ffff-ffffffffffff"
UNKNOWN = "99999999-9999-9999-9999-999999999999"

#: The two sections both fixture papers ground, in canonical order.
S1 = "methodology"
S2 = "key_results"
COMPARABLE = (S1, S2)

#: (3, 0) exists in BOTH papers with different text — the collision that
#: makes merged allowlists dangerous. (5, 0) is only Paper 1's and (7, 0)
#: only Paper 2's, which is what the cross-citation tests exploit.
P1_CHUNKS = {
    (3, 0): "PAPER ONE METHOD. A two-stage hybrid detector is proposed.",
    (5, 0): "PAPER ONE RESULTS. Accuracy reaches 94.1 percent on the held-out set.",
}
P2_CHUNKS = {
    (3, 0): "PAPER TWO METHOD. A single-stage transformer detector is proposed.",
    (7, 0): "PAPER TWO RESULTS. Accuracy reaches 91.8 percent under the same protocol.",
}

#: Indexed but cited by nothing. Proves rehydration fetches only the
#: chunks a section actually referenced, rather than the whole paper.
P1_UNCITED = {
    (9, 0): "PAPER ONE APPENDIX. No section of the stored analysis cites this.",
}

ALLOWED_FOR_INTELLIGENCE = {
    (3, 0): "seed",
    (5, 0): "seed",
    (7, 0): "seed",
}
TOTAL_PAGES = 10

TEST_MODEL = "test-model/2c5"
STAMP_1 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
STAMP_2 = datetime(2026, 9, 24, 10, 5, 0, tzinfo=timezone.utc)


def _utc(value):
    """A datetime as aware UTC.

    SQLite reads timestamptz values back NAIVE while Postgres returns them
    aware, so a value written in this process and a value re-read from the
    offline database compare unequal despite being the same instant. Every
    stamp these stores write is UTC, so labelling is correct — the same
    assumption _as_utc() makes in paper_relationship_store.py.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _Point:
    """A Qdrant point as the evidence service reads it: `.payload` only."""

    def __init__(self, payload):
        self.payload = payload


def _point(owner_id, paper_id, page, chunk_id, text):
    return _Point(
        {
            "owner_id": owner_id,
            "paper_id": paper_id,
            "page": page,
            "chunk_id": chunk_id,
            "text": text,
            "total_pages": TOTAL_PAGES,
        }
    )


def _points_for(owner_id, paper_id, chunks):
    return [
        _point(owner_id, paper_id, page, chunk_id, text)
        for (page, chunk_id), text in chunks.items()
    ]


def _section(status="answered", summary="A grounded summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def _intelligence(s2_evidence=None, **overrides):
    """A validated PaperIntelligence: two grounded sections, eight not
    specified, unless a test overrides one.

    `s2_evidence` exists because each paper must cite chunks that actually
    exist IN THAT PAPER. Both papers hold (3, 0), but key_results lives at
    (5, 0) in Paper 1 and (7, 0) in Paper 2 — citing one paper's page from
    the other would leave the section with no rehydratable evidence and
    silently drop it from the comparison.
    """
    payload = {
        name: _section(status="not_specified", summary=None) for name in SECTION_NAMES
    }
    payload[S1] = _section(evidence=[{"page": 3, "chunk_id": 0}])
    payload[S2] = _section(
        evidence=s2_evidence
        if s2_evidence is not None
        else [{"page": 5, "chunk_id": 0}]
    )
    payload.update(overrides)
    return validate_intelligence(
        payload, allowed_evidence=ALLOWED_FOR_INTELLIGENCE, total_pages=TOTAL_PAGES
    )


def _intelligence_p1():
    """Paper 1's analysis: methodology at (3, 0), key_results at (5, 0)."""
    return _intelligence()


def _intelligence_p2():
    """Paper 2's analysis: methodology at (3, 0), key_results at (7, 0)."""
    return _intelligence(s2_evidence=[{"page": 7, "chunk_id": 0}])


def _model_response(
    sections=COMPARABLE,
    relation="aligned",
    statement="Both papers report detector accuracy on the same protocol.",
    cites_a=None,
    cites_b=None,
):
    body = {}
    for name in sections:
        body[name] = {
            "relation": relation,
            "statement": statement,
            "cites_a": (
                cites_a if cites_a is not None else [{"page": 3, "chunk_id": 0}]
            ),
            "cites_b": (
                cites_b if cites_b is not None else [{"page": 3, "chunk_id": 0}]
            ),
        }
    return json.dumps({"sections": body})


class _Result:
    """A GenerationResult as the endpoint reads it."""

    def __init__(self, text, finish_reason="stop"):
        self.text = text
        self.finish_reason = finish_reason


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------
class RelationshipApiTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = attach_sqlite_db(self)

        self._seed_paper(OWNER_A, PAPER_1, "Paper One")
        self._seed_paper(OWNER_A, PAPER_2, "Paper Two")
        self._seed_paper(OWNER_A, PAPER_3, "Paper Three")
        self._seed_paper(OWNER_B, FOREIGN, "Somebody Else's Paper")

        # Qdrant: honours the owner_id + paper_id filter itself.
        self.points = (
            _points_for(OWNER_A, PAPER_1, {**P1_CHUNKS, **P1_UNCITED})
            + _points_for(OWNER_A, PAPER_2, P2_CHUNKS)
        )
        self.scroll_calls = []

        def scroll(**kwargs):
            self.scroll_calls.append(kwargs)
            conditions = {c.key: c.match.value for c in kwargs["scroll_filter"].must}
            matched = [
                p
                for p in self.points
                if isinstance(getattr(p, "payload", None), dict)
                and all(p.payload.get(k) == v for k, v in conditions.items())
            ]
            return matched, None

        self.qdrant = MagicMock()
        self.qdrant.scroll.side_effect = scroll
        self._patch(patch.object(evidence_service, "client", self.qdrant))

        # Groq: one call, returning a valid response unless a test says
        # otherwise.
        self.generate = MagicMock(return_value=_Result(_model_response()))
        self._patch(patch.object(pipeline, "generate", self.generate))

        # Voyage and the reranker are patched ONLY so that reaching them
        # is a loud failure rather than a live call.
        self.encode_query = MagicMock()
        self.create_embeddings = MagicMock()
        self.rerank = MagicMock()
        self._patch(patch("app.rag.embedder.encode_query", self.encode_query))
        self._patch(patch("app.rag.embedder.create_embeddings", self.create_embeddings))
        self._patch(patch("app.rag.reranker.rerank_results", self.rerank))

        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

        from app.core.rate_limit import get_limiter

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_paper(self, owner_id, paper_id, title):
        with self.factory() as session:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(owner_id),
                    title=title,
                    content_hash=f"{paper_id[:8]}" + "h" * 56,
                    storage_path=f"{owner_id}/{paper_id}/original.pdf",
                    file_size_bytes=2048,
                    status="indexed",
                )
            )
            session.commit()

    # -- fixtures -------------------------------------------------------
    def given_intelligence(
        self, paper_id=None, *, generated_at=None, intelligence=None, owner_id=OWNER_A
    ):
        return save_intelligence(
            owner_id,
            paper_id,
            intelligence if intelligence is not None else _intelligence(),
            model=TEST_MODEL,
            generated_at=generated_at,
        )

    def given_both_analysed(self, stamp_1=STAMP_1, stamp_2=STAMP_2):
        self.given_intelligence(
            PAPER_1, generated_at=stamp_1, intelligence=_intelligence_p1()
        )
        self.given_intelligence(
            PAPER_2, generated_at=stamp_2, intelligence=_intelligence_p2()
        )

    def given_relationship(self, *, stamp_1=STAMP_1, stamp_2=STAMP_2, sections=COMPARABLE):
        relationship = validate_relationship(
            _model_response(sections=sections),
            comparable_sections=sections,
            allowed_evidence_a={pair: "t" for pair in P1_CHUNKS},
            allowed_evidence_b={pair: "t" for pair in P2_CHUNKS},
        )
        return save_relationship(
            OWNER_A,
            PAPER_1,
            PAPER_2,
            relationship,
            paper_a_generated_at=stamp_1,
            paper_b_generated_at=stamp_2,
            model=TEST_MODEL,
            generated_at=datetime(2026, 9, 24, 11, 0, 0, tzinfo=timezone.utc),
        )

    # -- requests -------------------------------------------------------
    def get(self, a=PAPER_1, b=PAPER_2, **extra):
        return self.client.get(
            "/paper-relationship",
            params={"paper_a_id": a, "paper_b_id": b, **extra},
        )

    def post(self, a=PAPER_1, b=PAPER_2, **extra):
        return self.client.post(
            "/paper-relationship",
            params={"paper_a_id": a, "paper_b_id": b, **extra},
        )

    # -- assertions -----------------------------------------------------
    def stored_rows(self):
        with self.factory() as session:
            return session.query(PaperRelationshipRow).all()

    def assertNothingPersisted(self):
        self.assertEqual(
            len(self.stored_rows()), 0, "a relationship row was written and must not be"
        )

    def assertNoProviderCalls(self):
        self.generate.assert_not_called()
        self.encode_query.assert_not_called()
        self.create_embeddings.assert_not_called()
        self.rerank.assert_not_called()

    def assertNoQdrant(self):
        self.qdrant.scroll.assert_not_called()


# ----------------------------------------------------------------------
# AUTH / OWNERSHIP
# ----------------------------------------------------------------------
class TestAuthAndOwnership(RelationshipApiTestCase):
    def test_unauthenticated_get_is_rejected(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.get().status_code, 401)
        self.assertNoProviderCalls()

    def test_unauthenticated_post_is_rejected(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.post().status_code, 401)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_owner_can_read_their_own_pair(self):
        self.given_both_analysed()
        self.given_relationship()

        body = self.get().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["paper_a_id"], PAPER_1)
        self.assertEqual(body["paper_b_id"], PAPER_2)

    def test_a_foreign_paper_in_the_pair_is_not_readable(self):
        body = self.get(b=FOREIGN)

        self.assertEqual(body.status_code, 404)
        self.assertEqual(body.json()["message"], pipeline.NOT_FOUND_MESSAGE)

    def test_a_foreign_paper_in_the_pair_cannot_be_generated_for(self):
        self.given_both_analysed()

        body = self.post(b=FOREIGN)

        self.assertEqual(body.status_code, 404)
        self.assertEqual(body.json()["message"], pipeline.NOT_FOUND_MESSAGE)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_a_foreign_paper_as_the_first_id_is_also_rejected(self):
        """Both positions are checked, not just the second."""
        self.assertEqual(self.post(a=FOREIGN, b=PAPER_1).status_code, 404)
        self.assertNoProviderCalls()

    def test_an_unknown_pair_answers_exactly_like_a_foreign_pair(self):
        """The 404 body must not distinguish "no such paper" from
        "somebody else's paper", or it becomes an existence oracle."""
        unknown = self.get(b=UNKNOWN)
        foreign = self.get(b=FOREIGN)

        self.assertEqual(unknown.status_code, foreign.status_code)
        self.assertEqual(unknown.json(), foreign.json())

    def test_a_malformed_id_answers_the_same_404(self):
        response = self.get(b="not-a-uuid")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], pipeline.NOT_FOUND_MESSAGE)

    def test_owner_id_comes_from_the_jwt_not_the_query(self):
        """A client-supplied owner_id is ignored: the route has no such
        parameter, so it cannot reach the store."""
        self.given_both_analysed()
        self.given_relationship()

        with_owner = self.get(owner_id=OWNER_B).json()
        without = self.get().json()

        self.assertEqual(with_owner, without)
        self.assertEqual(with_owner["status"], "success")

    def test_a_client_cannot_generate_as_another_owner(self):
        self.given_both_analysed()

        self.post(owner_id=OWNER_B)

        rows = self.stored_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].owner_id), OWNER_A)

    def test_another_owner_cannot_read_this_owners_relationship(self):
        self.given_both_analysed()
        self.given_relationship()

        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_B
        response = self.get()

        # OWNER_B does not own either paper, so resolution fails first and
        # no relationship metadata is disclosed at all.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], pipeline.NOT_FOUND_MESSAGE)
        self.assertNotIn("relationship", response.json())
        self.assertNotIn("generated_at", response.json())


# ----------------------------------------------------------------------
# GET
# ----------------------------------------------------------------------
class TestGet(RelationshipApiTestCase):
    def test_a_fresh_stored_relationship_is_returned(self):
        self.given_both_analysed()
        self.given_relationship()

        body = self.get().json()

        self.assertEqual(body["status"], "success")
        self.assertFalse(body["stale"])
        self.assertIsNone(body["stale_reason"])
        self.assertEqual(sorted(body["relationship"]["sections"]), sorted(COMPARABLE))

    def test_source_stamps_are_returned_alongside_the_relationship(self):
        self.given_both_analysed()
        stored = self.given_relationship()

        body = self.get().json()

        self.assertEqual(body["paper_a_generated_at"], STAMP_1.isoformat())
        self.assertEqual(body["paper_b_generated_at"], STAMP_2.isoformat())
        self.assertEqual(body["generated_at"], stored.generated_at.isoformat())
        self.assertNotEqual(body["generated_at"], body["paper_a_generated_at"])

    def test_either_order_finds_the_same_row(self):
        self.given_both_analysed()
        self.given_relationship()

        forward = self.get(a=PAPER_1, b=PAPER_2).json()
        reverse = self.get(a=PAPER_2, b=PAPER_1).json()

        self.assertEqual(forward, reverse)
        self.assertEqual(reverse["paper_a_id"], PAPER_1)

    def test_a_missing_relationship_is_a_clean_404(self):
        self.given_both_analysed()

        response = self.get()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], pipeline.NOT_GENERATED_CODE)

    def test_the_same_paper_twice_is_422(self):
        response = self.get(a=PAPER_1, b=PAPER_1)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], pipeline.SAME_PAPER_CODE)

    def test_get_makes_no_provider_call_and_no_qdrant_call(self):
        self.given_both_analysed()
        self.given_relationship()

        self.assertEqual(self.get().status_code, 200)

        self.assertNoProviderCalls()
        self.assertNoQdrant()

    def test_get_never_generates_even_when_nothing_is_stored(self):
        self.given_both_analysed()

        self.assertEqual(self.get().status_code, 404)

        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_get_never_writes_even_when_the_relationship_is_stale(self):
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=1))

        before = self.stored_rows()[0].updated_at
        body = self.get().json()

        self.assertTrue(body["stale"])
        self.assertEqual(self.stored_rows()[0].updated_at, before)
        self.assertNoProviderCalls()


class TestGetStaleness(RelationshipApiTestCase):
    def test_a_changed_source_stamp_marks_the_relationship_stale(self):
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(minutes=30))

        body = self.get().json()

        self.assertTrue(body["stale"])
        self.assertEqual(body["stale_reason"], pipeline.STALE_REASON_CHANGED)

    def test_either_side_moving_is_enough(self):
        self.given_both_analysed()
        self.given_relationship(stamp_2=STAMP_2 + timedelta(minutes=1))

        self.assertTrue(self.get().json()["stale"])

    def test_missing_current_intelligence_is_stale_and_says_so(self):
        """No analysis for one paper means the stored relationship compares
        claims that no longer exist. It is reported, never re-invented."""
        self.given_intelligence(PAPER_1, generated_at=STAMP_1)
        self.given_relationship()

        body = self.get().json()

        self.assertTrue(body["stale"])
        self.assertEqual(body["stale_reason"], pipeline.STALE_REASON_MISSING)
        # The stored relationship is still returned verbatim, not blanked.
        self.assertEqual(sorted(body["relationship"]["sections"]), sorted(COMPARABLE))

    def test_missing_intelligence_on_a_get_does_not_invent_a_relationship(self):
        self.given_intelligence(PAPER_1, generated_at=STAMP_1)

        response = self.get()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], pipeline.NOT_GENERATED_CODE)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_stale_is_machine_readable_not_prose(self):
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(minutes=5))

        body = self.get().json()

        self.assertIsInstance(body["stale"], bool)
        self.assertIn(
            body["stale_reason"],
            (pipeline.STALE_REASON_CHANGED, pipeline.STALE_REASON_MISSING),
        )


# ----------------------------------------------------------------------
# POST — deterministic guards, before any provider work
# ----------------------------------------------------------------------
class TestPostGuards(RelationshipApiTestCase):
    def test_the_same_paper_is_rejected_without_a_provider_call(self):
        self.given_both_analysed()

        response = self.post(a=PAPER_1, b=PAPER_1)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], pipeline.SAME_PAPER_CODE)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_the_same_foreign_paper_twice_is_404_not_422(self):
        """Ownership is checked before the self-pair check, so a foreign id
        never reveals itself through a different error code."""
        response = self.post(a=FOREIGN, b=FOREIGN)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], pipeline.NOT_FOUND_MESSAGE)

    def test_an_unknown_paper_is_rejected_without_a_provider_call(self):
        self.given_both_analysed()

        response = self.post(b=UNKNOWN)

        self.assertEqual(response.status_code, 404)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_missing_intelligence_on_one_side_does_not_call_groq(self):
        self.given_intelligence(PAPER_1, generated_at=STAMP_1)

        response = self.post()

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], pipeline.MISSING_INTELLIGENCE_CODE)
        self.assertTrue(body["paper_a_analysed"])
        self.assertFalse(body["paper_b_analysed"])
        self.assertNoProviderCalls()
        self.assertNoQdrant()
        self.assertNothingPersisted()

    def test_missing_intelligence_on_both_sides_does_not_call_groq(self):
        response = self.post()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], pipeline.MISSING_INTELLIGENCE_CODE)
        self.assertNoProviderCalls()
        self.assertNothingPersisted()

    def test_no_section_grounded_on_both_sides_does_not_call_groq(self):
        """Paper 2 grounds nothing, so there is no comparable section and
        nothing for the model to judge."""
        blank = {name: _section(status="not_specified", summary=None) for name in SECTION_NAMES}
        self.given_intelligence(PAPER_1, generated_at=STAMP_1)
        self.given_intelligence(
            PAPER_2,
            generated_at=STAMP_2,
            intelligence=validate_intelligence(
                blank,
                allowed_evidence=ALLOWED_FOR_INTELLIGENCE,
                total_pages=TOTAL_PAGES,
            ),
        )

        response = self.post()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], pipeline.NOT_COMPARABLE_CODE)
        self.assertEqual(response.json()["comparable_sections"], [])
        self.generate.assert_not_called()
        self.assertNothingPersisted()

    def test_a_section_grounded_on_only_one_side_is_not_asked_about(self):
        """Paper 1 grounds both sections, Paper 2 only the first."""
        only_s1 = {
            name: _section(status="not_specified", summary=None) for name in SECTION_NAMES
        }
        only_s1[S1] = _section(evidence=[{"page": 3, "chunk_id": 0}])

        self.given_intelligence(PAPER_1, generated_at=STAMP_1)
        self.given_intelligence(
            PAPER_2,
            generated_at=STAMP_2,
            intelligence=validate_intelligence(
                only_s1,
                allowed_evidence=ALLOWED_FOR_INTELLIGENCE,
                total_pages=TOTAL_PAGES,
            ),
        )
        self.generate.return_value = _Result(_model_response(sections=(S1,)))

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["comparable_sections"], [S1])
        self.assertEqual(list(body["relationship"]["sections"]), [S1])

    def test_evidence_that_no_longer_rehydrates_drops_the_section(self):
        """Paper 2's chunks are gone from Qdrant, so no section survives
        and the model is never asked to cite what it cannot see."""
        self.given_both_analysed()
        self.points = _points_for(OWNER_A, PAPER_1, P1_CHUNKS)

        response = self.post()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], pipeline.NOT_COMPARABLE_CODE)
        self.generate.assert_not_called()
        self.assertNothingPersisted()


# ----------------------------------------------------------------------
# POST — the fresh short-circuit
# ----------------------------------------------------------------------
class TestPostShortCircuit(RelationshipApiTestCase):
    def test_a_fresh_relationship_is_returned_without_regenerating(self):
        self.given_both_analysed()
        self.given_relationship()

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertFalse(body["regenerated"])
        self.generate.assert_not_called()
        self.assertNoQdrant()

    def test_a_fresh_relationship_costs_no_ai_unit(self):
        from app.core import limits as limits_config
        from app.core.quota import peek

        self.given_both_analysed()
        self.given_relationship()

        with patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "AI_RATE_PER_MINUTE": "100",
                "AI_QUOTA_PER_DAY": "50",
            },
            clear=False,
        ):
            self.post()
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_a_fresh_relationship_does_not_create_a_second_row(self):
        self.given_both_analysed()
        self.given_relationship()

        self.post()
        self.post()

        self.assertEqual(len(self.stored_rows()), 1)

    def test_a_stale_relationship_is_regenerated(self):
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=2))

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertTrue(body["regenerated"])
        self.generate.assert_called_once()
        # Still one row: a regeneration replaces in place.
        self.assertEqual(len(self.stored_rows()), 1)

    def test_regeneration_records_the_new_source_stamps(self):
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=2))

        body = self.post().json()

        self.assertEqual(body["paper_a_generated_at"], STAMP_1.isoformat())
        self.assertEqual(body["paper_b_generated_at"], STAMP_2.isoformat())
        self.assertFalse(body["stale"])


# ----------------------------------------------------------------------
# POST — evidence rehydration
# ----------------------------------------------------------------------
class TestPostEvidence(RelationshipApiTestCase):
    def test_only_the_two_owned_papers_are_scrolled(self):
        self.given_both_analysed()

        self.post()

        scoped = [
            {c.key: c.match.value for c in call["scroll_filter"].must}
            for call in self.scroll_calls
        ]
        self.assertEqual(len(scoped), 2)
        for conditions in scoped:
            self.assertEqual(conditions["owner_id"], OWNER_A)
            self.assertIn(conditions["paper_id"], {PAPER_1, PAPER_2})
        self.assertEqual(
            {c["paper_id"] for c in scoped}, {PAPER_1, PAPER_2}
        )

    def test_every_scroll_carries_both_owner_and_paper_filters(self):
        self.given_both_analysed()

        self.post()

        for call in self.scroll_calls:
            keys = {c.key for c in call["scroll_filter"].must}
            self.assertEqual(keys, {"owner_id", "paper_id"})

    def test_no_third_paper_is_ever_scrolled(self):
        self.given_both_analysed()
        self.given_intelligence(PAPER_3, generated_at=STAMP_1)

        self.post()

        scrolled = {
            c.match.value
            for call in self.scroll_calls
            for c in call["scroll_filter"].must
            if c.key == "paper_id"
        }
        self.assertNotIn(PAPER_3, scrolled)

    def test_retrieval_uses_scroll_only_never_search(self):
        """No query vector anywhere: that is what makes this exact
        retrieval rather than semantic search."""
        self.given_both_analysed()

        self.post()

        used = {name for name, _, _ in self.qdrant.mock_calls}
        self.assertEqual(used, {"scroll"})
        for call in self.scroll_calls:
            self.assertNotIn("query_vector", call)
            self.assertNotIn("query", call)
            self.assertFalse(call["with_vectors"])

    def test_no_voyage_embedding_and_no_reranker(self):
        self.given_both_analysed()

        self.post()

        self.encode_query.assert_not_called()
        self.create_embeddings.assert_not_called()
        self.rerank.assert_not_called()

    def test_the_context_contains_exactly_the_cited_chunks(self):
        self.given_both_analysed()

        self.post()

        context = self.generate.call_args[0][1]

        for text in (
            P1_CHUNKS[(3, 0)],
            P1_CHUNKS[(5, 0)],
            P2_CHUNKS[(3, 0)],
            P2_CHUNKS[(7, 0)],
        ):
            self.assertIn(text, context)

        # Indexed, owned, and in the same papers — but cited by no section,
        # so it is never fetched. This is what makes the retrieval "exact
        # cited chunks" rather than "the whole paper".
        self.assertNotIn(P1_UNCITED[(9, 0)], context)

    def test_the_prompt_never_contains_either_paper_title(self):
        """Title similarity is the single most tempting false signal, so
        the model is not shown titles at all."""
        self.given_both_analysed()

        self.post()

        question, context = self.generate.call_args[0][0], self.generate.call_args[0][1]
        prompt = self.generate.call_args[1]["system_prompt"]
        for title in ("Paper One", "Paper Two", "Somebody Else's Paper"):
            self.assertNotIn(title, context)
            self.assertNotIn(title, question)
            self.assertNotIn(title, prompt)

    def test_the_prompt_never_contains_ids_or_owner(self):
        self.given_both_analysed()

        self.post()

        context = self.generate.call_args[0][1]
        for value in (OWNER_A, OWNER_B, PAPER_1, PAPER_2):
            self.assertNotIn(value, context)

    def test_the_prompt_never_contains_the_stored_summary_prose(self):
        """Rehydration returns chunk text only, so model prose from the
        intelligence objects cannot reach the relationship context."""
        self.given_both_analysed()

        self.post()

        self.assertNotIn("A grounded summary.", self.generate.call_args[0][1])

    def test_paper_a_and_paper_b_evidence_are_separately_labelled(self):
        self.given_both_analysed()

        self.post()

        context = self.generate.call_args[0][1]
        self.assertIn("=== PAPER A EVIDENCE ===", context)
        self.assertIn("=== PAPER B EVIDENCE ===", context)
        # Paper A's block comes first, and each paper's text sits under its
        # own heading.
        a_at = context.index("=== PAPER A EVIDENCE ===")
        b_at = context.index("=== PAPER B EVIDENCE ===")
        self.assertLess(a_at, b_at)
        self.assertLess(context.index(P1_CHUNKS[(3, 0)]), b_at)
        self.assertGreater(context.index(P2_CHUNKS[(3, 0)]), b_at)

    def test_the_per_paper_budget_is_the_proven_number(self):
        self.assertEqual(pipeline.RELATIONSHIP_CONTEXT_CHARS_PER_PAPER, 12000)
        self.assertEqual(pipeline.RELATIONSHIP_MAX_TOKENS, 3000)

    def test_the_already_budgeted_context_is_not_cut_again(self):
        self.given_both_analysed()

        self.post()

        self.assertIsNone(self.generate.call_args[1]["max_context_chars"])


# ----------------------------------------------------------------------
# POST — validation is mandatory before persistence
# ----------------------------------------------------------------------
class TestPostValidation(RelationshipApiTestCase):
    def test_a_valid_generation_persists_exactly_one_row(self):
        self.given_both_analysed()

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        rows = self.stored_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].paper_a_id), PAPER_1)
        self.assertEqual(str(rows[0].paper_b_id), PAPER_2)
        self.generate.assert_called_once()

    def test_malformed_json_is_rejected_and_nothing_is_persisted(self):
        self.given_both_analysed()
        self.generate.return_value = _Result("this is not json at all")

        body = self.post().json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "malformed_json")
        self.assertNothingPersisted()

    def test_an_unknown_relation_value_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(_model_response(relation="contradicts"))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_not_comparable_carrying_evidence_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(relation="not_comparable")
        )

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_not_comparable_with_no_evidence_is_accepted(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(relation="not_comparable", cites_a=[], cites_b=[])
        )

        body = self.post().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(
            body["relationship"]["sections"][S1]["relation"], "not_comparable"
        )

    def test_a_grounded_relation_needs_evidence_from_paper_a(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(_model_response(cites_a=[]))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_a_grounded_relation_needs_evidence_from_paper_b(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(_model_response(cites_b=[]))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_all_three_grounded_relations_require_both_sides(self):
        for relation in ("aligned", "divergent", "complementary"):
            with self.subTest(relation=relation):
                for side in ("cites_a", "cites_b"):
                    self.setUp()
                    self.given_both_analysed()
                    self.generate.return_value = _Result(
                        _model_response(relation=relation, **{side: []})
                    )

                    body = self.post().json()

                    self.assertEqual(body["status"], "error")
                    self.assertNothingPersisted()

    def test_evidence_not_supplied_to_the_model_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(cites_a=[{"page": 42, "chunk_id": 9}])
        )

        body = self.post().json()

        self.assertEqual(body["code"], "evidence_not_supplied")
        self.assertNothingPersisted()

    def test_paper_bs_chunk_cannot_be_cited_as_paper_a_evidence(self):
        """(7, 0) exists only in Paper 2. Offered as cites_a it must be
        rejected, never remapped — the two namespaces are never merged."""
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(cites_a=[{"page": 7, "chunk_id": 0}])
        )

        body = self.post().json()

        self.assertEqual(body["code"], "evidence_not_supplied")
        self.assertNothingPersisted()

    def test_paper_as_chunk_cannot_be_cited_as_paper_b_evidence(self):
        """(5, 0) exists only in Paper 1."""
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(cites_b=[{"page": 5, "chunk_id": 0}])
        )

        body = self.post().json()

        self.assertEqual(body["code"], "evidence_not_supplied")
        self.assertNothingPersisted()

    def test_a_section_outside_the_comparison_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(
            _model_response(sections=COMPARABLE + ("limitations",))
        )

        body = self.post().json()

        self.assertEqual(body["code"], "unknown_section")
        self.assertNothingPersisted()

    def test_an_omitted_section_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(_model_response(sections=(S1,)))

        body = self.post().json()

        self.assertEqual(body["code"], "missing_section")
        self.assertNothingPersisted()

    def test_an_empty_statement_is_rejected(self):
        self.given_both_analysed()
        self.generate.return_value = _Result(_model_response(statement="   "))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_an_invented_extra_field_is_rejected(self):
        self.given_both_analysed()
        payload = json.loads(_model_response())
        payload["sections"][S1]["confidence"] = 0.9
        self.generate.return_value = _Result(json.dumps(payload))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_an_identifier_smuggled_into_the_object_is_rejected(self):
        self.given_both_analysed()
        payload = json.loads(_model_response())
        payload["owner_id"] = OWNER_B
        self.generate.return_value = _Result(json.dumps(payload))

        body = self.post().json()

        self.assertEqual(body["code"], "invalid_structure")
        self.assertNothingPersisted()

    def test_an_empty_generation_is_not_persisted(self):
        self.given_both_analysed()
        self.generate.return_value = _Result("")

        body = self.post().json()

        self.assertEqual(body["code"], pipeline.GENERATION_FAILED_CODE)
        self.assertNothingPersisted()

    def test_a_refusal_is_not_persisted(self):
        from app.agents.qa_agent import REFUSAL

        self.given_both_analysed()
        self.generate.return_value = _Result(REFUSAL)

        body = self.post().json()

        self.assertEqual(body["code"], pipeline.GENERATION_FAILED_CODE)
        self.assertNothingPersisted()

    def test_the_persisted_json_carries_no_identifiers(self):
        self.given_both_analysed()

        self.post()

        stored = json.dumps(self.stored_rows()[0].relationship)
        for value in (OWNER_A, OWNER_B, PAPER_1, PAPER_2, "Paper One", "Paper Two"):
            self.assertNotIn(value, stored)

    def test_only_a_validated_object_reaches_the_store(self):
        """The store refuses a dict outright, so there is no path from raw
        model output to the table that skips the validator."""
        from app.services.paper_relationship_store import save_relationship as save

        with self.assertRaises(TypeError):
            save(
                OWNER_A,
                PAPER_1,
                PAPER_2,
                {"sections": {}},
                paper_a_generated_at=STAMP_1,
                paper_b_generated_at=STAMP_2,
                model=TEST_MODEL,
            )


# ----------------------------------------------------------------------
# POST — staleness during generation
# ----------------------------------------------------------------------
class TestPostStaleWrite(RelationshipApiTestCase):
    def test_a_source_analysis_moving_mid_generation_blocks_the_write(self):
        """The model call takes time. If a paper is re-analysed while it
        runs, the relationship just produced describes claims that no
        longer exist, so it must not be written."""
        self.given_both_analysed()

        def regenerate_source(*args, **kwargs):
            # Happens between the stamp read and the save.
            self.given_intelligence(
                PAPER_1, generated_at=STAMP_1 + timedelta(minutes=10)
            )
            return _Result(_model_response())

        self.generate.side_effect = regenerate_source

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], pipeline.STALE_SOURCE_CODE)
        self.assertNothingPersisted()

    def test_the_conflict_does_not_overwrite_an_existing_relationship(self):
        self.given_both_analysed()
        original = self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=1))

        def regenerate_source(*args, **kwargs):
            self.given_intelligence(
                PAPER_2, generated_at=STAMP_2 + timedelta(minutes=10)
            )
            return _Result(_model_response())

        self.generate.side_effect = regenerate_source

        response = self.post()

        self.assertEqual(response.status_code, 409)
        kept = get_relationship(OWNER_A, PAPER_1, PAPER_2)
        self.assertEqual(_utc(kept.generated_at), _utc(original.generated_at))
        self.assertEqual(len(self.stored_rows()), 1)

    def test_an_unchanged_source_still_writes(self):
        """The conflict guard must not reject the ordinary case."""
        self.given_both_analysed()

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.stored_rows()), 1)

    def test_a_newer_stored_row_is_not_overwritten_by_a_slower_run(self):
        """The store's own stale-write guard, reached through the endpoint:
        a row generated later than this run wins, and the response reports
        the stored object as superseded."""
        self.given_both_analysed()
        self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=1))

        with self.factory() as session:
            row = session.query(PaperRelationshipRow).one()
            row.generated_at = datetime(2090, 1, 1, tzinfo=timezone.utc)
            session.commit()

        body = self.post().json()

        self.assertTrue(body["superseded"])
        self.assertEqual(
            self.stored_rows()[0].generated_at.replace(tzinfo=timezone.utc),
            datetime(2090, 1, 1, tzinfo=timezone.utc),
        )

    def test_the_pair_is_canonical_however_it_is_requested(self):
        """A reversed request must not produce a second row, and must not
        store reversed content."""
        self.given_both_analysed()

        self.post(a=PAPER_2, b=PAPER_1)

        rows = self.stored_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].paper_a_id), PAPER_1)
        self.assertEqual(str(rows[0].paper_b_id), PAPER_2)

    def test_a_reversed_request_reuses_the_same_row(self):
        self.given_both_analysed()

        self.post(a=PAPER_1, b=PAPER_2)
        self.generate.return_value = _Result(_model_response())
        self.post(a=PAPER_2, b=PAPER_1)

        self.assertEqual(len(self.stored_rows()), 1)

    def test_a_reversed_request_orients_evidence_to_the_canonical_pair(self):
        """Paper 1 is canonical A whichever order it is asked in, so its
        chunks must appear under PAPER A both times."""
        self.given_both_analysed()

        self.post(a=PAPER_2, b=PAPER_1)

        context = self.generate.call_args[0][1]
        b_at = context.index("=== PAPER B EVIDENCE ===")
        self.assertLess(context.index(P1_CHUNKS[(3, 0)]), b_at)
        self.assertGreater(context.index(P2_CHUNKS[(3, 0)]), b_at)


# ----------------------------------------------------------------------
# POST — quota
# ----------------------------------------------------------------------
class TestPostQuota(RelationshipApiTestCase):
    def _enforced(self, per_day="50"):
        return patch.dict(
            "os.environ",
            {
                "QUOTA_ENFORCEMENT": "on",
                "AI_RATE_PER_MINUTE": "100",
                "AI_QUOTA_PER_DAY": per_day,
            },
            clear=False,
        )

    def test_a_generation_charges_exactly_one_unit(self):
        from app.core import limits as limits_config
        from app.core.quota import peek

        self.given_both_analysed()

        with self._enforced():
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_deterministic_rejections_charge_nothing(self):
        from app.core import limits as limits_config
        from app.core.quota import peek

        with self._enforced():
            self.post(b=UNKNOWN)
            self.post(a=PAPER_1, b=PAPER_1)
            self.post()  # no intelligence yet

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)

            self.given_both_analysed()
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 1)

    def test_an_exhausted_quota_suppresses_the_provider_call(self):
        """A per_day of 1, spent, then a second attempt. Note that 0 would
        NOT express this: app/core/limits.py treats a falsy per_day as "no
        limit configured", so a 0 would silently disable enforcement and
        the test would prove nothing."""
        self.given_both_analysed()

        with self._enforced(per_day="1"):
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.generate.call_count, 1)

            second = self.post()

        self.assertEqual(second.status_code, 429)
        # Refused in the dependency, before the handler body ran, so no
        # second generation was attempted.
        self.assertEqual(self.generate.call_count, 1)
        # The first generation's row is untouched; the refusal wrote nothing.
        self.assertEqual(len(self.stored_rows()), 1)

    def test_the_quota_rejection_is_not_flattened_into_a_200(self):
        self.given_both_analysed()

        with self._enforced(per_day="1"):
            self.post()
            response = self.post()

        self.assertEqual(response.status_code, 429)
        self.assertNotEqual(response.json().get("status"), "success")

    def test_a_rejected_generation_still_costs_the_unit_it_spent(self):
        """The charge sits before the provider call, so a model response
        that fails validation has genuinely consumed a generation. It must
        not be silently refunded — that would make the counter a lie."""
        from app.core import limits as limits_config
        from app.core.quota import peek

        self.given_both_analysed()
        self.generate.return_value = _Result("not json")

        with self._enforced():
            self.post()
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 1)

        self.assertNothingPersisted()

    def test_get_does_not_consume_the_ai_allowance(self):
        from app.core import limits as limits_config
        from app.core.quota import peek

        self.given_both_analysed()
        self.given_relationship()

        with self._enforced():
            self.assertEqual(self.get().status_code, 200)
            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)


# ----------------------------------------------------------------------
# POST — provider failures
# ----------------------------------------------------------------------
class TestPostProviderFailures(RelationshipApiTestCase):
    def test_a_provider_timeout_does_not_persist_a_relationship(self):
        self.given_both_analysed()
        self.generate.side_effect = ProviderTimeout()

        body = self.post().json()

        self.assertEqual(body["code"], PROVIDER_TIMEOUT)
        self.assertNothingPersisted()

    def test_a_truncated_generation_does_not_persist_a_relationship(self):
        self.given_both_analysed()
        self.generate.side_effect = IncompleteGeneration("length")

        body = self.post().json()

        self.assertEqual(body["code"], GENERATION_INCOMPLETE)
        self.assertNothingPersisted()

    def test_a_provider_failure_does_not_overwrite_a_stored_relationship(self):
        self.given_both_analysed()
        original = self.given_relationship(stamp_1=STAMP_1 - timedelta(hours=1))
        self.generate.side_effect = ProviderTimeout()

        self.post()

        kept = get_relationship(OWNER_A, PAPER_1, PAPER_2)
        self.assertEqual(_utc(kept.generated_at), _utc(original.generated_at))

    def test_a_raw_provider_error_is_never_exposed(self):
        """The whole exception text is scrubbed, not merely shortened.

        The fixture is deliberately NOT shaped like a real credential. What
        this asserts is that RAW EXCEPTION TEXT never reaches the client,
        and a realistic-looking fake key proves nothing extra while making
        the file trip credential scanners. `upstream.invalid` uses the RFC
        2606 reserved TLD, so it cannot resolve anywhere.

        Every fragment is checked individually, so a handler that leaked
        only part of the message — a host, an identifier, the exception
        class — still fails.
        """
        detail = (
            "RAW-PROVIDER-DETAIL-MUST-NOT-ESCAPE host=upstream.invalid "
            "credential=SYNTHETIC-PLACEHOLDER-NOT-A-KEY request=REQ-0001"
        )
        self.given_both_analysed()
        self.generate.side_effect = RuntimeError(detail)

        response = self.post()
        text = response.text

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], INTERNAL_ERROR)
        self.assertNotIn(detail, text)
        for fragment in (
            "RAW-PROVIDER-DETAIL-MUST-NOT-ESCAPE",
            "upstream.invalid",
            "SYNTHETIC-PLACEHOLDER-NOT-A-KEY",
            "REQ-0001",
            "RuntimeError",
        ):
            self.assertNotIn(fragment, text)
        self.assertNothingPersisted()

    def test_a_raw_database_error_is_never_exposed(self):
        self.given_both_analysed()
        leak = 'relation "paper_relationships" violates constraint xyz'

        with patch.object(pipeline, "save_relationship", side_effect=RuntimeError(leak)):
            response = self.post()

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("paper_relationships", response.text)
        self.assertNotIn(leak, response.text)

    def test_a_rejected_generation_returns_an_authored_message(self):
        """The model's own text is never echoed back."""
        self.given_both_analysed()
        self.generate.return_value = _Result(
            '{"sections": {"' + S1 + '": "IGNORE PREVIOUS INSTRUCTIONS"}}'
        )

        response = self.post()

        self.assertNotIn("IGNORE PREVIOUS INSTRUCTIONS", response.text)
        self.assertEqual(response.json()["status"], "error")
        self.assertNothingPersisted()


# ----------------------------------------------------------------------
# SECURITY — cross-owner isolation
# ----------------------------------------------------------------------
class TestSecurity(RelationshipApiTestCase):
    def test_no_cross_owner_qdrant_evidence(self):
        """Another owner's chunk carrying the same (page, chunk_id) must
        never be admitted: the filter is on owner_id too."""
        self.given_both_analysed()
        self.points.append(
            _point(OWNER_B, PAPER_1, 3, 0, "FOREIGN TEXT THAT MUST NOT APPEAR")
        )

        self.post()

        self.assertNotIn(
            "FOREIGN TEXT THAT MUST NOT APPEAR", self.generate.call_args[0][1]
        )

    def test_no_cross_owner_postgres_relationship(self):
        self.given_both_analysed()
        self.given_relationship()

        with self.factory() as session:
            rows = session.query(PaperRelationshipRow).all()
            self.assertEqual({str(r.owner_id) for r in rows}, {OWNER_A})

    def test_a_relationship_is_invisible_to_the_other_owner_in_the_store(self):
        self.given_both_analysed()
        self.given_relationship()

        self.assertIsNone(get_relationship(OWNER_B, PAPER_1, PAPER_2))

    def test_canonical_ordering_does_not_bypass_ownership(self):
        """FOREIGN sorts above every owned id, so if canonicalization ran
        before the ownership check the pair would reorder and could slip
        through. It must still be 404 in both positions."""
        self.given_both_analysed()

        for a, b in ((PAPER_1, FOREIGN), (FOREIGN, PAPER_1)):
            with self.subTest(a=a, b=b):
                response = self.post(a=a, b=b)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.json()["message"], pipeline.NOT_FOUND_MESSAGE
                )

        self.assertNothingPersisted()
        self.assertNoProviderCalls()

    def test_a_foreign_papers_intelligence_is_never_read(self):
        self.given_both_analysed()
        self.given_intelligence(FOREIGN, generated_at=STAMP_1, owner_id=OWNER_B)

        response = self.post(b=FOREIGN)

        self.assertEqual(response.status_code, 404)
        self.assertNoProviderCalls()

    def test_get_reveals_no_metadata_for_a_foreign_pair(self):
        self.given_both_analysed()
        self.given_relationship()

        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_B
        body = self.get().json()

        for leaked in (
            "relationship",
            "generated_at",
            "paper_a_generated_at",
            "paper_b_generated_at",
            "model",
            "schema_version",
            "stale",
        ):
            self.assertNotIn(leaked, body)

    def test_the_route_declares_no_owner_id_parameter(self):
        """Structural, not behavioural: if a future edit adds one, this
        fails immediately rather than at review time."""
        import inspect

        for handler in (pipeline.read_paper_relationship, pipeline.paper_relationship):
            params = inspect.signature(handler).parameters
            self.assertNotIn("owner_id_param", params)
            self.assertIs(
                params["owner_id"].default.dependency.__module__.startswith("app.core"),
                True,
            )


# ----------------------------------------------------------------------
# The deterministic gate, in isolation
# ----------------------------------------------------------------------
class TestComparableSections(unittest.TestCase):
    """Pure: no database, no provider, no request."""

    def _intel(self, **sections):
        base = {name: {"status": "not_specified", "summary": None, "evidence": []} for name in SECTION_NAMES}
        base.update(sections)
        return base

    def _grounded(self, page=3, chunk_id=0):
        return {
            "status": "answered",
            "summary": "A claim.",
            "evidence": [{"page": page, "chunk_id": chunk_id}],
        }

    def test_both_grounded_is_comparable(self):
        a = self._intel(**{S1: self._grounded()})
        b = self._intel(**{S1: self._grounded()})

        self.assertEqual(pipeline.comparable_sections(a, b), (S1,))

    def test_one_sided_is_not_comparable(self):
        a = self._intel(**{S1: self._grounded()})
        b = self._intel()

        self.assertEqual(pipeline.comparable_sections(a, b), ())

    def test_not_specified_is_not_grounded(self):
        self.assertFalse(
            pipeline.is_grounded(
                {"status": "not_specified", "summary": None, "evidence": []}
            )
        )

    def test_answered_without_a_summary_is_not_grounded(self):
        self.assertFalse(
            pipeline.is_grounded(
                {"status": "answered", "summary": "  ", "evidence": [{"page": 1, "chunk_id": 0}]}
            )
        )

    def test_answered_without_evidence_is_not_grounded(self):
        self.assertFalse(
            pipeline.is_grounded({"status": "answered", "summary": "A claim.", "evidence": []})
        )

    def test_one_unusable_evidence_item_ungrounds_the_whole_section(self):
        self.assertFalse(
            pipeline.is_grounded(
                {
                    "status": "answered",
                    "summary": "A claim.",
                    "evidence": [{"page": 1, "chunk_id": 0}, {"page": "x", "chunk_id": 0}],
                }
            )
        )

    def test_a_boolean_page_is_not_a_usable_reference(self):
        """True IS an int in Python; without the explicit check it would
        pass as page 1."""
        self.assertFalse(
            pipeline.is_grounded(
                {
                    "status": "answered",
                    "summary": "A claim.",
                    "evidence": [{"page": True, "chunk_id": 0}],
                }
            )
        )

    def test_sections_come_back_in_canonical_order(self):
        a = self._intel(**{S2: self._grounded(), S1: self._grounded()})
        b = self._intel(**{S2: self._grounded(), S1: self._grounded()})

        result = pipeline.comparable_sections(a, b)

        self.assertEqual(result, (S1, S2))
        self.assertEqual(
            result, tuple(n for n in SECTION_NAMES if n in set(result))
        )

    def test_a_non_mapping_section_is_not_grounded(self):
        for value in (None, "answered", 7, [], ()):
            with self.subTest(value=value):
                self.assertFalse(pipeline.is_grounded(value))


if __name__ == "__main__":
    unittest.main()
