"""
Phase 2 tests for the Qdrant collection configuration
(app/rag/vector_store.py).

IMPORTANT: create_collection/create_payload_index are mocked in every
test here. This suite NEVER creates, modifies, or deletes anything on
the real, live Qdrant cluster — only read (get_collections) is ever
exercised elsewhere against the real cluster, and never here either.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from qdrant_client.models import Distance

import app.rag.vector_store as vector_store


class TestVectorStoreConfig(unittest.TestCase):

    def test_collection_name_is_v2_not_the_old_collection(self):
        self.assertEqual(vector_store.COLLECTION_NAME, "researchmind_v2")
        self.assertNotEqual(vector_store.COLLECTION_NAME, "researchmind")

    def test_create_collection_uses_1024_cosine(self):
        empty_response = MagicMock()
        empty_response.collections = []

        with patch.object(vector_store.client, "get_collections", return_value=empty_response), \
             patch.object(vector_store.client, "create_collection") as mock_create, \
             patch.object(vector_store.client, "create_payload_index"):

            vector_store.create_collection()

        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["collection_name"], "researchmind_v2")
        vectors_config = kwargs["vectors_config"]
        self.assertEqual(vectors_config.size, 1024)
        self.assertEqual(vectors_config.distance, Distance.COSINE)

    def test_create_collection_configures_owner_id_and_paper_id_indexes(self):
        empty_response = MagicMock()
        empty_response.collections = []

        with patch.object(vector_store.client, "get_collections", return_value=empty_response), \
             patch.object(vector_store.client, "create_collection"), \
             patch.object(vector_store.client, "create_payload_index") as mock_index:

            vector_store.create_collection()

        indexed_fields = [c.kwargs["field_name"] for c in mock_index.call_args_list]
        self.assertIn("owner_id", indexed_fields, "owner_id must be an indexed payload field")
        self.assertIn("paper_id", indexed_fields)

    def test_create_collection_is_a_noop_if_already_exists(self):
        existing = MagicMock()
        existing.name = "researchmind_v2"
        response = MagicMock()
        response.collections = [existing]

        with patch.object(vector_store.client, "get_collections", return_value=response), \
             patch.object(vector_store.client, "create_collection") as mock_create:

            vector_store.create_collection()

        mock_create.assert_not_called()

    def test_no_code_path_touches_the_old_researchmind_collection(self):
        path = os.path.join(BACKEND_DIR, "app", "rag", "vector_store.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('"researchmind"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
