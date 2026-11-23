"""
The paper lifecycle against real PostgreSQL 17.

test_paper_lifecycle_coordination.py proves the claim PROTOCOL on SQLite,
which serialises writers. What only PostgreSQL can show is that two
conditional UPDATEs of one row really cannot both succeed: under READ
COMMITTED the second waits for the first's row lock and then re-checks its
WHERE clause against the committed row. These tests make that happen —
independent connections, real transactions, a row lock held open while a
competing claim provably waits on it — and run as the restricted app role,
so row-level security is the production one.

OPT-IN, AND FAIL CLOSED. Skipped by declaration unless
RUN_POSTGRES_LIFECYCLE_TESTS=1. Once enabled, every test ERRORS — never
skips — unless POSTGRES_LIFECYCLE_TEST_URL names a disposable database:
  - host 127.0.0.1 or ::1 with an explicit port,
  - a database whose name starts with researchmind_lifecycle_test,
  - the researchmind_app role, which the server confirms is neither a
    superuser nor able to bypass RLS, with RLS forced on papers,
  - and never the URL in DATABASE_URL.
The application's own engine is never built: get_session_factory() is
patched to one bound to that URL, so DATABASE_URL is never read.

Database setup (as a superuser, on a throwaway PostgreSQL 17): apply
tests/fixtures/postgres/supabase_shim.sql, migrations/bootstrap/app_role.sql
and migrations 0001, 0003-0007, then give researchmind_app a password.
Nothing else: in particular NOT `usage on schema auth`, which production's
researchmind_app does not hold either.
"""
import json
import os
import sys
import threading
import time
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import ArgumentError, DBAPIError, IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.api.upload import CONTENT_UNIQUE_CONSTRAINT, _is_duplicate_file  # noqa: E402
from app.db.models import Paper  # noqa: E402
from app.db.session import _new_session  # noqa: E402
from app.services import paper_lifecycle  # noqa: E402
from app.services.retrieval_guard import keep_live_hits, live_paper_ids  # noqa: E402

RUN_FLAG = "RUN_POSTGRES_LIFECYCLE_TESTS"
URL_VARIABLE = "POSTGRES_LIFECYCLE_TEST_URL"
REPORT_VARIABLE = "POSTGRES_LIFECYCLE_REPORT_DIR"
DATABASE_PREFIX = "researchmind_lifecycle_test"
APP_ROLE = "researchmind_app"

#: In auth.users (tests/fixtures/postgres/supabase_shim.sql).
OWNER_A = "a1a1a1a1-6666-4666-8666-a1a1a1a1a1a1"
OWNER_B = "b2b2b2b2-6666-4666-8666-b2b2b2b2b2b2"
#: Deliberately NOT in auth.users.
NOT_A_USER = "c3c3c3c3-6666-4666-8666-c3c3c3c3c3c3"
A, B = uuid.UUID(OWNER_A), uuid.UUID(OWNER_B)

ROUNDS = int(os.environ.get("POSTGRES_LIFECYCLE_ROUNDS", "50"))
RACERS = 8

requires_postgres = unittest.skipUnless(
    os.environ.get(RUN_FLAG) == "1",
    f"PostgreSQL lifecycle test — needs a disposable database. Set {RUN_FLAG}=1 "
    f"and {URL_VARIABLE}. Skipped by declaration.",
)


def checked_url():
    """The test database URL, or an error naming every reason to refuse it."""
    raw = os.environ.get(URL_VARIABLE, "")
    try:
        url = make_url(raw)
    except ArgumentError:
        raise RuntimeError(f"refusing to run: {URL_VARIABLE} is missing or malformed")
    refusals = []
    if url.drivername != "postgresql+psycopg":
        refusals.append("driver is not postgresql+psycopg")
    if url.host not in ("127.0.0.1", "::1"):
        refusals.append("host is not 127.0.0.1 or ::1")
    if not url.port:
        refusals.append("no explicit port")
    if not (url.database or "").startswith(DATABASE_PREFIX):
        refusals.append(f"database name does not start with {DATABASE_PREFIX}")
    if url.username != APP_ROLE:
        refusals.append(f"role is not {APP_ROLE}")
    production = os.environ.get("DATABASE_URL")
    if production and production.strip() == raw.strip():
        refusals.append("it is DATABASE_URL")
    if refusals:
        raise RuntimeError("refusing to run: " + "; ".join(refusals))
    return url


