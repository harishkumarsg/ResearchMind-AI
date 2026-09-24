"""
Phase 2B step 2 — durable persistence for Paper Intelligence.

Two halves, deliberately different in kind.

The migration half reads backend/migrations/0005_paper_intelligence.sql
as text, because the SQL is the artifact that will actually be applied to
Supabase by hand and no test here is allowed to execute it. Comments are
stripped before any assertion: the file explains itself at length, and a
grep that matches its own prose proves nothing.

The behaviour half drives the REAL store against a per-test in-memory
SQLite database, reached through the existing tests/sqlite_harness.py so
the real session_scope() and the real functions run. SQLite has no RLS,
so these tests assert the application half of the isolation — the
explicit owner_id term in every query, and the ownership check before
every write. The Postgres half is asserted against the migration text
above, and the claim-binding mechanism it relies on is already covered
by test_c1_jwt_claims.py.

Offline and deterministic: no Postgres, no Qdrant, no Groq, no Voyage,
no Supabase Storage, no network.
"""
import ast
import json
import os
import socket
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app.db.session as session_module
from app.db.models import Paper, PaperIntelligenceRow
from app.db.session import JWT_CLAIMS_SETTING, _bind_jwt_claims, session_scope
from app.services.intelligence_schema import (
    SECTION_NAMES,
    IntelligenceValidationError,
    PaperIntelligence,
    validate_intelligence,
)
from app.services.paper_intelligence_store import (
    PAPER_INTELLIGENCE_SCHEMA_VERSION,
    PaperNotOwned,
    delete_intelligence,
    get_intelligence,
    save_intelligence,
)
from tests.sqlite_harness import attach_sqlite_db

MIGRATIONS_DIR = os.path.join(BACKEND_DIR, "migrations")
MIGRATION_PATH = os.path.join(MIGRATIONS_DIR, "0005_paper_intelligence.sql")
STORE_PATH = os.path.join(
    BACKEND_DIR, "app", "services", "paper_intelligence_store.py"
)

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
STRANGER = "ffffffff-ffff-ffff-ffff-ffffffffffff"

PAPER_A = "11111111-1111-1111-1111-111111111111"
PAPER_B = "22222222-2222-2222-2222-222222222222"
UNKNOWN_PAPER = "33333333-3333-3333-3333-333333333333"

TEST_MODEL = "test-model/step2"
GENERATED_AT = datetime(2026, 9, 22, 11, 30, 0, tzinfo=timezone.utc)

TOTAL_PAGES = 9

#: The allowlist a server would have built for these fixtures. (3, 0) and
#: (7, 0) share a chunk_id on different pages, which is the pair identity
#: this whole feature depends on.
ALLOWED = {
    (1, 0): "We address defect detection in PCB inspection.",
    (3, 0): "METHODOLOGY. A two-stage hybrid framework is proposed.",
    (3, 1): "Stage two performs zero-shot classification.",
    (7, 0): "LIMITATIONS. The approach depends on prompt quality.",
}


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
def _section(status="answered", summary="A summary.", evidence=None):
    if status == "answered" and evidence is None:
        evidence = [{"page": 3, "chunk_id": 0, "quote": ALLOWED[(3, 0)]}]
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else [],
    }


def _payload(**overrides):
    payload = {name: _section() for name in SECTION_NAMES}
    payload.update(overrides)
    return payload


