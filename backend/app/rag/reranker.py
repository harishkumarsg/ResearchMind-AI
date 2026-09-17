"""
Voyage AI reranking (rerank-3). Replaces the local ms-marco-MiniLM-L-6-v2
cross-encoder.

The public interface — rerank_results(question, results) -> results,
gated by RERANK_ENABLED, truncated to MAX_RERANK first — is preserved
exactly as it was, so every existing caller (ask_stream.py, search.py,
research.py, summarize_paper.py, compare_papers.py) needs no changes
beyond what Phase 2 already touches them for.

VOYAGE_API_KEY is read from the environment only, lazily, and is never
logged, printed, or hardcoded.
"""
import os
from typing import Optional

import voyageai

from app.core.providers import voyage_client_options

VOYAGE_RERANK_MODEL = "rerank-3"
MAX_RERANK = 5

_client: Optional[voyageai.Client] = None


def _rerank_enabled() -> bool:
    # Keep cloud instances stable on low memory by default.
    return os.environ.get("RERANK_ENABLED", "false").lower() == "true"


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("VOYAGE_API_KEY", "")
        if not api_key:
            raise RuntimeError("VOYAGE_API_KEY is not configured")
        # Explicit timeout, no SDK retries. See app/core/providers.py.
        _client = voyageai.Client(api_key=api_key, **voyage_client_options())
    return _client


def rerank_results(question, results):

    if not results:
        return []

    if not _rerank_enabled():
        return results[:MAX_RERANK]

    results = results[:MAX_RERANK]

    documents = [hit.payload.get("text", "")[:400] for hit in results]

    reranking = _get_client().rerank(
        query=question,
        documents=documents,
        model=VOYAGE_RERANK_MODEL,
    )

    return [results[r.index] for r in reranking.results]
