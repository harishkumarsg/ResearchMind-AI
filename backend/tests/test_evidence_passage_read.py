"""
Evidence Passage View — GET /paper-evidence, the read path.

The endpoint turns the references in a stored analysis back into the
chunk text they point at. What matters most is what it must NOT do:

  * it never returns a chunk the stored analysis does not cite, so it
    cannot be used to walk a paper;
  * it never returns another owner's or another paper's text, even when
    the (page, chunk_id) is identical — which it constantly is;
  * it never substitutes: a reference that does not resolve comes back
    with a status and no text;
  * it never generates, never charges the AI allowance, and never writes.

Fully offline. The database is per-test in-memory SQLite through the
existing harness. Qdrant is a fake that HONOURS the owner_id + paper_id
filter itself — a fake that returned everything regardless would make
the isolation tests pass whatever the production filter did.
"""
import ast
import inspect
import json
import os
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import event

import app.api.paper_evidence as evidence_api
import app.api.paper_intelligence as intelligence_api
import app.db.session as session_module
import app.services.paper_relationship_evidence as evidence_service
from app.core.auth import get_current_owner_id
from app.db.models import Paper, PaperIntelligenceRow
from app.services.intelligence_schema import SECTION_NAMES, validate_intelligence
from app.services.paper_intelligence_store import save_intelligence
from app.services.paper_relationship_evidence import EVIDENCE_CONTEXT_CHARS
from tests.sqlite_harness import attach_sqlite_db

MODULE_PATH = os.path.join(BACKEND_DIR, "app", "api", "paper_evidence.py")
INDEXER_PATH = os.path.join(BACKEND_DIR, "app", "api", "index_document.py")

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"
PAPER_A2 = "33333333-3333-3333-3333-333333333333"
UNKNOWN_PAPER = "99999999-9999-9999-9999-999999999999"

TEST_MODEL = "test-model/evidence"

# Relative to the real clock, so a test that performs a genuine row
# UPDATE (stamped "now" by the ORM) lands AFTER the analysis whatever
# day the suite runs on.
_NOW = datetime.now(timezone.utc)
INDEXED_AT = _NOW - timedelta(days=2)
GENERATED_AT = _NOW - timedelta(days=1)

#: Chunk text as the indexer stores it — hard line breaks and a ligature
#: left exactly as PDF extraction produced them.
METHOD_TEXT = (
    "METHODOLOGY.\nA two-stage hybrid framework is proposed.\n"
    "It uses a ﬁne-tuned detector."
)
QUOTE = "A two-stage hybrid framework is proposed."

CHUNKS = {
    (1, 0): "We address defect detection in PCB inspection.",
    (3, 0): METHOD_TEXT,
    (3, 1): "Stage two performs zero-shot classification.",
    (7, 0): "LIMITATIONS. The approach depends on prompt quality.",
    # Never cited by the analysis below.
    (7, 1): "UNCITED-CHUNK-ON-A-CITED-PAGE.",
    (9, 0): "UNCITED-CHUNK-ON-AN-UNCITED-PAGE.",
}
TOTAL_PAGES = 9

#: What the default stored analysis cites, in the order the endpoint
#: returns it: page ascending, then chunk_id ascending.
CITED = [(1, 0), (3, 0), (3, 1), (7, 0)]

SUMMARY_MARKER = "AI-WRITTEN-SUMMARY-MARKER"
ABSTRACT_MARKER = "AN-ABSTRACT-THAT-IS-NOT-CHUNK-TEXT"
POINT_ID = "point-uuid-regenerated-every-index-run"


def _section(status="answered", summary=None, evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0}]
    return {
        "status": status,
        "summary": (summary or SUMMARY_MARKER) if status == "answered" else None,
        "evidence": evidence if evidence is not None else [],
    }


def _validated(**overrides):
    """A validated analysis citing exactly CITED.

    Four sections cite four distinct chunks; the remaining six all cite
    (3, 0), so that chunk is cited many times over and must still appear
    once.
    """
    payload = {name: _section() for name in SECTION_NAMES}
    payload["research_problem"] = _section(evidence=[{"page": 1, "chunk_id": 0}])
    payload["methodology"] = _section(
        evidence=[{"page": 3, "chunk_id": 0, "quote": QUOTE}]
    )
    payload["dataset"] = _section(evidence=[{"page": 3, "chunk_id": 1}])
    payload["limitations"] = _section(evidence=[{"page": 7, "chunk_id": 0}])
    payload.update(overrides)
    return validate_intelligence(
        payload, allowed_evidence=CHUNKS, total_pages=TOTAL_PAGES
    )


