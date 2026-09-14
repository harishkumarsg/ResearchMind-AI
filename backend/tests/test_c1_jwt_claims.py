"""
C1 Stage 3 — JWT claims plumbing.

Proves that every database transaction binds the VERIFIED owner identity
to Postgres as a transaction-local setting, that the value can only come
from the verified JWT, and that a plain session-level SET is never used.

Offline and deterministic. The claims listener no-ops on SQLite, so the
statement it would emit is asserted directly against a fake PostgreSQL
connection rather than by round-tripping through a real database. The
live behaviour (auth.uid() equality, cross-owner leakage on a shared
pooled connection) is verified separately against real Postgres.
"""
import json
import os
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.session as session_module
from app.core.auth import get_current_owner_id
from app.db.models import Base
from app.db.session import (
    JWT_CLAIMS_SETTING,
    OWNER_ID_KEY,
    _bind_jwt_claims,
    _new_session,
    get_db_session,
    session_scope,
)

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
MALICIOUS = "ffffffff-ffff-ffff-ffff-ffffffffffff"

APP_DIR = os.path.join(BACKEND_DIR, "app")


def _pg_connection():
    """A stand-in connection that reports the PostgreSQL dialect, so the
    listener takes its real branch and we can inspect what it emits."""
    conn = MagicMock()
    conn.dialect.name = "postgresql"
    return conn


def _sqlite_connection():
    conn = MagicMock()
    conn.dialect.name = "sqlite"
    return conn


def _emitted(conn):
    """(sql_text, params) of the single statement the listener ran."""
    conn.execute.assert_called_once()
    args, _ = conn.execute.call_args
    return str(args[0]), args[1]


class SQLiteBackedTestCase(unittest.TestCase):
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
        patcher = patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.dispose()


class TestClaimsAreDerivedFromTheVerifiedOwner(SQLiteBackedTestCase):

    def test_new_session_stores_the_owner_on_session_info(self):
        session = _new_session(OWNER_A)
        try:
            self.assertEqual(session.info[OWNER_ID_KEY], OWNER_A)
        finally:
            session.close()

    def test_listener_emits_transaction_local_set_config(self):
        session = _new_session(OWNER_A)
        try:
            conn = _pg_connection()
            _bind_jwt_claims(session, None, conn)
            sql, params = _emitted(conn)

            self.assertIn("set_config", sql)
            self.assertEqual(params["setting"], JWT_CLAIMS_SETTING)
            self.assertEqual(json.loads(params["claims"]), {"sub": OWNER_A})
        finally:
            session.close()

    def test_set_config_third_argument_is_true_meaning_transaction_local(self):
        """set_config(name, value, is_local) — is_local=true is SET LOCAL.
        If this were false the claim would persist on the pooled
        connection and leak into the next request."""
        session = _new_session(OWNER_A)
        try:
            conn = _pg_connection()
            _bind_jwt_claims(session, None, conn)
            sql, _ = _emitted(conn)
            normalised = " ".join(sql.split()).lower()
            self.assertIn(", true)", normalised)
            self.assertNotIn(", false)", normalised)
        finally:
            session.close()

    def test_owner_is_parameterised_not_interpolated_into_sql(self):
        """The identity must never be concatenated into the statement."""
        session = _new_session(OWNER_A)
        try:
            conn = _pg_connection()
            _bind_jwt_claims(session, None, conn)
            sql, params = _emitted(conn)
            self.assertNotIn(OWNER_A, sql)
            self.assertIn(OWNER_A, params["claims"])
        finally:
            session.close()

    def test_each_owner_gets_its_own_claim(self):
        for owner in (OWNER_A, OWNER_B):
            session = _new_session(owner)
            try:
                conn = _pg_connection()
                _bind_jwt_claims(session, None, conn)
                _, params = _emitted(conn)
                self.assertEqual(json.loads(params["claims"])["sub"], owner)
            finally:
                session.close()

    def test_no_owner_means_no_statement(self):
        """A session built outside _new_session carries no identity and
        must not have claims invented for it."""
        session = self.SessionLocal()
        try:
            conn = _pg_connection()
            _bind_jwt_claims(session, None, conn)
            conn.execute.assert_not_called()
        finally:
            session.close()

    def test_non_postgres_dialect_is_a_noop(self):
        session = _new_session(OWNER_A)
        try:
            conn = _sqlite_connection()
            _bind_jwt_claims(session, None, conn)
            conn.execute.assert_not_called()
        finally:
            session.close()


