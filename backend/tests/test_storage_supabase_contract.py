"""
Storage identity contract, against the REAL pinned supabase-py client.

Background: two papers failed on 2026-09-12 with exactly
    {'statusCode': 403, 'error': Unauthorized,
     'message': new row violates row-level security policy}
because uploads reached Storage carrying the anon key instead of the
user's JWT (see test_storage_user_scoped_auth.py). The regression test
written then uses a FAKE supabase client that imitates the library's
behaviour. These tests use the real supabase-py and storage3 code paths
and intercept only httpx's final send(), so they observe the request
that would actually go over the wire — headers, method and URL — after
every layer of header merging the library does.

Two parts:

  1. Contract with the real client: our user-scoped Storage calls send
     the user's JWT, and the library really does freeze headers at the
     first `.storage` access (the behaviour storage.py is written
     around). If a supabase-py upgrade changes either, these fail.

  2. A policy model of backend/migrations/0002_storage_policies.sql,
     enforced by the fake server against the identity in each request:
     owner A may write to A's folder; A is refused B's folder; anon is
     refused. This is a MODEL of the policies, not the policies
     themselves — only a live, non-production Supabase project can
     prove what Postgres enforces. What it does prove is that our client
     code presents the identity and path those policies are written for.

No network: httpx.Client.send is replaced for the duration of each test,
and the hermetic guard refuses any connection that slips past it.
"""
import base64
import json
import os
import re
import sys
import unittest
from importlib import metadata
from unittest.mock import patch
from urllib.parse import unquote

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import httpx  # noqa: E402
from storage3.exceptions import StorageApiError  # noqa: E402
from supabase import create_client  # noqa: E402

from app.services import storage  # noqa: E402

PINNED_SUPABASE_VERSION = "2.31.0"

SUPABASE_URL = "https://storagecontract.supabase.co"
STORAGE_PREFIX = "/storage/v1/"

OWNER_A = "aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa"
OWNER_B = "bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb"
PAPER_ID = "cccccccc-0000-5000-8000-cccccccccccc"
PDF_BYTES = b"%PDF-1.4 storage contract"

