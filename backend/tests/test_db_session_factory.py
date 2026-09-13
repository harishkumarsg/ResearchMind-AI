"""
D2 step 1 — tests for the public session factory in app/db/session.py.

Fully offline and deterministic: every session is bound to an in-memory
SQLite engine created per-test. No Postgres, no Supabase, no network, no
external service of any kind. The lazy-engine contract is verified by
asserting the RuntimeError path rather than by letting a real engine be
constructed.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.session as session_module
from app.db.models import Base, Report
from app.db.session import get_db_session, get_session_factory, session_scope

OWNER = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _report(query="q"):
    return Report(owner_id=OWNER, query=query, report_markdown="body")


class SQLiteBackedTestCase(unittest.TestCase):
    """Per-test in-memory database. StaticPool keeps every connection
    pointed at the same :memory: database so a second session can verify
    what the first one committed."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False
        )

    def tearDown(self):
        self.engine.dispose()

    def committed_reports(self):
        verifier = self.SessionLocal()
        try:
            return verifier.query(Report).count()
        finally:
            verifier.close()


class TestSessionScopeTransactions(SQLiteBackedTestCase):

    def test_commits_on_clean_exit(self):
        """The whole point of the contextmanager: callers must not have
        to remember to commit."""
        with patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        ):
            with session_scope() as db:
                db.add(_report())

        self.assertEqual(
            self.committed_reports(), 1,
            "session_scope() must commit when the block exits cleanly",
        )

    def test_rolls_back_and_reraises_on_exception(self):
        """A failure inside the block must leave nothing behind, and must
        not be swallowed — ask_stream's generator catches exceptions and
        turns them into SSE error events, so a silently-suppressed write
        failure would be invisible."""
        with patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        ):
            with self.assertRaises(ValueError):
                with session_scope() as db:
                    db.add(_report())
                    raise ValueError("boom")

        self.assertEqual(
            self.committed_reports(), 0,
            "session_scope() must roll back when the block raises",
        )

    def test_partial_work_before_exception_is_not_persisted(self):
        """Two writes, failure after the first: neither may survive."""
        with patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        ):
            with self.assertRaises(RuntimeError):
                with session_scope() as db:
                    db.add(_report("first"))
                    db.flush()
                    db.add(_report("second"))
                    raise RuntimeError("boom")

        self.assertEqual(self.committed_reports(), 0)


class TestSessionScopeAlwaysCloses(SQLiteBackedTestCase):
    """Connection lease duration is the reason this helper exists, so
    'it always closes' is the load-bearing guarantee, not a detail."""

    def _factory_returning_spied_session(self):
        session = self.SessionLocal()
        patcher = patch.object(session, "close", wraps=session.close)
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        return (lambda: session), session, spy

    def test_closes_on_success(self):
        factory, session, spy = self._factory_returning_spied_session()
        with patch.object(session_module, "get_session_factory", return_value=factory):
            with session_scope() as db:
                self.assertIs(db, session)
                db.add(_report())

        spy.assert_called_once()
        self.assertFalse(
            session.in_transaction(),
            "no transaction may remain open after session_scope() exits",
        )

    def test_closes_on_exception(self):
        factory, session, spy = self._factory_returning_spied_session()
        with patch.object(session_module, "get_session_factory", return_value=factory):
            with self.assertRaises(ValueError):
                with session_scope():
                    raise ValueError("boom")

        spy.assert_called_once()
        self.assertFalse(session.in_transaction())


class TestGetSessionFactory(SQLiteBackedTestCase):

    def test_returns_a_callable_producing_sessions(self):
        with patch.object(session_module, "_engine", self.engine), \
             patch.object(session_module, "_SessionLocal", self.SessionLocal):
            factory = get_session_factory()
            produced = factory()
            try:
                self.assertIsInstance(produced, Session)
            finally:
                produced.close()

    def test_raises_when_database_url_is_not_configured(self):
        """The lazy-engine contract: importing this module must never
        need configuration, but asking for a session without
        DATABASE_URL must fail loudly rather than silently."""
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(session_module, "_engine", None), \
             patch.object(session_module, "_SessionLocal", None):
            with self.assertRaises(RuntimeError) as ctx:
                get_session_factory()

        self.assertIn("DATABASE_URL", str(ctx.exception))


class TestGetDbSessionUnchanged(SQLiteBackedTestCase):
    """get_db_session() was rewired to build its session through
    get_session_factory(). Its observable contract must be identical:
    yield exactly one Session, close it on teardown, and never commit."""

    def test_yields_one_session_and_closes_it(self):
        session = self.SessionLocal()
        patcher = patch.object(session, "close", wraps=session.close)
        spy = patcher.start()
        self.addCleanup(patcher.stop)

        with patch.object(
            session_module, "get_session_factory", return_value=(lambda: session)
        ):
            generator = get_db_session()
            yielded = next(generator)
            self.assertIs(yielded, session)
            spy.assert_not_called()

            with self.assertRaises(StopIteration):
                next(generator)

        spy.assert_called_once()

    def test_does_not_commit_on_behalf_of_the_caller(self):
        """Endpoints manage their own commit boundaries; a hidden commit
        here would change five existing endpoints' semantics."""
        with patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        ):
            generator = get_db_session()
            db = next(generator)
            db.add(_report())
            with self.assertRaises(StopIteration):
                next(generator)

        self.assertEqual(
            self.committed_reports(), 0,
            "get_db_session() must not commit — only session_scope() does",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
