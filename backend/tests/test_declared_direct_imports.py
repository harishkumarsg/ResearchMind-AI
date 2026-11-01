"""
Every third-party module the application imports must come from a
distribution declared in requirements.in, the direct dependencies the
hashed lock (requirements.txt) is compiled from. The lock lists every
transitive package too, so checking against it would pass anything. A
package present only because something else depends on it can change or
disappear whenever that dependency is upgraded.

Scope: every .py file under app/, the package uvicorn serves. Excluded, so
they cannot produce false positives:

  - the standard library (sys.stdlib_module_names);
  - the application's own package (`app`) and relative imports;
  - transitive-only dependencies: only modules the application itself
    imports are examined, so a package that is installed but never imported
    directly is never reported.

Import names are mapped to distributions from installed metadata
(importlib.metadata.packages_distributions), so `fitz` resolves to PyMuPDF
and `jwt` to PyJWT without a hand-kept table. A module that maps to no
installed distribution fails the test rather than being skipped.

Not detected: imports made dynamically (importlib.import_module, __import__).
"""
import ast
import os
import sys
import unittest
from importlib import metadata
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

from tests.declared_requirements import REQUIREMENTS_IN_PATH, normalize, read_requirements  # noqa: E402

APP_DIR = Path(BACKEND_DIR) / "app"
LOCAL_PACKAGES = {"app"}

#: Direct imports knowingly left undeclared. Empty: httpx and pydantic, the
#: two found in the Phase 4 review, are declared in requirements.in. The test
#: requires this set to match exactly, so any new undeclared import fails.
KNOWN_UNDECLARED = {}


def imported_top_level_names(source, filename="<source>"):
    """Top-level names of every absolute import in the source, wherever it
    appears (module level, inside functions, inside try blocks)."""
    names = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def is_third_party(name):
    return name not in sys.stdlib_module_names and name not in LOCAL_PACKAGES


def application_imports():
    """{top-level third-party module: [relative/path.py, ...]} for app/."""
    found = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(Path(BACKEND_DIR)).as_posix()
        for name in imported_top_level_names(path.read_text(encoding="utf-8"), relative):
            if is_third_party(name):
                found.setdefault(name, []).append(relative)
    return found


class TestImportClassifier(unittest.TestCase):
    """The scanner itself: what counts as a third-party direct import."""

    def test_standard_library_local_and_relative_imports_are_excluded(self):
        source = (
            "import os\nimport json, re\nfrom typing import Optional\n"
            "import app.core.auth\nfrom app.db import models\n"
            "from . import sibling\nfrom .. import parent\nfrom .x import y\n"
        )
        names = {n for n in imported_top_level_names(source) if is_third_party(n)}
        self.assertEqual(names, set())

    def test_dotted_and_from_imports_reduce_to_the_top_level_name(self):
        source = "import qdrant_client.models\nfrom langchain_text_splitters import X\n"
        self.assertEqual(imported_top_level_names(source), {"qdrant_client", "langchain_text_splitters"})

    def test_imports_inside_functions_and_try_blocks_are_found(self):
        source = "def f():\n    import fitz\ntry:\n    import yaml\nexcept ImportError:\n    pass\n"
        self.assertEqual(imported_top_level_names(source), {"fitz", "yaml"})


class TestApplicationImportsAreDeclared(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.imports = application_imports()
        cls.declared = {r.key for r in read_requirements(REQUIREMENTS_IN_PATH)}
        cls.providers = metadata.packages_distributions()

    def test_the_scan_finds_the_application_imports(self):
        # A scan that found nothing would make the next test pass vacuously.
        for expected in ("fastapi", "fitz", "langchain_text_splitters", "sqlalchemy"):
            self.assertIn(expected, self.imports)

    def test_every_third_party_import_maps_to_an_installed_distribution(self):
        unmapped = {name: files for name, files in self.imports.items() if not self.providers.get(name)}
        self.assertEqual(
            unmapped, {},
            "imported by the application but provided by no installed distribution: "
            + "; ".join(f"{n} ({', '.join(f)})" for n, f in sorted(unmapped.items())),
        )

    def test_undeclared_direct_imports_are_exactly_the_known_gaps(self):
        undeclared = {}
        for name, files in self.imports.items():
            distributions = self.providers.get(name, [])
            if distributions and not any(normalize(d) in self.declared for d in distributions):
                undeclared[name] = (sorted(set(distributions)), files)

        report = "; ".join(
            f"{name} from {'/'.join(dists)} (imported by {', '.join(files)})"
            for name, (dists, files) in sorted(undeclared.items())
        )
        self.assertEqual(
            sorted(undeclared), sorted(KNOWN_UNDECLARED),
            "undeclared direct imports changed. Declare new ones in requirements.in; remove "
            "an entry from KNOWN_UNDECLARED once its package is declared. Now undeclared: "
            + (report or "none"),
        )


if __name__ == "__main__":
    unittest.main()
