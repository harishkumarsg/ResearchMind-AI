"""
Questions about the library itself, answered from Postgres.

"How many papers are there?" cannot be answered from retrieval: the model
sees only the passages retrieved for one question, so it counts the papers
that happen to appear in them and reports that number with confidence. The
prompt's LIBRARY_SCOPE_RESPONSE rule was meant to stop that, and the model
ignores it for some phrasings. /ask-stream therefore recognises these
questions before any provider call and answers them from the papers table:
no embedding, no generation, no AI allowance.

The matcher is deliberately narrow. A false positive replaces a real
answer about the papers' content with a list of titles, so a question
matches only when the WHOLE question is about the library: a known
opening ("how many", "list", "which ... do I have"), a plural library noun,
and nothing after it except a library-scope tail ("are there", "in my
library", "have I uploaded"). "Which papers use vision-language models?"
has content after the noun, so it is left to retrieval.
"""
import re
import uuid
from typing import List

from app.db.models import Paper
from app.db.session import session_scope

#: Titles listed in one reply. Beyond this the reply gives the count and
#: points to the sidebar, so a large library cannot produce a huge answer.
LIBRARY_LIST_LIMIT = 50

_NOUN = r"(?:papers|documents|docs|pdfs|files|articles)"
_QUALIFIER = r"(?:(?:indexed|uploaded) )?"
_PLACE = r"(?:in|on) (?:my|the) (?:library|workspace|collection)"
# "are there", "do I have", "have I uploaded", ...: the ways of saying
# "that I own" which can follow the noun.
_OWNED = (
    r"(?:are there|there are|do i have(?: (?:indexed|uploaded))?|have i got"
    r"|are available|are (?:indexed|uploaded)"
    r"|have i (?:uploaded|indexed|added)|did i (?:upload|index|add)"
    r"|i (?:have )?(?:uploaded|indexed|added)|i have)"
)
#: Everything allowed after the noun. Nothing else may follow it.
_TAIL = r"(?:" + _OWNED + r"(?: " + _PLACE + r")?|(?:are )?" + _PLACE + r")"
_WHEN = r"(?: (?:so far|now|right now|currently|in total|total))?"
# "please", "can you", "can you tell me", "do you know", ... before the
# question proper.
_PREFIX = (
    r"(?:please )?(?:(?:can|could|would) you (?:please )?)?"
    r"(?:(?:tell me|do you know|i want to know) )?"
)
_SUFFIX = r"(?: please)?"

_LIBRARY_PATTERNS = tuple(
    re.compile(r"^" + _PREFIX + body + _SUFFIX + r"$")
    for body in (
        # how many papers / how many indexed papers do I have in my library
        r"how many " + _QUALIFIER + _NOUN + r"(?: " + _TAIL + r")?" + _WHEN,
        # list / show me / count all my papers
        r"(?:list|show|show me|display|count|give me|tell me)"
        r"(?: all| all of| all the| the| my| all of my| all my)?"
        r" " + _QUALIFIER + _NOUN + r"(?: " + _TAIL + r")?" + _WHEN,
        # what / which papers do I have / are in my library. The tail is
        # REQUIRED here: "which papers" alone is how content questions
        # start ("which papers use ...").
        r"(?:what|which) " + _QUALIFIER + _NOUN + r" " + _TAIL + _WHEN,
        # what are my papers / what are the papers in my library
        r"what are (?:my|all my|all the|the) " + _QUALIFIER + _NOUN
        + r"(?: " + _PLACE + r")?" + _WHEN,
        # what is in my library / what do I have in my library
        r"what (?:is|do i have|have i got) " + _PLACE + _WHEN,
    )
)

#: Characters CommonMark treats as markup. A title is the user's own
#: filename-derived text; escaping keeps "my_paper_v2" from rendering in
#: italics on the Ask page. The page does not render raw HTML, so this is
#: about display, not injection.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")


def _normalise(question: str) -> str:
    text = (question or "").lower()
    text = text.replace("’", "'")
    text = re.sub(r"\bwhat's\b", "what is", text)
    text = re.sub(r"\bwhat're\b", "what are", text)
    # Words and digits only; punctuation and apostrophes become spaces.
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def is_library_question(question: str) -> bool:
    """True only when the whole question asks about the library itself."""
    text = _normalise(question)
    if not text:
        return False
    return any(pattern.match(text) for pattern in _LIBRARY_PATTERNS)


def indexed_paper_titles(owner_id: str) -> List[str]:
    """This owner's indexed paper titles, sorted by title.

    The owner filter is the verified JWT identity the route passes in, and
    the session binds the same claims for RLS. Only papers with status
    "indexed" count: the same set /papers reports as ready to query.
    """
    owner_uuid = uuid.UUID(owner_id)
    with session_scope(owner_id) as db:
        rows = (
            db.query(Paper.title)
            .filter(Paper.owner_id == owner_uuid, Paper.status == "indexed")
            .order_by(Paper.title)
            .all()
        )
    return [row[0] for row in rows]


def _escape(title: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join((title or "").split()))


def library_reply_parts(titles: List[str]) -> List[str]:
    """The reply as the pieces streamed as token frames, in order.

    Joined, they form one Markdown answer: a count line, then the titles
    as a bulleted list.
    """
    count = len(titles)
    if count == 0:
        return [
            "You have no indexed papers yet. Upload a PDF and index it, "
            "and it will appear in the sidebar under Indexed Papers."
        ]

    noun = "paper" if count == 1 else "papers"
    parts = [f"You have {count} indexed {noun}:\n"]
    shown = titles[:LIBRARY_LIST_LIMIT]
    parts.extend(f"\n- {_escape(title)}" for title in shown)
    hidden = count - len(shown)
    if hidden:
        parts.append(
            f"\n\n…and {hidden} more. The full list is in the sidebar "
            "under Indexed Papers."
        )
    return parts