def point(page, chunk_id, text, *, owner_id=OWNER_A, paper_id=PAPER_A, **extra):
    """A Qdrant point as index_document.py writes them."""
    p = MagicMock()
    p.id = POINT_ID
    payload = {
        "text": text,
        "source": "paper.pdf",
        "paper": "A Paper",
        "paper_id": paper_id,
        "owner_id": owner_id,
        "authors": "Someone",
        "abstract": ABSTRACT_MARKER,
        "keywords": "defects",
        "page": page,
        "total_pages": TOTAL_PAGES,
        "chunk_id": chunk_id,
        "chunk_count": 3,
    }
    payload.update(extra)
    p.payload = payload
    return p


def points_for(chunks=None, **kw):
    return [
        point(page, chunk_id, text, **kw)
        for (page, chunk_id), text in (chunks or CHUNKS).items()
    ]


class EvidenceReadTestCase(unittest.TestCase):
    """SQLite + a filter-honouring fake Qdrant + an authenticated client."""

    authenticated_as = OWNER_A

    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self._seed_paper(OWNER_A, PAPER_A, "Paper A")
        self._seed_paper(OWNER_A, PAPER_A2, "Paper A second")
        self._seed_paper(OWNER_B, PAPER_B, "Paper B")

        self.all_points = []
        self.scroll_calls = []

        def scroll(**kwargs):
            self.scroll_calls.append(kwargs)
            conditions = {c.key: c.match.value for c in kwargs["scroll_filter"].must}
            matched = [
                p
                for p in self.all_points
                if isinstance(getattr(p, "payload", None), dict)
                and all(p.payload.get(k) == v for k, v in conditions.items())
            ]
            return matched, None

        self.qdrant = MagicMock()
        self.qdrant.scroll.side_effect = scroll
        patcher = patch.object(evidence_service, "client", self.qdrant)
        patcher.start()
        self.addCleanup(patcher.stop)

        from fastapi.testclient import TestClient
        from app.main import app

        self.app = app
        self.app.dependency_overrides.clear()
        if self.authenticated_as is not None:
            owner = self.authenticated_as
            self.app.dependency_overrides[get_current_owner_id] = lambda: owner
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

    # -- seeding -------------------------------------------------------
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
                    # Explicit, and BEFORE the analysis: the ORM default
                    # would stamp "now", which is after GENERATED_AT.
                    created_at=INDEXED_AT,
                    updated_at=INDEXED_AT,
                )
            )
            session.commit()

    def store(self, owner_id=OWNER_A, paper_id=PAPER_A, **kw):
        return save_intelligence(
            owner_id,
            paper_id,
            kw.pop("intelligence", None) or _validated(),
            model=kw.pop("model", TEST_MODEL),
            generated_at=kw.pop("generated_at", GENERATED_AT),
        )

    def given(self, *points):
        self.all_points = list(points)

    def analysed_and_indexed(self):
        """The ordinary case: a stored analysis and every chunk present."""
        self.store()
        self.given(*points_for())

    # -- requests ------------------------------------------------------
    def get(self, paper_id=PAPER_A, **params):
        return self.client.get(
            "/paper-evidence", params={"paper_id": paper_id, **params}
        )

    def items(self, **kw):
        body = self.get(**kw).json()
        self.assertEqual(body["status"], "success", body)
        return body["items"]

    def by_key(self, **kw):
        return {(i["page"], i["chunk_id"]): i for i in self.items(**kw)}

    def set_paper_updated_at(self, value, paper_id=PAPER_A):
        with self.factory() as session:
            paper = session.get(Paper, uuid.UUID(paper_id))
            paper.updated_at = value
            session.commit()

    @contextmanager
    def enforced(self, **env):
        """Limits ON. tests/__init__.py turns enforcement off by default,
        so a test about quota or burst limits must opt in."""
        from app.core.rate_limit import get_limiter

        get_limiter().reset()
        self.addCleanup(get_limiter().reset)
        settings = {
            "QUOTA_ENFORCEMENT": "on",
            "READ_RATE_PER_MINUTE": "100",
            "AI_QUOTA_PER_DAY": "50",
        }
        settings.update(env)
        with patch.dict("os.environ", settings, clear=False):
            from app.core.limits import enforcement_enabled

            assert enforcement_enabled(), "enforcement must be on for this test"
            yield


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------
class TestAuthenticationRequired(EvidenceReadTestCase):
    authenticated_as = None

    def test_an_unauthenticated_read_is_rejected(self):
        r = self.client.get("/paper-evidence", params={"paper_id": PAPER_A})

        self.assertEqual(r.status_code, 401)

    def test_a_bogus_bearer_token_is_rejected(self):
        r = self.client.get(
            "/paper-evidence",
            params={"paper_id": PAPER_A},
            headers={"Authorization": "Bearer not-a-real-token"},
        )

        self.assertEqual(r.status_code, 401)

    def test_a_rejection_leaks_no_source_text_and_reads_nothing(self):
        self.store()
        self.given(*points_for())

        r = self.client.get("/paper-evidence", params={"paper_id": PAPER_A})

        self.assertEqual(r.status_code, 401)
        self.assertNotIn("items", r.text)
        self.assertNotIn("PCB inspection", r.text)
        self.qdrant.scroll.assert_not_called()


