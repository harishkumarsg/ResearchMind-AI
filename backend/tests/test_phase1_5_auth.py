"""
Phase 1.5 auth tests — fully offline, no live Supabase project needed.

Uses a locally generated RSA keypair to sign synthetic JWTs shaped like
Supabase's, and exercises app.core.auth's verification logic directly
against them. This proves the verification logic itself is correct
independent of whether a real JWKS endpoint exists yet.
"""
import base64
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.core import auth as auth_module

TEST_SUPABASE_URL = "https://test-project.supabase.co"
TEST_ISSUER = "https://test-project.supabase.co/auth/v1"
TEST_OWNER_ID = "11111111-1111-1111-1111-111111111111"
TEST_KID = "test-kid"


def _make_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_token(private_key, *, sub=TEST_OWNER_ID, aud="authenticated",
                 iss=TEST_ISSUER, exp_delta=3600, include_exp=True,
                 include_sub=True, include_iat=True):
    now = int(time.time())
    payload = {"aud": aud, "iss": iss}
    if include_sub:
        payload["sub"] = sub
    if include_exp:
        payload["exp"] = now + exp_delta
    if include_iat:
        payload["iat"] = now
    return jwt.encode(payload, private_key, algorithm="RS256")


class TestTokenVerificationLogic(unittest.TestCase):
    """Exercises _verify_token_with_key directly — the pure verification
    step, with no network/JWKS-fetching involved at all."""

    @classmethod
    def setUpClass(cls):
        cls.private_key, cls.public_key = _make_keypair()

    def test_valid_token_returns_owner_id(self):
        token = _make_token(self.private_key)
        owner_id = auth_module._verify_token_with_key(
            token, self.public_key, TEST_ISSUER
        )
        self.assertEqual(owner_id, TEST_OWNER_ID)

    def test_expired_token_rejected(self):
        token = _make_token(self.private_key, exp_delta=-3600)
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_audience_rejected(self):
        token = _make_token(self.private_key, aud="some-other-app")
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_issuer_rejected(self):
        token = _make_token(self.private_key, iss="https://not-our-project.supabase.co/auth/v1")
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_tampered_signature_rejected(self):
        """Signed with a DIFFERENT private key, verified against the
        original public key — simulates a forged token."""
        attacker_private_key, _ = _make_keypair()
        forged_token = _make_token(attacker_private_key, sub="attacker-controlled-id")
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(forged_token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_sub_claim_rejected(self):
        token = _make_token(self.private_key, include_sub=False)
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_exp_claim_rejected(self):
        token = _make_token(self.private_key, include_exp=False)
        with self.assertRaises(Exception) as ctx:
            auth_module._verify_token_with_key(token, self.public_key, TEST_ISSUER)
        self.assertEqual(ctx.exception.status_code, 401)


class TestGetCurrentIdentityDependency(unittest.TestCase):
    """End-to-end through get_current_identity()/get_current_owner_id(),
    with the JWKS fetch itself mocked out (no network call), proving the
    FastAPI dependency wiring is correct, not just the inner verify step."""

    @classmethod
    def setUpClass(cls):
        cls.private_key, cls.public_key = _make_keypair()

    def _fake_request(self, auth_header: str = None):
        req = MagicMock()
        req.headers = {"Authorization": auth_header} if auth_header else {}
        return req

    def test_missing_authorization_header_rejected(self):
        req = self._fake_request()
        with self.assertRaises(Exception) as ctx:
            auth_module.get_current_owner_id(req)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_malformed_header_rejected(self):
        req = self._fake_request("NotBearer sometoken")
        with self.assertRaises(Exception) as ctx:
            auth_module.get_current_owner_id(req)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_valid_bearer_token_resolves_identity(self):
        token = _make_token(self.private_key)
        req = self._fake_request(f"Bearer {token}")

        fake_signing_key = MagicMock()
        fake_signing_key.key = self.public_key

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test-project.supabase.co"}):
            with patch.object(auth_module, "_get_jwks_client") as mock_jwks:
                mock_jwks.return_value.get_signing_key_from_jwt.return_value = fake_signing_key
                identity = auth_module.get_current_identity(req)

        self.assertEqual(identity.owner_id, TEST_OWNER_ID)
        self.assertEqual(identity.token, token)

    def test_auth_not_configured_fails_closed(self):
        """If SUPABASE_URL is unset, verification must fail (500), never
        silently allow the request through."""
        token = _make_token(self.private_key)
        req = self._fake_request(f"Bearer {token}")

        env_without_supabase = {
            k: v for k, v in os.environ.items() if k != "SUPABASE_URL"
        }
        with patch.dict(os.environ, env_without_supabase, clear=True):
            with self.assertRaises(Exception) as ctx:
                auth_module.get_current_owner_id(req)
            self.assertEqual(ctx.exception.status_code, 500)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


_VALID_HEADER = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": TEST_KID}).encode())


class TestMalformedBearerTokens(unittest.TestCase):
    """Regression: a malformed bearer token escaped get_signing_key_from_jwt()
    as an uncaught jwt.DecodeError and became an HTTP 500.

    These tests run the REAL PyJWKClient, replacing only its JWKS fetch, so
    the token is genuinely parsed. The dependency tests above mock the whole
    client and therefore never reached the code path that failed."""

    MALFORMED_TOKENS = {
        "no segments": "not-a-real-token",
        "two segments": "abc.def",
        "garbage segments": "abc.def.ghi",
        "empty segments": "..",
        "non-base64 segments": "%%%.%%%.%%%",
        "header is a JSON array": _b64url(b"[1,2]") + "." + _b64url(b"{}") + ".sig",
        "payload is not JSON": _VALID_HEADER + "." + _b64url(b"not json") + ".sig",
        "payload is not base64": _VALID_HEADER + ".!!!!.sig",
    }

    @classmethod
    def setUpClass(cls):
        cls.private_key, cls.public_key = _make_keypair()
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(cls.public_key, as_dict=True)
        cls.jwks = {"keys": [dict(jwk, kid=TEST_KID, use="sig", alg="RS256")]}

        app = FastAPI()

        @app.get("/protected")
        def protected(owner_id: str = Depends(auth_module.get_current_owner_id)):
            return {"owner_id": owner_id}

        cls.client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        env = patch.dict(os.environ, {"SUPABASE_URL": TEST_SUPABASE_URL})
        env.start()
        self.addCleanup(env.stop)

        real_client = jwt.PyJWKClient(f"{TEST_SUPABASE_URL}/auth/v1/.well-known/jwks.json")
        jwks_client = patch.object(auth_module, "_get_jwks_client", return_value=real_client)
        jwks_client.start()
        self.addCleanup(jwks_client.stop)

        # No network: the JWKS document is served from here.
        fetch = patch.object(jwt.PyJWKClient, "fetch_data", return_value=self.jwks)
        self.fetch_mock = fetch.start()
        self.addCleanup(fetch.stop)

    def _signed_token(self, key=None, kid=TEST_KID):
        now = int(time.time())
        payload = {"aud": "authenticated", "iss": TEST_ISSUER, "sub": TEST_OWNER_ID,
                   "iat": now, "exp": now + 3600}
        return jwt.encode(payload, key or self.private_key, algorithm="RS256", headers={"kid": kid})

    def _get(self, authorization=None):
        headers = {"Authorization": authorization} if authorization is not None else {}
        return self.client.get("/protected", headers=headers)

    def test_malformed_token_returns_401_never_500(self):
        self.fetch_mock.side_effect = AssertionError("JWKS must not be fetched for a malformed token")
        for name, token in self.MALFORMED_TOKENS.items():
            with self.subTest(name):
                response = self._get(f"Bearer {token}")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Unable to verify token"})
                self.assertNotIn(token, response.text)
        self.fetch_mock.assert_not_called()

    def test_structurally_invalid_jwt_raises_http_401_not_decode_error(self):
        self.fetch_mock.side_effect = AssertionError("JWKS must not be fetched for a malformed token")
        request = MagicMock()
        request.headers = {"Authorization": "Bearer abc.def.ghi"}
        with self.assertRaises(HTTPException) as ctx:
            auth_module.get_current_owner_id(request)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_valid_token_still_resolves_owner_through_real_jwks_client(self):
        response = self._get(f"Bearer {self._signed_token()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"owner_id": TEST_OWNER_ID})
        self.fetch_mock.assert_called()

    def test_forged_signature_is_still_rejected(self):
        attacker_key, _ = _make_keypair()
        response = self._get(f"Bearer {self._signed_token(key=attacker_key)}")
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["detail"].startswith("Invalid token"))

    def test_unknown_signing_key_is_401_without_echoing_the_header(self):
        kid = "attacker-chosen-kid"
        response = self._get(f"Bearer {self._signed_token(kid=kid)}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Unable to verify token"})
        self.assertNotIn(kid, response.text)

    def test_jwks_without_usable_keys_fails_closed_with_401(self):
        self.fetch_mock.return_value = {"keys": []}
        response = self._get(f"Bearer {self._signed_token()}")
        self.assertEqual(response.status_code, 401)

    def test_missing_or_non_bearer_authorization_still_401(self):
        self.fetch_mock.side_effect = AssertionError("JWKS must not be fetched without a bearer token")
        for name, header in (("missing", None), ("other scheme", "Token abc"),
                             ("lowercase bearer", "bearer abc.def.ghi")):
            with self.subTest(name):
                response = self._get(header)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Missing or malformed Authorization header"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
