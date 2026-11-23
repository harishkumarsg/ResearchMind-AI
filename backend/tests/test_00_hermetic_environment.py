"""
The offline suite cannot reach the network.

`python -m unittest discover -s tests` (the CI command, and the README's)
imports each test module as a TOP-LEVEL module, so tests/__init__.py —
which blanks QDRANT_URL and installs tests/network_guard.py — never runs
on its own. Measured before this was fixed: with `-p` selecting a single
existing file, app.rag.vector_store built its QdrantClient from
backend/.env and tried to reach the production cluster.

The guarantee therefore lives in every test module: each one starts with
`import tests`, before any `app`, `dotenv` or `tests.<helper>` import. TestEveryModuleBootstrapsFirst enforces that
statically, so a new file that forgets cannot pass. (This file also sorts
first in discovery, which is a convenience, not the safety mechanism.)

The runtime assertions are skipped when RUN_LIVE_INFRA_TESTS=1, because the
live tests need the real network and the bootstrap deliberately stands
down for them. The static rule is never skipped.
"""
import ast
import os
import pathlib
import sys
import unittest
from urllib.parse import urlparse

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import socket  # noqa: E402

from tests import network_guard  # noqa: E402
from tests.live_infra import live_infra_enabled  # noqa: E402

# TEST-NET-3 (RFC 5737): reserved for documentation, never routed.
UNROUTABLE_ADDRESS = ("203.0.113.10", 443)


@unittest.skipIf(live_infra_enabled(), "live infra opted in: the network guard stands down")
class TestHermeticEnvironment(unittest.TestCase):

    def setUp(self):
        # These tests provoke refusals on purpose; keep them out of the
        # record so BLOCKED_ATTEMPTS lists only genuine leaks.
        recorded = len(network_guard.BLOCKED_ATTEMPTS)
        self.addCleanup(lambda: network_guard.BLOCKED_ATTEMPTS.__delitem__(slice(recorded, None)))

    def test_the_network_guard_is_installed(self):
        self.assertTrue(network_guard.is_installed())

    def test_resolving_an_external_name_is_refused_before_any_dns_query(self):
        with self.assertRaises(network_guard.HermeticNetworkError):
            socket.getaddrinfo("example.com", 443)

    def test_a_high_level_connection_to_an_external_host_is_refused(self):
        with self.assertRaises(network_guard.HermeticNetworkError):
            socket.create_connection(("example.com", 443), timeout=1)

    def test_a_raw_connect_to_an_external_address_is_refused(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(network_guard.HermeticNetworkError):
            sock.connect(UNROUTABLE_ADDRESS)
        with self.assertRaises(network_guard.HermeticNetworkError):
            sock.connect_ex(UNROUTABLE_ADDRESS)

    def test_loopback_still_works(self):
        # asyncio's Windows event loop depends on exactly this.
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.sendall(b"ok")
        self.assertEqual(right.recv(2), b"ok")

    def test_qdrant_settings_are_blanked_before_any_app_import(self):
        self.assertEqual(os.environ.get("QDRANT_URL"), "")
        self.assertEqual(os.environ.get("QDRANT_API_KEY"), "")

    def test_the_import_time_qdrant_client_targets_loopback(self):
        """The regression this file exists for. If an app module is ever
        imported before the bootstrap again, this client will have been
        built from backend/.env and point at a real cluster."""
        from app.rag import vector_store

        host = urlparse(vector_store.client._client.rest_uri).hostname
        self.assertIn(host, ("localhost", "127.0.0.1", "::1"))


def _is_project_import(node) -> bool:
    """An import that can load app code or the developer's .env."""
    if isinstance(node, ast.Import):
        return any(
            alias.name == "app" or alias.name.startswith(("app.", "dotenv", "tests."))
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return module == "app" or module.startswith(("app.", "dotenv", "tests"))
    return False


def _is_bootstrap(node) -> bool:
    return isinstance(node, ast.Import) and any(alias.name == "tests" for alias in node.names)


def _executed_on_import(node):
    """`node` and everything inside it that runs at import time — i.e. not
    the bodies of functions, methods or lambdas."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield from _executed_on_import(child)


class TestEveryModuleBootstrapsFirst(unittest.TestCase):
    """Static, so it holds however a module is selected: whole-suite
    discovery, `-p <file>`, `tests.<module>`, or `python tests/<file>.py`."""

    def test_no_test_module_imports_project_code_before_the_bootstrap(self):
        tests_dir = pathlib.Path(__file__).resolve().parent
        offenders = []
        for path in sorted(tests_dir.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # No exemption for modules that import no project code today:
            # a deferred test that gains `from app.main import app` later
            # must already be guarded when it does.
            for statement in tree.body:
                if _is_bootstrap(statement):
                    break
                if any(_is_project_import(n) for n in _executed_on_import(statement)):
                    offenders.append(f"{path.name}:{statement.lineno} imports project code first")
                    break
            else:
                # Project imports only inside functions still run unguarded.
                offenders.append(f"{path.name}: no top-level `import tests`")
        self.assertEqual(
            offenders,
            [],
            "these test modules can import app code (and so build clients from "
            "backend/.env) before the hermetic bootstrap runs; add "
            "`import tests` before their first app/dotenv/tests.* import",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