# ----------------------------------------------------------------------
# Unknown, malformed and foreign ids are one answer
# ----------------------------------------------------------------------
class TestNotFound(EvidenceReadTestCase):

    def _owner_b_has_everything(self):
        """Owner B's paper is analysed AND indexed, so a leak would have
        real text to leak."""
        self.store(OWNER_B, PAPER_B)
        self.given(*points_for(owner_id=OWNER_B, paper_id=PAPER_B))

    def test_an_unknown_paper_is_404(self):
        r = self.get(paper_id=UNKNOWN_PAPER)

        self.assertEqual(r.status_code, 404)
        self.assertEqual(
            r.json(), {"status": "error", "message": "Paper not found."}
        )

    def test_another_owners_paper_is_404(self):
        self._owner_b_has_everything()

        r = self.get(paper_id=PAPER_B)

        self.assertEqual(r.status_code, 404)
        self.assertNotIn("items", r.json())

    def test_a_malformed_paper_id_is_404_not_a_crash(self):
        for bad in ("not-a-uuid", "", "' OR 1=1 --", "12345", PAPER_A + "0"):
            with self.subTest(paper_id=bad):
                self.assertEqual(self.get(paper_id=bad).status_code, 404)

    def test_unknown_malformed_and_foreign_are_indistinguishable(self):
        self._owner_b_has_everything()

        unknown = self.get(paper_id=UNKNOWN_PAPER)
        malformed = self.get(paper_id="not-a-uuid")
        foreign = self.get(paper_id=PAPER_B)

        self.assertEqual(
            {unknown.status_code, malformed.status_code, foreign.status_code}, {404}
        )
        self.assertEqual(unknown.json(), malformed.json())
        self.assertEqual(unknown.json(), foreign.json())

    def test_a_miss_never_reads_qdrant(self):
        self._owner_b_has_everything()

        for paper_id in (UNKNOWN_PAPER, "not-a-uuid", PAPER_B):
            self.get(paper_id=paper_id)

        self.qdrant.scroll.assert_not_called()

    def test_a_foreign_paper_leaks_none_of_its_text(self):
        self._owner_b_has_everything()

        rendered = self.get(paper_id=PAPER_B).text

        for text in CHUNKS.values():
            self.assertNotIn(text, rendered)

    def test_the_wording_cannot_drift_from_the_other_paper_endpoints(self):
        self.assertEqual(
            evidence_api.NOT_FOUND_MESSAGE, intelligence_api.NOT_FOUND_MESSAGE
        )
        self.assertEqual(
            evidence_api.NOT_GENERATED_MESSAGE, intelligence_api.NOT_GENERATED_MESSAGE
        )
        self.assertEqual(
            evidence_api.UNREADABLE_MESSAGE, intelligence_api.UNREADABLE_MESSAGE
        )


# ----------------------------------------------------------------------
# The owner is the verified JWT, and only that
# ----------------------------------------------------------------------
class TestOwnerComesFromTheToken(EvidenceReadTestCase):

    def test_a_client_supplied_owner_id_cannot_reach_another_owners_paper(self):
        self.store(OWNER_B, PAPER_B)
        self.given(*points_for(owner_id=OWNER_B, paper_id=PAPER_B))

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
        self.qdrant.scroll.assert_not_called()

    def test_a_client_supplied_owner_id_does_not_reach_the_qdrant_filter(self):
        self.analysed_and_indexed()

        self.get(owner_id=OWNER_B)

        conditions = {
            c.key: c.match.value for c in self.scroll_calls[0]["scroll_filter"].must
        }
        self.assertEqual(conditions["owner_id"], OWNER_A)

    def test_the_route_declares_no_owner_parameter(self):
        """Structural: a client cannot name an owner because the handler
        has no such parameter to bind."""
        params = inspect.signature(evidence_api.read_paper_evidence).parameters

        self.assertEqual(sorted(params), ["owner_id", "paper_id", "response"])
        self.assertTrue(hasattr(params["owner_id"].default, "dependency"))

    def test_the_route_uses_the_cheap_read_guard(self):
        from app.core.usage_guard import guard_cheap_read

        params = inspect.signature(evidence_api.read_paper_evidence).parameters

        self.assertIs(params["owner_id"].default.dependency, guard_cheap_read)

    def test_owner_id_is_never_returned_to_the_client(self):
        self.analysed_and_indexed()

        rendered = json.dumps(self.get().json())

        self.assertNotIn(OWNER_A, rendered)
        self.assertNotIn("owner_id", rendered)