def verify_server(engine):
    """The server must confirm what the URL claims, or nothing runs."""
    with engine.connect() as connection:
        found = connection.execute(text(
            "select current_user, current_database(), r.rolsuper, r.rolbypassrls, "
            "c.relrowsecurity, c.relforcerowsecurity "
            "from pg_roles r, pg_class c "
            "where r.rolname = current_user and c.oid = 'public.papers'::regclass"
        )).one()
    refusals = []
    if found[0] != APP_ROLE:
        refusals.append(f"connected as {found[0]}")
    if not found[1].startswith(DATABASE_PREFIX):
        refusals.append(f"database {found[1]}")
    if found[2] or found[3]:
        refusals.append("role is a superuser or bypasses RLS")
    if not (found[4] and found[5]):
        refusals.append("RLS is not enabled and forced on papers")
    if refusals:
        raise RuntimeError("refusing to run: " + "; ".join(refusals))


@requires_postgres
class PostgresTestCase(unittest.TestCase):
    engine = None
    factory = None

    @classmethod
    def setUpClass(cls):
        url = checked_url()
        # lock_timeout / statement_timeout turn a hang into a failure.
        cls.engine = create_engine(
            url,
            pool_size=RACERS + 8,
            max_overflow=8,
            pool_pre_ping=True,
            connect_args={"options": "-c lock_timeout=10000 -c statement_timeout=60000"},
        )
        cls.factory = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False)
        cls._factory_patch = patch("app.db.session.get_session_factory", return_value=cls.factory)
        cls._factory_patch.start()
        try:
            verify_server(cls.engine)
        except Exception:
            cls._factory_patch.stop()
            cls.engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._factory_patch.stop()
        cls.engine.dispose()

    # -- sessions ----------------------------------------------------------------

    def session(self, owner=None) -> Session:
        """A session as the app opens one: bound to `owner` (the verified JWT
        sub), so every transaction it begins carries that identity — or, with
        no owner, no identity at all."""
        return _new_session(owner) if owner else self.factory()

    def held(self, owner) -> Session:
        """A session whose commits are deferred. A real lifecycle function run
        in it leaves its statement uncommitted, holding the row lock, until
        settle() commits or rolls back."""
        session = self.session(owner)
        session.commit = lambda: None
        return session

    @staticmethod
    def settle(session, commit=True):
        (Session.commit if commit else Session.rollback)(session)
        session.close()

    def contend(self, owner, call):
        """Runs call(session) on another thread and connection. Returns the
        thread, that connection's backend pid, and where the outcome lands."""
        session = self.session(owner)
        backend = session.execute(text("select pg_backend_pid()")).scalar()
        outcome = {}

        def run():
            try:
                outcome["result"] = call(session)
            except Exception as exc:  # recorded, then asserted on
                outcome["error"] = exc
            finally:
                session.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, backend, outcome

    def assert_waits_on_a_row_lock(self, backend, timeout=10.0):
        """Proves the competing statement is BLOCKED on the held row, so the
        result that follows comes from PostgreSQL's re-check after the
        holder commits — not from the two simply running one after another."""
        deadline = time.monotonic() + timeout
        with self.engine.connect() as connection:
            while time.monotonic() < deadline:
                state = connection.execute(
                    text("select wait_event_type from pg_stat_activity where pid = :pid"),
                    {"pid": backend},
                ).scalar()
                connection.rollback()  # a fresh pg_stat_activity snapshot each poll
                if state == "Lock":
                    return
                time.sleep(0.02)
        self.fail(f"backend {backend} never waited on a lock")

    def finish(self, thread, outcome):
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the competing statement never returned")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    # -- rows ----------------------------------------------------------------------

    def seed(self, owner=OWNER_A, status="uploaded", *, content_hash=None, updated_at=None,
             status_detail=None):
        paper_id = uuid.uuid4()
        with self.session(owner) as session:
            session.add(Paper(
                id=paper_id, owner_id=uuid.UUID(owner), title=f"{status}.pdf",
                content_hash=content_hash or uuid.uuid4().hex * 2,
                storage_path=f"{owner}/{paper_id}/original.pdf", file_size_bytes=1,
                status=status, status_detail=status_detail,
                updated_at=updated_at or paper_lifecycle.now(),
            ))
            session.commit()
        return paper_id

    def expired(self, status, lease, owner=OWNER_A, **kwargs):
        """A paper whose claim lapsed: (id, the token its stalled worker holds)."""
        stale = paper_lifecycle.now() - lease - timedelta(seconds=1)
        return self.seed(owner, status, updated_at=stale, **kwargs), stale

    def row(self, paper_id, owner=OWNER_A):
        with self.session(owner) as session:
            found = session.execute(
                text("select status, status_detail, updated_at from papers where id = :id"),
                {"id": paper_id},
            ).one_or_none()
            session.commit()
            return found


