"""
JWT verification against Supabase's JWKS endpoint.

owner_id is derived HERE ONLY, from a server-verified token's `sub` claim.
No other code path may accept owner_id/user_id from a request body, query
parameter, or path segment for authorization purposes.

Everything that touches the network or environment is resolved lazily
(inside functions, not at import time) so importing this module never
fails just because SUPABASE_URL isn't configured yet in this environment.
"""
import os
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

_jwks_client: Optional[PyJWKClient] = None


def _get_supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise HTTPException(status_code=500, detail="Auth is not configured")
    return url


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{_get_supabase_url()}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    return _jwks_client


def _verify_token_with_key(token: str, signing_key, issuer: str) -> str:
    """Pure verification step, isolated from JWKS network fetching so it
    can be unit-tested with a synthetic key and no network access."""
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
            issuer=issuer,
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    owner_id = payload.get("sub")
    if not owner_id:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    return owner_id


def verify_jwt_and_get_owner_id(token: str) -> str:
    supabase_url = _get_supabase_url()
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as e:
        raise HTTPException(status_code=401, detail=f"Unable to verify token: {e}")

    issuer = f"{supabase_url}/auth/v1"
    return _verify_token_with_key(token, signing_key.key, issuer)


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or malformed Authorization header"
        )
    token = auth_header[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")
    return token


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Carries both the verified owner_id AND the raw token, for the
    handful of endpoints (upload/delete) that must call Supabase Storage
    using the caller's own JWT so Storage's RLS policies actually apply."""
    owner_id: str
    token: str


def get_current_identity(request: Request) -> AuthenticatedIdentity:
    """FastAPI dependency for endpoints that also need the raw JWT."""
    token = _extract_bearer_token(request)
    owner_id = verify_jwt_and_get_owner_id(token)
    return AuthenticatedIdentity(owner_id=owner_id, token=token)


def get_current_owner_id(request: Request) -> str:
    """FastAPI dependency. Use as: owner_id: str = Depends(get_current_owner_id)

    Every protected route uses this (or get_current_identity), unconditionally,
    in every environment. There is no flag or config value anywhere that
    disables this check.
    """
    return get_current_identity(request).owner_id