class TestEachOwnerSeesOnlyTheirOwn(EvidenceReadTestCase):
    authenticated_as = OWNER_B

    def test_owner_b_reads_their_own_paper_and_not_owner_as(self):
        self.store(OWNER_A, PAPER_A)
        self.store(OWNER_B, PAPER_B)
        self.given(
            *points_for({(3, 0): "OWNER-A-TEXT"}, owner_id=OWNER_A, paper_id=PAPER_A),
            *points_for({(3, 0): "OWNER-B-TEXT"}, owner_id=OWNER_B, paper_id=PAPER_B),
        )

        own = self.by_key(paper_id=PAPER_B)

        self.assertEqual(own[(3, 0)]["text"], "OWNER-B-TEXT")
        self.assertEqual(self.get(paper_id=PAPER_A).status_code, 404)


# ----------------------------------------------------------------------
# The Qdrant read is exact, and scoped
# ----------------------------------------------------------------------
class TestQdrantFilter(EvidenceReadTestCase):

    def test_the_filter_carries_the_verified_owner_and_the_resolved_paper(self):
        self.analysed_and_indexed()

        self.get()

        self.assertEqual(len(self.scroll_calls), 1)
        conditions = {
            c.key: c.match.value for c in self.scroll_calls[0]["scroll_filter"].must
        }
        self.assertEqual(conditions, {"owner_id": OWNER_A, "paper_id": PAPER_A})

    def test_the_resolved_id_is_used_not_the_raw_input(self):
        """An id the database resolves but that is not in canonical form
        must reach Qdrant as the canonical id, never as typed."""
        self.analysed_and_indexed()

        raw = "{" + PAPER_A.upper() + "}"
        self.assertEqual(self.get(paper_id=raw).json()["status"], "success")

        conditions = {
            c.key: c.match.value for c in self.scroll_calls[0]["scroll_filter"].must
        }
        self.assertEqual(conditions["paper_id"], PAPER_A)

    def test_the_read_requests_no_vectors_and_performs_no_search(self):
        self.analysed_and_indexed()

        self.get()

        self.assertFalse(self.scroll_calls[0]["with_vectors"])
        self.qdrant.query_points.assert_not_called()
        self.qdrant.search.assert_not_called()

    def test_another_owners_identical_page_and_chunk_is_not_returned(self):
        self.store()
        self.given(
            *points_for(),
            point(3, 0, "ANOTHER-OWNERS-TEXT", owner_id=OWNER_B, paper_id=PAPER_A),
        )

        found = self.by_key()

        self.assertEqual(found[(3, 0)]["status"], "found")
        self.assertEqual(found[(3, 0)]["text"], METHOD_TEXT)
        self.assertNotIn("ANOTHER-OWNERS-TEXT", json.dumps(self.get().json()))

    def test_another_papers_identical_page_and_chunk_is_not_returned(self):
        self.store()
        self.given(
            *points_for(),
            point(3, 0, "ANOTHER-PAPERS-TEXT", owner_id=OWNER_A, paper_id=PAPER_A2),
        )

        found = self.by_key()

        self.assertEqual(found[(3, 0)]["text"], METHOD_TEXT)
        self.assertNotIn("ANOTHER-PAPERS-TEXT", json.dumps(self.get().json()))


# ----------------------------------------------------------------------
# Only what the stored analysis cites
# ----------------------------------------------------------------------
class TestOnlyCitedReferences(EvidenceReadTestCase):

    def test_exactly_the_cited_references_are_returned(self):
        self.analysed_and_indexed()

        keys = [(i["page"], i["chunk_id"]) for i in self.items()]

        self.assertEqual(keys, CITED)

    def test_uncited_chunks_are_never_returned(self):
        self.analysed_and_indexed()

        rendered = json.dumps(self.get().json())

        self.assertNotIn("UNCITED-CHUNK-ON-A-CITED-PAGE", rendered)
        self.assertNotIn("UNCITED-CHUNK-ON-AN-UNCITED-PAGE", rendered)

    def test_a_chunk_cited_by_many_sections_appears_once(self):
        self.analysed_and_indexed()

        keys = [(i["page"], i["chunk_id"]) for i in self.items()]

        # (3, 0) is cited by seven of the ten sections.
        self.assertEqual(keys.count((3, 0)), 1)

    def test_items_are_ordered_by_page_then_chunk_whatever_qdrant_returns(self):
        self.store()
        self.given(*reversed(points_for()))

        keys = [(i["page"], i["chunk_id"]) for i in self.items()]

        self.assertEqual(keys, sorted(keys))

    def test_the_request_cannot_name_a_chunk(self):
        """Extra parameters are not an API: the stored analysis decides."""
        self.analysed_and_indexed()

        keys = [
            (i["page"], i["chunk_id"])
            for i in self.items(page=9, chunk_id=0, pages="7,9")
        ]

        self.assertEqual(keys, CITED)

    def test_an_analysis_citing_nothing_reads_nothing(self):
        nothing = validate_intelligence(
            {name: _section(status="not_specified") for name in SECTION_NAMES},
            allowed_evidence=CHUNKS,
            total_pages=TOTAL_PAGES,
        )
        self.store(intelligence=nothing)
        self.given(*points_for())

        body = self.get().json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["items"], [])
        self.qdrant.scroll.assert_not_called()


