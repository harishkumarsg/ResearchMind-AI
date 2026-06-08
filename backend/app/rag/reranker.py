import os

from sentence_transformers import CrossEncoder

_cross_encoder = None


def _rerank_enabled() -> bool:
    # Keep cloud instances stable on low memory by default.
    return os.environ.get("RERANK_ENABLED", "false").lower() == "true"


def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        _cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    return _cross_encoder

MAX_RERANK = 5


def rerank_results(
    question,
    results
):

    if not results:
        return []

    # On low-memory environments, skip cross-encoder and keep vector order.
    if not _rerank_enabled():
        return results[:MAX_RERANK]

    results = results[:MAX_RERANK]

    pairs = [
        [
            question,
            hit.payload.get(
                "text",
                ""
            )[:400]
        ]
        for hit in results
    ]

    scores = _get_cross_encoder().predict(
        pairs,
        show_progress_bar=False
    )

    reranked = sorted(
        zip(results, scores),
        key=lambda x: x[1],
        reverse=True
    )

    return [
        item[0]
        for item in reranked
    ]