def _validated(**overrides) -> PaperIntelligence:
    """A PaperIntelligence exactly as Step 3 will obtain one: through the
    validator, never by hand."""
    return validate_intelligence(
        _payload(**overrides), allowed_evidence=ALLOWED, total_pages=TOTAL_PAGES
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite reads timestamps back naive; every stored value is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sql_without_comments(text: str) -> str:
    """The migration's executable SQL, lowercased and whitespace-collapsed.

    Every `--` comment is dropped first. This file documents its own
    reasoning at length — including the names of columns it deliberately
    does NOT create — so an assertion made against the raw text would be
    matching prose as readily as DDL.
    """
    lines = []
    for line in text.splitlines():
        marker = line.find("--")
        if marker != -1:
            line = line[:marker]
        lines.append(line)
    return " ".join(" ".join(lines).split()).lower()


# ----------------------------------------------------------------------
# 1-3 + RLS/grants: the migration file itself
# ----------------------------------------------------------------------
class TestMigrationFile(unittest.TestCase):
    """Static assertions against 0005. Nothing here executes SQL."""

    @classmethod
    def setUpClass(cls):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as handle:
            cls.raw = handle.read()
        cls.sql = _sql_without_comments(cls.raw)

    # -- 1. the table exists -------------------------------------------
    def test_migration_creates_the_paper_intelligence_table(self):
        self.assertIn("create table if not exists paper_intelligence (", self.sql)

    def test_the_migration_sequence_is_contiguous_and_contains_0005(self):
        """Originally an exact-list snapshot of the five migrations that
        existed when Step 2 shipped. Later gates legitimately add
        migrations (0006 arrived with Phase 2C), so the assertion now pins
        what it actually cares about: 0005 is present and the sequence has
        no gaps or duplicates."""
        names = sorted(
            name for name in os.listdir(MIGRATIONS_DIR) if name.endswith(".sql")
        )
        self.assertIn("0005_paper_intelligence.sql", names)

        numbers = [int(name[:4]) for name in names]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    # -- 2. required columns -------------------------------------------
    def test_required_columns_exist_with_the_expected_types(self):
        expected = [
            "id uuid primary key default gen_random_uuid()",
            "owner_id uuid not null references auth.users(id) on delete cascade",
            "paper_id uuid not null references papers(id) on delete cascade",
            "intelligence jsonb not null",
            "generated_at timestamptz not null",
            "model text not null",
            "schema_version text not null",
            "created_at timestamptz not null default now()",
            "updated_at timestamptz not null default now()",
        ]
        for fragment in expected:
            with self.subTest(column=fragment.split(" ")[0]):
                self.assertIn(fragment, self.sql)

    def test_intelligence_is_jsonb_not_text(self):
        self.assertIn("intelligence jsonb", self.sql)
        self.assertNotIn("intelligence text", self.sql)

    def test_no_denormalised_evidence_pages_column(self):
        """Page numbers live inside the evidence entries. A second copy
        could only ever disagree with the first."""
        self.assertNotIn("evidence_pages", self.sql)

    def test_no_qdrant_point_id_column(self):
        for forbidden in ("point_id", "qdrant", "vector_id"):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, self.sql)

    # -- 3. one current row per (owner, paper) -------------------------
    def test_unique_constraint_on_owner_and_paper(self):
        self.assertIn("unique (owner_id, paper_id)", self.sql)

    def test_no_second_redundant_uniqueness_mechanism(self):
        """One UNIQUE, and no unique index restating it."""
        self.assertEqual(self.sql.count("unique (owner_id, paper_id)"), 1)
        self.assertNotIn("create unique index", self.sql)

    def test_no_speculative_indexes(self):
        """UNIQUE(owner_id, paper_id) is backed by a btree with owner_id
        leftmost, so a standalone owner index would be redundant — the
        same call 0003 and 0004 already made."""
        self.assertNotIn("create index", self.sql)

    # -- RLS ------------------------------------------------------------
    def test_row_level_security_is_enabled_and_forced(self):
        self.assertIn(
            "alter table paper_intelligence enable row level security", self.sql
        )
        self.assertIn(
            "alter table paper_intelligence force row level security", self.sql
        )

    def test_policy_covers_reads_and_writes(self):
        """USING governs reads and which rows may be updated; WITH CHECK
        governs what may be written. This table is upserted, so it needs
        both — without WITH CHECK an owner could insert a row attributed
        to somebody else."""
        self.assertIn("create policy paper_intelligence_owner_only", self.sql)
        self.assertIn("using (owner_id = auth.uid())", self.sql)
        self.assertIn("with check (owner_id = auth.uid())", self.sql)

    def test_policy_uses_the_established_auth_uid_convention(self):
        self.assertEqual(self.sql.count("auth.uid()"), 2)
        self.assertNotIn("current_user", self.sql)

    def test_migration_is_rerunnable(self):
        self.assertIn("create table if not exists", self.sql)
        self.assertIn(
            "drop policy if exists paper_intelligence_owner_only", self.sql
        )

    # -- grants ---------------------------------------------------------
    def test_grants_only_what_the_store_uses_and_only_to_the_app_role(self):
        self.assertIn(
            "grant select, insert, update, delete on paper_intelligence "
            "to researchmind_app",
            self.sql,
        )
        self.assertEqual(self.sql.count("grant "), 1)

    def test_no_broad_or_public_grants(self):
        for forbidden in (
            "grant all",
            "to public",
            "to anon",
            "to authenticated",
            "to service_role",
        ):
            with self.subTest(grant=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_no_role_is_created_or_altered(self):
        for forbidden in ("create role", "alter role", "bypassrls"):
            with self.subTest(statement=forbidden):
                self.assertNotIn(forbidden, self.sql)

    # -- it must not weaken anything existing ---------------------------
    def test_migration_touches_no_existing_table(self):
        for existing in ("papers", "reports", "chat_sessions", "chat_messages",
                         "usage_counters", "storage.objects"):
            with self.subTest(table=existing):
                self.assertNotIn(f"alter table {existing} ", self.sql)

    def test_migration_drops_no_existing_policy_or_object(self):
        self.assertNotIn("drop table", self.sql)
        self.assertNotIn("drop column", self.sql)
        self.assertNotIn("disable row level security", self.sql)
        # The only DROP is the idempotence guard for this table's own policy.
        self.assertEqual(self.sql.count("drop "), 1)


class TestModelMatchesMigration(unittest.TestCase):
    """models.py and the migration are kept in sync by hand — there is no
    migration-generation tool in this project — so the columns are
    compared directly."""

    def test_orm_columns_match_the_migration_columns(self):
        self.assertEqual(
            sorted(c.name for c in PaperIntelligenceRow.__table__.columns),
            [
                "created_at",
                "generated_at",
                "id",
                "intelligence",
                "model",
                "owner_id",
                "paper_id",
                "schema_version",
                "updated_at",
            ],
        )

    def test_orm_table_name_matches(self):
        self.assertEqual(PaperIntelligenceRow.__tablename__, "paper_intelligence")

    def test_orm_declares_the_same_uniqueness(self):
        uniques = [
            tuple(sorted(col.name for col in constraint.columns))
            for constraint in PaperIntelligenceRow.__table__.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        ]
        self.assertEqual(uniques, [("owner_id", "paper_id")])

    def test_orm_adds_no_index_the_migration_lacks(self):
        self.assertEqual(list(PaperIntelligenceRow.__table__.indexes), [])


# ----------------------------------------------------------------------
# Behaviour: the real store against an isolated in-memory database
# ----------------------------------------------------------------------
class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = attach_sqlite_db(self)
        self._seed_paper(OWNER_A, PAPER_A, "Owner A's paper")
        self._seed_paper(OWNER_B, PAPER_B, "Owner B's paper")

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

    def _rows(self, owner_id=None):
        """Every stored row, read with no owner filter at all, so a test
        can see what the store would hide from a caller."""
        with self.factory() as session:
            query = session.query(PaperIntelligenceRow)
            if owner_id is not None:
                query = query.filter(
                    PaperIntelligenceRow.owner_id == uuid.UUID(owner_id)
                )
            return query.all()

    def _raw_intelligence(self, owner_id, paper_id):
        rows = [
            row
            for row in self._rows(owner_id)
            if row.paper_id == uuid.UUID(paper_id)
        ]
        self.assertEqual(len(rows), 1)
        return rows[0].intelligence


class TestRoundTrip(StoreTestCase):
    """4-7: what goes in comes back out, unchanged."""

    def test_jsonb_intelligence_round_trips(self):
        original = _validated()

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        restored = get_intelligence(OWNER_A, PAPER_A)

        self.assertIsNotNone(restored)
        self.assertEqual(
            restored.intelligence.model_dump(mode="json"),
            original.model_dump(mode="json"),
        )

    def test_every_section_survives_including_not_specified_ones(self):
        original = _validated(
            dataset=_section(status="not_specified", summary=None),
            limitations=_section(status="not_specified", summary=None),
        )

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        restored = get_intelligence(OWNER_A, PAPER_A).intelligence

        self.assertEqual(sorted(restored.sections()), sorted(SECTION_NAMES))
        self.assertEqual(restored.dataset.status, "not_specified")
        self.assertIsNone(restored.dataset.summary)
        self.assertEqual(restored.dataset.evidence, [])
        self.assertEqual(restored.methodology.status, "answered")

    def test_status_summary_and_quote_are_stored_verbatim(self):
        original = _validated(
            key_results=_section(
                summary="Precision rose to 0.94 on the held-out split.",
                evidence=[{"page": 7, "chunk_id": 0, "quote": ALLOWED[(7, 0)]}],
            )
        )

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        section = get_intelligence(OWNER_A, PAPER_A).intelligence.key_results

        self.assertEqual(section.status, "answered")
        self.assertEqual(section.summary, "Precision rose to 0.94 on the held-out split.")
        self.assertEqual(section.evidence[0].quote, ALLOWED[(7, 0)])

    def test_generated_at_persists(self):
        save_intelligence(
            OWNER_A, PAPER_A, _validated(), model=TEST_MODEL,
            generated_at=GENERATED_AT,
        )

        restored = get_intelligence(OWNER_A, PAPER_A)

        self.assertEqual(_as_utc(restored.generated_at), GENERATED_AT)

    def test_generated_at_defaults_to_now_when_not_supplied(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=1)

        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        stamped = _as_utc(get_intelligence(OWNER_A, PAPER_A).generated_at)
        self.assertGreaterEqual(stamped, before)

    def test_model_persists(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        self.assertEqual(get_intelligence(OWNER_A, PAPER_A).model, TEST_MODEL)

    def test_schema_version_persists_and_defaults_to_the_constant(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        restored = get_intelligence(OWNER_A, PAPER_A)

        self.assertEqual(restored.schema_version, PAPER_INTELLIGENCE_SCHEMA_VERSION)
        self.assertEqual(PAPER_INTELLIGENCE_SCHEMA_VERSION, "1")

    def test_an_explicit_schema_version_is_recorded_as_given(self):
        save_intelligence(
            OWNER_A, PAPER_A, _validated(), model=TEST_MODEL, schema_version="7",
        )

        self.assertEqual(get_intelligence(OWNER_A, PAPER_A).schema_version, "7")

    def test_save_returns_what_a_later_read_returns(self):
        saved = save_intelligence(
            OWNER_A, PAPER_A, _validated(), model=TEST_MODEL,
            generated_at=GENERATED_AT,
        )
        read = get_intelligence(OWNER_A, PAPER_A)

        self.assertEqual(saved.paper_id, read.paper_id)
        self.assertEqual(saved.model, read.model)
        self.assertEqual(saved.schema_version, read.schema_version)
        self.assertEqual(_as_utc(saved.generated_at), _as_utc(read.generated_at))
        self.assertEqual(
            saved.intelligence.model_dump(mode="json"),
            read.intelligence.model_dump(mode="json"),
        )

    def test_missing_intelligence_reads_as_none(self):
        self.assertIsNone(get_intelligence(OWNER_A, PAPER_A))

    def test_unknown_or_malformed_paper_reads_as_none(self):
        for paper_id in (UNKNOWN_PAPER, "not-a-uuid", "", None):
            with self.subTest(paper_id=paper_id):
                self.assertIsNone(get_intelligence(OWNER_A, paper_id))


class TestUpsert(StoreTestCase):
    """8: one current row per (owner, paper), never two."""

    def test_saving_twice_replaces_rather_than_duplicates(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model="first")
        save_intelligence(
            OWNER_A,
            PAPER_A,
            _validated(key_results=_section(summary="A revised finding.")),
            model="second",
        )

        rows = self._rows()
        self.assertEqual(len(rows), 1)

        restored = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(restored.model, "second")
        self.assertEqual(
            restored.intelligence.key_results.summary, "A revised finding."
        )

    def test_regeneration_moves_generated_at_and_updated_at(self):
        first = save_intelligence(
            OWNER_A, PAPER_A, _validated(), model=TEST_MODEL,
            generated_at=GENERATED_AT,
        )
        later = GENERATED_AT + timedelta(hours=2)
        second = save_intelligence(
            OWNER_A, PAPER_A, _validated(), model=TEST_MODEL, generated_at=later,
        )

        self.assertEqual(_as_utc(first.generated_at), GENERATED_AT)
        self.assertEqual(_as_utc(second.generated_at), later)
        self.assertGreaterEqual(
            _as_utc(second.updated_at), _as_utc(first.updated_at)
        )

    def test_each_paper_keeps_its_own_row(self):
        self._seed_paper(OWNER_A, UNKNOWN_PAPER, "Owner A's second paper")

        save_intelligence(OWNER_A, PAPER_A, _validated(), model="paper-one")
        save_intelligence(OWNER_A, UNKNOWN_PAPER, _validated(), model="paper-two")

        self.assertEqual(len(self._rows(OWNER_A)), 2)
        self.assertEqual(get_intelligence(OWNER_A, PAPER_A).model, "paper-one")
        self.assertEqual(
            get_intelligence(OWNER_A, UNKNOWN_PAPER).model, "paper-two"
        )

    def test_a_concurrent_first_insert_resolves_to_one_row(self):
        """UNIQUE(owner_id, paper_id) is what makes the loser of the race
        retry into an update instead of creating a second current row."""
        from sqlalchemy.exc import IntegrityError

        calls = {"n": 0}
        real_scope = session_scope

        def racing_scope(owner_id):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the winner committing between our SELECT and
                # our INSERT: the constraint rejects this attempt.
                raise IntegrityError("insert", {}, Exception("unique violation"))
            return real_scope(owner_id)

        with patch(
            "app.services.paper_intelligence_store.session_scope",
            side_effect=racing_scope,
        ):
            save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        self.assertEqual(calls["n"], 2, "the store should have retried once")
        self.assertEqual(len(self._rows()), 1)


class TestOwnerIsolation(StoreTestCase):
    """9-11, 13: nothing crosses between owners."""

    def setUp(self):
        super().setUp()
        self.saved = save_intelligence(
            OWNER_A, PAPER_A, _validated(), model="owner-a-model",
            generated_at=GENERATED_AT,
        )

    # -- 9. reads -------------------------------------------------------
    def test_owner_b_cannot_read_owner_a_intelligence(self):
        self.assertIsNone(get_intelligence(OWNER_B, PAPER_A))

    def test_owner_b_reading_its_own_paper_sees_only_its_own(self):
        save_intelligence(OWNER_B, PAPER_B, _validated(), model="owner-b-model")

        self.assertEqual(get_intelligence(OWNER_B, PAPER_B).model, "owner-b-model")
        self.assertEqual(get_intelligence(OWNER_A, PAPER_A).model, "owner-a-model")
        self.assertIsNone(get_intelligence(OWNER_B, PAPER_A))
        self.assertIsNone(get_intelligence(OWNER_A, PAPER_B))

    # -- 10. writes -----------------------------------------------------
    def test_owner_b_cannot_update_owner_a_intelligence(self):
        with self.assertRaises(PaperNotOwned):
            save_intelligence(
                OWNER_B,
                PAPER_A,
                _validated(key_results=_section(summary="Injected by B.")),
                model="owner-b-model",
            )

        untouched = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(untouched.model, "owner-a-model")
        self.assertEqual(untouched.intelligence.key_results.summary, "A summary.")
        self.assertEqual(len(self._rows()), 1)

    def test_a_rejected_cross_owner_write_creates_no_row_of_its_own(self):
        with self.assertRaises(PaperNotOwned):
            save_intelligence(OWNER_B, PAPER_A, _validated(), model="owner-b-model")

        self.assertEqual(self._rows(OWNER_B), [])

    # -- 11. deletes ----------------------------------------------------
    def test_owner_b_cannot_delete_owner_a_intelligence(self):
        self.assertFalse(delete_intelligence(OWNER_B, PAPER_A))

        self.assertEqual(len(self._rows()), 1)
        self.assertIsNotNone(get_intelligence(OWNER_A, PAPER_A))

    def test_an_owner_can_delete_its_own(self):
        self.assertTrue(delete_intelligence(OWNER_A, PAPER_A))

        self.assertEqual(self._rows(), [])
        self.assertIsNone(get_intelligence(OWNER_A, PAPER_A))

    def test_deleting_what_is_not_there_is_false_not_an_error(self):
        for owner, paper in (
            (OWNER_A, UNKNOWN_PAPER),
            (OWNER_A, "not-a-uuid"),
            (OWNER_B, PAPER_B),
        ):
            with self.subTest(owner=owner, paper=paper):
                self.assertFalse(delete_intelligence(owner, paper))

    # -- 13. an arbitrary owner_id buys nothing -------------------------
    def test_a_stranger_owner_id_cannot_read_write_or_delete(self):
        self.assertIsNone(get_intelligence(STRANGER, PAPER_A))
        self.assertFalse(delete_intelligence(STRANGER, PAPER_A))
        with self.assertRaises(PaperNotOwned):
            save_intelligence(STRANGER, PAPER_A, _validated(), model="stranger")

        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(get_intelligence(OWNER_A, PAPER_A).model, "owner-a-model")

    def test_writing_to_a_paper_that_does_not_exist_is_refused(self):
        with self.assertRaises(PaperNotOwned):
            save_intelligence(OWNER_A, UNKNOWN_PAPER, _validated(), model=TEST_MODEL)

    def test_ownership_is_decided_against_the_database_not_the_argument(self):
        """The paper row is what says who owns it. Nothing the caller
        passes alongside can stand in for that."""
        with self.assertRaises(PaperNotOwned):
            save_intelligence(OWNER_B, PAPER_A, _validated(), model=TEST_MODEL)

        # ... and the same owner/paper pairing succeeds once the paper is
        # genuinely theirs.
        self.assertIsNotNone(
            save_intelligence(OWNER_B, PAPER_B, _validated(), model=TEST_MODEL)
        )

    def test_every_query_carries_an_explicit_owner_filter(self):
        """RLS is the backstop; the application filter is load-bearing,
        and the offline database has no RLS at all. Asserted at the
        source so it cannot be quietly dropped later."""
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        filters = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "filter"
        ]
        self.assertTrue(filters, "the store should filter its queries")

        for call in filters:
            rendered = [ast.unparse(arg) for arg in call.args]
            self.assertTrue(
                any("owner_id ==" in arg for arg in rendered),
                f"a query filters without an owner term: {rendered}",
            )


# ----------------------------------------------------------------------
# 12: identity binding
# ----------------------------------------------------------------------
class TestClaimsBinding(StoreTestCase):
    """The store must reach the database only through the session that
    binds the verified identity. test_c1_jwt_claims.py owns the mechanism;
    this asserts this module actually uses it."""

    def _pg_connection(self):
        conn = MagicMock()
        conn.dialect.name = "postgresql"
        return conn

    def test_every_store_call_opens_a_session_with_the_given_owner(self):
        seen = []
        real_new_session = session_module._new_session

        def spy(owner_id):
            seen.append(owner_id)
            return real_new_session(owner_id)

        with patch.object(session_module, "_new_session", side_effect=spy):
            save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)
            get_intelligence(OWNER_A, PAPER_A)
            delete_intelligence(OWNER_A, PAPER_A)

        self.assertTrue(seen)
        self.assertEqual(set(seen), {OWNER_A})

    def test_a_session_with_no_owner_binds_no_claim(self):
        """Which is the point: under FORCE RLS, auth.uid() is then null,
        `owner_id = auth.uid()` is null for every row, and the table is
        empty and unwritable rather than wide open."""
        session = self.factory()
        try:
            conn = self._pg_connection()
            _bind_jwt_claims(session, None, conn)
            conn.execute.assert_not_called()
        finally:
            session.close()

    def test_a_session_opened_for_an_owner_binds_that_owner(self):
        with session_scope(OWNER_A) as db:
            conn = self._pg_connection()
            _bind_jwt_claims(db, None, conn)
            conn.execute.assert_called_once()
            _, params = conn.execute.call_args[0]
            self.assertEqual(params["setting"], JWT_CLAIMS_SETTING)
            self.assertEqual(json.loads(params["claims"]), {"sub": OWNER_A})

    def test_the_store_never_opens_its_own_connection(self):
        """One session mechanism, no second engine or connection path."""
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("create_engine", "sessionmaker", "psycopg", "connect("):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


# ----------------------------------------------------------------------
# 14-16: validation, evidence identity, no point ids
# ----------------------------------------------------------------------
class TestValidationIsEnforcedBeforePersistence(StoreTestCase):

    def test_a_raw_dict_cannot_be_persisted(self):
        """The only way to obtain a PaperIntelligence is through the
        validator, so requiring the type here is what closes the path
        from raw model output to the table."""
        with self.assertRaises(TypeError):
            save_intelligence(OWNER_A, PAPER_A, _payload(), model=TEST_MODEL)

        self.assertEqual(self._rows(), [])

    def test_a_json_string_cannot_be_persisted(self):
        with self.assertRaises(TypeError):
            save_intelligence(
                OWNER_A, PAPER_A, json.dumps(_payload()), model=TEST_MODEL
            )

        self.assertEqual(self._rows(), [])

    def test_unsupplied_evidence_never_reaches_the_store(self):
        """The validator refuses first; there is nothing left to persist."""
        with self.assertRaises(IntelligenceValidationError):
            _validated(
                methodology=_section(evidence=[{"page": 4, "chunk_id": 0}])
            )

        self.assertEqual(self._rows(), [])

    def test_an_answered_section_with_no_evidence_never_reaches_the_store(self):
        with self.assertRaises(IntelligenceValidationError):
            _validated(dataset=_section(summary="Claimed.", evidence=[]))

        self.assertEqual(self._rows(), [])

    def test_unreadable_stored_json_is_refused_rather_than_repaired(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        # Simulate a row that predates a schema change, or was edited by
        # hand: a section has gone missing.
        with self.factory() as session:
            row = session.query(PaperIntelligenceRow).one()
            broken = dict(row.intelligence)
            broken.pop("limitations")
            row.intelligence = broken
            session.commit()

        with self.assertRaises(IntelligenceValidationError) as ctx:
            get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(ctx.exception.code, "invalid_structure")


class TestEvidenceIdentityIsPreserved(StoreTestCase):
    """15-16: (page, chunk_id), and nothing else."""

    def test_evidence_keeps_page_and_chunk_id_through_a_round_trip(self):
        original = _validated(
            methodology=_section(
                evidence=[
                    {"page": 3, "chunk_id": 0, "quote": ALLOWED[(3, 0)]},
                    {"page": 3, "chunk_id": 1, "quote": ALLOWED[(3, 1)]},
                    {"page": 7, "chunk_id": 0, "quote": ALLOWED[(7, 0)]},
                ]
            )
        )

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        evidence = get_intelligence(OWNER_A, PAPER_A).intelligence.methodology.evidence

        self.assertEqual(
            [(item.page, item.chunk_id) for item in evidence],
            [(3, 0), (3, 1), (7, 0)],
        )

    def test_same_chunk_id_on_different_pages_stays_distinguishable(self):
        """chunk_id is an index within its page. A store that flattened
        evidence to chunk_id alone would collapse these two."""
        original = _validated(
            key_results=_section(
                evidence=[
                    {"page": 3, "chunk_id": 0},
                    {"page": 7, "chunk_id": 0},
                ]
            )
        )

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        evidence = get_intelligence(OWNER_A, PAPER_A).intelligence.key_results.evidence

        self.assertEqual([item.page for item in evidence], [3, 7])
        self.assertEqual(len({(e.page, e.chunk_id) for e in evidence}), 2)

    def test_evidence_order_is_preserved(self):
        original = _validated(
            contributions=_section(
                evidence=[
                    {"page": 7, "chunk_id": 0},
                    {"page": 1, "chunk_id": 0},
                    {"page": 3, "chunk_id": 1},
                ]
            )
        )

        save_intelligence(OWNER_A, PAPER_A, original, model=TEST_MODEL)
        evidence = get_intelligence(OWNER_A, PAPER_A).intelligence.contributions.evidence

        self.assertEqual([item.page for item in evidence], [7, 1, 3])

    def test_stored_evidence_carries_no_field_beyond_the_three(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        stored = self._raw_intelligence(OWNER_A, PAPER_A)
        for name, section in stored.items():
            for item in section["evidence"]:
                with self.subTest(section=name):
                    self.assertEqual(
                        set(item), {"page", "chunk_id", "quote"}
                    )

    def test_no_qdrant_point_id_is_persisted(self):
        """Point ids are regenerated on every index run, so a stored one
        would rot into a dangling reference. The schema has no field for
        one, and this asserts none arrives by another route."""
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        blob = json.dumps(self._raw_intelligence(OWNER_A, PAPER_A))

        for forbidden in ("point_id", "point", "qdrant", "vector_id"):
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, blob.lower())

    def test_a_model_supplied_point_id_is_rejected_before_the_store(self):
        with self.assertRaises(IntelligenceValidationError):
            _validated(
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

        self.assertEqual(self._rows(), [])


# ----------------------------------------------------------------------
# 17: nothing leaves the process
# ----------------------------------------------------------------------
class TestNoProviderOrNetworkAccess(unittest.TestCase):

    def _imported_roots(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots.add((node.module or "").split(".")[0])
        return roots

    def test_the_store_imports_nothing_beyond_its_stated_dependencies(self):
        """Parsed, not grepped: the module's own docstring names the
        providers it does not touch, and a text search would match that
        prose."""
        self.assertEqual(
            self._imported_roots(),
            {"uuid", "dataclasses", "datetime", "typing", "pydantic",
             "sqlalchemy", "app"},
        )

    def test_no_provider_or_transport_library_is_imported(self):
        forbidden = {
            "groq", "qdrant_client", "voyageai", "openai", "supabase",
            "httpx", "requests", "urllib", "urllib3", "http", "socket",
            "aiohttp", "boto3",
        }
        self.assertEqual(self._imported_roots() & forbidden, set())

    def test_no_second_orm_or_database_abstraction(self):
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("sqlmodel", "asyncpg", "databases", "peewee", "alembic"):
            with self.subTest(library=forbidden):
                self.assertNotIn(forbidden, source)


class TestNoSocketIsOpened(StoreTestCase):
    """The strongest form of 17: a full save/read/delete cycle with the
    socket constructor booby-trapped."""

    def test_a_full_cycle_opens_no_socket(self):
        def explode(*args, **kwargs):
            raise AssertionError("the store opened a network socket")

        with patch.object(socket, "socket", explode):
            save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)
            restored = get_intelligence(OWNER_A, PAPER_A)
            removed = delete_intelligence(OWNER_A, PAPER_A)

        self.assertIsNotNone(restored)
        self.assertTrue(removed)


# ----------------------------------------------------------------------
# 18: a failed write leaves nothing behind
# ----------------------------------------------------------------------
class TestTransactionalIntegrity(StoreTestCase):

    def test_a_failure_mid_write_leaves_no_partial_row(self):
        boom = RuntimeError("failed after the insert, before the commit")

        with patch(
            "app.services.paper_intelligence_store._parse_stored", side_effect=boom
        ):
            with self.assertRaises(RuntimeError):
                save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        self.assertEqual(self._rows(), [])
        self.assertIsNone(get_intelligence(OWNER_A, PAPER_A))

    def test_a_failed_regeneration_leaves_the_previous_row_intact(self):
        save_intelligence(
            OWNER_A, PAPER_A, _validated(), model="original",
            generated_at=GENERATED_AT,
        )

        with patch(
            "app.services.paper_intelligence_store._parse_stored",
            side_effect=RuntimeError("failed mid-update"),
        ):
            with self.assertRaises(RuntimeError):
                save_intelligence(
                    OWNER_A,
                    PAPER_A,
                    _validated(key_results=_section(summary="Half-written.")),
                    model="replacement",
                )

        restored = get_intelligence(OWNER_A, PAPER_A)
        self.assertEqual(restored.model, "original")
        self.assertEqual(_as_utc(restored.generated_at), GENERATED_AT)
        self.assertEqual(restored.intelligence.key_results.summary, "A summary.")
        self.assertEqual(len(self._rows()), 1)

    def test_a_failed_delete_leaves_the_row(self):
        save_intelligence(OWNER_A, PAPER_A, _validated(), model=TEST_MODEL)

        original_commit = type(self.factory()).commit

        def failing_commit(self, *args, **kwargs):
            raise RuntimeError("commit failed")

        with patch.object(type(self.factory()), "commit", failing_commit):
            with self.assertRaises(RuntimeError):
                delete_intelligence(OWNER_A, PAPER_A)

        self.assertEqual(len(self._rows()), 1)
        self.assertIsNotNone(get_intelligence(OWNER_A, PAPER_A))
        self.assertIsNotNone(original_commit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
