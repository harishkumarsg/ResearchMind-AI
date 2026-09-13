"""
Phase 0 baseline tests — INFRASTRUCTURE, READ-ONLY ONLY.

Exactly two network calls exist in this file, both read-only:
  1. Groq `models.list()` — lists available models, generates nothing,
     costs nothing, creates/modifies/deletes nothing.
  2. Qdrant `get_collections()` with a short timeout — lists collections,
     creates/modifies/deletes nothing. Expected to be UNREACHABLE right
     now (the previous cluster is gone) and is designed to SKIP, not
     fail or fake a pass, when that happens.

No indexing endpoint is called. No collection is created. No .env file
is modified (only read). No PDF is re-indexed.
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from tests.live_infra import requires_live_infra

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")

# The literal model strings currently hardcoded in the app (read directly
# from source, not by importing the modules, so this file never
# constructs a Groq client via app code).
QA_AGENT_PATH = os.path.join(BACKEND_DIR, "app", "agents", "qa_agent.py")
ASK_STREAM_PATH = os.path.join(BACKEND_DIR, "app", "api", "ask_stream.py")


def _read_hardcoded_groq_model(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("GROQ_MODEL"):
                # GROQ_MODEL = "llama-3.1-8b-instant"
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


class TestGroqModelConfigDeterministic(unittest.TestCase):
    """Deterministic replacement for the source-level half of the old
    baseline check: it verifies what the repository actually declares,
    with no network call, so it runs on every suite execution."""

    def test_both_modules_declare_the_same_groq_model(self):
        qa_model = _read_hardcoded_groq_model(QA_AGENT_PATH)
        stream_model = _read_hardcoded_groq_model(ASK_STREAM_PATH)

        self.assertIsNotNone(qa_model, "qa_agent.py declares no GROQ_MODEL")
        self.assertIsNotNone(stream_model, "ask_stream.py declares no GROQ_MODEL")
        self.assertEqual(
            qa_model,
            stream_model,
            "qa_agent.py and ask_stream.py must specify the same GROQ_MODEL",
        )

    def test_declared_model_is_the_phase1_replacement(self):
        self.assertEqual(_read_hardcoded_groq_model(QA_AGENT_PATH), "openai/gpt-oss-120b")


@requires_live_infra
class TestGroqBaseline(unittest.TestCase):
    """Read-only check of Groq credential validity and live model
    availability. Gated: it contacts the real Groq API.

    When opted in, a failure here is a REAL failure — the previous
    version swallowed connection errors into skipTest, which made an
    outage indistinguishable from a pass."""

    @classmethod
    def setUpClass(cls):
        if not GROQ_API_KEY or GROQ_API_KEY == "your_groq_api_key_here":
            raise unittest.SkipTest("GROQ_API_KEY not configured")
        from groq import Groq

        cls.client = Groq(api_key=GROQ_API_KEY)
        cls.live_models = [m.id for m in cls.client.models.list().data]

    def test_groq_credential_is_valid(self):
        self.assertIsInstance(self.live_models, list)
        self.assertGreater(
            len(self.live_models), 0, "Groq returned an empty model list"
        )
        print(f"\n[baseline] Groq live models available to this key: {self.live_models}")

    def test_current_hardcoded_model_is_available(self):
        """
        KNOWN BASELINE DEFECT (expected to FAIL until Phase 1):
        qa_agent.py and ask_stream.py hardcode GROQ_MODEL = "llama-3.1-8b-instant",
        which prior investigation confirmed is Enterprise-only and does not
        appear in this account's live model list.
        """
        qa_model = _read_hardcoded_groq_model(QA_AGENT_PATH)
        stream_model = _read_hardcoded_groq_model(ASK_STREAM_PATH)

        print(f"\n[baseline] qa_agent.py GROQ_MODEL      = {qa_model!r}")
        print(f"[baseline] ask_stream.py GROQ_MODEL     = {stream_model!r}")
        print(f"[baseline] is it in the live list?       "
              f"{qa_model in self.live_models}")

        self.assertIn(
            qa_model,
            self.live_models,
            f"BASELINE DEFECT (expected today): {qa_model!r} is not available "
            f"to this Groq account. This is the known issue Phase 1 fixes by "
            f"switching to 'openai/gpt-oss-120b'.",
        )
        self.assertEqual(
            qa_model,
            stream_model,
            "qa_agent.py and ask_stream.py currently specify different "
            "GROQ_MODEL values — they should already match.",
        )


@requires_live_infra
class TestQdrantBaseline(unittest.TestCase):
    """Read-only reachability probe. Gated: it contacts the real Qdrant
    cluster.

    There is deliberately no mocked counterpart for this one. The only
    thing it asserts is that a real remote cluster answers — mocking that
    would assert nothing, and a tautological test is worse than an
    honestly-gated one. Collection configuration IS covered
    deterministically, with a mocked client, in
    test_phase2_vector_store.py."""

    def test_qdrant_cluster_reachable_read_only(self):
        if not QDRANT_URL:
            self.skipTest("QDRANT_URL not configured")

        from qdrant_client import QdrantClient

        # No try/except: when opted in, an unreachable cluster is a real
        # failure, not a silent skip.
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=5)
        collections = client.get_collections()

        names = [c.name for c in collections.collections]
        print(f"\n[baseline] Qdrant reachable. Existing collections: {names}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
