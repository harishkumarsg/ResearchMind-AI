"""
Voyage AI embeddings (voyage-4). Replaces the local BGE SentenceTransformer.

Query and document/passage embedding are deliberately separate functions,
each with its own input_type — a query must never be embedded with
input_type="document" or vice versa; mixing them degrades retrieval
quality in a way that's easy to miss since both still "work" (they just
produce vectors from a slightly different objective than the model
expects for that role).

VOYAGE_API_KEY is read from the environment only, lazily (on first use,
not at import time), and is never logged, printed, or hardcoded.

Document/passage embedding (encode_passages, used only by the indexing
pipeline) is rate-limit-aware: requests are batched by an ESTIMATED token
budget (not a fixed chunk count, which can wildly over- or under-shoot
the account's real per-minute token limit depending on chunk length),
paced to stay under the account's requests-per-minute limit, and retried
with bounded exponential backoff specifically for RateLimitError. Query
embedding (encode_query) is a single latency-sensitive call per request
and is deliberately left unpaced/unbatched.
"""
import os
import time
from typing import List, Optional

import voyageai
from voyageai.error import RateLimitError

VOYAGE_EMBED_MODEL = "voyage-4"

# Conservative token-budget batching: chunks are accumulated into a
# request until the next one would exceed this estimated token budget,
# rather than a fixed chunk COUNT per request. Comfortably below the
# free-tier's 10K TPM cap, leaving headroom since the estimate itself is
# an approximation, not an exact tokenizer count.
MAX_TOKENS_PER_EMBED_REQUEST = int(os.environ.get("VOYAGE_MAX_TOKENS_PER_REQUEST", "6000"))

# Secondary, absolute cap on chunks per request regardless of estimated
# token count — a defense-in-depth backstop, not the primary control.
MAX_CHUNKS_PER_EMBED_REQUEST = int(os.environ.get("VOYAGE_MAX_CHUNKS_PER_REQUEST", "50"))

# The free tier's default is 3 requests/minute (20s minimum spacing);
# pacing at 21s leaves a small safety margin. Configurable so a
# higher-throughput account can lower it without a code change.
EMBED_REQUEST_PACING_SECONDS = float(os.environ.get("VOYAGE_EMBED_PACING_SECONDS", "21"))

MAX_RATE_LIMIT_RETRIES = int(os.environ.get("VOYAGE_MAX_RETRIES", "5"))
RATE_LIMIT_BACKOFF_BASE_SECONDS = float(os.environ.get("VOYAGE_BACKOFF_BASE_SECONDS", "5"))
RATE_LIMIT_BACKOFF_MAX_SECONDS = float(os.environ.get("VOYAGE_BACKOFF_MAX_SECONDS", "60"))

_client: Optional[voyageai.Client] = None


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("VOYAGE_API_KEY", "")
        if not api_key:
            raise RuntimeError("VOYAGE_API_KEY is not configured")
        _client = voyageai.Client(api_key=api_key)
    return _client


def estimate_tokens(text: str) -> int:
    """Conservative, fully offline token estimate — no tokenizer call, no
    network request. English text averages roughly 4 characters per
    token; dividing by 3 instead deliberately overestimates so real
    requests stay safely under the configured budget even when this
    heuristic is off for a given chunk."""
    return max(1, len(text) // 3)


def batch_by_token_budget(
    texts: List[str],
    max_tokens: Optional[int] = None,
    max_items: Optional[int] = None,
) -> List[List[str]]:
    """Groups texts into batches that each stay under max_tokens estimated
    tokens (max_items is only a hard backstop) — never assumes chunks are
    similarly sized, unlike a fixed batch COUNT.

    max_tokens/max_items default to the module-level constants, looked up
    at CALL time (not bound as a function-default at import time) so that
    overriding the module constant — e.g. via an env var reload, or
    patch.object() in tests — actually takes effect.
    """
    if max_tokens is None:
        max_tokens = MAX_TOKENS_PER_EMBED_REQUEST
    if max_items is None:
        max_items = MAX_CHUNKS_PER_EMBED_REQUEST

    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0

    for text in texts:
        text_tokens = estimate_tokens(text)
        would_exceed = current and (
            current_tokens + text_tokens > max_tokens or len(current) >= max_items
        )
        if would_exceed:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += text_tokens

    if current:
        batches.append(current)

    return batches


def _embed_with_retry(texts: List[str], input_type: str) -> List[List[float]]:
    """Calls Voyage's embed() with bounded exponential backoff, but ONLY
    for RateLimitError — any other VoyageError/exception propagates
    immediately, since retrying those would just repeat the same failure
    for no benefit."""
    attempt = 0
    while True:
        try:
            result = _get_client().embed(texts, model=VOYAGE_EMBED_MODEL, input_type=input_type)
            return result.embeddings
        except RateLimitError:
            attempt += 1
            if attempt > MAX_RATE_LIMIT_RETRIES:
                raise
            backoff = min(
                RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                RATE_LIMIT_BACKOFF_MAX_SECONDS,
            )
            time.sleep(backoff)


def encode_query(text: str) -> List[float]:
    """Embeds a single search query with input_type='query'. Deliberately
    unbatched/unpaced/unretried — this is a single, latency-sensitive call
    per ask/search request, not the bulk indexing path."""
    result = _get_client().embed(
        [text], model=VOYAGE_EMBED_MODEL, input_type="query"
    )
    return result.embeddings[0]


def encode_passages(texts: List[str]) -> List[List[float]]:
    """Embeds one or more document/passage chunks with input_type='document'.

    Batches by estimated token budget (not a fixed chunk count), paces
    requests between batches to stay within the account's requests-per-
    minute limit, and retries ONLY rate-limit errors with bounded
    exponential backoff. Raises (after exhausting retries, or immediately
    on a non-rate-limit error) rather than returning partial results —
    callers must treat any exception here as "no embeddings were
    produced," never as a partial success.
    """
    if not texts:
        return []

    batches = batch_by_token_budget(texts)
    all_embeddings: List[List[float]] = []
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(EMBED_REQUEST_PACING_SECONDS)
        all_embeddings.extend(_embed_with_retry(batch, input_type="document"))
    return all_embeddings


def create_embeddings(chunks: List[str]) -> List[List[float]]:
    """Kept for call-site compatibility with index_document.py, which
    embeds document/passage chunks for storage — always the 'document'
    side of the query/document asymmetry."""
    return encode_passages(chunks)
