"""
External provider timeouts and failure classification.

Two responsibilities, kept in one place so the numbers and the wording
cannot drift apart across call sites:

1. Timeout configuration for the provider clients, applied where each
   client is constructed rather than endpoint by endpoint.

2. Classification of provider failures into three neutral categories —
   timeout, unavailable, rate-limited — so a user never sees a provider's
   raw exception text, name, URL, key or billing wording.

Every setting is a server-side environment variable with a code default,
read at CALL time. Nothing here is exposed to the frontend.

Retry semantics, stated precisely because they differ by provider:

  * Groq — the SDK's own retry loop, bounded to GROQ_MAX_RETRIES (default
    1, so at most 2 attempts). This is an intentional reduction from the
    SDK default of 2 retries (3 attempts), chosen to cap latency and
    repeat billing. The SDK retries timeouts, connection errors and HTTP
    408/409/429/5xx, and only before a response starts: a failure midway
    through a streamed answer is never retried.

  * Voyage — the SDK retry loop stays disabled (max_retries=0). The only
    retry remains app/rag/embedder.py's existing RateLimitError backoff,
    unchanged. A Voyage timeout is not a RateLimitError, so it is never
    retried.

  * Qdrant — unchanged, explicit 120s timeout, no retries.
"""
import os
from typing import Optional

import groq
import httpx
import voyageai.error as voyage_error
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def _float_env(name: str, default: float) -> float:
    """A positive number of seconds, falling back to the default for
    anything missing, unparseable or not positive."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _retries_env(name: str, default: int) -> int:
    """A non-negative retry count. Zero is valid and meaningful here — it
    disables retries — which is why this cannot reuse a positive-only
    parser."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def groq_timeout_seconds() -> float:
    """Read, write and pool timeout for Groq requests. For a streamed answer
    this bounds each individual read, not the whole stream."""
    return _float_env("GROQ_TIMEOUT_SECONDS", 30.0)


def groq_connect_timeout_seconds() -> float:
    return _float_env("GROQ_CONNECT_TIMEOUT_SECONDS", 5.0)


def groq_max_retries() -> int:
    return _retries_env("GROQ_MAX_RETRIES", 1)


def groq_stream_deadline_seconds() -> float:
    """End-to-end limit on a streamed answer, measured from just before the
    request is made. Checked as each chunk arrives, so a stream that keeps
    trickling in slowly still ends; a single stalled read is separately
    bounded by the read timeout."""
    return _float_env("GROQ_STREAM_DEADLINE_SECONDS", 90.0)


def voyage_timeout_seconds() -> float:
    """Passed to the Voyage SDK, which hands it to `requests` — so it bounds
    the connect and read phases separately, not their sum."""
    return _float_env("VOYAGE_TIMEOUT_SECONDS", 30.0)


def groq_client_options() -> dict:
    """Keyword arguments for every Groq client this application constructs."""
    return {
        "timeout": groq.Timeout(groq_timeout_seconds(), connect=groq_connect_timeout_seconds()),
        "max_retries": groq_max_retries(),
    }


def voyage_client_options() -> dict:
    """Keyword arguments for every Voyage client this application constructs.
    max_retries stays 0: the embedder's RateLimitError backoff is the only
    Voyage retry, and adding a second loop would multiply calls."""
    return {
        "timeout": voyage_timeout_seconds(),
        "max_retries": 0,
    }


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

PROVIDER_TIMEOUT = "provider_timeout"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_RATE_LIMITED = "provider_rate_limited"
GENERATION_INCOMPLETE = "generation_incomplete"

#: Authored here, never derived from an exception. The rate-limit wording
#: deliberately says "rate-limited" so the frontend's existing rate-limit
#: recognition still applies; the other two deliberately do not.
_MESSAGES = {
    PROVIDER_TIMEOUT: "The research service took too long to respond. Please try again.",
    PROVIDER_UNAVAILABLE: "The research service is temporarily unavailable. Please try again shortly.",
    PROVIDER_RATE_LIMITED: "The research service is rate-limited right now. Please wait a moment and try again.",
    GENERATION_INCOMPLETE: "The answer could not be completed. Please try again.",
}


class ProviderFailure(Exception):
    """A classified failure of an external provider.

    Carries only a code and an application-authored message. The original
    exception is kept as __cause__ for server-side debugging but never
    rendered.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        self.message = _MESSAGES[code]
        super().__init__(self.message)

    def to_payload(self) -> dict:
        """The existing 200 error-body shape, plus an additive `code`."""
        return {"status": "error", "code": self.code, "message": self.message}


class ProviderTimeout(ProviderFailure):
    def __init__(self) -> None:
        super().__init__(PROVIDER_TIMEOUT)


class IncompleteGeneration(ProviderFailure):
    """The model stopped because it ran out of completion budget.

    Raised when finish_reason == "length". It is a ProviderFailure so that
    the endpoints' existing classify_provider_error branches return the
    neutral payload without needing a second error path — and so that a
    truncated answer can never be mistaken for a finished one and
    persisted. The cut-off text is deliberately discarded rather than
    returned: half a report stored as a whole one is how a truncated
    report reached the database in the first place.
    """

    def __init__(self, finish_reason: Optional[str] = "length") -> None:
        super().__init__(GENERATION_INCOMPLETE)
        self.finish_reason = finish_reason


def classify_provider_error(exc: BaseException) -> Optional[ProviderFailure]:
    """Map a provider exception to a neutral ProviderFailure, or None if it
    is not a provider failure.

    Order matters: groq.APITimeoutError subclasses groq.APIConnectionError,
    so timeouts are recognised before connection errors.
    """
    if isinstance(exc, ProviderFailure):
        return exc

    # --- Timeouts ------------------------------------------------------
    if isinstance(exc, (groq.APITimeoutError, voyage_error.Timeout, httpx.TimeoutException)):
        # httpx.TimeoutException: a read timeout during a streamed answer
        # surfaces as a raw httpx.ReadTimeout, not as a Groq exception.
        return ProviderFailure(PROVIDER_TIMEOUT)

    if isinstance(exc, ResponseHandlingException):
        # Qdrant wraps its transport error; the wrapped source says which.
        source = getattr(exc, "source", None)
        if isinstance(source, httpx.TimeoutException):
            return ProviderFailure(PROVIDER_TIMEOUT)
        return ProviderFailure(PROVIDER_UNAVAILABLE)

    # --- Rate limits ---------------------------------------------------
    if isinstance(exc, (groq.RateLimitError, voyage_error.RateLimitError)):
        return ProviderFailure(PROVIDER_RATE_LIMITED)

    if isinstance(exc, UnexpectedResponse):
        status = getattr(exc, "status_code", None) or 0
        if status == 429:
            return ProviderFailure(PROVIDER_RATE_LIMITED)
        if status >= 500:
            return ProviderFailure(PROVIDER_UNAVAILABLE)
        return None

    # --- Unavailable ---------------------------------------------------
    if isinstance(
        exc,
        (
            groq.APIConnectionError,
            groq.InternalServerError,
            voyage_error.APIConnectionError,
            voyage_error.ServiceUnavailableError,
            voyage_error.ServerError,
            voyage_error.TryAgain,
            httpx.TransportError,
        ),
    ):
        return ProviderFailure(PROVIDER_UNAVAILABLE)

    if isinstance(exc, groq.APIStatusError) and (getattr(exc, "status_code", 0) or 0) >= 500:
        return ProviderFailure(PROVIDER_UNAVAILABLE)

    return None
