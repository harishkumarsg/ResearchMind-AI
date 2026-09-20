"""
Phase 1.5 ownership tests.

Uses an in-memory SQLite database (via the app's cross-dialect
SQLAlchemy Uuid/JSON types) standing in for Postgres, and mocks Supabase
Storage + Qdrant entirely — no live Supabase project or Qdrant cluster
exists yet. This validates the OWNERSHIP LOGIC itself: that one user can
never read, index, or delete another user's paper by any means, and
that the failure-recovery ordering designed earlier actually holds.
"""
import hashlib
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

from app.api.upload import compute_paper_id
from app.core.auth import AuthenticatedIdentity, get_current_identity, get_current_owner_id
from app.db.models import Base, Paper
from app.db.session import get_db_session

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

MINIMAL_VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF"
)

MINIMAL_VALID_PDF_HASH = hashlib.sha256(MINIMAL_VALID_PDF).hexdigest()


def _expected_paper_id(owner_id: str) -> uuid.UUID:
    """The exact paper_id the /upload endpoint will compute for
    MINIMAL_VALID_PDF + this owner — a seeded row must use this ID to
    actually be found by the endpoint's existing-row lookup."""
    return compute_paper_id(owner_id, MINIMAL_VALID_PDF_HASH)


def _new_sqlite_engine_and_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # expire_on_commit=False: several tests inspect ORM objects captured by
    # a mock's call_args AFTER the request (and its session) has finished —
    # SQLAlchemy's default would expire and try to re-fetch those attributes
    # from an already-closed session, raising DetachedInstanceError. This is
    # a test-harness convenience only; production's session.py keeps the
    # SQLAlchemy default.
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine, factory


class OwnershipTestBase(unittest.TestCase):
    """Common app + fresh-per-test in-memory DB + auth override wiring."""

    def setUp(self):
        self._engine, self.SessionLocal = _new_sqlite_engine_and_session_factory()
        self.current_test_owner = USER_A

        from app.main import app
        self.app = app

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        def override_identity():
            return AuthenticatedIdentity(owner_id=self.current_test_owner, token="test-jwt-token")

        def override_owner_id():
            return self.current_test_owner

        self.app.dependency_overrides[get_db_session] = override_db
        self.app.dependency_overrides[get_current_identity] = override_identity
        self.app.dependency_overrides[get_current_owner_id] = override_owner_id

        # raise_server_exceptions=False: some tests deliberately trigger an
        # unhandled exception (e.g. simulated Qdrant outage) and assert on
        # the resulting HTTP 500 — the behavior a real deployment would
        # show. TestClient's default re-raises instead, for debuggability.
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._engine.dispose()

    def as_user(self, owner_id):
        self.current_test_owner = owner_id
        return self

    def _seed_paper(self, owner_id, title, status="uploaded", content_hash=None):
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=uuid.uuid4(),
                owner_id=uuid.UUID(owner_id),
                title=title,
                content_hash=content_hash or ("a" * 64),
                storage_path=f"{owner_id}/x/original.pdf",
                file_size_bytes=100,
                status=status,
            )
            session.add(paper)
            session.commit()
            return paper.id
        finally:
            session.close()


