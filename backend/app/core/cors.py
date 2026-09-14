"""
CORS origin allowlist.

Starlette's CORSMiddleware compares the browser's Origin header to each
entry by exact string match; the only wildcard it understands is a bare
"*", which must never be combined with allow_credentials=True. Every entry
is therefore a full origin — scheme://host[:port], no path, no trailing
slash — and a pattern such as "https://*.vercel.app" would silently match
nothing.
"""
import os

EXTRA_ORIGINS_ENV_VAR = "CORS_EXTRA_ORIGINS"

DEFAULT_ALLOWED_ORIGINS = (
    # Local development
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    # Production frontend (Vercel)
    "https://research-mind-ai-indol.vercel.app",
)


def get_allowed_origins() -> list[str]:
    """Built-in origins plus any comma-separated extras from
    CORS_EXTRA_ORIGINS (for example a custom domain), in order and without
    duplicates. Trailing slashes are stripped because a browser's Origin
    header never carries one; blank entries and a bare "*" are ignored."""
    origins = list(DEFAULT_ALLOWED_ORIGINS)
    for entry in os.environ.get(EXTRA_ORIGINS_ENV_VAR, "").split(","):
        origin = entry.strip().rstrip("/")
        if origin and origin != "*" and origin not in origins:
            origins.append(origin)
    return origins
