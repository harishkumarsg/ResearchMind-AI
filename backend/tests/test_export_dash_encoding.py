"""
/export-report — dash characters reportlab cannot render.

The export builds its PDF with the base-14 Helvetica, which reportlab
renders with WinAnsiEncoding. WinAnsi has no glyph for U+2010, U+2011,
U+2012, U+2015 or U+2212, so reportlab emitted .notdef and a shipped PDF
showed a solid black square mid-word: "multi<square>modal" wherever the
model had written a NON-BREAKING HYPHEN.

Pinned here:

  * those five become ASCII "-";
  * U+2013 EN DASH, U+2014 EM DASH and U+2022 BULLET are left alone,
    because WinAnsi does have them and they already render correctly --
    a fix that rewrote them would be changing correct output;
  * normalization reaches all three report-derived paths: body, research
    query and the references page;
  * the stored row is NOT rewritten. The database keeps what the model
    produced; only the rendered PDF is normalized.

Fully offline: in-memory SQLite, no network. The real reportlab build
runs, which is the point.
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
from app.api.export_report import normalize_dashes
from app.core.auth import get_current_owner_id
from app.db.models import Base, Report
from app.db.session import get_db_session

OWNER_A = "dddddddd-dddd-dddd-dddd-dddddddddddd"
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

#: The five WinAnsi cannot represent.
UNSUPPORTED = {
    "‐": "HYPHEN",
    "‑": "NON-BREAKING HYPHEN",
    "‒": "FIGURE DASH",
    "―": "HORIZONTAL BAR",
    "−": "MINUS SIGN",
}

#: The three WinAnsi does have, which must survive untouched.
EN_DASH = "–"
EM_DASH = "—"
BULLET = "•"


# ======================================================================
# 1. The helper on its own
# ======================================================================
class TestNormalizeDashes(unittest.TestCase):
    def test_every_unsupported_character_becomes_an_ascii_hyphen(self):
        for char, name in UNSUPPORTED.items():
            with self.subTest(name=name):
                self.assertEqual(
                    normalize_dashes(f"multi{char}modal"),
                    "multi-modal",
                    f"{name} (U+{ord(char):04X}) was not normalized",
                )

    def test_en_dash_is_preserved(self):
        # WinAnsi 0x96 -- renders correctly, so rewriting it would change
        # output that is already right.
        self.assertEqual(normalize_dashes(f"pages 10{EN_DASH}20"), f"pages 10{EN_DASH}20")

    def test_em_dash_is_preserved(self):
        # WinAnsi 0x97.
        self.assertEqual(normalize_dashes(f"a{EM_DASH}b"), f"a{EM_DASH}b")

    def test_bullet_is_preserved(self):
        # WinAnsi 0x95. The export prefixes every list item with this.
        self.assertEqual(normalize_dashes(f"{BULLET} item"), f"{BULLET} item")

    def test_ascii_hyphen_is_untouched(self):
        self.assertEqual(normalize_dashes("multi-modal"), "multi-modal")

    def test_surrounding_text_is_not_otherwise_altered(self):
        original = "ResNet‑50 and GPT‑generated text, 95% F1."
        self.assertEqual(
            normalize_dashes(original),
            "ResNet-50 and GPT-generated text, 95% F1.",
        )

    def test_several_in_one_string_are_all_replaced(self):
        joined = "".join(UNSUPPORTED)
        self.assertEqual(normalize_dashes(joined), "-" * len(UNSUPPORTED))

    def test_empty_and_falsy_input_is_returned_unchanged(self):
        self.assertEqual(normalize_dashes(""), "")
        self.assertIsNone(normalize_dashes(None))

    def test_text_without_dashes_is_returned_identical(self):
        self.assertEqual(normalize_dashes("plain text"), "plain text")


# ======================================================================
# 2. End to end through /export-report
# ======================================================================
class ExportTestCase(unittest.TestCase):
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
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER_A
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(self.engine.dispose)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def seed(self, query, markdown, citations):
        session = self.SessionLocal()
        try:
            session.add(
                Report(
                    owner_id=uuid.UUID(OWNER_A),
                    query=query,
                    report_markdown=markdown,
                    citations=citations,
                    created_at=EPOCH,
                )
            )
            session.commit()
        finally:
            session.close()

    def export_capturing_paragraphs(self):
        """Every string handed to reportlab's Paragraph, with the real
        Paragraph still doing the work."""
        recorded = []
        real = export_module.Paragraph

        def spy(text, *args, **kwargs):
            recorded.append(text)
            return real(text, *args, **kwargs)

        with patch.object(export_module, "Paragraph", side_effect=spy):
            resp = self.client.get("/export-report")

        return resp, recorded


class TestRenderedPdfHasNoUnsupportedDashes(ExportTestCase):
    def test_the_report_body_is_normalized(self):
        self.seed(
            "vision models",
            "### Findings\n\nA multi‑modal, zero‑shot ResNet‑50 study.",
            [],
        )

        resp, recorded = self.export_capturing_paragraphs()
        rendered = "\n".join(recorded)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("multi-modal", rendered)
        self.assertIn("zero-shot", rendered)
        self.assertIn("ResNet-50", rendered)

    def test_the_research_query_is_normalized(self):
        self.seed("large‑scale question‑answering", "### Findings\n\nBody.", [])

        _, recorded = self.export_capturing_paragraphs()
        rendered = "\n".join(recorded)

        self.assertIn("large-scale question-answering", rendered)

    def test_citation_paper_names_are_normalized(self):
        self.seed(
            "vision",
            "### Findings\n\nBody.",
            [{"paper": "Vision‑Language.pdf", "source": "Vision‑Language.pdf", "page": 3}],
        )

        _, recorded = self.export_capturing_paragraphs()
        rendered = "\n".join(recorded)

        self.assertIn("Vision-Language.pdf", rendered)

    def test_no_unsupported_character_survives_anywhere(self):
        body = "### Two‒stage\n\nA multi‐modal x−y study.\n\n- GPT‑generated item"
        self.seed(
            "zero‑shot",
            body,
            [{"paper": "Vision―Language.pdf", "source": "s‑1.pdf", "page": 1}],
        )

        resp, recorded = self.export_capturing_paragraphs()
        rendered = "\n".join(recorded)

        self.assertEqual(resp.status_code, 200)
        for char, name in UNSUPPORTED.items():
            with self.subTest(name=name):
                self.assertNotIn(
                    char,
                    rendered,
                    f"{name} (U+{ord(char):04X}) reached reportlab and renders as a black square",
                )

    def test_every_rendered_string_is_winansi_encodable(self):
        """The direct statement of the defect: anything reportlab cannot
        encode in WinAnsi is what produced the black squares."""
        self.seed(
            "multi‑modal",
            "### Findings\n\nResNet‑50 and two‒stage x−y.\n\n- GPT‑generated",
            [{"paper": "Vision‑Language.pdf", "source": "v.pdf", "page": 2}],
        )

        _, recorded = self.export_capturing_paragraphs()

        for text in recorded:
            with self.subTest(text=text[:40]):
                text.encode("cp1252")


class TestSupportedCharactersSurvive(ExportTestCase):
    def test_bullets_en_dashes_and_em_dashes_reach_the_pdf(self):
        self.seed(
            "ranges",
            f"### Findings\n\nPages 10{EN_DASH}20 {EM_DASH} see below.\n\n- a bullet item",
            [],
        )

        _, recorded = self.export_capturing_paragraphs()
        rendered = "\n".join(recorded)

        self.assertIn(EN_DASH, rendered)
        self.assertIn(EM_DASH, rendered)
        self.assertIn(BULLET, rendered)


class TestStoredReportIsNotRewritten(ExportTestCase):
    def test_export_does_not_modify_the_database_row(self):
        original = "### Findings\n\nA multi‑modal study."
        self.seed("vision", original, [])

        resp = self.client.get("/export-report")
        self.assertEqual(resp.status_code, 200)

        session = self.SessionLocal()
        try:
            stored = session.query(Report).one()
            # Normalization is a render-time concern. The durable record
            # must still hold exactly what the model produced.
            self.assertEqual(stored.report_markdown, original)
            self.assertIn("‑", stored.report_markdown)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
