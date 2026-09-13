"""
Phase 1 focused tests — verify each of the 6 approved safety fixes.

Updated for Phase 1.5: /upload, /index-document, and /paper/{name} now
require authentication and a database, neither of which exist live yet.
This file uses the same in-memory-SQLite + auth-dependency-override
harness as test_phase1_5_ownership.py so the ORIGINAL Phase 1
guarantees (POST-only, delete_collection never called, path traversal
rejected, magic-byte check, size limit) can still be verified end to
end, in an authenticated context, without any live Supabase/Qdrant
infrastructure.

Any file this suite might have touched under backend/uploads/papers/ in
its Phase 1 form no longer applies — uploads go to (mocked) Supabase
Storage now, never local disk — so there is nothing to clean up there
anymore.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id
from app.db.models import Base
from app.db.session import get_db_session
from tests.live_infra import requires_live_infra

TEST_OWNER = "cccccccc-cccc-cccc-cccc-cccccccccccc"

MINIMAL_VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF"
)


class AuthenticatedTestCase(unittest.TestCase):
    """Every route these tests exercise now requires auth + a database.
    Neither exists live yet, so both are overridden the same way
    test_phase1_5_ownership.py does it."""

    def setUp(self):
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self.SessionLocal = sessionmaker(
            bind=self._engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

        from app.main import app
        self.app = app

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[get_db_session] = override_db
        self.app.dependency_overrides[get_current_identity] = (
            lambda: AuthenticatedIdentity(owner_id=TEST_OWNER, token="test-jwt-token")
        )
        self.app.dependency_overrides[get_current_owner_id] = lambda: TEST_OWNER

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._engine.dispose()


class TestIndexDocumentIsPostOnlyAndNonDestructive(AuthenticatedTestCase):
    """Fix #1: index_document.py duplicate removed, GET gone, POST-only,
    delete_collection() no longer part of the flow."""

    def test_get_index_document_is_no_longer_allowed(self):
        # Method-not-allowed is a routing-level decision made before any
        # dependency (including auth) is resolved, so this needs no auth.
        resp = self.client.get("/index-document")
        self.assertEqual(
            resp.status_code, 405,
            "GET /index-document should be rejected (405) now that "
            "indexing is POST-only",
        )

    def test_post_index_document_does_not_404_or_405(self):
        resp = self.client.post("/index-document")
        self.assertNotIn(
            resp.status_code, (404, 405),
            "POST /index-document should be a recognized, allowed route",
        )
        body = resp.json()
        self.assertIn("status", body)
        self.assertEqual(body["papers_found"], 0)  # nothing seeded in this test

    def test_source_contains_no_delete_collection_call(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "index_document.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        # Checks for the actual call pattern, not a bare substring — an
        # explanatory comment is allowed to mention the old bug by name
        # without tripping this check.
        self.assertNotIn(
            ".delete_collection(", source,
            "index_document.py must not call delete_collection() anywhere",
        )

    def test_no_duplicate_route_registration(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "index_document.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertEqual(
            source.count('"/index-document"'), 1,
            "Expected exactly one /index-document route definition "
            "(the duplicate module must be fully removed)",
        )

    def test_indexing_never_calls_delete_collection_at_runtime(self):
        """Behavioral guarantee, not just a source-text check: patch the
        real Qdrant client's delete_collection to explode if invoked,
        then call the endpoint and confirm it never fires."""
        from app.rag import vector_store

        with patch.object(
            vector_store.client,
            "delete_collection",
            side_effect=AssertionError("delete_collection must never be called"),
        ) as mock_delete:
            resp = self.client.post("/index-document")
            mock_delete.assert_not_called()
            self.assertIsNotNone(resp)


class TestUploadValidation(AuthenticatedTestCase):
    """Fix #2: filename sanitization, size limit, PDF magic-byte check.
    Storage is mocked — these tests verify validation happens BEFORE
    any Storage call is attempted, not Storage integration itself
    (that's covered in test_phase1_5_ownership.py)."""

    def test_rejects_non_pdf_extension(self):
        with patch("app.api.upload.upload_pdf") as mock_upload:
            resp = self.client.post(
                "/upload",
                files={"file": ("notes.txt", b"just some text", "text/plain")},
            )
            self.assertEqual(resp.status_code, 400)
            mock_upload.assert_not_called()

    def test_rejects_pdf_extension_with_wrong_magic_bytes(self):
        with patch("app.api.upload.upload_pdf") as mock_upload:
            resp = self.client.post(
                "/upload",
                files={"file": ("fake.pdf", b"NOT A REAL PDF FILE CONTENT", "application/pdf")},
            )
            self.assertEqual(resp.status_code, 400)
            mock_upload.assert_not_called()

    def test_rejects_oversized_file(self):
        import app.api.upload as upload_module

        with patch("app.api.upload.upload_pdf") as mock_upload, \
             patch.object(upload_module, "MAX_UPLOAD_BYTES", 10):
            resp = self.client.post(
                "/upload",
                files={"file": ("big.pdf", MINIMAL_VALID_PDF, "application/pdf")},
            )
            self.assertEqual(resp.status_code, 413)
            mock_upload.assert_not_called()

    def test_accepts_valid_pdf_and_returns_expected_shape(self):
        with patch("app.api.upload.upload_pdf") as mock_upload:
            mock_upload.return_value = "some/storage/path.pdf"
            resp = self.client.post(
                "/upload",
                files={"file": ("real_paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["message"], "PDF uploaded successfully")
            self.assertEqual(body["filename"], "real_paper.pdf")
            self.assertEqual(body["status"], "uploaded")
            self.assertIn("paper_id", body)
            mock_upload.assert_called_once()


class TestDeletePaperOwnershipReplacesFilenameSanitization(AuthenticatedTestCase):
    """Fix #3, as redesigned in Phase 1.5: paper_name-pattern sanitization
    was replaced by an owner-scoped database lookup (see
    test_phase1_5_ownership.py for the full malicious-access matrix).
    This test preserves the original guarantee this fix protected —
    "a name that doesn't resolve to a real, owned paper never reaches
    Qdrant" — under the new mechanism."""

    def test_nonexistent_paper_name_never_reaches_qdrant(self):
        from app.rag import vector_store

        with patch.object(
            vector_store.client,
            "scroll",
            side_effect=AssertionError("scroll must not be reached for a nonexistent paper"),
        ) as mock_scroll:
            resp = self.client.delete("/paper/..secret")
            mock_scroll.assert_not_called()
            self.assertEqual(resp.status_code, 404)


class TestGroqModelUpdated(unittest.TestCase):
    """Fix #4/#5: both hardcoded Groq model locations updated, and the
    new model is actually available to this account (flips the Phase 0
    baseline failure to a pass)."""

    EXPECTED_MODEL = "openai/gpt-oss-120b"

    def test_qa_agent_model_updated(self):
        from app.agents.qa_agent import GROQ_MODEL
        self.assertEqual(GROQ_MODEL, self.EXPECTED_MODEL)

    def test_ask_stream_model_updated(self):
        from app.api.ask_stream import GROQ_MODEL
        self.assertEqual(GROQ_MODEL, self.EXPECTED_MODEL)

    @requires_live_infra
    def test_new_model_is_available_to_this_account(self):
        """Gated: contacts the real Groq API. When opted in, a failed
        call is a REAL failure — the previous version swallowed it into
        skipTest, so an outage or expired key looked identical to a pass.

        The deterministic half of this guarantee (that both modules
        declare the expected model) is covered by the two tests above,
        which always run."""
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key or api_key == "your_groq_api_key_here":
            self.skipTest("GROQ_API_KEY not configured")

        from groq import Groq
        client = Groq(api_key=api_key)
        live_models = [m.id for m in client.models.list().data]

        self.assertIn(
            self.EXPECTED_MODEL, live_models,
            f"{self.EXPECTED_MODEL} should now be available — this was "
            f"the whole point of the Phase 1 model swap",
        )


class TestFrontendUsesPost(unittest.TestCase):
    """Fix #6: api.ts indexDocuments() now issues a POST."""

    def test_index_documents_function_uses_post(self):
        path = os.path.join(
            os.path.dirname(BACKEND_DIR), "research-compass-main",
            "src", "lib", "api.ts",
        )
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        start = source.index("export async function indexDocuments")
        end = source.index("export async function", start + 1)
        function_body = source[start:end]

        self.assertIn(
            'method: "POST"', function_body,
            "indexDocuments() must issue a POST request",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