class TestUploadOwnership(OwnershipTestBase):

    @patch("app.api.upload.upload_pdf")
    def test_upload_creates_owned_paper_row(self, mock_upload):
        mock_upload.return_value = "some/storage/path.pdf"
        self.as_user(USER_A)

        resp = self.client.post(
            "/upload",
            files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "uploaded")

        session = self.SessionLocal()
        try:
            paper = session.query(Paper).filter(Paper.id == uuid.UUID(body["paper_id"])).first()
            self.assertIsNotNone(paper)
            self.assertEqual(str(paper.owner_id), USER_A)
        finally:
            session.close()

        mock_upload.assert_called_once()
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["owner_id"], USER_A)
        self.assertEqual(kwargs["user_jwt"], "test-jwt-token")

    @patch("app.api.upload.upload_pdf")
    def test_duplicate_upload_same_user_same_bytes_is_detected(self, mock_upload):
        mock_upload.return_value = "some/storage/path.pdf"
        self.as_user(USER_A)

        first = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        second = self.client.post(
            "/upload", files={"file": ("paper_renamed.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(first.json()["paper_id"], second.json()["paper_id"])
        self.assertTrue(second.json().get("duplicate"))
        mock_upload.assert_called_once()

    @patch("app.api.upload.upload_pdf")
    def test_two_users_uploading_identical_bytes_get_different_paper_ids(self, mock_upload):
        mock_upload.return_value = "some/storage/path.pdf"

        self.as_user(USER_A)
        resp_a = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.as_user(USER_B)
        resp_b = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertNotEqual(resp_a.json()["paper_id"], resp_b.json()["paper_id"])

    @patch("app.api.upload.upload_pdf", side_effect=RuntimeError("storage is down"))
    def test_storage_failure_leaves_a_visible_retryable_row_not_an_orphan(self, mock_upload):
        self.as_user(USER_A)
        resp = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(resp.status_code, 502)

        session = self.SessionLocal()
        try:
            papers = session.query(Paper).filter(Paper.owner_id == uuid.UUID(USER_A)).all()
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0].status, "failed")
            # status_detail is rendered in the UI, so it is now an
            # application-authored message rather than str(e). The row is
            # still marked failed, which is what this test is about.
            from app.api.upload import STORAGE_FAILED_DETAIL

            self.assertEqual(papers[0].status_detail, STORAGE_FAILED_DETAIL)
            self.assertNotIn("storage is down", papers[0].status_detail)
        finally:
            session.close()

    def test_rejects_non_pdf_and_oversized_exactly_as_phase1(self):
        """Confirms Phase 1's validation guarantees survive the rewrite."""
        self.as_user(USER_A)
        resp = self.client.post(
            "/upload", files={"file": ("notes.txt", b"just text", "text/plain")},
        )
        self.assertEqual(resp.status_code, 400)

    @patch("app.api.upload.storage_object_exists", return_value=False)
    @patch("app.api.upload.upload_pdf", side_effect=RuntimeError("storage is down"))
    def test_failed_row_is_never_reported_as_a_successful_duplicate(self, mock_upload, mock_exists):
        """Regression test for the manual-testing finding: retrying an
        upload whose Paper row is already status='failed' used to return
        HTTP 200 with duplicate=true — a false-positive success that never
        actually touched Storage again."""
        self.as_user(USER_A)
        first = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(first.status_code, 502)

        second = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(second.status_code, 502)
        body = second.json()
        self.assertNotIn("duplicate", body)
        self.assertEqual(mock_upload.call_count, 2)

    @patch("app.api.upload.storage_object_exists", return_value=False)
    @patch("app.api.upload.upload_pdf")
    def test_failed_upload_can_be_retried_and_succeeds(self, mock_upload, mock_exists):
        """First attempt fails (Storage down), second attempt with the
        identical bytes actually retries — and succeeds once Storage
        recovers — reusing the SAME row/paper_id rather than creating a
        second one or reporting a stale duplicate."""
        mock_upload.side_effect = [RuntimeError("storage is down"), "some/storage/path.pdf"]
        self.as_user(USER_A)

        first = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(first.status_code, 502)
        failed_paper_id = None
        session = self.SessionLocal()
        try:
            papers = session.query(Paper).filter(Paper.owner_id == uuid.UUID(USER_A)).all()
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0].status, "failed")
            failed_paper_id = papers[0].id
        finally:
            session.close()

        second = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertNotIn("duplicate", body)
        self.assertEqual(uuid.UUID(body["paper_id"]), failed_paper_id)

        session = self.SessionLocal()
        try:
            papers = session.query(Paper).filter(Paper.owner_id == uuid.UUID(USER_A)).all()
            # Same row reused, not a second one created for identical content.
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0].id, failed_paper_id)
            self.assertEqual(papers[0].status, "uploaded")
            self.assertIsNone(papers[0].status_detail)
        finally:
            session.close()

        self.assertEqual(mock_upload.call_count, 2)

    @patch("app.api.upload.upload_pdf")
    def test_genuinely_indexed_duplicate_is_still_reported_as_duplicate(self, mock_upload):
        """An already-'indexed' paper must still short-circuit as a real
        duplicate — the fix only changes behavior for 'failed' rows."""
        self.as_user(USER_A)
        first = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(first.status_code, 200)
        paper_id = first.json()["paper_id"]

        session = self.SessionLocal()
        try:
            paper = session.query(Paper).filter(Paper.id == uuid.UUID(paper_id)).first()
            paper.status = "indexed"
            session.commit()
        finally:
            session.close()

        second = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["status"], "indexed")
        # Only the first, original call — the "indexed" duplicate never
        # re-touches Storage.
        mock_upload.assert_called_once()

    @patch("app.api.upload.storage_object_exists", return_value=False)
    @patch("app.api.upload.upload_pdf", side_effect=RuntimeError("storage is down"))
    def test_retry_of_a_failed_row_remains_owner_scoped(self, mock_upload, mock_exists):
        """A failed row belonging to USER_A must never be retried, reused,
        or reported as a duplicate for USER_B uploading identical bytes —
        paper_id is derived from (owner_id, content_hash), so USER_B's
        upload must land on an entirely distinct row."""
        self.as_user(USER_A)
        resp_a = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(resp_a.status_code, 502)

        self.as_user(USER_B)
        resp_b = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        self.assertEqual(resp_b.status_code, 502)

        session = self.SessionLocal()
        try:
            papers = session.query(Paper).all()
            self.assertEqual(len(papers), 2)
            self.assertNotEqual(papers[0].id, papers[1].id)
            owners = {str(p.owner_id) for p in papers}
            self.assertEqual(owners, {USER_A, USER_B})
            self.assertTrue(all(p.status == "failed" for p in papers))
        finally:
            session.close()

    @patch("app.api.upload.upload_pdf")
    @patch("app.api.upload.storage_object_exists", return_value=True)
    def test_retry_skips_reupload_when_storage_object_already_exists(
        self, mock_exists, mock_upload
    ):
        """Regression test for the confirmed 409-Duplicate bug: a paper
        that failed AFTER Storage already succeeded (e.g. during
        indexing) must not be re-uploaded on retry — it must be detected
        as already present and proceed straight to being indexable."""
        self.as_user(USER_A)
        expected_paper_id = _expected_paper_id(USER_A)
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=expected_paper_id,
                owner_id=uuid.UUID(USER_A),
                title="paper.pdf",
                content_hash=MINIMAL_VALID_PDF_HASH,
                storage_path=f"{USER_A}/{expected_paper_id}/original.pdf",
                file_size_bytes=100,
                status="failed",
                status_detail="Voyage rate limit exceeded",
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()

        resp = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertNotIn("duplicate", body)
        mock_upload.assert_not_called()
        mock_exists.assert_called_once()

        session = self.SessionLocal()
        try:
            papers = session.query(Paper).filter(Paper.owner_id == uuid.UUID(USER_A)).all()
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0].status, "uploaded")
            self.assertIsNone(papers[0].status_detail)
        finally:
            session.close()

    @patch("app.api.upload.upload_pdf")
    @patch("app.api.upload.storage_object_exists", return_value=False)
    def test_retry_uploads_when_storage_object_is_genuinely_missing(
        self, mock_exists, mock_upload
    ):
        mock_upload.return_value = "some/storage/path.pdf"
        self.as_user(USER_A)
        first = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )
        # First attempt: no existing row yet, so the exists-check path
        # isn't hit at all — force it into "failed" to set up the retry.
        self.assertEqual(first.status_code, 200)
        paper_id = first.json()["paper_id"]
        session = self.SessionLocal()
        try:
            paper = session.query(Paper).filter(Paper.id == uuid.UUID(paper_id)).first()
            paper.status = "failed"
            paper.status_detail = "Voyage rate limit exceeded"
            session.commit()
        finally:
            session.close()

        second = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "uploaded")
        mock_exists.assert_called_once()
        # Called once for the original fresh upload, and once more for
        # the retry (since the object was confirmed missing).
        self.assertEqual(mock_upload.call_count, 2)

    @patch("app.api.upload.upload_pdf")
    @patch("app.api.upload.storage_object_exists")
    def test_existence_check_uses_server_verified_identity_not_client_input(
        self, mock_exists, mock_upload
    ):
        """The exists-check must be called with the SAME owner_id/paper_id
        the server itself derived (from the verified JWT + the owner-
        scoped row lookup) — never anything a client could influence."""
        mock_exists.return_value = True
        self.as_user(USER_A)
        expected_paper_id = _expected_paper_id(USER_A)
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=expected_paper_id,
                owner_id=uuid.UUID(USER_A),
                title="paper.pdf",
                content_hash=MINIMAL_VALID_PDF_HASH,
                storage_path=f"{USER_A}/{expected_paper_id}/original.pdf",
                file_size_bytes=100,
                status="failed",
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()

        self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        mock_exists.assert_called_once_with(USER_A, str(expected_paper_id), "test-jwt-token")

    @patch("app.api.upload.upload_pdf")
    @patch("app.api.upload.storage_object_exists", return_value=True)
    def test_storage_reuse_retry_does_not_create_a_duplicate_paper_row(
        self, mock_exists, mock_upload
    ):
        self.as_user(USER_A)
        expected_paper_id = _expected_paper_id(USER_A)
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=expected_paper_id,
                owner_id=uuid.UUID(USER_A),
                title="paper.pdf",
                content_hash=MINIMAL_VALID_PDF_HASH,
                storage_path=f"{USER_A}/{expected_paper_id}/original.pdf",
                file_size_bytes=100,
                status="failed",
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()

        resp = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(resp.json()["paper_id"], str(expected_paper_id))
        session = self.SessionLocal()
        try:
            papers = session.query(Paper).filter(Paper.owner_id == uuid.UUID(USER_A)).all()
            self.assertEqual(len(papers), 1)
        finally:
            session.close()

    @patch("app.api.upload.upload_pdf", side_effect=RuntimeError("storage is down"))
    @patch("app.api.upload.storage_object_exists", return_value=False)
    def test_genuine_storage_failure_on_retry_still_marks_failed(self, mock_exists, mock_upload):
        """A retry where the object is genuinely missing and the re-upload
        itself fails must still produce a proper, visible failure — the
        reuse-skip logic must not mask a real Storage outage."""
        self.as_user(USER_A)
        expected_paper_id = _expected_paper_id(USER_A)
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=expected_paper_id,
                owner_id=uuid.UUID(USER_A),
                title="paper.pdf",
                content_hash=MINIMAL_VALID_PDF_HASH,
                storage_path=f"{USER_A}/{expected_paper_id}/original.pdf",
                file_size_bytes=100,
                status="failed",
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()

        resp = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(resp.status_code, 502)
        session = self.SessionLocal()
        try:
            paper = session.query(Paper).filter(Paper.id == expected_paper_id).first()
            self.assertEqual(paper.status, "failed")
            from app.api.upload import STORAGE_FAILED_DETAIL

            self.assertEqual(paper.status_detail, STORAGE_FAILED_DETAIL)
            self.assertNotIn("storage is down", paper.status_detail)
        finally:
            session.close()

    @patch("app.api.upload.upload_pdf")
    @patch("app.api.upload.storage_object_exists", side_effect=RuntimeError("network blip"))
    def test_existence_check_failure_fails_safe_by_attempting_upload(
        self, mock_exists, mock_upload
    ):
        """If we can't verify whether the object exists, we must not
        assume it does — the normal upload path is attempted instead."""
        mock_upload.return_value = "some/storage/path.pdf"
        self.as_user(USER_A)
        expected_paper_id = _expected_paper_id(USER_A)
        session = self.SessionLocal()
        try:
            paper = Paper(
                id=expected_paper_id,
                owner_id=uuid.UUID(USER_A),
                title="paper.pdf",
                content_hash=MINIMAL_VALID_PDF_HASH,
                storage_path=f"{USER_A}/{expected_paper_id}/original.pdf",
                file_size_bytes=100,
                status="failed",
            )
            session.add(paper)
            session.commit()
        finally:
            session.close()

        resp = self.client.post(
            "/upload", files={"file": ("paper.pdf", MINIMAL_VALID_PDF, "application/pdf")},
        )

        self.assertEqual(resp.status_code, 200)
        mock_upload.assert_called_once()


