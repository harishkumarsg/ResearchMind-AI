"""
Storage failures are logged: exception type and status codes, nothing else.

Background: on 2026-11-23 every upload returned 502 and the PDF viewer
said the file was unavailable, and neither the API's logs nor the paper
row said why. upload.py and paper_file.py caught the Storage exception and
dropped it. The cause (the project's legacy JWT API keys had been
disabled) was found only from Supabase's own logs.

Now each failed Storage call logs one warning line through
storage.log_storage_failure:

    storage upload failed: type=StorageApiError http_status=400 storage_status=403

and never the exception's message (built from the response body), the
request URL (its object path holds the owner's id), a key or the user's
JWT.

The Storage errors here come from the REAL pinned storage3 client, run
against an httpx.MockTransport, so the exception chain is the one
production raises. No network: MockTransport answers in-process.
"""
import hashlib
import logging
import os
import sys
import unittest
import uuid
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — test bootstrap; must precede every app import

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from storage3 import SyncStorageClient  # noqa: E402

from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id  # noqa: E402
from app.db.models import Base, Paper  # noqa: E402
from app.services.storage import (  # noqa: E402
    BUCKET_NAME,
    build_storage_path,
    storage_failure_fields,
)

OWNER = "a1a1a1a1-0000-4000-8000-a1a1a1a1a1a1"
PAPER = "b2b2b2b2-0000-4000-8000-b2b2b2b2b2b2"
PDF_BYTES = b"%PDF-1.4 storage failure logging"

# Sentinels: none of these may appear in any log record.
SECRET_KEY = "sb_secret_SENTINELKEY0000"
USER_JWT = "user-jwt-SENTINELTOKEN"
RLS_MESSAGE = "new row violates row-level security policy SENTINELMSG"
STORAGE_URL = "https://loggingtest.supabase.co/storage/v1/"

FORBIDDEN_IN_LOGS = (
    SECRET_KEY,
    USER_JWT,
    "SENTINELMSG",
    "row-level security",
    "Legacy API keys",
    "loggingtest.supabase.co",
    "/storage/v1",
    OWNER,
    "original.pdf",
    "is not configured",
)


def storage_error(status, body, operation="upload", path=None):
    """The exception the real storage3 client raises for this response."""

    def respond(request):
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    client = SyncStorageClient(
        url=STORAGE_URL,
        headers={"apikey": SECRET_KEY, "Authorization": f"Bearer {USER_JWT}"},
        http_client=http_client,
    )
    bucket = client.from_(BUCKET_NAME)
    path = path or build_storage_path(OWNER, PAPER)
    try:
        if operation == "upload":
            bucket.upload(path, PDF_BYTES, file_options={"content-type": "application/pdf"})
        else:
            bucket.download(path)
    except Exception as exc:  # the point: capture whatever storage3 raises
        return exc
    finally:
        http_client.close()
    raise AssertionError("storage3 did not raise for an error response")


def rls_rejection():
    # What Supabase Storage answers when RLS refuses an upload: HTTP 400,
    # with the "real" status only in the body.
    return storage_error(
        400, {"statusCode": "403", "error": "Unauthorized", "message": RLS_MESSAGE}
    )


def gateway_rejection():
    # The API gateway, not Storage, answers when a disabled legacy key is
    # used, and its body has no statusCode. storage3 2.31.0's fallback for
    # that case calls .text on the parsed dict, so what production actually
    # raises is an AttributeError, chained to the KeyError and the 401
    # HTTPStatusError. The helper still finds the 401 through the chain.
    return storage_error(
        401,
        {"message": "Legacy API keys are disabled", "hint": "Use a publishable or secret key."},
        operation="download",
    )


def connect_error():
    request = httpx.Request("GET", STORAGE_URL + f"object/{BUCKET_NAME}/" + build_storage_path(OWNER, PAPER))
    return httpx.ConnectError("connection refused to " + str(request.url), request=request)


