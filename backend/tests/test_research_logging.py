"""
/research must never write the user's query to stdout, stderr or any log.

It used to print the query and debug banners on every request, which put
personal data in the deployment's logs. It now logs counts at DEBUG and,
on failure, only the exception's type name at WARNING.

Offline: Qdrant, the embedder, the reranker and the research agent are
mocked, and persistence runs on in-memory SQLite.
"""
import ast
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — test bootstrap; must precede every app import

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.auth import get_current_owner_id  # noqa: E402
from app.db.models import Base  # noqa: E402

OWNER = "abababab-abab-abab-abab-abababababab"
SECRET_QUERY = "my-private-question-6c1f9e"
RESEARCH_SOURCE = os.path.join(BACKEND_DIR, "app", "api", "research.py")


def _hit():
    hit = MagicMock()
    hit.payload = {
        "paper": "paper-a",
        "source": "paper-a.pdf",
        "page": 1,
        "chunk_id": 0,
        "text": "a passage",
        "authors": "A. Author",
        "abstract": "An abstract.",
        "keywords": "kw",
    }
    return hit


class ResearchLoggingTest(unittest.TestCase):

    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        factory = patch(
            "app.db.session.get_session_factory",
            return_value=sessionmaker(bind=engine, autoflush=False, autocommit=False),
        )
        factory.start()
        self.addCleanup(factory.stop)

        from app.main import app
        self.app = app
        self.app.dependency_overrides[get_current_owner_id] = lambda: OWNER
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(app, raise_server_exceptions=False)

        qdrant = patch("app.api.research.client")
        mock_client = qdrant.start()
        self.addCleanup(qdrant.stop)
        mock_client.query_points.return_value = MagicMock(points=[_hit()])
        for target, kwargs in (
            ("app.api.research.encode_query", {"return_value": [0.1] * 1024}),
            ("app.api.research.rerank_results", {"side_effect": lambda q, r: r}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def _call_capturing_everything(self):
        out, err = io.StringIO(), io.StringIO()
        # Root logger at DEBUG: captures every record from every logger.
        with self.assertLogs(level="DEBUG") as logs, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            response = self.client.get("/research", params={"query": SECRET_QUERY})
        return response, logs, out.getvalue() + err.getvalue()

    def assertQueryNotLogged(self, logs, console):
        self.assertNotIn(SECRET_QUERY, console)
        for record in logs.records:
            # The test client's own httpx logger records the request URL,
            # which carries the query string. That is the client side of
            # this test, not the application, so it is excluded here.
            if record.name.startswith("httpx"):
                continue
            self.assertNotIn(SECRET_QUERY, record.getMessage())

    @patch("app.api.research.research_agent", return_value="report body")
    def test_success_logs_counts_but_never_the_query(self, _agent):
        response, logs, console = self._call_capturing_everything()

        self.assertEqual(response.json()["status"], "success")
        self.assertQueryNotLogged(logs, console)
        own = [r for r in logs.records if r.name == "app.api.research"]
        self.assertTrue(own, "expected debug records from app.api.research")
        self.assertTrue(all(r.levelname == "DEBUG" for r in own))

    @patch("app.api.research.research_agent")
    def test_failure_logs_the_type_name_only(self, agent):
        agent.side_effect = RuntimeError(f"provider rejected prompt: {SECRET_QUERY}")

        response, logs, console = self._call_capturing_everything()

        self.assertEqual(response.json()["status"], "error")
        self.assertNotIn(SECRET_QUERY, response.text)
        self.assertQueryNotLogged(logs, console)
        warnings = [
            r for r in logs.records
            if r.name == "app.api.research" and r.levelname == "WARNING"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("RuntimeError", warnings[0].getMessage())


class ResearchHasNoPrintCallsTest(unittest.TestCase):

    def test_research_module_does_not_call_print(self):
        with open(RESEARCH_SOURCE, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        prints = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        self.assertEqual(prints, [], f"print() calls at lines {prints}")


if __name__ == "__main__":
    unittest.main()