#: What storage3 renders for an RLS rejection — byte-for-byte the text in
#: the two failed rows' status_detail (after the old "Storage upload
#: failed: " prefix).
SEPTEMBER_12_ERROR_TEXT = (
    "{'statusCode': 403, 'error': Unauthorized, "
    "'message': new row violates row-level security policy}"
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned_jwt(claims: dict) -> str:
    """JWT-shaped, never verified. The fake server only READS the claims,
    the way Storage derives auth.uid() and the role; signature checking is
    not what is under test here (test_phase1_5_auth.py covers it)."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.{_b64url(b'not-a-real-signature')}"


def _user_jwt(owner_id: str) -> str:
    return _unsigned_jwt({"sub": owner_id, "role": "authenticated", "aud": "authenticated"})


ANON_KEY = _unsigned_jwt({"role": "anon", "iss": "supabase"})
SERVICE_ROLE_KEY = _unsigned_jwt({"role": "service_role", "iss": "supabase"})


def _claims_from_authorization(value: str) -> dict:
    token = value.removeprefix("Bearer ").strip()
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class FakeStorageServer:
    """The Storage API at the HTTP boundary, with 0002's three policies.

    For every request it derives the caller the way Storage does — role
    and sub from the bearer token — and applies:

        to authenticated
        bucket_id = 'papers'
        and (storage.foldername(name))[1] = auth.uid()::text

    An INSERT that fails the check is answered with the API's own error
    body. SELECT and DELETE are filtered silently, as RLS filters rows:
    an object outside the caller's folder is simply not there for them.

    The service-role key bypasses RLS and sees every object in the bucket,
    which is what makes it usable to confirm that a delete took effect.
    """

    BUCKET = "papers"

    def __init__(self):
        self.objects = {}  # object name -> bytes
        self.requests = []

    @staticmethod
    def _uid_if_authenticated(request):
        claims = _claims_from_authorization(request.headers.get("authorization", ""))
        if claims.get("role") != "authenticated":
            return None
        return claims.get("sub")

    @staticmethod
    def _is_service_role(request) -> bool:
        claims = _claims_from_authorization(request.headers.get("authorization", ""))
        return claims.get("role") == "service_role"

    def _visible(self, bucket, name, uid) -> bool:
        folders = name.split("/")[:-1]  # storage.foldername(name)
        return uid is not None and bucket == self.BUCKET and bool(folders) and folders[0] == uid

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Multipart upload bodies are streamed; buffer before reading.
        request.read()
        self.requests.append(request)
        path = unquote(request.url.path)
        assert path.startswith(STORAGE_PREFIX), path
        route = path[len(STORAGE_PREFIX):]
        uid = self._uid_if_authenticated(request)
        service_role = self._is_service_role(request)

        if request.method == "POST" and route.startswith("object/list/"):
            bucket = route[len("object/list/"):]
            prefix = json.loads(request.content)["prefix"].rstrip("/") + "/"
            entries = [
                {"name": name[len(prefix):], "id": name}
                for name in self.objects
                if name.startswith(prefix)
                and "/" not in name[len(prefix):]
                and (
                    (service_role and bucket == self.BUCKET)
                    or self._visible(bucket, name, uid)
                )
            ]
            return httpx.Response(200, json=entries, request=request)

        if request.method == "POST" and route.startswith("object/"):
            bucket, _, name = route[len("object/"):].partition("/")
            if not self._visible(bucket, name, uid):
                return httpx.Response(
                    400,
                    json={
                        "statusCode": "403",
                        "error": "Unauthorized",
                        "message": "new row violates row-level security policy",
                    },
                    request=request,
                )
            self.objects[name] = request.content
            return httpx.Response(200, json={"Key": f"{bucket}/{name}", "Id": name}, request=request)

        if request.method == "DELETE" and route.startswith("object/"):
            bucket = route[len("object/"):]
            deleted = []
            for name in json.loads(request.content)["prefixes"]:
                if name in self.objects and self._visible(bucket, name, uid):
                    del self.objects[name]
                    deleted.append({"name": name})
            return httpx.Response(200, json=deleted, request=request)

        raise AssertionError(f"unexpected Storage request: {request.method} {path}")


class StorageContractTestCase(unittest.TestCase):

    def setUp(self):
        env = patch.dict(
            os.environ,
            {
                "SUPABASE_URL": SUPABASE_URL,
                "SUPABASE_ANON_KEY": ANON_KEY,
                "SUPABASE_SERVICE_ROLE_KEY": SERVICE_ROLE_KEY,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # The service-role client is cached per process; build a fresh one
        # against this test's settings, and drop it afterwards.
        cached = patch.object(storage, "_service_client", None)
        cached.start()
        self.addCleanup(cached.stop)

        self.server = FakeStorageServer()
        server = self.server

        def send(client, request, **kwargs):
            return server.handle(request)

        transport = patch.object(httpx.Client, "send", new=send)
        transport.start()
        self.addCleanup(transport.stop)

    def last_request(self):
        self.assertTrue(self.server.requests, "no Storage request reached the transport")
        return self.server.requests[-1]

    def path_for(self, owner_id):
        return storage.build_storage_path(owner_id, PAPER_ID)


class TestRealClientSendsTheUsersJwt(StorageContractTestCase):
    """Part 1: the real, pinned client."""

    def test_the_installed_supabase_is_the_pinned_version(self):
        """Everything below is a contract with ONE library version. If an
        upgrade lands, this fails first, as a prompt to re-verify the
        header-freeze contract rather than trust it."""
        with open(os.path.join(BACKEND_DIR, "requirements.txt"), encoding="utf-8") as f:
            pin = re.search(r"^supabase==(\S+)", f.read(), re.MULTILINE)
        self.assertIsNotNone(pin, "supabase is no longer pinned in requirements.txt")
        self.assertEqual(pin.group(1), PINNED_SUPABASE_VERSION)
        self.assertEqual(metadata.version("supabase"), PINNED_SUPABASE_VERSION)

    def test_an_upload_sends_the_users_jwt_not_the_anon_key(self):
        token = _user_jwt(OWNER_A)

        storage.upload_pdf(owner_id=OWNER_A, paper_id=PAPER_ID, user_jwt=token, file_bytes=PDF_BYTES)

        request = self.last_request()
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            unquote(request.url.path), f"{STORAGE_PREFIX}object/papers/{self.path_for(OWNER_A)}"
        )
        self.assertEqual(request.headers["authorization"], f"Bearer {token}")
        self.assertNotEqual(request.headers["authorization"], f"Bearer {ANON_KEY}")
        # The anon key still travels as the project API key, as it must.
        self.assertEqual(request.headers["apikey"], ANON_KEY)
        # A plain INSERT: never an overwrite of an existing object.
        self.assertEqual(request.headers.get("x-upsert"), "false")

    def test_the_existence_check_and_delete_send_the_users_jwt(self):
        token = _user_jwt(OWNER_A)

        storage.storage_object_exists(OWNER_A, PAPER_ID, token)
        storage.delete_pdf(OWNER_A, PAPER_ID, token)

        # existence check (user), remove (user), deletion confirmation
        # (service role — a read: the one check RLS cannot hide from).
        self.assertEqual(
            [(r.method, r.url.path.rsplit("/", 2)[-2]) for r in self.server.requests],
            [("POST", "list"), ("DELETE", "object"), ("POST", "list")],
        )
        check, remove, confirm = self.server.requests
        for request in (check, remove):
            with self.subTest(method=request.method):
                self.assertEqual(request.headers["authorization"], f"Bearer {token}")
        self.assertEqual(confirm.headers["authorization"], f"Bearer {SERVICE_ROLE_KEY}")
        # The service role never deletes; only the user's own token does.
        for request in self.server.requests:
            if request.method == "DELETE":
                self.assertNotEqual(request.headers["authorization"], f"Bearer {SERVICE_ROLE_KEY}")

    def test_the_library_freezes_headers_at_first_storage_access(self):
        """The behaviour storage._get_user_scoped_client is written around,
        observed on the real library: a JWT set AFTER `.storage` is first
        touched never reaches the wire. This is the pre-fix ordering, and
        it is rejected with the September 12 error text."""
        token = _user_jwt(OWNER_A)
        client = create_client(SUPABASE_URL, ANON_KEY)
        bucket = client.storage.from_("papers")  # first access freezes headers
        client.options.headers["Authorization"] = f"Bearer {token}"

        with self.assertRaises(StorageApiError) as ctx:
            bucket.upload(self.path_for(OWNER_A), PDF_BYTES, {"content-type": "application/pdf"})

        self.assertEqual(self.last_request().headers["authorization"], f"Bearer {ANON_KEY}")
        self.assertEqual(str(ctx.exception), SEPTEMBER_12_ERROR_TEXT)
        self.assertEqual(self.server.objects, {})


class TestStoragePolicyModel(StorageContractTestCase):
    """Part 2: 0002_storage_policies.sql, modelled by the fake server."""

    def test_owner_a_can_upload_into_owner_as_folder(self):
        storage.upload_pdf(
            owner_id=OWNER_A, paper_id=PAPER_ID, user_jwt=_user_jwt(OWNER_A), file_bytes=PDF_BYTES
        )

        self.assertIn(self.path_for(OWNER_A), self.server.objects)
        self.assertTrue(self.path_for(OWNER_A).startswith(f"{OWNER_A}/"))

    def test_owner_a_is_refused_a_write_into_owner_bs_folder(self):
        with self.assertRaises(StorageApiError) as ctx:
            storage.upload_pdf(
                owner_id=OWNER_B, paper_id=PAPER_ID, user_jwt=_user_jwt(OWNER_A), file_bytes=PDF_BYTES
            )

        self.assertEqual(str(ctx.exception), SEPTEMBER_12_ERROR_TEXT)
        self.assertEqual(self.server.objects, {})

    def test_owner_a_can_neither_see_nor_delete_owner_bs_object(self):
        self.server.objects[self.path_for(OWNER_B)] = PDF_BYTES

        self.assertFalse(storage.storage_object_exists(OWNER_B, PAPER_ID, _user_jwt(OWNER_A)))
        # RLS filters the delete silently (200, nothing removed); the
        # confirmation still sees the object, so the refusal is reported
        # rather than mistaken for a successful delete.
        with self.assertRaises(storage.StorageDeletionError):
            storage.delete_pdf(OWNER_B, PAPER_ID, _user_jwt(OWNER_A))

        self.assertIn(self.path_for(OWNER_B), self.server.objects, "A deleted B's object")
        # Control: the owner does see it, so the miss above is the policy.
        self.assertTrue(storage.storage_object_exists(OWNER_B, PAPER_ID, _user_jwt(OWNER_B)))

    def test_anonymous_upload_is_refused(self):
        anonymous = create_client(SUPABASE_URL, ANON_KEY)

        with self.assertRaises(StorageApiError) as ctx:
            anonymous.storage.from_("papers").upload(
                self.path_for(OWNER_A), PDF_BYTES, {"content-type": "application/pdf"}
            )

        self.assertEqual(self.last_request().headers["authorization"], f"Bearer {ANON_KEY}")
        self.assertEqual(str(ctx.exception), SEPTEMBER_12_ERROR_TEXT)
        self.assertEqual(self.server.objects, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
