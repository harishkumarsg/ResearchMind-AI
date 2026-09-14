"""
SQLAlchemy engine/session, resolved lazily. Importing this module must
never fail just because DATABASE_URL isn't configured yet — engine
creation only happens on first actual use, matching the existing
lazy-initialization pattern already used for the embedding model and
the Qdrant client elsewhere in this codebase.
"""
import json
import os
from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import get_current_owner_id

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None

#: Where the verified owner id is stashed for the duration of a Session.
OWNER_ID_KEY = "researchmind_owner_id"

#: The Postgres setting auth.uid() reads. See the function's definition:
#: it coalesces request.jwt.claim.sub with request.jwt.claims->>'sub'.
JWT_CLAIMS_SETTING = "request.jwt.claims"


@event.listens_for(Session, "after_begin")
def _bind_jwt_claims(session: Session, transaction, connection) -> None:
    """Bind the verified identity to the transaction that just began.

    Fires on EVERY transaction start, which is the point. A one-shot
    SET LOCAL before the first statement would be discarded by the
    first commit, and /upload, /index-document and /paper/{name} all
    commit several times per request — every statement after the first
    commit would then run with no identity at all.

    `set_config(..., true)` is SET LOCAL: scoped to this transaction and
    discarded on commit or rollback, so a pooled connection cannot carry
    one request's identity into the next. A plain session-level SET
    would, and is never used anywhere in this codebase.

    The value comes from session.info, which only _new_session() writes,
    and only from an owner_id already derived from a verified JWT.

    No-ops on non-PostgreSQL dialects so the offline SQLite suite is
    unaffected; the real behaviour is verified against Postgres.
    """
    owner_id = session.info.get(OWNER_ID_KEY)
    if not owner_id:
        return
    if connection.dialect.name != "postgresql":
        return

    connection.execute(
        text("select set_config(:setting, :claims, true)"),
        {
            "setting": JWT_CLAIMS_SETTING,
            "claims": json.dumps({"sub": str(owner_id)}),
        },
    )


def _get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not configured — database access is unavailable"
            )
        _engine = create_engine(database_url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session_factory() -> sessionmaker:
    """Public accessor for the configured sessionmaker.

    The only supported way to reach a Session from code that is not a
    FastAPI endpoint parameter. get_db_session() below is a generator
    meant for Depends(), so calling it directly yields a generator
    object rather than a Session, and _SessionLocal is private and
    None until the engine has been built.

    Tests substitute a SQLite-backed factory by patching this function.
    """
    _get_engine()
    return _SessionLocal


def _new_session(owner_id: str) -> Session:
    """Build a Session carrying the verified identity.

    The single place OWNER_ID_KEY is ever written. owner_id must already
    have come from app.core.auth — a verified JWT `sub` — never from a
    query parameter, request body, or header.
    """
    session = get_session_factory()()
    session.info[OWNER_ID_KEY] = owner_id
    return session


@contextmanager
def session_scope(owner_id: str) -> Iterator[Session]:
    """Short-lived transactional session for use outside request scope.

    Commits on clean exit, rolls back and re-raises on exception, and
    closes in all cases. Note the contrast with get_db_session(), which
    deliberately does NOT commit — its callers are endpoints that manage
    their own commit boundaries.

    This exists for /ask-stream. Its writes happen inside the
    StreamingResponse generator, spanning an LLM call that can run for
    many seconds. Reusing the request-scoped session there would hold a
    pooled Postgres connection checked out for that entire duration, per
    concurrent user, against Supabase's pooler limits. Each `with
    session_scope()` block instead holds a connection only for the
    milliseconds its write takes.

    It also avoids depending on FastAPI's dependency-teardown ordering
    relative to streaming response bodies, which is undocumented and has
    changed more than once across releases.

    owner_id is required, not optional: a session with no identity would
    see nothing once RLS is enforced, and making the caller pass it means
    the omission is a TypeError rather than a silent empty result.
    """
    session = _new_session(owner_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session(owner_id: str = Depends(get_current_owner_id)) -> Session:
    """FastAPI dependency: db: Session = Depends(get_db_session)

    owner_id resolves through the same verified-JWT dependency every
    caller of this already uses, so the identity bound to the database
    transaction and the identity used in `WHERE owner_id = :owner_id`
    are guaranteed to be the same value from the same source.
    """
    session = _new_session(owner_id)
    try:
        yield session
    finally:
        session.close()
