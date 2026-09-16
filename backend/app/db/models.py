"""
SQLAlchemy models matching backend/migrations/0001_core_schema.sql.
Keep these two in sync by hand — there is no migration-generation tool
wired up in this project yet.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    JSON,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Uuid/JSON (plain sqlalchemy, not sqlalchemy.dialects.postgresql) are
# cross-dialect: native uuid/jsonb on Postgres in production, but also
# usable against an in-memory SQLite engine for fully offline testing of
# the ownership logic before any real database exists.

PAPER_STATUSES = ("uploading", "uploaded", "indexing", "indexed", "failed", "deleting")

CHAT_ROLES = ("user", "assistant")


def _utcnow():
    return datetime.now(timezone.utc)


class Paper(Base):
    __tablename__ = "papers"

    id = Column(Uuid, primary_key=True)
    owner_id = Column(Uuid, nullable=False, index=True)
    title = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    storage_path = Column(Text, nullable=False)
    file_size_bytes = Column(BigInteger, nullable=False)
    status = Column(String(20), nullable=False, default="uploading")
    status_detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Report(Base):
    __tablename__ = "reports"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id = Column(Uuid, nullable=False, index=True)
    query = Column(Text, nullable=False)
    report_markdown = Column(Text, nullable=False)
    citations = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    # UNIQUE in Postgres (migration 0003): exactly one active session per
    # owner, which is what makes the session upsert deterministic.
    owner_id = Column(Uuid, nullable=False, index=True, unique=True)
    current_topic = Column(Text, nullable=True)
    current_paper_id = Column(
        Uuid, ForeignKey("papers.id", ondelete="SET NULL"), nullable=True
    )
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ChatMessage(Base):
    """Durable turn-by-turn chat history (migration 0003).

    Replaces the in-process UserMemory.chat_history list as the source of
    truth. Nothing reads or writes this yet — the persistence wiring in
    ask_stream.py is a later D2 step.
    """

    __tablename__ = "chat_messages"

    # No standalone index on session_id: migration 0003 provides the
    # composite chat_messages_session_created_idx (session_id, created_at),
    # which already serves session lookups as a leftmost-prefix match.
    # A separate single-column index would be redundant.
    __table_args__ = (
        Index("chat_messages_session_created_idx", "session_id", "created_at"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id = Column(
        Uuid, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from chat_sessions so every read can filter by owner
    # directly, with no join — matching the explicit WHERE owner_id
    # pattern used everywhere else — and so the RLS policy can be written
    # against this table alone.
    owner_id = Column(Uuid, nullable=False, index=True)
    role = Column(String(16), nullable=False)  # one of CHAT_ROLES
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class UsageCounter(Base):
    """Durable per-user daily usage counters (migration 0004).

    One row per (owner_id, day, metric); the composite primary key is
    also the only index the lookup needs. `day` is a UTC date computed in
    Python, never from the database clock, so PostgreSQL and the offline
    SQLite harness agree on when a day rolls over.

    Rows are written exclusively by the conditional UPSERT in
    app/core/quota.py — never by the ORM — so that the increment stays a
    single atomic statement. This model exists so the table is visible to
    Base.metadata (tests create it) and so the schema is documented in
    the same place as its siblings.

    Deletion is by cascade from auth.users only: the application never
    deletes counters, which is why the app role is granted no DELETE on
    this table (see migration 0004).
    """

    __tablename__ = "usage_counters"

    owner_id = Column(Uuid, primary_key=True)
    day = Column(Date, primary_key=True)
    metric = Column(String(40), primary_key=True)
    count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
