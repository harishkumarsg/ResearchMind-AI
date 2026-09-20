"""
D2 step 3 — /export-report must render the owner's durable Postgres
report, never in-process memory, never the old .txt fallback, and must
leave nothing on disk.

Fully offline and deterministic: the database is per-test in-memory
SQLite injected through the get_db_session dependency override. No
Postgres, no Qdrant, no Voyage, no Groq, no Supabase, no network. The
real reportlab PDF build runs — that is the thing under test.
"""
import datetime
import os
import sys
import unittest
import uuid
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.export_report as export_module
from app.core.auth import get_current_owner_id
from app.db.models import Base, Report
from app.db.session import get_db_session

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OWNER_B = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class ExportReportTestCase(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
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
        self.client = TestClient(self.app, raise_server_exceptions=False)

        self._temp_files = []

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()
        for path in self._temp_files:
            if os.path.exists(path):
                os.remove(path)

    def authenticate_as(self, owner_id):
        self.app.dependency_overrides[get_current_owner_id] = lambda: owner_id

    def seed_report(self, owner_id, query, markdown, citations, minutes_old=0):
        session = self.SessionLocal()
        try:
            session.add(
                Report(
                    owner_id=uuid.UUID(owner_id),
                    query=query,
                    report_markdown=markdown,
                    citations=citations,
                    created_at=EPOCH + datetime.timedelta(minutes=minutes_old),
                )
            )
            session.commit()
        finally:
            session.close()

    def write_stale_fallback(self, owner_id, text):
        path = f"latest_report_{owner_id}.txt"
        self._temp_files.append(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def pdf_files_in_cwd(self):
        return {n for n in os.listdir(".") if n.lower().endswith(".pdf")}

    def capture_pdf_text(self):
        """Records every string handed to reportlab's Paragraph while
        delegating to the real one, so assertions can inspect rendered
        content without depending on PDF stream compression."""
        recorded = []
        real = export_module.Paragraph

        def spy(text, *args, **kwargs):
            recorded.append(text)
            return real(text, *args, **kwargs)

        return patch.object(export_module, "Paragraph", side_effect=spy), recorded


class TestOwnerCanExportLatestReport(ExportReportTestCase):

    def test_returns_a_pdf_for_the_authenticated_owner(self):
        self.seed_report(OWNER_A, "transformers", "# Findings\n\nBody text.", [])
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertTrue(
            resp.content.startswith(b"%PDF-"),
            "response body must be a real PDF, not JSON or an empty buffer",
        )
        self.assertGreater(len(resp.content), 500)

    def test_exports_the_most_recent_report_not_an_older_one(self):
        self.seed_report(OWNER_A, "old topic", "OLD REPORT BODY", [], minutes_old=0)
        self.seed_report(OWNER_A, "new topic", "NEW REPORT BODY", [], minutes_old=10)
        self.authenticate_as(OWNER_A)

        patcher, recorded = self.capture_pdf_text()
        with patcher:
            resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 200)
        rendered = "\n".join(recorded)
        self.assertIn("NEW REPORT BODY", rendered)
        self.assertNotIn("OLD REPORT BODY", rendered)
        self.assertIn("new topic", rendered)

    def test_persisted_citations_are_rendered_in_the_references_section(self):
        self.seed_report(
            OWNER_A,
            "attention",
            "# Findings\n\nSome body.",
            [
                {"paper": "Attention Is All You Need",
                 "source": "attention.pdf", "page": 3},
                {"paper": "Attention Is All You Need",
                 "source": "attention.pdf", "page": 9},
                {"paper": "BERT", "source": "bert.pdf", "page": 1},
            ],
        )
        self.authenticate_as(OWNER_A)

        patcher, recorded = self.capture_pdf_text()
        with patcher:
            resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 200)
        rendered = "\n".join(recorded)

        self.assertIn("References", rendered)
        self.assertIn("Attention Is All You Need", rendered)
        self.assertIn("BERT", rendered)
        self.assertIn("Source: attention.pdf", rendered)
        # Two pages of the same paper must group onto one entry.
        self.assertIn("Pages Referenced: 3, 9", rendered)

    def test_null_citations_column_does_not_break_the_export(self):
        self.seed_report(OWNER_A, "q", "body", None)
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"%PDF-"))