class TestClaimsFireOnEveryTransaction(SQLiteBackedTestCase):
    """upload/index-document/delete_paper commit several times per
    request. A one-shot SET LOCAL would be discarded by the first
    commit, leaving every later statement unauthenticated."""

    def test_listener_reapplies_after_each_begin(self):
        session = _new_session(OWNER_A)
        try:
            for _ in range(3):
                conn = _pg_connection()
                _bind_jwt_claims(session, None, conn)
                _, params = _emitted(conn)
                self.assertEqual(json.loads(params["claims"])["sub"], OWNER_A)
        finally:
            session.close()

    def test_listener_is_registered_for_the_after_begin_event(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        self.assertTrue(
            event.contains(Session, "after_begin", _bind_jwt_claims),
            "claims must be bound on every transaction start",
        )


class TestSessionScopeRequiresAnOwner(SQLiteBackedTestCase):

    def test_owner_id_is_a_required_argument(self):
        with self.assertRaises(TypeError):
            with session_scope():
                pass

    def test_session_scope_binds_the_owner_it_was_given(self):
        with session_scope(OWNER_B) as db:
            self.assertEqual(db.info[OWNER_ID_KEY], OWNER_B)


class TestGetDbSessionUsesTheVerifiedDependency(unittest.TestCase):

    def test_default_is_the_verified_owner_dependency(self):
        """Not a query parameter, not a header — the same verified-JWT
        dependency every caller already uses."""
        import inspect

        default = inspect.signature(get_db_session).parameters["owner_id"].default
        self.assertIsInstance(default, type(Depends(get_current_owner_id)))
        self.assertIs(default.dependency, get_current_owner_id)


class TestClientCannotSupplyTheOwner(unittest.TestCase):
    """End-to-end through a real route: a client-supplied owner_id must
    not reach the database session."""

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
        patcher = patch.object(
            session_module, "get_session_factory", return_value=self.SessionLocal
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        from app.main import app
        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_query_string_owner_id_is_ignored_by_the_session(self):
        seen = []
        real_new_session = session_module._new_session

        def spy(owner_id):
            seen.append(owner_id)
            return real_new_session(owner_id)

        with patch.object(session_module, "_new_session", side_effect=spy):
            resp = self.client.get(
                "/papers", params={"owner_id": MALICIOUS, "user_id": MALICIOUS}
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(seen, "the endpoint should have opened a session")
        for owner in seen:
            self.assertEqual(owner, OWNER_A)
            self.assertNotEqual(owner, MALICIOUS)


class TestNoSessionLevelSetInSource(unittest.TestCase):
    """A plain `SET` persists on a pooled connection and would leak one
    request's identity into the next. Only set_config(..., true) /
    SET LOCAL is acceptable, and this asserts it at the source level so
    the rule cannot be quietly broken later."""

    def _app_sources(self):
        for root, dirs, files in os.walk(APP_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, "r", encoding="utf-8") as f:
                        yield path, f.read()

    def test_no_bare_set_request_jwt_anywhere(self):
        offenders = []
        for path, src in self._app_sources():
            lowered = src.lower()
            for needle in ("set request.jwt", 'set "request.jwt', "set session request.jwt"):
                if needle in lowered:
                    offenders.append((path, needle))
        self.assertEqual(offenders, [], f"session-level SET found: {offenders}")

    def test_no_set_config_with_is_local_false(self):
        offenders = []
        for path, src in self._app_sources():
            flat = " ".join(src.split()).lower()
            if "set_config" in flat and ", false)" in flat:
                offenders.append(path)
        self.assertEqual(
            offenders, [], f"set_config(..., false) is session-level: {offenders}"
        )

    def test_the_only_claims_writer_is_the_listener(self):
        """Exactly one place in the application may set the claim."""
        hits = []
        for path, src in self._app_sources():
            if JWT_CLAIMS_SETTING in src:
                hits.append(os.path.relpath(path, BACKEND_DIR).replace("\\", "/"))
        self.assertEqual(
            sorted(hits), ["app/db/session.py"],
            "request.jwt.claims must only be written in app/db/session.py",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
