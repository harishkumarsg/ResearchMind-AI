"""
Regression test for the Supabase Storage user-authentication bug found
during manual upload testing: _get_user_scoped_client() used to mutate
client.storage.session.headers AFTER already accessing .storage, which
has no effect — supabase-py's .storage property lazily constructs (and
caches) a storage3 SyncStorageClient that freezes a copy of whatever
headers it's given AT THAT MOMENT. Every real Storage request then goes
through storage3's own frozen snapshot (not client.storage.session.headers),
so the caller's JWT never actually reached Storage — every upload ran as
anon and was correctly rejected by RLS with 'new row violates row-level
security policy'.

This test uses a fake Supabase client whose .storage property reproduces
exactly that freeze-on-first-access behavior (without needing the real
supabase-py/storage3/network stack), so it fails against the old code and
passes against the fix: the JWT must land in client.options.headers
BEFORE .storage is ever accessed.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from unittest.mock import MagicMock

from app.services.storage import _get_user_scoped_client, storage_object_exists


class FakeStorageClient:
    """Mimics storage3.SyncStorageClient: freezes a COPY of whatever
    headers dict it's constructed with. Real requests use this frozen
    copy, not the mutable .session.headers exposed alongside it."""

    def __init__(self, headers):
        self.captured_headers = dict(headers)
        self.session = SimpleNamespace(headers=dict(headers))


class FakeSupabaseClient:
    """Mimics supabase-py's Client: .storage is a lazy, cached property
    that constructs its storage sub-client from self.options.headers AT
    THE MOMENT OF FIRST ACCESS — this is the exact mechanism the real bug
    hinged on."""

    def __init__(self):
        self.options = SimpleNamespace(headers={})
        self._storage = None

    @property
    def storage(self):
        if self._storage is None:
            self._storage = FakeStorageClient(self.options.headers)
        return self._storage


class TestUserScopedStorageClientAuth(unittest.TestCase):

    @patch("app.services.storage.create_client")
    def test_jwt_is_set_on_options_headers_before_storage_is_constructed(self, mock_create_client):
        fake_client = FakeSupabaseClient()
        mock_create_client.return_value = fake_client

        result = _get_user_scoped_client("user-jwt-abc123")

        # The frozen snapshot storage3 actually uses for requests must
        # already contain the user's JWT — this fails against the old
        # code, which accessed (and froze) .storage BEFORE ever setting
        # the JWT anywhere options-related.
        self.assertEqual(
            result.storage.captured_headers.get("Authorization"),
            "Bearer user-jwt-abc123",
        )
        self.assertEqual(
            result.options.headers.get("Authorization"),
            "Bearer user-jwt-abc123",
        )

    @patch("app.services.storage.create_client")
    def test_uses_anon_key_not_service_role_key(self, mock_create_client):
        fake_client = FakeSupabaseClient()
        mock_create_client.return_value = fake_client

        with patch.dict(
            os.environ,
            {"SUPABASE_ANON_KEY": "the-anon-key", "SUPABASE_SERVICE_ROLE_KEY": "the-service-role-key"},
        ):
            _get_user_scoped_client("user-jwt-abc123")

        args, _ = mock_create_client.call_args
        self.assertIn("the-anon-key", args)
        self.assertNotIn("the-service-role-key", args)

    @patch("app.services.storage.create_client")
    def test_different_users_get_independent_clients_not_a_shared_cached_one(self, mock_create_client):
        mock_create_client.side_effect = lambda *a, **k: FakeSupabaseClient()

        client_a = _get_user_scoped_client("jwt-for-user-a")
        client_b = _get_user_scoped_client("jwt-for-user-b")

        self.assertIsNot(client_a, client_b)
        self.assertEqual(client_a.options.headers["Authorization"], "Bearer jwt-for-user-a")
        self.assertEqual(client_b.options.headers["Authorization"], "Bearer jwt-for-user-b")


class TestStorageObjectExists(unittest.TestCase):
    """Direct unit coverage for storage_object_exists()'s own path
    construction and list-matching logic, independent of how any caller
    uses its return value."""

    def _mock_client_listing(self, entries):
        fake_client = MagicMock()
        fake_client.storage.from_.return_value.list.return_value = entries
        return fake_client

    @patch("app.services.storage._get_user_scoped_client")
    def test_returns_true_when_the_object_is_present(self, mock_get_client):
        mock_get_client.return_value = self._mock_client_listing(
            [{"name": "original.pdf", "id": "abc"}]
        )

        self.assertTrue(storage_object_exists("owner-1", "paper-1", "jwt"))

    @patch("app.services.storage._get_user_scoped_client")
    def test_returns_false_when_the_folder_is_empty(self, mock_get_client):
        mock_get_client.return_value = self._mock_client_listing([])

        self.assertFalse(storage_object_exists("owner-1", "paper-1", "jwt"))

    @patch("app.services.storage._get_user_scoped_client")
    def test_checks_the_exact_owner_scoped_folder_derived_from_its_arguments(
        self, mock_get_client
    ):
        fake_client = self._mock_client_listing([{"name": "original.pdf"}])
        mock_get_client.return_value = fake_client

        storage_object_exists("owner-1", "paper-1", "jwt")

        fake_client.storage.from_.return_value.list.assert_called_once_with("owner-1/paper-1")

    @patch("app.services.storage._get_user_scoped_client")
    def test_uses_the_callers_own_jwt_not_a_service_role_client(self, mock_get_client):
        mock_get_client.return_value = self._mock_client_listing([])

        storage_object_exists("owner-1", "paper-1", "the-users-jwt")

        mock_get_client.assert_called_once_with("the-users-jwt")

    @patch("app.services.storage._get_user_scoped_client")
    def test_propagates_a_genuine_check_failure_rather_than_returning_false(
        self, mock_get_client
    ):
        mock_get_client.side_effect = RuntimeError("network blip")

        with self.assertRaises(RuntimeError):
            storage_object_exists("owner-1", "paper-1", "jwt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