# ---------------------------------------------------------------------------
# T1 — deletion versus indexing
# ---------------------------------------------------------------------------
class TestDeletionVersusIndexing(PostgresTestCase):

    def test_a_held_index_claim_makes_deletion_wait_then_fail_once_it_commits(self):
        paper = self.seed()
        holder = self.held(OWNER_A)
        self.assertIsNotNone(paper_lifecycle.claim(
            holder, paper, A, from_status="uploaded", to_status="indexing"))

        thread, backend, outcome = self.contend(
            OWNER_A, lambda s: paper_lifecycle.claim_for_deletion(s, paper, A))
        self.assert_waits_on_a_row_lock(backend)
        self.settle(holder, commit=True)

        self.assertIsNone(self.finish(thread, outcome))
        self.assertEqual(self.row(paper).status, "indexing")

    def test_a_rolled_back_index_claim_lets_the_waiting_deletion_win(self):
        paper = self.seed()
        holder = self.held(OWNER_A)
        self.assertIsNotNone(paper_lifecycle.claim(
            holder, paper, A, from_status="uploaded", to_status="indexing"))

        thread, backend, outcome = self.contend(
            OWNER_A, lambda s: paper_lifecycle.claim_for_deletion(s, paper, A))
        self.assert_waits_on_a_row_lock(backend)
        self.settle(holder, commit=False)

        token = self.finish(thread, outcome)
        self.assertIsNotNone(token)
        found = self.row(paper)
        self.assertEqual((found.status, found.updated_at), ("deleting", token))

    def test_a_held_deletion_claim_makes_indexing_wait_then_fail_and_never_proceed(self):
        paper = self.seed()
        holder = self.held(OWNER_A)
        deletion = paper_lifecycle.claim_for_deletion(holder, paper, A)
        self.assertIsNotNone(deletion)

        thread, backend, outcome = self.contend(
            OWNER_A, lambda s: paper_lifecycle.claim(s, paper, A, from_status="uploaded", to_status="indexing"))
        self.assert_waits_on_a_row_lock(backend)
        self.settle(holder, commit=True)

        self.assertIsNone(self.finish(thread, outcome))
        found = self.row(paper)
        self.assertEqual((found.status, found.updated_at), ("deleting", deletion))
        # No indexer can confirm a claim on it either, whatever token it holds.
        with self.session(OWNER_A) as session:
            self.assertIsNone(paper_lifecycle.renew(session, paper, A, "indexing", deletion))

    def test_deletion_cannot_claim_a_paper_while_it_is_being_indexed(self):
        paper = self.seed()
        with self.session(OWNER_A) as session:
            indexing = paper_lifecycle.claim(session, paper, A, from_status="uploaded", to_status="indexing")
        with self.session(OWNER_A) as session:
            self.assertIsNone(paper_lifecycle.claim_for_deletion(session, paper, A))
        found = self.row(paper)
        self.assertEqual((found.status, found.updated_at), ("indexing", indexing))

    def test_an_expired_index_claim_is_taken_over_and_its_stalled_worker_is_fenced_out(self):
        paper, stale = self.expired("indexing", paper_lifecycle.INDEXING_LEASE)
        with self.session(OWNER_A) as session:
            deletion = paper_lifecycle.claim_for_deletion(session, paper, A)
        self.assertIsNotNone(deletion)

        with self.session(OWNER_A) as session:
            # The stalled indexer's last check before writing vectors fails...
            self.assertIsNone(paper_lifecycle.renew(session, paper, A, "indexing", stale))
            # ...and so would its final transition.
            self.assertFalse(paper_lifecycle.transition(session, paper, A, "indexing", stale, to_status="indexed"))
        found = self.row(paper)
        self.assertEqual((found.status, found.updated_at), ("deleting", deletion))