class TestCrossUserIsolation(ExportReportTestCase):

    def test_other_owner_cannot_export_someone_elses_report(self):
        self.seed_report(OWNER_A, "owner A topic", "OWNER A SECRET BODY", [])
        self.authenticate_as(OWNER_B)

        resp = self.client.get("/export-report")

        # 404 now, not a 200 error body. The isolation assertion below is
        # the point of this test and is unchanged: another owner's report
        # is simply not in the result set.
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("No research report found", body["message"])
        self.assertNotIn(b"OWNER A SECRET BODY", resp.content)

    def test_each_owner_exports_only_their_own_latest(self):
        self.seed_report(OWNER_A, "a topic", "OWNER A BODY", [], minutes_old=0)
        self.seed_report(OWNER_B, "b topic", "OWNER B BODY", [], minutes_old=10)

        self.authenticate_as(OWNER_A)
        patcher, recorded = self.capture_pdf_text()
        with patcher:
            self.client.get("/export-report")
        rendered_a = "\n".join(recorded)

        self.authenticate_as(OWNER_B)
        patcher, recorded = self.capture_pdf_text()
        with patcher:
            self.client.get("/export-report")
        rendered_b = "\n".join(recorded)

        self.assertIn("OWNER A BODY", rendered_a)
        self.assertNotIn("OWNER B BODY", rendered_a)

        self.assertIn("OWNER B BODY", rendered_b)
        self.assertNotIn("OWNER A BODY", rendered_b)

    def test_client_supplied_owner_id_cannot_redirect_the_lookup(self):
        self.seed_report(OWNER_A, "a topic", "OWNER A BODY", [])
        self.authenticate_as(OWNER_B)

        resp = self.client.get(
            "/export-report",
            params={"owner_id": OWNER_A, "user_id": OWNER_A},
        )

        self.assertEqual(resp.json()["status"], "error")

    def test_unauthenticated_request_is_rejected(self):
        self.seed_report(OWNER_A, "a topic", "OWNER A BODY", [])
        resp = self.client.get("/export-report")
        self.assertEqual(resp.status_code, 401)


class TestMissingReportHandling(ExportReportTestCase):

    def test_no_report_returns_a_clean_error(self):
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        # Previously 200, which left response.ok true and let the browser
        # save a JSON body named research-report.pdf.
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            resp.json(),
            {"status": "error",
             "message": "No research report found. Generate a report first."},
        )

    def test_stale_txt_fallback_is_ignored(self):
        """The .txt fallback is gone. A leftover file from before this
        step must not resurrect a report that Postgres does not have."""
        self.write_stale_fallback(OWNER_A, "STALE FALLBACK CONTENT")
        self.authenticate_as(OWNER_A)

        resp = self.client.get("/export-report")

        self.assertEqual(resp.json()["status"], "error")
        self.assertNotIn(b"STALE FALLBACK CONTENT", resp.content)


class TestNoPersistentPdfOnDisk(ExportReportTestCase):

    def test_export_leaves_no_pdf_file_behind(self):
        self.seed_report(OWNER_A, "q", "# Findings\n\nBody.", [{"paper": "P", "source": "p.pdf", "page": 1}])
        self.authenticate_as(OWNER_A)

        before = self.pdf_files_in_cwd()
        # Stale PDFs from long before this step sit in the working
        # directory. Asserting they are absent would test repo tidiness,
        # not behaviour — so assert instead that the export neither adds
        # a PDF nor writes over the legacy filenames.
        legacy_mtimes = {
            name: os.path.getmtime(name)
            for name in ("ResearchMind_Report.pdf", "research_report.pdf")
            if os.path.exists(name)
        }

        resp = self.client.get("/export-report")
        after = self.pdf_files_in_cwd()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"%PDF-"))
        self.assertEqual(
            after - before, set(),
            "export must not write any PDF to disk — it is built in memory",
        )
        self.assertFalse(
            os.path.exists(f"ResearchMind_Report_{OWNER_A}.pdf"),
            "the owner-scoped PDF the old code wrote must never appear",
        )
        for name, mtime in legacy_mtimes.items():
            self.assertEqual(
                os.path.getmtime(name), mtime,
                f"export must not write over the legacy {name}",
            )

    def test_repeated_exports_still_leave_nothing(self):
        self.seed_report(OWNER_A, "q", "body", [])
        self.authenticate_as(OWNER_A)

        before = self.pdf_files_in_cwd()
        for _ in range(3):
            self.assertEqual(self.client.get("/export-report").status_code, 200)

        self.assertEqual(self.pdf_files_in_cwd() - before, set())


class TestSourceHasNoFallbackPaths(unittest.TestCase):
    """Belt and braces: the fallback must be gone from the source, not
    merely unreachable at runtime."""

    def test_export_report_no_longer_references_the_txt_files(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "export_report.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn("latest_report_{", source)
        self.assertNotIn("latest_sources_{", source)
        self.assertNotIn("FileResponse", source)

    def test_research_no_longer_writes_the_txt_files(self):
        path = os.path.join(BACKEND_DIR, "app", "api", "research.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn('f"latest_report_{owner_id}.txt"', source)
        self.assertNotIn('f"latest_sources_{owner_id}.txt"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