# ----------------------------------------------------------------------
# Found text is the chunk's own text, exactly
# ----------------------------------------------------------------------
class TestExactText(EvidenceReadTestCase):

    def test_found_text_is_byte_for_byte_the_stored_chunk(self):
        self.analysed_and_indexed()

        found = self.by_key()

        # Line breaks and the ligature survive untouched.
        self.assertEqual(found[(3, 0)]["text"], METHOD_TEXT)
        self.assertIn("\n", found[(3, 0)]["text"])
        self.assertIn("ﬁ", found[(3, 0)]["text"])

    def test_every_cited_chunk_carries_its_own_text(self):
        self.analysed_and_indexed()

        found = self.by_key()

        for key in CITED:
            with self.subTest(reference=key):
                self.assertEqual(found[key]["status"], "found")
                self.assertEqual(found[key]["text"], CHUNKS[key])

    def test_the_same_chunk_id_on_another_page_is_a_different_chunk(self):
        self.analysed_and_indexed()

        found = self.by_key()

        self.assertNotEqual(found[(1, 0)]["text"], found[(3, 0)]["text"])
        self.assertNotEqual(found[(3, 0)]["text"], found[(7, 0)]["text"])

    def test_the_response_shape_is_exactly_the_contract(self):
        self.analysed_and_indexed()

        body = self.get().json()

        self.assertEqual(
            sorted(body),
            ["generated_at", "items", "paper_id", "reindexed_since_analysis", "status"],
        )
        self.assertEqual(body["paper_id"], PAPER_A)
        self.assertEqual(
            datetime.fromisoformat(body["generated_at"]), GENERATED_AT
        )
        for item in body["items"]:
            self.assertEqual(sorted(item), ["chunk_id", "page", "status", "text"])

    def test_no_summary_and_no_quote_are_returned(self):
        """The model's prose and the stored quote have no route into what
        this endpoint presents as source text."""
        self.analysed_and_indexed()

        body = self.get().json()
        rendered = json.dumps(body)

        self.assertNotIn(SUMMARY_MARKER, rendered)
        self.assertNotIn("summary", rendered)
        self.assertNotIn("quote", rendered)

    def test_text_comes_from_the_text_field_not_from_metadata(self):
        self.analysed_and_indexed()

        rendered = json.dumps(self.get().json())

        self.assertNotIn(ABSTRACT_MARKER, rendered)
        self.assertNotIn("Someone", rendered)

    def test_no_qdrant_point_id_is_returned(self):
        self.analysed_and_indexed()

        self.assertNotIn(POINT_ID, json.dumps(self.get().json()))


# ----------------------------------------------------------------------
# Unresolved references: a status, and no text
# ----------------------------------------------------------------------
class TestUnresolvedReferences(EvidenceReadTestCase):

    def test_a_missing_chunk_is_reported_not_substituted(self):
        self.store()
        # (7, 0) is gone; its neighbour (7, 1) is still there.
        self.given(*points_for({k: v for k, v in CHUNKS.items() if k != (7, 0)}))

        found = self.by_key()

        self.assertEqual(found[(7, 0)]["status"], "missing_chunk")
        self.assertIsNone(found[(7, 0)]["text"])
        self.assertNotIn(
            "UNCITED-CHUNK-ON-A-CITED-PAGE", json.dumps(self.get().json())
        )

    def test_a_duplicate_identity_returns_neither_candidate(self):
        self.store()
        self.given(*points_for(), point(3, 1, "A-SECOND-CLAIMANT"))

        body = self.get().json()
        found = {(i["page"], i["chunk_id"]): i for i in body["items"]}

        self.assertEqual(found[(3, 1)]["status"], "duplicate_chunk_identity")
        self.assertIsNone(found[(3, 1)]["text"])
        self.assertNotIn("A-SECOND-CLAIMANT", json.dumps(body))
        self.assertNotIn(CHUNKS[(3, 1)], json.dumps(body))

    def test_a_chunk_with_no_text_is_malformed_not_found(self):
        for empty in ("", "   ", None, 12345):
            with self.subTest(text=empty):
                self.store()
                chunks = dict(CHUNKS)
                chunks[(1, 0)] = empty
                self.given(*points_for(chunks))

                found = self.by_key()

                self.assertEqual(found[(1, 0)]["status"], "malformed_payload")
                self.assertIsNone(found[(1, 0)]["text"])

    def test_unresolved_references_sit_alongside_resolved_ones(self):
        self.store()
        self.given(*points_for({k: v for k, v in CHUNKS.items() if k != (1, 0)}))

        found = self.by_key()

        self.assertEqual(found[(1, 0)]["status"], "missing_chunk")
        self.assertEqual(found[(3, 0)]["status"], "found")
        self.assertEqual(found[(7, 0)]["status"], "found")

    def test_a_paper_with_no_chunks_at_all_resolves_nothing(self):
        self.store()
        self.given()

        items = self.items()

        self.assertEqual([i["status"] for i in items], ["missing_chunk"] * 4)
        self.assertTrue(all(i["text"] is None for i in items))

    def test_the_response_is_bounded_by_the_existing_evidence_budget(self):
        """Whole chunks only, in order, within the service's own budget.
        A chunk that does not fit is reported, never cut."""
        self.store()
        big = {key: "X" * 5000 for key in CITED}
        self.given(*points_for(big))

        items = self.items()

        self.assertEqual(
            [i["status"] for i in items],
            ["found", "found", "over_budget", "over_budget"],
        )
        self.assertTrue(all(i["text"] is None for i in items[2:]))
        returned = sum(len(i["text"] or "") for i in items)
        self.assertLessEqual(returned, EVIDENCE_CONTEXT_CHARS)

    def test_a_qdrant_failure_is_sanitized(self):
        self.store()
        self.qdrant.scroll.side_effect = RuntimeError(
            "connection refused: internal-qdrant-host:6333 api-key=SECRET"
        )

        r = self.get()
        body = r.json()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "internal_error")
        self.assertNotIn("items", body)
        self.assertNotIn("internal-qdrant-host", r.text)
        self.assertNotIn("SECRET", r.text)