# ---------------------------------------------------------------------------
# T2 — expired leases and stale workers
# ---------------------------------------------------------------------------
class TestExpiredLeasesAndStaleWorkers(PostgresTestCase):

    def expired_deletion(self, **kwargs):
        return self.expired("deleting", paper_lifecycle.DELETION_LEASE, **kwargs)

    def test_two_concurrent_takeovers_of_an_expired_lease_yield_exactly_one_claim(self):
        paper, _ = self.expired_deletion()
        barrier = threading.Barrier(2)
        tokens = []

        def takeover():
            with self.session(OWNER_A) as session:
                session.execute(text("select 1"))  # connection and identity ready
                barrier.wait()
                tokens.append(paper_lifecycle.claim_for_deletion(session, paper, A))

        threads = [threading.Thread(target=takeover) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        winners = [t for t in tokens if t is not None]
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(winners), 1, tokens)
        found = self.row(paper)
        self.assertEqual((found.status, found.status_detail, found.updated_at), ("deleting", None, winners[0]))

    def test_a_stale_token_can_neither_finish_release_renew_nor_transition(self):
        paper, stale = self.expired_deletion()
        with self.session(OWNER_A) as session:
            current = paper_lifecycle.claim_for_deletion(session, paper, A)
        self.assertIsNotNone(current)

        with self.session(OWNER_A) as session:
            self.assertFalse(paper_lifecycle.finish_deletion(session, paper, A, stale))
            self.assertFalse(paper_lifecycle.release_deletion(session, paper, A, stale))
            self.assertIsNone(paper_lifecycle.renew(session, paper, A, "deleting", stale))
            self.assertFalse(paper_lifecycle.transition(session, paper, A, "deleting", stale, to_status="failed"))

        # Untouched, to the microsecond, and still the rightful holder's.
        found = self.row(paper)
        self.assertEqual((found.status, found.status_detail, found.updated_at), ("deleting", None, current))
        with self.session(OWNER_A) as session:
            self.assertTrue(paper_lifecycle.finish_deletion(session, paper, A, current))
        self.assertIsNone(self.row(paper))

    def test_a_stale_finish_and_a_takeover_never_both_succeed(self):
        # The stalled worker's finish is in flight when the takeover arrives.
        paper, stale = self.expired_deletion()
        holder = self.held(OWNER_A)
        self.assertTrue(paper_lifecycle.finish_deletion(holder, paper, A, stale))
        thread, backend, outcome = self.contend(
            OWNER_A, lambda s: paper_lifecycle.claim_for_deletion(s, paper, A))
        self.assert_waits_on_a_row_lock(backend)
        self.settle(holder, commit=True)
        self.assertIsNone(self.finish(thread, outcome))
        self.assertIsNone(self.row(paper))

        # The takeover commits first: the stalled worker's finish then fails.
        paper, stale = self.expired_deletion()
        with self.session(OWNER_A) as session:
            current = paper_lifecycle.claim_for_deletion(session, paper, A)
        with self.session(OWNER_A) as session:
            self.assertFalse(paper_lifecycle.finish_deletion(session, paper, A, stale))
        self.assertEqual(self.row(paper).updated_at, current)

    def test_a_stale_token_cannot_touch_the_next_incarnation_of_the_paper(self):
        content_hash = uuid.uuid4().hex * 2
        old, stale = self.expired_deletion(content_hash=content_hash)
        with self.session(OWNER_A) as session:
            current = paper_lifecycle.claim_for_deletion(session, old, A)
        with self.session(OWNER_A) as session:
            self.assertTrue(paper_lifecycle.finish_deletion(session, old, A, current))

        # The same file uploaded again: a new row, a fresh id.
        new = self.seed(OWNER_A, "uploaded", content_hash=content_hash)
        self.assertNotEqual(new, old)
        before = self.row(new)

        with self.session(OWNER_A) as session:
            for paper in (old, new):
                self.assertFalse(paper_lifecycle.finish_deletion(session, paper, A, stale))
                self.assertFalse(paper_lifecycle.release_deletion(session, paper, A, stale))
            self.assertIsNone(paper_lifecycle.renew(session, new, A, "uploaded", stale))
            self.assertFalse(paper_lifecycle.transition(session, new, A, "uploaded", stale, to_status="failed"))
        self.assertEqual(tuple(self.row(new)), tuple(before))


