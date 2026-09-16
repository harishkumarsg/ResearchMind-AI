"""
Mechanism C: a single global indexing slot.

Indexing is the only operation that issues dozens of embedding calls in
one request, paced 21s apart. Two runs overlapping makes both slower and
markedly more likely to be throttled upstream, so only one run holds the
slot at a time.

What this is NOT: it does not enforce, model, or replace the provider's
rate limit. Voyage's limit is an account-wide budget measured in requests
and tokens per minute; this semaphore only serialises *our* indexing
runs inside *one* process. Provider throttling is still handled where it
always was — the bounded backoff in app/rag/embedder.py, which is
untouched.

Acquisition is bounded. A real run takes minutes, so waiting for one to
finish would pin an HTTP worker for minutes; the short wait exists only
to absorb the tail of a departing run (its final upsert and commit).
Beyond that the caller is told to come back, rather than being parked.

Like mechanism A this is per-process state. With the current single
uvicorn worker that is the whole service; it would become per-replica if
the API is ever scaled out, at which point this needs to move to a shared
lock. That is a deliberate, documented limitation, not an oversight.

The slot is NOT disabled by QUOTA_ENFORCEMENT: it protects request
duration and upstream pressure, which is not a per-user fairness policy.
"""
import threading
from contextlib import contextmanager
from typing import Iterator

from app.core import limits as limits_config
from app.core.limits import IndexingBusy

#: Seconds a busy caller is told to wait. A run in progress typically has
#: minutes left, so retrying sooner would simply fail again.
BUSY_RETRY_AFTER_SECONDS = 60

_slot = threading.Semaphore(1)


@contextmanager
def indexing_slot() -> Iterator[None]:
    """Holds the single indexing slot for the duration of the block.

    Raises IndexingBusy if the slot cannot be acquired within
    INDEX_SLOT_WAIT_SECONDS. Always released, including on error, so a
    failed run never strands the slot for everyone else.
    """
    acquired = _slot.acquire(timeout=limits_config.index_slot_wait_seconds())
    if not acquired:
        raise IndexingBusy(
            "Another indexing run is in progress. Please try again in a minute.",
            retry_after_seconds=BUSY_RETRY_AFTER_SECONDS,
        )
    try:
        yield
    finally:
        _slot.release()


def get_slot() -> threading.Semaphore:
    """Accessor so tests can hold the slot deliberately."""
    return _slot
