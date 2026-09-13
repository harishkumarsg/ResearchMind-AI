"""
Shared in-memory SQLite harness.

Several endpoints now read and write Postgres. Suites that drive those
endpoints but are not themselves about persistence still need SOME
database, or they would build a real engine from the .env DATABASE_URL
and read and write the live Supabase project on every run.

Stubbing the persistence functions out was the other option and is the
wrong one here: /ask-stream's follow-up detection and topic carry-over
now genuinely depend on stored state, so a suite with those functions
stubbed would assert against behaviour that cannot happen. A real
SQLite database keeps those tests meaningful and still fully offline.
"""
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base


def attach_sqlite_db(test_case):
    """Give `test_case` a private in-memory database for its duration.

    Patches app.db.session.get_session_factory, so the real
    session_scope() and the real chat_store functions run against
    SQLite. Returns the sessionmaker for direct seeding/inspection.
    Cleanup is registered with addCleanup — no tearDown edit needed.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    patcher = patch(
        "app.db.session.get_session_factory", return_value=factory
    )
    patcher.start()
    test_case.addCleanup(patcher.stop)
    test_case.addCleanup(engine.dispose)

    return factory
