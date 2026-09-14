"""
CORS allowlist tests.

Starlette's CORSMiddleware matches the browser's Origin header by exact
string comparison, so the allowlist must name the real production frontend
origin. These tests pin that origin, keep localhost development working,
and keep out the old placeholder domain (which serves an unrelated app)
and the "*.vercel.app" entry that never matched anything.

Preflight (OPTIONS) requests are answered by the middleware itself, and
/health touches no database, Storage or Qdrant code.
"""
import os
import sys
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from fastapi.testclient import TestClient

from app.core.cors import (
    DEFAULT_ALLOWED_ORIGINS,
    EXTRA_ORIGINS_ENV_VAR,
    get_allowed_origins,
)

PRODUCTION_ORIGIN = "https://research-mind-ai-indol.vercel.app"
PLACEHOLDER_ORIGIN = "https://researchmind-ai.vercel.app"
PREVIEW_STYLE_ORIGIN = "https://research-mind-ai-git-master-example.vercel.app"


class TestAllowedOrigins(unittest.TestCase):
    def origins_with_extras(self, value):
        with patch.dict(os.environ, {EXTRA_ORIGINS_ENV_VAR: value}):
            return get_allowed_origins()

    def test_production_frontend_is_allowed_by_default(self):
        self.assertIn(PRODUCTION_ORIGIN, self.origins_with_extras(""))

    def test_local_development_origins_are_allowed_by_default(self):
        origins = self.origins_with_extras("")
        self.assertIn("http://localhost:8080", origins)
        self.assertIn("http://127.0.0.1:8080", origins)

    def test_placeholder_and_pattern_entries_are_gone(self):
        origins = self.origins_with_extras("")
        self.assertNotIn(PLACEHOLDER_ORIGIN, origins)
        self.assertFalse([o for o in origins if "*" in o])

    def test_extra_origins_are_trimmed_deduplicated_and_appended(self):
        origins = self.origins_with_extras(
            f" https://custom.example.com/ , ,{PRODUCTION_ORIGIN},https://custom.example.com"
        )
        self.assertEqual(origins, list(DEFAULT_ALLOWED_ORIGINS) + ["https://custom.example.com"])

    def test_bare_wildcard_extra_is_ignored(self):
        self.assertEqual(self.origins_with_extras("*"), list(DEFAULT_ALLOWED_ORIGINS))

    def test_main_module_no_longer_hardcodes_the_old_entries(self):
        with open(os.path.join(BACKEND_DIR, "app", "main.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn(PLACEHOLDER_ORIGIN, source)
        self.assertNotIn("*.vercel.app", source)
        self.assertIn("get_allowed_origins()", source)


class TestCorsMiddleware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        cls.client = TestClient(app)

    def preflight(self, origin):
        return self.client.options(
            "/papers",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    def test_production_preflight_is_allowed_with_authorization_header(self):
        resp = self.preflight(PRODUCTION_ORIGIN)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), PRODUCTION_ORIGIN)
        self.assertIn("authorization", resp.headers.get("access-control-allow-headers", "").lower())

    def test_localhost_preflight_is_allowed(self):
        resp = self.preflight("http://localhost:8080")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "http://localhost:8080")

    def test_placeholder_origin_preflight_is_rejected(self):
        resp = self.preflight(PLACEHOLDER_ORIGIN)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(resp.headers.get("access-control-allow-origin"))

    def test_preview_style_vercel_origin_is_rejected(self):
        resp = self.preflight(PREVIEW_STYLE_ORIGIN)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(resp.headers.get("access-control-allow-origin"))

    def test_simple_request_from_production_gets_allow_origin_header(self):
        resp = self.client.get("/health", headers={"Origin": PRODUCTION_ORIGIN})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), PRODUCTION_ORIGIN)

    def test_simple_request_from_unknown_origin_gets_no_allow_origin_header(self):
        resp = self.client.get("/health", headers={"Origin": PLACEHOLDER_ORIGIN})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
