"""
/upload rejects missing, malformed and expired credentials at the door.

The existing auth suite (test_phase1_5_auth.py) proves the verification
logic and the dependency in isolation; every /upload test so far
overrides get_current_identity. Nothing drove the real endpoint with a
bad credential, so nothing proved the property that matters for an
upload: a rejected caller leaves NO trace — no paper row, no Storage
call, no quota unit, no burst-window entry.

Everything here is real except the JWKS document (served from a locally
generated RSA key, as in TestMalformedBearerTokens) and the Storage
functions (spies). Quota enforcement is switched ON, against a real
in-memory counter table, so "no charge" is an observation rather than a
consequence of enforcement being off.

A positive control with a valid token runs through the identical
harness, so the rejections cannot pass vacuously.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import tests  # noqa: E402,F401 — hermetic bootstrap; must precede every app import

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import auth as auth_module  # noqa: E402
from app.core import quota  # noqa: E402
from app.core import usage_guard  # noqa: E402
from app.core.limits import UPLOAD, enforcement_enabled  # noqa: E402
from app.core.rate_limit import get_limiter  # noqa: E402
from app.db.models import Base, Paper, UsageCounter  # noqa: E402

TEST_SUPABASE_URL = "https://upload-auth-test.supabase.co"
TEST_ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
TEST_KID = "upload-auth-kid"
OWNER = "abababab-1111-4111-8111-abababababab"
PDF_BYTES = b"%PDF-1.4 upload auth test"


class UploadAuthTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key(), as_dict=True)
        cls.jwks = {"keys": [dict(jwk, kid=TEST_KID, use="sig", alg="RS256")]}

    def setUp(self):
        # Real in-memory database for paper rows AND usage counters.
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        self._start(patch("app.db.session.get_session_factory", return_value=self.SessionLocal))
        self.addCleanup(self.engine.dispose)

        # Enforcement ON — tests/__init__.py turns it off suite-wide.
        self._start(patch.dict(os.environ, {"QUOTA_ENFORCEMENT": "on", "SUPABASE_URL": TEST_SUPABASE_URL}))
        assert enforcement_enabled(), "quota enforcement must be on, or 'no charge' is vacuous"
        get_limiter().reset()
        self.addCleanup(get_limiter().reset)

        # The REAL PyJWKClient; only its HTTP fetch is replaced.
        jwks_client = jwt.PyJWKClient(f"{TEST_ISSUER}/.well-known/jwks.json")
        self._start(patch.object(auth_module, "_get_jwks_client", return_value=jwks_client))
        self._start(patch.object(jwt.PyJWKClient, "fetch_data", return_value=self.jwks))

        # Storage: spies only. create_client is watched too, so even a
        # client being BUILT for a rejected caller would be caught.
        self.upload_pdf = self._start(patch("app.api.upload.upload_pdf"))
        self.object_exists = self._start(patch("app.api.upload.storage_object_exists", return_value=False))
        self.create_client = self._start(patch("app.services.storage.create_client"))

        # Usage machinery: real behaviour, observed.
        self.enforce_burst = self._start(
            patch.object(usage_guard, "enforce_burst", wraps=usage_guard.enforce_burst)
        )
        self.ensure_within_quota = self._start(
            patch.object(quota, "ensure_within_quota", wraps=quota.ensure_within_quota)
        )
        self.charge = self._start(patch.object(quota, "charge", wraps=quota.charge))

        from app.main import app
        self.client = TestClient(app, raise_server_exceptions=False)

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _token(self, *, exp_delta=3600, key=None):
        now = int(time.time())
        payload = {
            "aud": "authenticated",
            "iss": TEST_ISSUER,
            "sub": OWNER,
            "role": "authenticated",
            "iat": now - 7200,
            "exp": now + exp_delta,
        }
        return jwt.encode(
            payload, key or self.private_key, algorithm="RS256", headers={"kid": TEST_KID}
        )

    def _upload(self, authorization=None):
        headers = {} if authorization is None else {"Authorization": authorization}
        return self.client.post(
            "/upload",
            files={"file": ("paper.pdf", PDF_BYTES, "application/pdf")},
            headers=headers,
        )

    def _counts(self):
        with self.SessionLocal() as db:
            return db.query(Paper).count(), db.query(UsageCounter).count()

    def assert_rejected_without_side_effects(self, resp, credential=None):
        self.assertEqual(resp.status_code, 401, resp.text)
        papers, counters = self._counts()
        self.assertEqual(papers, 0, "a rejected caller must not create a paper row")
        self.assertEqual(counters, 0, "a rejected caller must not create a usage counter")
        self.assertEqual(quota.peek(OWNER, UPLOAD), 0)
        self.upload_pdf.assert_not_called()
        self.object_exists.assert_not_called()
        self.create_client.assert_not_called()
        self.enforce_burst.assert_not_called()
        self.ensure_within_quota.assert_not_called()
        self.charge.assert_not_called()
        if credential:
            self.assertNotIn(credential, resp.text, "the credential must never be echoed")


class TestUploadRejectsBadCredentials(UploadAuthTestCase):

    def test_missing_authorization_header(self):
        self.assert_rejected_without_side_effects(self._upload())

    def test_non_bearer_scheme(self):
        self.assert_rejected_without_side_effects(self._upload("Basic dXNlcjpwYXNz"))

    def test_empty_bearer_token(self):
        self.assert_rejected_without_side_effects(self._upload("Bearer "))

    def test_malformed_bearer_token(self):
        for token in ("not-a-real-token", "abc.def.ghi", "..", "%%%.%%%.%%%"):
            with self.subTest(token=token):
                self.assert_rejected_without_side_effects(
                    self._upload(f"Bearer {token}"), credential=token if len(token) > 3 else None
                )

    def test_expired_token(self):
        token = self._token(exp_delta=-60)
        resp = self._upload(f"Bearer {token}")
        self.assert_rejected_without_side_effects(resp, credential=token)
        self.assertEqual(resp.json()["detail"], "Token expired")

    def test_token_signed_by_an_unknown_key(self):
        forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = self._token(key=forger)
        self.assert_rejected_without_side_effects(self._upload(f"Bearer {token}"), credential=token)


class TestUploadPositiveControl(UploadAuthTestCase):
    """Same harness, valid token: proves the rejections above are not an
    artefact of a harness that could never succeed."""

    def test_a_valid_token_uploads_and_is_charged_exactly_once(self):
        token = self._token()

        resp = self._upload(f"Bearer {token}")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "uploaded")
        papers, _ = self._counts()
        self.assertEqual(papers, 1)
        self.assertEqual(quota.peek(OWNER, UPLOAD), 1)
        self.upload_pdf.assert_called_once()
        _, kwargs = self.upload_pdf.call_args
        # Identity for Storage comes from the verified token, and the SAME
        # token is what Storage receives.
        self.assertEqual(kwargs["owner_id"], OWNER)
        self.assertEqual(kwargs["user_jwt"], token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