class TestStorageFailureFields(unittest.TestCase):
    def test_rls_rejection_keeps_both_statuses(self):
        self.assertEqual(
            storage_failure_fields(rls_rejection()),
            {"type": "StorageApiError", "http_status": 400, "storage_status": 403},
        )

    def test_gateway_rejection_reports_the_real_http_status(self):
        # The exception is not even a StorageApiError (see gateway_rejection);
        # the 401 is recovered from the chained HTTPStatusError.
        self.assertEqual(
            storage_failure_fields(gateway_rejection()),
            {"type": "AttributeError", "http_status": 401, "storage_status": None},
        )

    def test_network_error_has_no_status(self):
        self.assertEqual(
            storage_failure_fields(connect_error()),
            {"type": "ConnectError", "http_status": None, "storage_status": None},
        )

    def test_missing_configuration_has_no_status(self):
        error = RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")
        self.assertEqual(
            storage_failure_fields(error),
            {"type": "RuntimeError", "http_status": None, "storage_status": None},
        )

    def test_a_non_numeric_body_status_is_never_passed_through(self):
        for status in ("Unauthorized " + SECRET_KEY, "4O3", "403 ", "²²²", True, 1000, None):
            with self.subTest(status=status):
                error = rls_rejection()
                error.status = status
                self.assertIsNone(storage_failure_fields(error)["storage_status"])

    def test_a_cyclic_exception_chain_terminates(self):
        first, second = ValueError("a"), ValueError("b")
        first.__context__, second.__context__ = second, first
        self.assertEqual(storage_failure_fields(first)["http_status"], None)