class TestDeleteOwnership(OwnershipTestBase):

    @patch("app.api.delete_paper.delete_pdf")
    @patch("app.api.delete_paper.client")
    def test_user_cannot_delete_another_users_paper_by_name(self, mock_qdrant, mock_delete_pdf):
        self._seed_paper(USER_A, title="Confidential_Research")

        self.as_user(USER_B)
        resp = self.client.delete("/paper/Confidential_Research")

        self.assertEqual(resp.status_code, 404)
        mock_qdrant.scroll.assert_not_called()
        mock_delete_pdf.assert_not_called()

        session = self.SessionLocal()
        try:
            still_there = session.query(Paper).filter(Paper.title == "Confidential_Research").first()
            self.assertIsNotNone(still_there)
            self.assertEqual(still_there.status, "uploaded")
        finally:
            session.close()

    @patch("app.api.delete_paper.delete_pdf")
    @patch("app.api.delete_paper.client")
    def test_owner_can_delete_their_own_paper(self, mock_qdrant, mock_delete_pdf):
        self._seed_paper(USER_A, title="My_Own_Paper")
        mock_qdrant.scroll.return_value = ([], None)

        self.as_user(USER_A)
        resp = self.client.delete("/paper/My_Own_Paper")

        self.assertEqual(resp.status_code, 200)
        mock_delete_pdf.assert_called_once()

        session = self.SessionLocal()
        try:
            gone = session.query(Paper).filter(Paper.title == "My_Own_Paper").first()
            self.assertIsNone(gone)
        finally:
            session.close()

    def test_delete_rejected_while_indexing(self):
        self._seed_paper(USER_A, title="Busy_Paper", status="indexing")
        self.as_user(USER_A)
        resp = self.client.delete("/paper/Busy_Paper")
        self.assertEqual(resp.status_code, 409)

    @patch("app.api.delete_paper.delete_pdf")
    @patch("app.api.delete_paper.client")
    def test_qdrant_failure_leaves_row_resumable_not_vanished(self, mock_qdrant, mock_delete_pdf):
        self._seed_paper(USER_A, title="Flaky_Delete")
        mock_qdrant.scroll.side_effect = RuntimeError("qdrant unreachable")

        self.as_user(USER_A)
        resp = self.client.delete("/paper/Flaky_Delete")

        self.assertEqual(resp.status_code, 500)

        session = self.SessionLocal()
        try:
            still_there = session.query(Paper).filter(Paper.title == "Flaky_Delete").first()
            self.assertIsNotNone(still_there)
            self.assertEqual(still_there.status, "deleting")
        finally:
            session.close()
        mock_delete_pdf.assert_not_called()


