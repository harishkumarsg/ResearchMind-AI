from sentence_transformers import CrossEncoder

cross_encoder = CrossEncoder(
    "cross-encoder/ms-marco-MiniLM-L-6-v2"
)

MAX_RERANK = 5


def rerank_results(
    question,
    results
):

    if not results:
        return []

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

    scores = cross_encoder.predict(
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