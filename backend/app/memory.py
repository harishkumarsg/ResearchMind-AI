"""
Per-owner conversational/result state, keyed EXCLUSIVELY by the
authenticated Supabase JWT owner_id — never by a client-supplied
session ID, header, or any other client-controlled value.

This replaces the previous global module-level variables, which were
shared across every authenticated user's requests — a real cross-user
data leakage bug (one user's chat history, current topic, or last
generated report/summary/comparison was readable by, and overwritable
by, every other user).

The fix here is ISOLATION, not just thread-safety: a lock around the
old globals would still let User B read User A's conversation, just
without corrupting it mid-read. The lock below protects only the
dict's own internal consistency during concurrent inserts; the actual
per-user boundary comes from keying every lookup by owner_id.

In-process only, and deliberately so: what remains here is the
best-effort scratch state for summaries, comparisons and reports,
which is regenerated on demand and was never durable. The chat state
that used to live alongside it has moved to Postgres
(chat_sessions/chat_messages, migration 0003) and is no longer
mirrored here.
"""
import threading
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class UserMemory:
    # Conversation state used to live here — chat_history, current_topic,
    # topic_context, current_paper, last_question, last_answer. It is now
    # held in chat_sessions/chat_messages (migration 0003) and read from
    # there by /ask-stream, so the in-process copies were write-only and
    # have been removed. Keeping them would only invite drift between two
    # stores of the same fact.

    # Summarization
    last_summary: str = ""
    last_summary_paper: str = ""

    # Comparison
    last_comparison: str = ""
    last_compared_paper1: str = ""
    last_compared_paper2: str = ""

    # Research / reports
    last_research_query: str = ""
    last_research_report: str = ""
    last_research_context: str = ""
    last_research_sources: list = field(default_factory=list)
    last_citations: list = field(default_factory=list)


class UserMemoryStore:
    """Thread-safe map of owner_id -> UserMemory."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_owner: Dict[str, UserMemory] = {}

    def get(self, owner_id: str) -> UserMemory:
        if not owner_id:
            raise ValueError("owner_id is required to access user memory")
        with self._lock:
            if owner_id not in self._by_owner:
                self._by_owner[owner_id] = UserMemory()
            return self._by_owner[owner_id]

    def clear(self, owner_id: str) -> None:
        """Test/utility helper — not called from any production request path."""
        with self._lock:
            self._by_owner.pop(owner_id, None)

    def owner_count(self) -> int:
        """Test/diagnostic helper."""
        with self._lock:
            return len(self._by_owner)


_store = UserMemoryStore()


def get_user_memory(owner_id: str) -> UserMemory:
    """The one entry point every route uses. owner_id must come from the
    verified Supabase JWT (app.core.auth.get_current_owner_id /
    get_current_identity), never from client-supplied request data."""
    return _store.get(owner_id)
