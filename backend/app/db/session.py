"""
SQLAlchemy engine/session, resolved lazily. Importing this module must
never fail just because DATABASE_URL isn't configured yet — engine
creation only happens on first actual use, matching the existing
lazy-initialization pattern already used for the embedding model and
the Qdrant client elsewhere in this codebase.
"""
import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


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


@contextmanager
def session_scope() -> Iterator[Session]:
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
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session() -> Session:
    """FastAPI dependency: db: Session = Depends(get_db_session)"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