# ----------------------------------------------------------------------
# No analysis yet
# ----------------------------------------------------------------------
class TestNotGenerated(EvidenceReadTestCase):

    def test_an_owned_paper_without_an_analysis_is_a_clean_empty_state(self):
        self.given(*points_for(paper_id=PAPER_A2))

        r = self.get(paper_id=PAPER_A2)
        body = r.json()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["status"], "not_generated")
        self.assertEqual(body["paper_id"], PAPER_A2)
        self.assertNotIn("items", body)

    def test_not_generated_never_reads_qdrant(self):
        self.given(*points_for(paper_id=PAPER_A2))

        self.get(paper_id=PAPER_A2)

        self.qdrant.scroll.assert_not_called()

    def test_reading_does_not_create_an_analysis(self):
        self.get(paper_id=PAPER_A2)

        with self.factory() as session:
            self.assertEqual(session.query(PaperIntelligenceRow).count(), 0)

    def test_one_papers_analysis_does_not_leak_to_another(self):
        self.analysed_and_indexed()

        self.assertEqual(self.get(paper_id=PAPER_A2).json()["status"], "not_generated")


# ----------------------------------------------------------------------
# A stored row that no longer parses
# ----------------------------------------------------------------------
class TestUnreadableStoredRow(EvidenceReadTestCase):

    def _corrupt(self):
        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            broken = dict(row.intelligence)
            broken.pop("limitations")
            row.intelligence = broken
            session.commit()

    def test_a_corrupted_row_is_reported_and_reads_nothing(self):
        self.analysed_and_indexed()
        self._corrupt()

        body = self.get().json()

        self.assertEqual(body["status"], "error")
        self.assertEqual(body["code"], "invalid_structure")
        self.assertEqual(body["message"], evidence_api.UNREADABLE_MESSAGE)
        self.assertNotIn("items", body)
        self.qdrant.scroll.assert_not_called()

    def test_a_corrupted_row_leaks_no_stored_or_source_text(self):
        self.analysed_and_indexed()
        self._corrupt()

        rendered = json.dumps(self.get().json())

        self.assertNotIn(SUMMARY_MARKER, rendered)
        self.assertNotIn("PCB inspection", rendered)