class TestIndexingOwnership(OwnershipTestBase):

    def test_indexing_only_processes_the_calling_users_own_papers(self):
        self._seed_paper(USER_A, "A_Paper_One")
        self._seed_paper(USER_A, "A_Paper_Two")
        self._seed_paper(USER_B, "B_Paper_One")

        with patch("app.api.index_document.index_one_paper", return_value=5) as mock_index:
            self.as_user(USER_A)
            resp = self.client.post("/index-document")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["papers_found"], 2)
        self.assertEqual(mock_index.call_count, 2)
        for call in mock_index.call_args_list:
            paper_arg = call.args[0]
            self.assertEqual(str(paper_arg.owner_id), USER_A)

    def test_already_indexed_papers_are_not_reprocessed(self):
        self._seed_paper(USER_A, "Already_Done", status="indexed")

        with patch("app.api.index_document.index_one_paper") as mock_index:
            self.as_user(USER_A)
            resp = self.client.post("/index-document")

        self.assertEqual(resp.json()["papers_found"], 0)
        mock_index.assert_not_called()

    def test_indexing_failure_marks_paper_failed_without_crashing_the_batch(self):
        self._seed_paper(USER_A, "Good_Paper")
        self._seed_paper(USER_A, "Bad_Paper")

        # index_one_paper now takes an optional before_provider_work hook
        # (E1 per-paper metering), so this double must accept it too.
        def side_effect(paper, before_provider_work=None):
            if paper.title == "Bad_Paper":
                raise ValueError("Voyage API failure")
            if before_provider_work is not None:
                before_provider_work()
            return 3

        with patch("app.api.index_document.index_one_paper", side_effect=side_effect):
            self.as_user(USER_A)
            resp = self.client.post("/index-document")

        body = resp.json()
        self.assertEqual(body["papers_indexed"], 1)
        self.assertEqual(body["papers_failed"], 1)

        session = self.SessionLocal()
        try:
            bad = session.query(Paper).filter(Paper.title == "Bad_Paper").first()
            good = session.query(Paper).filter(Paper.title == "Good_Paper").first()
            self.assertEqual(bad.status, "failed")
            # An unclassified indexing failure is authored too; a
            # classified provider failure keeps its neutral provider
            # message. Either way the raw text must not survive.
            from app.api.index_document import INDEXING_FAILED_DETAIL

            self.assertEqual(bad.status_detail, INDEXING_FAILED_DETAIL)
            self.assertNotIn("Voyage API failure", bad.status_detail)
            self.assertEqual(good.status, "indexed")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