class RouteLoggingTestCase(unittest.TestCase):
    """Real in-memory SQLite and the real app; Storage calls patched."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        patcher = patch("app.db.session.get_session_factory", return_value=self.SessionLocal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

        env = patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": SECRET_KEY}, clear=False)
        env.start()
        self.addCleanup(env.stop)

        from app.main import app

        self.app = app
        self.addCleanup(self.app.dependency_overrides.clear)
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER
        self.app.dependency_overrides[get_current_identity] = lambda: AuthenticatedIdentity(
            owner_id=OWNER, token=USER_JWT
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def seed_paper(self, paper_id, status, content_hash="hash"):
        session = self.SessionLocal()
        try:
            session.add(
                Paper(
                    id=uuid.UUID(paper_id),
                    owner_id=uuid.UUID(OWNER),
                    title="paper.pdf",
                    content_hash=content_hash,
                    storage_path=build_storage_path(OWNER, paper_id),
                    file_size_bytes=len(PDF_BYTES),
                    status=status,
                )
            )
            session.commit()
        finally:
            session.close()

    def paper_row(self, paper_id):
        session = self.SessionLocal()
        try:
            return session.get(Paper, uuid.UUID(paper_id))
        finally:
            session.close()

    def paper_row_by_content(self, content_hash):
        # Paper ids are random per incarnation (see PAPER IDENTITY in
        # app/api/upload.py), so a row the upload created is found the way
        # the upload itself finds duplicates: by (owner, content_hash).
        session = self.SessionLocal()
        try:
            return (
                session.query(Paper)
                .filter(Paper.owner_id == uuid.UUID(OWNER), Paper.content_hash == content_hash)
                .one()
            )
        finally:
            session.close()

    def assert_nothing_sensitive(self, records):
        text = "\n".join(
            f"{record.name} {record.getMessage()} {record.exc_text or ''}" for record in records
        )
        for forbidden in FORBIDDEN_IN_LOGS:
            self.assertNotIn(forbidden, text)
        for record in records:
            # A traceback would print the message and the URL.
            self.assertIsNone(record.exc_info, record.getMessage())

    def storage_lines(self, records, logger_name):
        return [
            record.getMessage()
            for record in records
            if record.name == logger_name and record.getMessage().startswith("storage ")
        ]

    def upload(self):
        return self.client.post(
            "/upload", files={"file": ("paper.pdf", PDF_BYTES, "application/pdf")}
        )


class TestUploadLogsStorageFailures(RouteLoggingTestCase):
    def test_a_failed_upload_logs_type_and_statuses(self):
        with patch("app.api.upload.upload_pdf", side_effect=rls_rejection()), \
                self.assertLogs(level="DEBUG") as captured:
            resp = self.upload()

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(
            self.storage_lines(captured.records, "app.api.upload"),
            ["storage upload failed: type=StorageApiError http_status=400 storage_status=403"],
        )
        self.assert_nothing_sensitive(captured.records)
        self.assertNotIn("SENTINELMSG", resp.text)

    def test_the_row_still_gets_the_authored_detail(self):
        from app.api.upload import STORAGE_FAILED_DETAIL

        with patch("app.api.upload.upload_pdf", side_effect=rls_rejection()), \
                self.assertLogs("app.api.upload", level="WARNING"):
            self.upload()

        row = self.paper_row_by_content(hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertEqual((row.status, row.status_detail), ("failed", STORAGE_FAILED_DETAIL))

    def test_missing_configuration_is_logged_by_type_only(self):
        error = RuntimeError("SUPABASE_ANON_KEY is not configured")
        with patch("app.api.upload.upload_pdf", side_effect=error), \
                self.assertLogs(level="DEBUG") as captured:
            resp = self.upload()

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(
            self.storage_lines(captured.records, "app.api.upload"),
            ["storage upload failed: type=RuntimeError http_status=None storage_status=None"],
        )
        self.assert_nothing_sensitive(captured.records)

    def test_a_failed_existence_check_on_retry_is_logged(self):
        content_hash = hashlib.sha256(PDF_BYTES).hexdigest()
        self.seed_paper(str(uuid.uuid4()), "failed", content_hash=content_hash)

        with patch("app.api.upload.storage_object_exists", side_effect=connect_error()), \
                patch("app.api.upload.upload_pdf", return_value="ignored"), \
                self.assertLogs(level="DEBUG") as captured:
            resp = self.upload()

        # The check failing is not fatal: the upload is attempted anyway.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.storage_lines(captured.records, "app.api.upload"),
            ["storage existence check failed: type=ConnectError http_status=None storage_status=None"],
        )
        self.assert_nothing_sensitive(captured.records)

    def test_a_successful_upload_logs_no_storage_failure(self):
        logger = logging.getLogger("app.api.upload")
        with patch("app.api.upload.upload_pdf", return_value="ignored"), \
                patch.object(logger, "warning") as warning:
            resp = self.upload()

        self.assertEqual(resp.status_code, 200)
        warning.assert_not_called()


class TestPaperFileLogsStorageFailures(RouteLoggingTestCase):
    def setUp(self):
        super().setUp()
        self.seed_paper(PAPER, "indexed")

    def get_file(self):
        return self.client.get("/paper-file", params={"paper_id": PAPER})

    def test_a_failed_download_logs_the_real_http_status(self):
        from app.api.paper_file import UNAVAILABLE_MESSAGE

        with patch("app.api.paper_file.fetch_pdf", side_effect=gateway_rejection()), \
                self.assertLogs(level="DEBUG") as captured:
            resp = self.get_file()

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["message"], UNAVAILABLE_MESSAGE)
        self.assertEqual(
            self.storage_lines(captured.records, "app.api.paper_file"),
            ["storage download failed: type=AttributeError http_status=401 storage_status=None"],
        )
        self.assert_nothing_sensitive(captured.records)

    def test_missing_service_key_is_logged_by_type_only(self):
        error = RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")
        with patch("app.api.paper_file.fetch_pdf", side_effect=error), \
                self.assertLogs(level="DEBUG") as captured:
            resp = self.get_file()

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(
            self.storage_lines(captured.records, "app.api.paper_file"),
            ["storage download failed: type=RuntimeError http_status=None storage_status=None"],
        )
        self.assert_nothing_sensitive(captured.records)

    def test_another_owners_paper_never_reaches_storage_or_the_log(self):
        other_paper = "c3c3c3c3-0000-4000-8000-c3c3c3c3c3c3"
        logger = logging.getLogger("app.api.paper_file")
        with patch("app.api.paper_file.fetch_pdf") as fetch, \
                patch.object(logger, "warning") as warning:
            resp = self.client.get("/paper-file", params={"paper_id": other_paper})

        self.assertEqual(resp.status_code, 404)
        fetch.assert_not_called()
        warning.assert_not_called()


class TestRoutesLogOnlyThroughTheHelper(unittest.TestCase):
    """A later edit must not add a logger call that formats the exception."""

    def test_no_direct_logger_calls_in_the_storage_routes(self):
        import app.api.paper_file as paper_file_mod
        import app.api.upload as upload_mod

        for mod in (upload_mod, paper_file_mod):
            with open(mod.__file__, encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    self.assertNotRegex(
                        stripped,
                        r"\blogger\.(debug|info|warning|error|exception|critical|log)\(",
                        f"{os.path.basename(mod.__file__)}:{number}",
                    )
                    self.assertNotIn("print(", stripped, f"{os.path.basename(mod.__file__)}:{number}")


if __name__ == "__main__":
    unittest.main()
