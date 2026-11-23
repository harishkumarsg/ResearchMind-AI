"""
Supabase Storage wrapper for original PDFs.

Two client types, used deliberately for different callers:
  - a user-JWT-scoped client for user-facing upload/delete, so Storage's
    own RLS policies (backend/migrations/0002_storage_policies.sql) are a
    REAL second enforcement layer, not decoration.
  - the service-role client for server-side READS only: the indexing job,
    which has no live user request to scope a JWT to, and confirming that
    a user's delete really removed the object. It never uploads or
    deletes on a user's behalf.

All network/env access is lazy — importing this module must not fail
just because Supabase env vars aren't configured yet.
"""
import logging
import os
from typing import Optional

import httpx
from supabase import Client, create_client

BUCKET_NAME = "papers"

#: How far to follow __cause__/__context__ looking for the HTTP response.
#: storage3 chains at most two levels (StorageApiError <- KeyError <-
#: HTTPStatusError); the bound only guards against a pathological chain.
_MAX_CAUSE_DEPTH = 5


def _status_code(value) -> Optional[int]:
    """A plain HTTP status code, or None. Anything else is never passed
    through: StorageApiError.status is copied from the response body, so it
    is server-controlled text, not guaranteed to be a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def storage_failure_fields(exc: BaseException) -> dict:
    """What may be logged about a failed Storage call: the exception's type
    name and its status codes. Nothing else.

    Never the message, the URL or a key. storage3 builds StorageApiError's
    message from the response body, and an httpx error's text carries the
    request URL, whose object path holds the owner's id.

    - http_status: the real response status, from the httpx.HTTPStatusError
      storage3 chains behind its own exception.
    - storage_status: the statusCode in Storage's error body. Supabase
      Storage often sets it differently from the HTTP status (an RLS
      rejection arrives as HTTP 400 with statusCode "403"), so both are kept.

    Either is None when absent, e.g. a network error or missing config.
    """
    http_status = None
    seen = set()
    current: Optional[BaseException] = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            http_status = _status_code(current.response.status_code)
            break
        current = current.__cause__ or current.__context__

    return {
        "type": type(exc).__name__,
        "http_status": http_status,
        "storage_status": _status_code(getattr(exc, "status", None)),
    }


def log_storage_failure(logger: logging.Logger, operation: str, exc: BaseException) -> None:
    """One warning line per failed Storage call. `operation` must be a
    fixed literal chosen by the caller, never request data."""
    fields = storage_failure_fields(exc)
    logger.warning(
        "storage %s failed: type=%s http_status=%s storage_status=%s",
        operation,
        fields["type"],
        fields["http_status"],
        fields["storage_status"],
    )

_service_client: Optional[Client] = None


def _get_supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "")
    if not url:
        raise RuntimeError("SUPABASE_URL is not configured")
    return url


class StorageDeletionError(RuntimeError):
    """A PDF could not be confirmed deleted. Raised, never swallowed, so
    the caller keeps its record of the paper and can retry the delete."""


def _get_service_client() -> Client:
    """Full-access client, bypasses RLS. Server-side reads only — never
    used to upload or delete on a user's behalf.

    SUPABASE_SERVICE_ROLE_KEY holds the project's SECRET key (sb_secret_…).
    The variable keeps its historical name; legacy JWT keys are disabled
    on this project."""
    global _service_client
    if _service_client is None:
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not key:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")
        _service_client = create_client(_get_supabase_url(), key)
    return _service_client


def _get_user_scoped_client(user_jwt: str) -> Client:
    """A fresh client per call, authenticated as the calling user so
    Storage RLS policies actually apply. Not cached/reused across users.

    The JWT MUST be set on client.options.headers BEFORE .storage is ever
    accessed: supabase-py's .storage property lazily constructs and caches
    a storage3 SyncStorageClient, which freezes a copy of the headers
    passed in at that moment. Every later storage3 request re-injects that
    frozen snapshot (storage3's _request() does headers.update(self._headers)),
    so mutating client.storage.session.headers afterward has no effect —
    the calling user's JWT would never actually reach Storage, and every
    upload would silently run as anon and get rejected by RLS.

    SUPABASE_ANON_KEY holds the project's PUBLISHABLE key (sb_publishable_…);
    it only identifies the project, and the user's JWT carries the identity.
    """
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not anon_key:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured")
    client = create_client(_get_supabase_url(), anon_key)
    client.options.headers["Authorization"] = f"Bearer {user_jwt}"
    return client


def build_storage_path(owner_id: str, paper_id: str) -> str:
    return f"{owner_id}/{paper_id}/original.pdf"


def storage_object_exists(owner_id: str, paper_id: str, user_jwt: str) -> bool:
    """Read-only existence check for original.pdf at the path derived from
    these two arguments — callers must always pass a server-verified
    owner_id (from the JWT) and a paper_id already scoped to that owner
    (e.g. from a Paper row query filtered on owner_id), never client-
    supplied values, so this can never be pointed at another owner's
    object.

    Uses the caller's own JWT (same as upload_pdf/delete_pdf) — RLS's
    SELECT policy applies exactly as it does for a real download, so this
    stays within the same per-user authorization model rather than
    reaching for the service-role client.

    Raises on a genuine failure to check (network/API error) rather than
    returning False — callers must not treat 'couldn't verify' the same
    as 'confirmed absent'.
    """
    path = build_storage_path(owner_id, paper_id)
    folder, filename = path.rsplit("/", 1)
    client = _get_user_scoped_client(user_jwt)
    entries = client.storage.from_(BUCKET_NAME).list(folder)
    return any(entry.get("name") == filename for entry in entries)


def upload_pdf(owner_id: str, paper_id: str, user_jwt: str, file_bytes: bytes) -> str:
    """Uploads using the caller's own JWT — RLS enforces the folder match."""
    path = build_storage_path(owner_id, paper_id)
    client = _get_user_scoped_client(user_jwt)
    client.storage.from_(BUCKET_NAME).upload(
        path, file_bytes, file_options={"content-type": "application/pdf"}
    )
    return path


def fetch_pdf(owner_id: str, paper_id: str) -> bytes:
    """Used only by the indexing job — service-role key, no live user JWT
    to scope to at that point."""
    path = build_storage_path(owner_id, paper_id)
    client = _get_service_client()
    return client.storage.from_(BUCKET_NAME).download(path)


def _object_present_to_service_role(owner_id: str, paper_id: str) -> bool:
    """Whether original.pdf exists, as the service role sees it.

    Read-only, scoped to the one folder derived from the same two
    server-verified values the delete used. A user-scoped check cannot do
    this job: the RLS predicate that can block a user's DELETE also hides
    the object from that user's own listing, so "not listed" would read as
    "deleted". storage3's exists() cannot either — it reports every error
    response (403, 500, …) as False.

    Raises on any failure to check: "could not check" is never "absent".
    """
    folder, filename = build_storage_path(owner_id, paper_id).rsplit("/", 1)
    entries = _get_service_client().storage.from_(BUCKET_NAME).list(folder)
    return any(entry.get("name") == filename for entry in entries)


def delete_pdf(owner_id: str, paper_id: str, user_jwt: str) -> None:
    """Deletes using the caller's own JWT — RLS enforces the folder match —
    and returns only once the object is confirmed gone.

    An object that is already absent counts as deleted, so retrying after a
    partially completed delete is safe. Everything else raises: an error
    response (authorization, server) or network failure from remove(), a
    failed confirmation check, and an object that is still there afterwards.
    The last case is not hypothetical: Storage deletes as the caller, under
    RLS, and answers a delete that RLS filtered out exactly as it answers a
    delete of nothing — success, with an empty list — so remove()'s answer
    alone can never establish that the object is gone.
    """
    path = build_storage_path(owner_id, paper_id)
    client = _get_user_scoped_client(user_jwt)
    client.storage.from_(BUCKET_NAME).remove([path])

    if _object_present_to_service_role(owner_id, paper_id):
        raise StorageDeletionError("The PDF is still present after the delete request.")