# ----------------------------------------------------------------------
# No generation, no charge, no write
# ----------------------------------------------------------------------
class TestNoSideEffects(EvidenceReadTestCase):

    def _writes_during(self, action):
        """Every INSERT / UPDATE / DELETE the database saw during action."""
        engine = self.factory.kw["bind"]
        seen = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split(None, 1)[0].upper() in (
                "INSERT",
                "UPDATE",
                "DELETE",
                "REPLACE",
            ):
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            action()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return seen

    def test_a_read_writes_nothing_to_the_database(self):
        self.analysed_and_indexed()

        with self.enforced():
            writes = self._writes_during(
                lambda: [self.assertEqual(self.get().status_code, 200) for _ in range(3)]
            )

        self.assertEqual(writes, [])

    def test_no_kind_of_request_writes(self):
        self.analysed_and_indexed()

        def all_outcomes():
            self.get()  # success
            self.get(paper_id=PAPER_A2)  # not_generated
            self.get(paper_id=UNKNOWN_PAPER)  # 404
            self.get(paper_id=PAPER_B)  # foreign
            self.get(paper_id="not-a-uuid")  # malformed

        with self.enforced():
            self.assertEqual(self._writes_during(all_outcomes), [])

    def test_the_stored_analysis_and_the_paper_are_left_exactly_as_they_were(self):
        self.analysed_and_indexed()

        def snapshot():
            with self.factory() as session:
                row = session.query(PaperIntelligenceRow).one()
                paper = session.get(Paper, uuid.UUID(PAPER_A))
                return (
                    json.dumps(row.intelligence, sort_keys=True),
                    row.generated_at,
                    row.updated_at,
                    paper.updated_at,
                    paper.status,
                    session.query(PaperIntelligenceRow).count(),
                )

        before = snapshot()
        for _ in range(3):
            self.get()

        self.assertEqual(snapshot(), before)

    def test_a_read_consumes_no_ai_generation_quota(self):
        from app.core import limits as limits_config
        from app.core.quota import peek

        self.analysed_and_indexed()

        with self.enforced():
            for _ in range(3):
                self.assertEqual(self.get().json()["status"], "success")

            self.assertEqual(peek(OWNER_A, limits_config.AI_GENERATION), 0)

    def test_the_cheap_read_burst_limit_applies(self):
        self.analysed_and_indexed()

        with self.enforced(READ_RATE_PER_MINUTE="2"):
            self.assertEqual(self.get().status_code, 200)
            self.assertEqual(self.get().status_code, 200)
            reads_before = self.qdrant.scroll.call_count

            third = self.get()

        self.assertEqual(third.status_code, 429)
        self.assertNotEqual(third.json().get("status"), "success")
        self.assertNotIn("items", third.json())
        # Refused in the dependency, before the handler body ran.
        self.assertEqual(self.qdrant.scroll.call_count, reads_before)

    def test_no_model_and_no_embedding_provider_is_called(self):
        import app.agents.qa_agent as qa_agent
        import app.rag.embedder as embedder

        self.analysed_and_indexed()

        with patch.object(
            intelligence_api, "generate", side_effect=AssertionError("called the model")
        ) as generate, patch.object(
            qa_agent, "generate", side_effect=AssertionError("called the model")
        ) as agent, patch.object(
            embedder, "encode_query", side_effect=AssertionError("called Voyage")
        ) as encode, patch.object(
            embedder, "create_embeddings", side_effect=AssertionError("called Voyage")
        ) as embed:
            self.assertEqual(self.get().json()["status"], "success")

        for trap in (generate, agent, encode, embed):
            trap.assert_not_called()

    def test_no_storage_call_is_made(self):
        from app.services import storage

        self.analysed_and_indexed()

        with patch.object(
            storage, "fetch_pdf", side_effect=AssertionError("called Storage")
        ) as trap:
            self.get()

        trap.assert_not_called()

    def test_exactly_one_qdrant_round_trip_per_read(self):
        self.analysed_and_indexed()

        self.get()

        self.assertEqual(self.qdrant.scroll.call_count, 1)
        self.qdrant.upsert.assert_not_called()
        self.qdrant.delete.assert_not_called()