# ---------------------------------------------------------------------------
# T3 — duplicate uploads and real driver diagnostics
# ---------------------------------------------------------------------------
class TestDuplicateUploadsOnPostgres(PostgresTestCase):

    def paper_row(self, owner, **overrides):
        values = dict(id=uuid.uuid4(), owner_id=uuid.UUID(owner), title="paper.pdf",
                      content_hash=uuid.uuid4().hex * 2, storage_path="x/y/original.pdf",
                      file_size_bytes=1, status="uploading", updated_at=paper_lifecycle.now())
        values.update(overrides)
        return Paper(**values)

    def integrity_error(self, owner, paper):
        with self.session(owner) as session:
            session.add(paper)
            with self.assertRaises(IntegrityError) as caught:
                session.commit()
            session.rollback()
        return caught.exception

    def test_concurrent_inserts_of_one_file_leave_one_row_and_a_recognised_unique_violation(self):
        content_hash = uuid.uuid4().hex * 2
        barrier = threading.Barrier(2)
        outcomes = []

        def insert():
            with self.session(OWNER_A) as session:
                session.execute(text("select 1"))
                session.add(self.paper_row(OWNER_A, content_hash=content_hash))
                barrier.wait()
                try:
                    session.commit()
                    outcomes.append(("stored", None))
                except IntegrityError as error:
                    session.rollback()
                    outcomes.append(("conflict", error))

        threads = [threading.Thread(target=insert) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(sorted(kind for kind, _ in outcomes), ["conflict", "stored"])
        error = next(e for kind, e in outcomes if kind == "conflict")
        self.assertEqual(error.orig.sqlstate, "23505")
        self.assertEqual(error.orig.diag.constraint_name, CONTENT_UNIQUE_CONSTRAINT)
        self.assertIs(_is_duplicate_file(error), True)
        with self.session(OWNER_A) as session:
            count = session.execute(
                text("select count(*) from papers where content_hash = :h"), {"h": content_hash}
            ).scalar()
        self.assertEqual(count, 1)

    def test_unrelated_integrity_errors_are_not_classified_as_duplicates(self):
        taken = self.seed(OWNER_A, "uploaded")
        cases = {
            # Bound as an identity with no auth.users row: RLS admits the
            # row (it is "theirs"), and the foreign key then rejects it.
            "foreign key": (NOT_A_USER, {"owner_id": uuid.UUID(NOT_A_USER)}, "23503", "papers_owner_id_fkey"),
            "not null": (OWNER_A, {"title": None}, "23502", None),
            "check": (OWNER_A, {"status": "bogus"}, "23514", "papers_status_check"),
            "primary key": (OWNER_A, {"id": taken}, "23505", "papers_pkey"),
        }
        for name, (owner, overrides, sqlstate, constraint) in cases.items():
            with self.subTest(violation=name):
                error = self.integrity_error(owner, self.paper_row(owner, **overrides))
                self.assertEqual(error.orig.sqlstate, sqlstate)
                self.assertEqual(error.orig.diag.constraint_name, constraint)
                self.assertIs(_is_duplicate_file(error), False)


# ---------------------------------------------------------------------------
# T4 — the restricted app role and row-level security
# ---------------------------------------------------------------------------
class TestRowLevelSecurityAsTheAppRole(PostgresTestCase):

    def test_the_app_role_holds_exactly_the_production_privileges(self):
        with self.engine.connect() as connection:
            role = connection.execute(text(
                "select current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                "rolreplication, rolinherit, "
                "has_schema_privilege(current_user, 'auth', 'USAGE') as auth_usage, "
                "has_schema_privilege(current_user, 'public', 'USAGE') as public_usage "
                "from pg_roles where rolname = current_user"
            )).one()
            # Found through the catalogs: naming auth.uid() would itself need
            # USAGE on schema auth.
            uid = connection.execute(text(
                "select p.oid from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                "where n.nspname = 'auth' and p.proname = 'uid'"
            )).scalar()
            execute = connection.execute(
                text("select has_function_privilege(current_user, :oid, 'EXECUTE')"), {"oid": uid}
            ).scalar()
            users = connection.execute(text(
                "select c.oid from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'auth' and c.relname = 'users'"
            )).scalar()
            tables = connection.execute(text(
                "select has_table_privilege(current_user, 'public.papers', 'SELECT'), "
                "has_table_privilege(current_user, 'public.papers', 'INSERT'), "
                "has_table_privilege(current_user, 'public.papers', 'UPDATE'), "
                "has_table_privilege(current_user, 'public.papers', 'DELETE'), "
                "has_table_privilege(current_user, :users, 'SELECT')"
            ), {"users": users}).one()
            rls = connection.execute(text(
                "select relrowsecurity, relforcerowsecurity from pg_class where oid = 'public.papers'::regclass"
            )).one()
            isolation = connection.execute(text("show default_transaction_isolation")).scalar()
            version = connection.execute(text("show server_version")).scalar()

        print(f"[T4] role={dict(role._mapping)} execute_auth_uid={execute} papers(S,I,U,D)={tuple(tables[:4])} "
              f"auth.users_select={tables[4]} rls={tuple(rls)} isolation={isolation} server={version}")
        self.assertEqual(role.current_user, APP_ROLE)
        self.assertEqual(
            (role.rolsuper, role.rolbypassrls, role.rolcreaterole, role.rolcreatedb, role.rolreplication, role.rolinherit),
            (False, False, False, False, False, False),
        )
        # Production fidelity: no USAGE on schema auth.
        self.assertIs(role.auth_usage, False)
        self.assertIs(role.public_usage, True)
        self.assertIs(execute, True)
        self.assertEqual(tuple(tables), (True, True, True, True, False))
        self.assertEqual(tuple(rls), (True, True))
        self.assertEqual(isolation, "read committed")

    def test_policies_evaluate_auth_uid_without_usage_on_schema_auth(self):
        paper = self.seed(OWNER_A, "indexed")
        try:
            with self.session(OWNER_A) as session:
                mine = session.execute(text("select count(*) from papers where id = :id"), {"id": paper}).scalar()
            with self.session(OWNER_B) as session:
                theirs = session.execute(text("select count(*) from papers where id = :id"), {"id": paper}).scalar()
        except DBAPIError as error:
            self.fail(f"RLS failed without USAGE on schema auth: sqlstate={error.orig.sqlstate} {error.orig}")
        self.assertEqual((mine, theirs), (1, 0))

    def test_a_direct_call_to_auth_uid_is_denied_without_usage_on_schema_auth(self):
        """Informational: the application never calls it directly — only
        the policies do, which the test above shows still works."""
        with self.session(OWNER_A) as session:
            with self.assertRaises(DBAPIError) as caught:
                session.execute(text("select auth.uid()"))
            session.rollback()
        print(f"[T4] direct auth.uid(): sqlstate={caught.exception.orig.sqlstate} {caught.exception.orig}")
        self.assertEqual(caught.exception.orig.sqlstate, "42501")

    def test_owner_a_can_neither_see_nor_claim_nor_change_owner_bs_paper(self):
        theirs = self.seed(OWNER_B, "indexed")
        with self.session(OWNER_A) as session:
            self.assertEqual(session.execute(
                text("select count(*) from papers where id = :id"), {"id": theirs}).scalar(), 0)
            # No application predicate here: RLS alone stops it.
            self.assertEqual(session.execute(
                text("update papers set status = 'deleting' where id = :id"), {"id": theirs}).rowcount, 0)
            self.assertEqual(session.execute(
                text("delete from papers where id = :id"), {"id": theirs}).rowcount, 0)
            session.commit()
        with self.session(OWNER_A) as session:
            # The app's own predicate names B; RLS still hides B's row from A.
            self.assertIsNone(paper_lifecycle.claim_for_deletion(session, theirs, B))
            self.assertIsNone(paper_lifecycle.current_status(session, theirs, B))
        self.assertEqual(self.row(theirs, owner=OWNER_B).status, "indexed")

    def test_an_owner_cannot_hand_a_paper_to_another_owner(self):
        mine = self.seed(OWNER_A, "indexed")
        with self.session(OWNER_A) as session:
            with self.assertRaises(DBAPIError) as caught:
                session.execute(text("update papers set owner_id = :b where id = :id"), {"b": B, "id": mine})
            session.rollback()
        self.assertEqual(caught.exception.orig.sqlstate, "42501")
        self.assertEqual(self.row(mine).status, "indexed")

    def test_without_a_bound_identity_no_paper_is_visible(self):
        self.seed(OWNER_A, "indexed")
        with self.session() as session:
            self.assertEqual(session.execute(text("select count(*) from papers")).scalar(), 0)

    def test_live_paper_ids_returns_only_the_owners_indexed_papers(self):
        a_live = self.seed(OWNER_A, "indexed")
        a_others = [self.seed(OWNER_A, status) for status in ("uploading", "uploaded", "indexing", "failed", "deleting")]
        b_live = self.seed(OWNER_B, "indexed")
        everything = [a_live, *a_others, b_live, uuid.uuid4()]

        self.assertEqual(live_paper_ids(OWNER_A, everything), {a_live})
        self.assertEqual(live_paper_ids(OWNER_B, everything), {b_live})

        class Hit:
            def __init__(self, paper):
                self.payload = {"paper_id": str(paper)}

        hits = [Hit(p) for p in everything]
        self.assertEqual([h.payload["paper_id"] for h in keep_live_hits(OWNER_A, hits)], [str(a_live)])


# ---------------------------------------------------------------------------
# T5 — repeated races
# ---------------------------------------------------------------------------
class TestRepeatedRaces(PostgresTestCase):

    def race(self, calls):
        """Every call on its own connection, released together by a barrier.
        Returns (results, errors)."""
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)
        errors = []

        def run(index, call):
            try:
                with self.session(OWNER_A) as session:
                    session.execute(text("select 1"))  # connection and identity ready
                    barrier.wait()
                    results[index] = call(session)
            except Exception as exc:
                errors.append(f"{index}: {exc!r}")

        threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return results, errors

    def record(self, name, summary, rounds):
        print(f"[T5] {name}: {json.dumps(summary)}")
        directory = os.environ.get(REPORT_VARIABLE)
        if directory:
            with open(os.path.join(directory, f"t5_{name}.json"), "w", encoding="utf-8") as handle:
                json.dump({"summary": summary, "rounds": rounds}, handle, indent=1, default=str)

    def test_deletion_and_indexing_race_and_exactly_one_wins_every_round(self):
        half = RACERS // 2
        tally = {"deletion": 0, "indexing": 0}
        rounds, unexpected = [], []
        for number in range(ROUNDS):
            paper = self.seed(OWNER_A, "uploaded")
            calls = ([lambda s, p=paper: paper_lifecycle.claim_for_deletion(s, p, A)] * half
                     + [lambda s, p=paper: paper_lifecycle.claim(s, p, A, from_status="uploaded",
                                                                 to_status="indexing")] * half)
            results, errors = self.race(calls)
            winners = [i for i, r in enumerate(results) if r is not None]
            final = self.row(paper)
            kind = ("deletion" if winners[0] < half else "indexing") if len(winners) == 1 else None
            expected = {"deletion": "deleting", "indexing": "indexing"}.get(kind)
            ok = (not errors and kind is not None and final.status == expected
                  and final.updated_at == results[winners[0]])
            rounds.append({"round": number, "winners": len(winners), "kind": kind,
                           "final_status": final.status, "errors": errors})
            if ok:
                tally[kind] += 1
            else:
                unexpected.append(rounds[-1])

        self.record("delete_vs_index", {"rounds": ROUNDS, "racers": RACERS, **tally,
                                        "unexpected": len(unexpected)}, rounds)
        self.assertEqual(unexpected, [])

    def test_an_expired_lease_stampede_yields_one_claim_every_round(self):
        rounds, unexpected = [], []
        for number in range(ROUNDS):
            paper, stale = self.expired("deleting", paper_lifecycle.DELETION_LEASE)
            results, errors = self.race(
                [lambda s, p=paper: paper_lifecycle.claim_for_deletion(s, p, A)] * RACERS)
            winners = [r for r in results if r is not None]
            final = self.row(paper)
            with self.session(OWNER_A) as session:
                stale_finish = paper_lifecycle.finish_deletion(session, paper, A, stale)
            ok = (not errors and len(winners) == 1 and final.status == "deleting"
                  and final.status_detail is None and final.updated_at == winners[0]
                  and stale_finish is False)
            rounds.append({"round": number, "winners": len(winners), "final_status": final.status,
                           "stale_finish": stale_finish, "errors": errors})
            if not ok:
                unexpected.append(rounds[-1])

        self.record("expired_lease_stampede", {"rounds": ROUNDS, "racers": RACERS,
                                               "unexpected": len(unexpected)}, rounds)
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
