"""
Phase 2 tests for the Voyage AI embedder (app/rag/embedder.py).
Fully mocked — this suite makes ZERO real Voyage API calls.
"""
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

import app.rag.embedder as embedder_module


def _mock_embed_result(vectors):
    result = MagicMock()
    result.embeddings = vectors
    return result


class TestVoyageEmbedder(unittest.TestCase):

    def setUp(self):
        embedder_module._client = None  # fresh client per test, no mock leakage

    def tearDown(self):
        embedder_module._client = None

    def test_encode_query_uses_query_input_type(self):
        fake_client = MagicMock()
        fake_client.embed.return_value = _mock_embed_result([[0.1, 0.2, 0.3]])

        with patch.object(embedder_module, "_get_client", return_value=fake_client):
            vector = embedder_module.encode_query("what is explainable AI?")

        fake_client.embed.assert_called_once()
        kwargs = fake_client.embed.call_args.kwargs
        self.assertEqual(kwargs["input_type"], "query")
        self.assertEqual(kwargs["model"], embedder_module.VOYAGE_EMBED_MODEL)
        self.assertEqual(vector, [0.1, 0.2, 0.3])

    def test_encode_passages_uses_document_input_type(self):
        fake_client = MagicMock()
        fake_client.embed.return_value = _mock_embed_result([[0.1, 0.2], [0.3, 0.4]])

        with patch.object(embedder_module, "_get_client", return_value=fake_client):
            vectors = embedder_module.encode_passages(["chunk one", "chunk two"])

        kwargs = fake_client.embed.call_args.kwargs
        self.assertEqual(kwargs["input_type"], "document")
        self.assertEqual(kwargs["model"], embedder_module.VOYAGE_EMBED_MODEL)
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])

    def test_query_and_document_input_types_are_never_the_same_call(self):
        """Directly guards against the H4-class bug: query and passage
        embedding must never accidentally share one code path/input_type."""
        fake_client = MagicMock()
        fake_client.embed.return_value = _mock_embed_result([[0.0]])

        with patch.object(embedder_module, "_get_client", return_value=fake_client):
            embedder_module.encode_query("q")
            embedder_module.encode_passages(["p"])

        input_types_used = [c.kwargs["input_type"] for c in fake_client.embed.call_args_list]
        self.assertEqual(input_types_used, ["query", "document"])

    def test_create_embeddings_delegates_to_document_side(self):
        fake_client = MagicMock()
        fake_client.embed.return_value = _mock_embed_result([[1.0]])

        with patch.object(embedder_module, "_get_client", return_value=fake_client):
            result = embedder_module.create_embeddings(["text"])

        self.assertEqual(fake_client.embed.call_args.kwargs["input_type"], "document")
        self.assertEqual(result, [[1.0]])

    def test_missing_api_key_raises_without_making_a_call(self):
        env_without_key = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            with self.assertRaises(RuntimeError):
                embedder_module.encode_query("test")

    def test_api_key_read_from_env_only_and_never_hardcoded(self):
        path = os.path.join(BACKEND_DIR, "app", "rag", "embedder.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn('os.environ.get("VOYAGE_API_KEY"', source)
        suspicious = re.findall(r'["\'][A-Za-z0-9_\-]{30,}["\']', source)
        self.assertEqual(suspicious, [], f"Found suspicious hardcoded-looking literals: {suspicious}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
