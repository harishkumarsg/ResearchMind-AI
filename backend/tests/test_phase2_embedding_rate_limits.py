"""
Phase 2: rate-limit-aware Voyage embedding pipeline.

Regression coverage for the manual-testing finding that a single
80-page/220-chunk PDF, embedded with a fixed 50-chunk-per-request batch
size, produced individual requests exceeding the free-tier's 10K TPM
limit and firing back-to-back well over its 3 RPM limit — triggering a
real Voyage RateLimitError that failed the whole paper.

Fully mocked: voyageai.Client and time.sleep are both replaced with test
doubles. No real Voyage API call, no network access, anywhere in this
file.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from voyageai.error import RateLimitError, AuthenticationError

from app.rag import embedder


def _rate_limit_error():
    # RateLimitError(message, http_body, http_status, json_body, headers)
    return RateLimitError("rate limited", None, 429, None, None)


class TestEstimateTokensAndBatching(unittest.TestCase):

    def test_estimate_tokens_is_a_positive_conservative_heuristic(self):
        self.assertEqual(embedder.estimate_tokens(""), 1)
        self.assertGreater(embedder.estimate_tokens("a" * 300), 90)

    def test_batches_stay_within_the_token_budget_regardless_of_chunk_length_distribution(self):
        # A deliberately uneven mix — some tiny chunks, some huge ones —
        # rather than assuming any particular average chunk size.
        texts = ["short"] * 5 + ["x" * 9000] * 3 + ["medium text here"] * 10
        max_tokens = 2000

        batches = embedder.batch_by_token_budget(texts, max_tokens=max_tokens, max_items=50)

        self.assertEqual(sum(len(b) for b in batches), len(texts))
        for batch in batches:
            batch_tokens = sum(embedder.estimate_tokens(t) for t in batch)
            # A single oversized chunk alone in a batch is allowed to
            # exceed the budget (it can't be split further), but a batch
            # with more than one item must never exceed it.
            if len(batch) > 1:
                self.assertLessEqual(batch_tokens, max_tokens)

    def test_batches_respect_the_max_items_backstop_even_with_tiny_chunks(self):
        texts = ["x"] * 500
        batches = embedder.batch_by_token_budget(texts, max_tokens=1_000_000, max_items=50)
        for batch in batches:
            self.assertLessEqual(len(batch), 50)

    def test_default_token_budget_is_comfortably_under_the_free_tier_tpm_cap(self):
        self.assertLess(embedder.MAX_TOKENS_PER_EMBED_REQUEST, 10_000)


class TestEncodePassagesPacingAndRetry(unittest.TestCase):

    def setUp(self):
        embedder._client = None

    @patch("app.rag.embedder._get_client")
    @patch("time.sleep")
    def test_multiple_batches_are_paced_between_requests(self, mock_sleep, mock_get_client):
        mock_client = MagicMock()
        mock_client.embed.side_effect = [
            MagicMock(embeddings=[[0.1] * 1024]),
            MagicMock(embeddings=[[0.2] * 1024]),
        ]
        mock_get_client.return_value = mock_client

        with patch.object(embedder, "MAX_TOKENS_PER_EMBED_REQUEST", 10):
            texts = ["x" * 100, "y" * 100]  # each forced into its own batch
            embeddings = embedder.encode_passages(texts)

        self.assertEqual(len(embeddings), 2)
        self.assertEqual(mock_client.embed.call_count, 2)
        # Paced BETWEEN batches only — one sleep for two batches, not zero
        # and not before the very first request.
        mock_sleep.assert_called_once_with(embedder.EMBED_REQUEST_PACING_SECONDS)

    @patch("app.rag.embedder._get_client")
    @patch("time.sleep")
    def test_single_batch_is_never_paced(self, mock_sleep, mock_get_client):
        mock_client = MagicMock()
        mock_client.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])
        mock_get_client.return_value = mock_client

        embedder.encode_passages(["one chunk"])

        mock_sleep.assert_not_called()

    @patch("app.rag.embedder._get_client")
    @patch("time.sleep")
    def test_rate_limit_error_is_retried_with_bounded_exponential_backoff(
        self, mock_sleep, mock_get_client
    ):
        mock_client = MagicMock()
        mock_client.embed.side_effect = [
            _rate_limit_error(),
            _rate_limit_error(),
            MagicMock(embeddings=[[0.5] * 1024]),
        ]
        mock_get_client.return_value = mock_client

        embeddings = embedder.encode_passages(["one chunk"])

        self.assertEqual(embeddings, [[0.5] * 1024])
        self.assertEqual(mock_client.embed.call_count, 3)
        # Two retries -> two backoff sleeps, strictly increasing.
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        self.assertEqual(len(sleep_calls), 2)
        self.assertLess(sleep_calls[0], sleep_calls[1])

    @patch("app.rag.embedder._get_client")
    @patch("time.sleep")
    def test_retries_are_bounded_not_infinite(self, mock_sleep, mock_get_client):
        mock_client = MagicMock()
        mock_client.embed.side_effect = _rate_limit_error()  # always raises
        mock_get_client.return_value = mock_client

        with self.assertRaises(RateLimitError):
            embedder.encode_passages(["one chunk"])

        # MAX_RATE_LIMIT_RETRIES retries + the original attempt.
        self.assertEqual(mock_client.embed.call_count, embedder.MAX_RATE_LIMIT_RETRIES + 1)

    @patch("app.rag.embedder._get_client")
    @patch("time.sleep")
    def test_non_rate_limit_errors_are_never_retried(self, mock_sleep, mock_get_client):
        mock_client = MagicMock()
        mock_client.embed.side_effect = AuthenticationError("bad key", None, 401, None, None)
        mock_get_client.return_value = mock_client

        with self.assertRaises(AuthenticationError):
            embedder.encode_passages(["one chunk"])

        mock_client.embed.assert_called_once()
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
