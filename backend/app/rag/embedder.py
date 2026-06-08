from sentence_transformers import SentenceTransformer


class LazyEmbedder:
    """Load the embedding model only when first used to reduce startup RAM."""

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            self._model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        return self._model

    def encode(self, inputs, **kwargs):
        return self._get_model().encode(inputs, **kwargs)


model = LazyEmbedder()


def create_embeddings(chunks):

    embeddings = model.encode(
        chunks,
        show_progress_bar=True
    )

    return embeddings