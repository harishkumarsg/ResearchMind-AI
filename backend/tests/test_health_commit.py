"""
/health reports the deployed commit from RAILWAY_GIT_COMMIT_SHA, or
"unknown" when the variable is unset or blank. It used to return a
hard-coded "version": "3.1.0", which could not show what was running.
"""
import os
import sys
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — test bootstrap; must precede every app import

from fastapi.testclient import TestClient  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


class HealthCommitTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.client = TestClient(app)

    def _health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_reports_the_railway_commit(self):
        with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}):
            body = self._health()
        self.assertEqual(body["commit"], SHA)
        self.assertEqual(body["status"], "healthy")

    def test_unknown_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "RAILWAY_GIT_COMMIT_SHA"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._health()["commit"], "unknown")

    def test_unknown_when_blank(self):
        with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "  "}):
            self.assertEqual(self._health()["commit"], "unknown")

    def test_no_hard_coded_version(self):
        with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}):
            self.assertNotIn("version", self._health())


if __name__ == "__main__":
    unittest.main()
