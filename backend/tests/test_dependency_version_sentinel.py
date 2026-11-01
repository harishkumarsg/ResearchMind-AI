"""
Installed versions must match what requirements.txt declares.

  - Every exact pin (`==`) must be installed at exactly that version. In CI
    this holds right after `pip install -r requirements.txt`; the check is
    for every other environment (a stale virtualenv, an image built earlier)
    and keeps holding once requirements.txt becomes a lock.
  - Every unpinned requirement must be installed. Its version cannot be
    compared with a declaration that names none, so instead the unpinned set
    itself must equal KNOWN_UNPINNED: a newly unpinned requirement fails.

requirements.txt is read through tests/declared_requirements.py, which
rejects syntax it does not understand rather than skipping it. Nothing is
installed, resolved or written. Extras such as psycopg[binary] are not
followed: the declared distributions themselves are checked.
"""
import os
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from tests.declared_requirements import read_requirements, versions_equal  # noqa: E402

#: Requirements declared without a version. Empty: requirements.txt is a
#: hashed lock in which every line is an exact pin.
KNOWN_UNPINNED = set()


def _installed(distribution):
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


class TestRequirementsParser(unittest.TestCase):

    def _parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requirements.txt"
            path.write_text(text, encoding="utf-8")
            return read_requirements(path)

    def test_the_forms_requirements_txt_uses_are_understood(self):
        parsed = self._parse(
            "# comment\nfastapi==0.136.3\n\nPyMuPDF\npsycopg[binary]==3.3.5  # trailing comment\n"
        )
        self.assertEqual(
            [(r.name, r.extras, r.pinned, r.line, r.key) for r in parsed],
            [("fastapi", (), "0.136.3", 2, "fastapi"),
             ("PyMuPDF", (), None, 4, "pymupdf"),
             ("psycopg", ("binary",), "3.3.5", 5, "psycopg")],
        )

    def test_hashed_lock_lines_with_continuations_are_understood(self):
        parsed = self._parse(
            "httpx==0.28.1 \\\n    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\nidna==3.20\n"
        )
        self.assertEqual([(r.name, r.pinned, r.line) for r in parsed], [("httpx", "0.28.1", 1), ("idna", "3.20", 4)])

    def test_unsupported_forms_are_rejected_not_skipped(self):
        for text in ("fastapi>=0.1\n", "numpy==2.4.6; python_version<'3.12'\n", "-r other.txt\n",
                     "--index-url https://example.invalid/simple\n", "pkg @ https://example.invalid/p.whl\n"):
            with self.subTest(line=text.strip()):
                with self.assertRaises(ValueError):
                    self._parse(text)

    def test_a_requirement_declared_twice_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("SQLAlchemy==2.0.50\nsqlalchemy==2.0.50\n")

    def test_exact_pin_comparison(self):
        self.assertTrue(versions_equal("2.0", "2.0.0"))
        self.assertTrue(versions_equal("1.1.3", "1.1.3"))
        self.assertFalse(versions_equal("2.0", "2.0.1"))
        self.assertFalse(versions_equal("1.1.3", "1.1.2"))
        self.assertTrue(versions_equal("2.0b1", "2.0B1"))
        self.assertFalse(versions_equal("2.0b1", "2.0"))


class TestInstalledVersionsMatchDeclarations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.requirements = read_requirements()

    def test_requirements_txt_is_readable_and_not_empty(self):
        self.assertTrue(self.requirements)

    def test_every_declared_requirement_is_installed(self):
        missing = [f"{r.name} (line {r.line})" for r in self.requirements if _installed(r.name) is None]
        self.assertEqual(missing, [], "declared but not installed: " + ", ".join(missing))

    def test_every_pinned_requirement_is_installed_at_exactly_its_pin(self):
        mismatched = []
        for requirement in self.requirements:
            installed = _installed(requirement.name)
            if requirement.pinned and installed is not None and not versions_equal(installed, requirement.pinned):
                mismatched.append(
                    f"{requirement.name}: declared =={requirement.pinned} "
                    f"(line {requirement.line}), installed {installed}"
                )
        self.assertEqual(mismatched, [], "; ".join(mismatched))

    def test_the_unpinned_requirements_are_exactly_the_known_set(self):
        unpinned = {r.key for r in self.requirements if r.pinned is None}
        self.assertEqual(
            unpinned, KNOWN_UNPINNED,
            "unpinned requirements changed. Pin new requirements exactly; remove an entry "
            "from KNOWN_UNPINNED once it is pinned.",
        )


if __name__ == "__main__":
    unittest.main()
