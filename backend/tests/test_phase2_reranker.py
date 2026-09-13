"""
Phase 2 tests for the Voyage AI reranker (app/rag/reranker.py).
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

import app.rag.reranker as reranker_module


def _make_hit(text):
    hit = MagicMock()
    hit.payload = {"text": text}
    return hit


class FakeRerankResult:
    def __init__(self, index, document, relevance_score):
        self.index = index
        self.document = document
        self.relevance_score = relevance_score


class TestVoyageReranker(unittest.TestCase):

    def setUp(self):
        reranker_module._client = None

    def tearDown(self):
        reranker_module._client = None

    def test_disabled_by_default_returns_vector_order_without_calling_voyage(self):
        hits = [_make_hit("a"), _make_hit("b"), _make_hit("c")]
        with patch.dict(os.environ, {"RERANK_ENABLED": "false"}):
            with patch.object(reranker_module, "_get_client") as mock_get_client:
                result = reranker_module.rerank_results("query", hits)
                mock_get_client.assert_not_called()
        self.assertEqual(result, hits[: reranker_module.MAX_RERANK])

    def test_enabled_calls_voyage_rerank_with_rerank3_and_reorders(self):
        hits = [_make_hit("alpha"), _make_hit("beta")]

        fake_result = MagicMock()
        fake_result.results = [
            FakeRerankResult(index=1, document="beta", relevance_score=0.9),
            FakeRerankResult(index=0, document="alpha", relevance_score=0.1),
        ]
        fake_client = MagicMock()
        fake_client.rerank.return_value = fake_result

        with patch.dict(os.environ, {"RERANK_ENABLED": "true"}):
            with patch.object(reranker_module, "_get_client", return_value=fake_client):
                result = reranker_module.rerank_results("my question", hits)

        fake_client.rerank.assert_called_once()
        kwargs = fake_client.rerank.call_args.kwargs
        self.assertEqual(kwargs["model"], "rerank-3")
        self.assertEqual(kwargs["query"], "my question")
        # Reordered per Voyage's returned order: hits[1] (beta) first
        self.assertEqual(result, [hits[1], hits[0]])

    def test_empty_results_returns_empty_without_calling_voyage(self):
        with patch.object(reranker_module, "_get_client") as mock_get_client:
            result = reranker_module.rerank_results("query", [])
            mock_get_client.assert_not_called()
        self.assertEqual(result, [])

    def test_missing_api_key_raises_when_enabled(self):
        hits = [_make_hit("a")]
        env_without_key = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        env_without_key["RERANK_ENABLED"] = "true"
        with patch.dict(os.environ, env_without_key, clear=True):
            with self.assertRaises(RuntimeError):
                reranker_module.rerank_results("query", hits)

    def test_api_key_read_from_env_only_and_never_hardcoded(self):
        path = os.path.join(BACKEND_DIR, "app", "rag", "reranker.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn('os.environ.get("VOYAGE_API_KEY"', source)
        suspicious = re.findall(r'["\'][A-Za-z0-9_\-]{30,}["\']', source)
        self.assertEqual(suspicious, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
