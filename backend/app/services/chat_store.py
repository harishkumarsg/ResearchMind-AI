"""
Durable chat persistence: one chat_sessions row per owner, one
chat_messages row per conversational turn (migration 0003).

Every function here takes owner_id as the verified Supabase JWT `sub`
supplied by app.core.auth — never a client-supplied value — and scopes
both its reads and its writes by it. There is no code path that lets a
caller name, read, or write another owner's session or messages.

Each function opens its own short-lived session_scope() and closes it
before returning. That is deliberate: /ask-stream calls these from
inside a StreamingResponse generator that spans a multi-second LLM call,
and a request-scoped session would hold a pooled Postgres connection
checked out for that whole duration, per concurrent user.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.db.models import ChatMessage, ChatSession, Paper
from app.db.session import session_scope

#: How many past turns to reconstruct for the prompt. Mirrors the
#: chat_history[-6:] slice this replaced — six message rows, i.e. three
#: exchanges, counted in messages rather than in pairs.
HISTORY_MESSAGE_LIMIT = 6


def _utcnow() -> datetime:
    """Matches app.db.models._utcnow. Kept as a named module-level
    function rather than an inline call so tests can pin the clock."""
    return datetime.now(timezone.utc)


def coerce_paper_id(raw) -> Optional[uuid.UUID]:
    """Qdrant payloads are untyped. Anything that isn't a parseable UUID
    becomes None rather than raising — a malformed payload must not be
    able to fail an otherwise good answer."""
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (AttributeError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ChatState:
    """A snapshot of one owner's durable conversation state.

    Plain values, not ORM objects: the session that produced them is
    already closed by the time this is returned, and the caller uses it
    across a multi-second LLM call.
    """

    session_id: Optional[uuid.UUID] = None
    history: List[str] = field(default_factory=list)
    current_topic: str = ""
    current_paper_id: Optional[uuid.UUID] = None


def load_chat_state(
    owner_id: str, limit: int = HISTORY_MESSAGE_LIMIT
) -> ChatState:
    """Read the authenticated owner's conversation state.

    Must be called BEFORE persist_user_turn for the current request,
    otherwise the question being asked would appear in its own history.

    Both queries filter on owner_id from the verified JWT, and the
    message query additionally filters on the session id that same
    lookup returned — so there is no path by which another owner's
    session or messages can be loaded.
    """
    owner_uuid = uuid.UUID(owner_id)

    with session_scope(owner_id) as db:
        chat_session = (
            db.query(ChatSession)
            .filter(ChatSession.owner_id == owner_uuid)
            .first()
        )

        if chat_session is None:
            # Fresh user: no session, no history, no pointers.
            return ChatState()

        recent = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == chat_session.id,
                ChatMessage.owner_id == owner_uuid,
            )
            # Newest-first with the limit applied in the database, then
            # reversed in Python, so only `limit` rows are fetched no
            # matter how long the conversation is. created_at alone is a
            # total, chronological order: _next_message_time never lets
            # two messages in a session share a timestamp. A random UUID
            # would be no tiebreak at all.
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .all()
        )

        history = [
            f"{'User' if message.role == 'user' else 'Assistant'}: {message.content}"
            for message in reversed(recent)
        ]

        return ChatState(
            session_id=chat_session.id,
            history=history,
            current_topic=chat_session.current_topic or "",
            current_paper_id=chat_session.current_paper_id,
        )


def _owns_paper(db, owner_uuid: uuid.UUID, paper_id: uuid.UUID) -> bool:
    """Does this owner have a papers row with this id?

    current_paper_id is a FK to papers.id whose value originates in a
    Qdrant payload, so it is verified before every write. Two reasons: a
    stale payload pointing at a deleted paper would raise a FK violation
    and sink an otherwise good request, and the owner_id term stops a
    payload from ever pointing a session at another owner's paper.
    """
    return (
        db.query(Paper.id)
        .filter(Paper.id == paper_id, Paper.owner_id == owner_uuid)
        .first()
    ) is not None


def _get_or_create_session(db, owner_uuid: uuid.UUID) -> ChatSession:
    # Row-locked until the transaction ends, so message timestamps
    # assigned under it are strictly ordered (see _next_message_time).
    existing = (
        db.query(ChatSession)
        .filter(ChatSession.owner_id == owner_uuid)
        .with_for_update()
        .first()
    )
    if existing is not None:
        return existing

    created = ChatSession(owner_id=owner_uuid)
    db.add(created)
    # flush (not commit) so the new row gets its id, and so a UNIQUE
    # violation surfaces here rather than at the end of the transaction.
    db.flush()
    return created


def _next_message_time(
    db, session_id: uuid.UUID, owner_uuid: uuid.UUID
) -> datetime:
    """created_at for the next message in this session: now, or one
    microsecond after the session's latest message if the clock has not
    moved past it.

    History is ordered by created_at alone, so two messages in a session
    must never share a timestamp, and the wall clock cannot promise that:
    consecutive turns can land inside a single clock tick (about 1 ms
    with Python 3.11 on Windows), and a clock can step backwards. One
    microsecond is the finest step timestamptz stores.

    The caller must hold the session row lock, taken in
    _get_or_create_session or persist_assistant_turn. Without it, two
    concurrent writers could read the same latest timestamp.
    """
    latest = (
        db.query(func.max(ChatMessage.created_at))
        .filter(
            ChatMessage.session_id == session_id,
            ChatMessage.owner_id == owner_uuid,
        )
        .scalar()
    )
    now = _utcnow()
    if latest is None:
        return now
    if latest.tzinfo is None:
        # SQLite, the offline test database, reads timestamps back naive.
        # Every stored value is UTC.
        latest = latest.replace(tzinfo=timezone.utc)
    return max(now, latest + timedelta(microseconds=1))


def persist_user_turn(owner_id: str, question: str) -> uuid.UUID:
    """Record the user's turn, creating the owner's session on first use.

    Returns the chat_sessions.id to hand to persist_assistant_turn.
    Session lookup and message insert share one transaction, so a failed
    message write also rolls back an accidentally-created session.
    """
    owner_uuid = uuid.UUID(owner_id)

    def attempt() -> uuid.UUID:
        with session_scope(owner_id) as db:
            chat_session = _get_or_create_session(db, owner_uuid)
            session_id = chat_session.id
            message = ChatMessage(
                session_id=session_id,
                owner_id=owner_uuid,
                role="user",
                content=question,
            )
            message.created_at = _next_message_time(db, session_id, owner_uuid)
            db.add(message)
            return session_id

    try:
        return attempt()
    except IntegrityError:
        # chat_sessions.owner_id is UNIQUE. Two concurrent first-ever
        # requests from the same owner can both see no row and both
        # insert; the loser lands here. The winner's row is committed by
        # now, so the retry finds it instead of creating one. This is
        # exactly what that UNIQUE constraint is for.
        return attempt()


def persist_assistant_turn(
    session_id: uuid.UUID,
    owner_id: str,
    answer: str,
    current_topic: Optional[str] = None,
    current_paper_id: Optional[uuid.UUID] = None,
) -> None:
    """Record the assistant's turn and advance the session pointers.

    current_topic / current_paper_id are written only when not None, so
    the caller can leave either untouched — matching the existing
    semantics where a follow-up question does not replace the topic.
    """
    owner_uuid = uuid.UUID(owner_id)

    with session_scope(owner_id) as db:
        # Lock the session row before stamping the message; see
        # _next_message_time.
        chat_session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.owner_id == owner_uuid,
            )
            .with_for_update()
            .first()
        )

        message = ChatMessage(
            session_id=session_id,
            owner_id=owner_uuid,
            role="assistant",
            content=answer,
        )
        message.created_at = _next_message_time(db, session_id, owner_uuid)
        db.add(message)

        if chat_session is None:
            return

        # Advanced explicitly, and unconditionally, so the column means
        # "last chat activity". The model's onupdate hook is not enough:
        # a pure follow-up leaves both pointers below untouched, so
        # nothing on the row would be dirty, SQLAlchemy would emit no
        # UPDATE, and onupdate would never fire.
        chat_session.updated_at = _utcnow()

        if current_topic is not None:
            chat_session.current_topic = current_topic

        if current_paper_id is not None and _owns_paper(
            db, owner_uuid, current_paper_id
        ):
            chat_session.current_paper_id = current_paper_id


def set_current_paper(owner_id: str, paper_id: Optional[uuid.UUID]) -> None:
    """Point the owner's session at a paper, without recording a turn.

    /summarize-paper calls this: summarising establishes which paper the
    conversation is about, which /ask-stream's follow-up lock then reads
    from chat_sessions.current_paper_id. Before this existed, that link
    lived only in in-process memory and was lost on restart.

    A no-op when paper_id is None (an absent or malformed Qdrant
    payload) or when the paper is not this owner's. Never writes a
    chat_messages row — a summary is not a conversational turn.
    """
    if paper_id is None:
        return

    owner_uuid = uuid.UUID(owner_id)

    def attempt() -> None:
        with session_scope(owner_id) as db:
            if not _owns_paper(db, owner_uuid, paper_id):
                return

            chat_session = _get_or_create_session(db, owner_uuid)
            chat_session.current_paper_id = paper_id
            # The session pointer changed, so the row's last-activity
            # stamp moves with it.
            chat_session.updated_at = _utcnow()

    try:
        attempt()
    except IntegrityError:
        # Same UNIQUE(owner_id) race persist_user_turn handles.
        attempt()
