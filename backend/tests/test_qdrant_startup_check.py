"""
Qdrant client construction and the startup reachability check.

The client used to run qdrant-client's version-compatibility request while
being built at import time, which logged an opaque "Failed to obtain server
version" warning whenever the cluster was slow or suspended. It is now built
with check_compatibility=False, and the app makes one explicit, logged
reachability request at startup that never stops the app from starting.

Offline: QdrantClient is mocked; nothing here opens a network connection.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — test bootstrap; must precede every app import

from fastapi.testclient import TestClient  # noqa: E402

import app.rag.vector_store as vector_store  # noqa: E402

CLUSTER_URL = "https://example-cluster.invalid:6333"
ERROR_TEXT = f"connection to {CLUSTER_URL} refused"
LOGGER = "app.rag.vector_store"


class BuildClientTest(unittest.TestCase):

    def test_cloud_client_skips_the_compatibility_check(self):
        with patch.object(vector_store, "_qdrant_url", CLUSTER_URL), \
                patch.object(vector_store, "_qdrant_api_key", "k"), \
                patch.object(vector_store, "QdrantClient") as qdrant:
            vector_store._build_client(timeout=7)

        kwargs = qdrant.call_args.kwargs
        self.assertIs(kwargs["check_compatibility"], False)
        self.assertEqual(kwargs["url"], CLUSTER_URL)
        self.assertEqual(kwargs["timeout"], 7)

    def test_local_client_skips_the_compatibility_check(self):
        with patch.object(vector_store, "_qdrant_url", ""), \
                patch.object(vector_store, "QdrantClient") as qdrant:
            vector_store._build_client(timeout=7)

        kwargs = qdrant.call_args.kwargs
        self.assertIs(kwargs["check_compatibility"], False)
        self.assertEqual(kwargs["host"], "localhost")


class ReachabilityCheckTest(unittest.TestCase):

    def _probe(self, **collection_exists):
        probe = MagicMock()
        probe.collection_exists = MagicMock(**collection_exists)
        builder = patch.object(vector_store, "_build_client", return_value=probe)
        built = builder.start()
        self.addCleanup(builder.stop)
        return probe, built

    def test_reachable_with_collection_logs_info(self):
        probe, built = self._probe(return_value=True)

        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertTrue(vector_store.check_qdrant_reachable())

        built.assert_called_once_with(timeout=vector_store.REACHABILITY_TIMEOUT_SECONDS)
        probe.collection_exists.assert_called_once_with(vector_store.COLLECTION_NAME)
        self.assertEqual([r.levelname for r in logs.records], ["INFO"])
        probe.close.assert_called_once()

    def test_reachable_without_collection_warns(self):
        probe, _ = self._probe(return_value=False)

        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertTrue(vector_store.check_qdrant_reachable())

        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("does not exist", logs.records[0].getMessage())
        probe.close.assert_called_once()

    def test_unreachable_warns_with_type_and_status_but_no_url_or_error_text(self):
        error = ConnectionError(ERROR_TEXT)
        error.status_code = 503
        probe, _ = self._probe(side_effect=error)

        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertFalse(vector_store.check_qdrant_reachable())

        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("ConnectionError", message)
        self.assertIn("HTTP 503", message)
        self.assertNotIn(CLUSTER_URL, message)
        self.assertNotIn("refused", message)
        probe.close.assert_called_once()

    def test_a_failure_building_the_probe_is_reported_not_raised(self):
        with patch.object(vector_store, "_build_client", side_effect=ValueError("bad url")), \
                self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(vector_store.check_qdrant_reachable())


class StartupRunsTheCheckTest(unittest.TestCase):

    def test_startup_runs_the_check_once(self):
        from app.main import app

        with patch.object(vector_store, "check_qdrant_reachable", return_value=True) as check:
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)

        check.assert_called_once_with()

    def test_app_still_starts_when_qdrant_is_unreachable(self):
        from app.main import app

        with patch.object(vector_store, "check_qdrant_reachable", return_value=False):
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
