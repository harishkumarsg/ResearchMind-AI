"""
Hermetic network guard for the offline test suite.

The default suite must never reach a real external service: not Qdrant,
Supabase, Voyage, Groq, nor anything else. Every test that needs one
already patches it, so an outbound connection during a test run is by
definition a leak — most often a module-level client built from a
developer's backend/.env at import time. app.rag.vector_store is the
concrete case: it constructs its QdrantClient on import, and
qdrant-client starts a background version check against that URL.

Installed by tests/__init__.py unless RUN_LIVE_INFRA_TESTS=1, so the
gated live tests (see tests/live_infra.py) keep working when opted in.

What is refused: DNS resolution of any non-loopback name, and any
connect() to a non-loopback address. Both raise HermeticNetworkError
BEFORE a packet leaves the machine — the resolver is never asked, so a
hostname is not even sent to DNS.

What stays open: loopback. asyncio's Windows event loop builds its
self-pipe from a loopback socketpair, and FastAPI's TestClient never
opens a socket at all, so the suite needs nothing else.

Every refusal is recorded in BLOCKED_ATTEMPTS so a run can report what
WOULD have left the machine, rather than relying on the leak failing
loudly — some callers (the Qdrant version check is one) swallow the
error and only print a warning.
"""
import ipaddress
import socket
from typing import List

#: Host (or host:port) of every refused attempt, in order.
BLOCKED_ATTEMPTS: List[str] = []

_installed = False


class HermeticNetworkError(OSError):
    """Raised instead of any non-loopback DNS lookup or connection.

    An OSError subclass, so client libraries treat it exactly like an
    unreachable network rather than crashing in an unexpected way.
    """


def _is_loopback(host) -> bool:
    # None / "" are passive lookups (binding a local socket), not egress.
    if host in (None, "", "localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        # Strip an IPv6 zone index ("::1%lo0") before parsing.
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _refuse(target: str) -> None:
    BLOCKED_ATTEMPTS.append(target)
    raise HermeticNetworkError(
        f"network access blocked by the hermetic test guard: {target}. "
        "Patch the client under test, or gate the test with "
        "tests.live_infra.requires_live_infra."
    )


def install() -> None:
    """Patch the socket module. Idempotent: a second call is a no-op."""
    global _installed
    if _installed:
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            _refuse(str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    def _check_address(sock, address) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else "?"
            _refuse(f"{host}:{port}")

    def guarded_connect(sock, address):
        _check_address(sock, address)
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        _check_address(sock, address)
        return real_connect_ex(sock, address)

    # socket.create_connection looks getaddrinfo up as a module global at
    # call time, and ssl.SSLSocket connects through socket.socket.connect,
    # so these three patches cover plain, TLS and higher-level clients.
    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    _installed = True


def is_installed() -> bool:
    return _installed
