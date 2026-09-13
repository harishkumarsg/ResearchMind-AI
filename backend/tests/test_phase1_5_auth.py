"""
Phase 1.5 auth tests — fully offline, no live Supabase project needed.

Uses a locally generated RSA keypair to sign synthetic JWTs shaped like
Supabase's, and exercises app.core.auth's verification logic directly
against them. This proves the verification logic itself is correct
independent of whether a real JWKS endpoint exists yet.
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.core import auth as auth_module

TEST_ISSUER = "https://test-project.supabase.co/auth/v1"
TEST_OWNER_ID = "11111111-1111-1111-1111-111111111111"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