class TestTheModuleCannotGenerate(unittest.TestCase):
    """Structural, parsed not grepped."""

    def setUp(self):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            self.tree = ast.parse(handle.read())

    def test_it_imports_no_model_embedding_or_storage_code(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        for forbidden in (
            "app.agents",
            "app.rag",
            "app.services.storage",
            "app.api.paper_intelligence",
            "app.api.paper_relationship",
            "app.services.paper_relationship_store",
            "groq",
            "voyageai",
            "qdrant_client",
        ):
            with self.subTest(module=forbidden):
                self.assertFalse(
                    any(m == forbidden or m.startswith(forbidden + ".") for m in imported),
                    f"paper_evidence.py imports {forbidden}",
                )

    def test_it_calls_nothing_that_generates_charges_or_writes(self):
        called = {
            n.func.id
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        } | {
            n.func.attr
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

        for forbidden in (
            "generate",
            "save_intelligence",
            "delete_intelligence",
            "save_relationship",
            "charge_ai_unit",
            "charge",
            "validate_intelligence",
            "encode_query",
            "create_embeddings",
            "fetch_pdf",
            "upsert",
            "delete",
            "add",
            "commit",
        ):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called)

        # The approved primitives, and only through the service.
        self.assertIn("get_intelligence", called)
        self.assertIn("resolve_owned_paper", called)
        self.assertIn("references_from_intelligence", called)
        self.assertIn("rehydrate_paper_evidence", called)
        self.assertNotIn("scroll", called)

    def test_the_only_route_is_a_get(self):
        routes = [(r.path, sorted(r.methods)) for r in evidence_api.router.routes]

        self.assertEqual(routes, [("/paper-evidence", ["GET"])])


# ----------------------------------------------------------------------
# GET /paper-intelligence keeps its database-only contract
# ----------------------------------------------------------------------
class TestIntelligenceReadIsUntouched(EvidenceReadTestCase):

    def test_reading_the_analysis_still_never_touches_the_evidence_service(self):
        self.analysed_and_indexed()

        r = self.client.get("/paper-intelligence", params={"paper_id": PAPER_A})

        self.assertEqual(r.json()["status"], "success")
        self.qdrant.scroll.assert_not_called()

    def test_reading_the_analysis_still_carries_no_chunk_text(self):
        self.analysed_and_indexed()

        body = self.client.get(
            "/paper-intelligence", params={"paper_id": PAPER_A}
        ).json()

        self.assertNotIn("items", body)
        self.assertNotIn(CHUNKS[(1, 0)], json.dumps(body))


# ----------------------------------------------------------------------
# reindexed_since_analysis — conservative, never a proof
# ----------------------------------------------------------------------
class TestReindexSignal(EvidenceReadTestCase):

    def flag(self):
        return self.get().json()["reindexed_since_analysis"]

    def test_false_when_the_paper_last_changed_before_the_analysis(self):
        self.analysed_and_indexed()

        self.assertIs(self.flag(), False)

    def test_true_when_the_paper_changed_after_the_analysis(self):
        self.analysed_and_indexed()
        self.set_paper_updated_at(GENERATED_AT + timedelta(hours=1))

        self.assertIs(self.flag(), True)

    def test_an_equal_stamp_is_not_a_later_change(self):
        self.analysed_and_indexed()
        self.set_paper_updated_at(GENERATED_AT)

        self.assertIs(self.flag(), False)

    def test_a_real_status_change_moves_the_stamp_the_flag_reads(self):
        """The assumption the flag rests on, exercised rather than
        asserted: an UPDATE of the paper row — which is what an index run
        is — moves `updated_at` on its own."""
        self.analysed_and_indexed()
        self.assertIs(self.flag(), False)

        with self.factory() as session:
            paper = session.get(Paper, uuid.UUID(PAPER_A))
            before = paper.updated_at
            paper.status = "indexing"
            session.commit()
            session.refresh(paper)
            self.assertGreater(paper.updated_at, before)

        self.assertIs(self.flag(), True)

    def test_the_flag_never_withholds_resolved_text(self):
        """It is a caution about the whole paper, not a verdict on any
        chunk — a chunk that still resolves is still returned."""
        self.analysed_and_indexed()
        self.set_paper_updated_at(GENERATED_AT + timedelta(hours=1))

        found = self.by_key()

        self.assertEqual(found[(3, 0)]["status"], "found")
        self.assertEqual(found[(3, 0)]["text"], METHOD_TEXT)

    def test_a_regenerated_analysis_clears_the_flag(self):
        self.analysed_and_indexed()
        self.set_paper_updated_at(GENERATED_AT + timedelta(hours=1))
        self.assertIs(self.flag(), True)

        self.store(generated_at=GENERATED_AT + timedelta(hours=2))

        self.assertIs(self.flag(), False)

    def test_the_indexer_still_updates_the_paper_row_on_every_run(self):
        """Pins the other half of the assumption. If the indexer ever
        stopped touching the row, `updated_at` would stop moving and the
        flag would silently read False after a re-index."""
        with open(INDEXER_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        assigned = {
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and any(
                isinstance(t, ast.Attribute)
                and t.attr == "status"
                and isinstance(t.value, ast.Name)
                and t.value.id == "paper"
                for t in node.targets
            )
        }

        self.assertIn("indexing", assigned)
        self.assertIn("indexed", assigned)


class TestReindexedSince(unittest.TestCase):
    """The comparison itself, including the SQLite/Postgres difference."""

    def test_later_is_true_and_earlier_is_false(self):
        at = datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc)

        self.assertTrue(evidence_api.reindexed_since(at + timedelta(seconds=1), at))
        self.assertFalse(evidence_api.reindexed_since(at - timedelta(seconds=1), at))
        self.assertFalse(evidence_api.reindexed_since(at, at))

    def test_a_naive_stamp_is_read_as_utc(self):
        """SQLite returns naive datetimes; Postgres returns aware ones."""
        aware = datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc)
        naive_later = datetime(2026, 9, 22, 11, 31)
        naive_earlier = datetime(2026, 9, 22, 11, 29)

        self.assertTrue(evidence_api.reindexed_since(naive_later, aware))
        self.assertFalse(evidence_api.reindexed_since(naive_earlier, aware))
        self.assertTrue(evidence_api.reindexed_since(aware, naive_earlier))

    def test_instants_are_compared_not_wall_clocks(self):
        """12:00 at +05:30 is 06:30 UTC — EARLIER than 07:00 UTC, though
        its wall-clock reading is later."""
        ist = timezone(timedelta(hours=5, minutes=30))
        updated = datetime(2026, 9, 22, 12, 0, tzinfo=ist)
        generated = datetime(2026, 9, 22, 7, 0, tzinfo=timezone.utc)

        self.assertFalse(evidence_api.reindexed_since(updated, generated))
        self.assertTrue(evidence_api.reindexed_since(generated, updated))

    def test_a_missing_stamp_is_not_evidence_of_a_change(self):
        at = datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc)

        self.assertFalse(evidence_api.reindexed_since(None, at))
        self.assertFalse(evidence_api.reindexed_since(at, None))
        self.assertFalse(evidence_api.reindexed_since(None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
