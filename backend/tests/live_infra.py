"""
Opt-in gate for tests that contact real external infrastructure.

The default test suite must be deterministic: same code in, same result
out, with no dependency on network reachability, third-party uptime, or
API quota. Tests that genuinely need a live service are gated behind an
explicit environment flag so they are SKIPPED BY DECLARATION rather than
silently skipped by a caught connection error.

That distinction matters. The previous pattern was:

    try:
        live_models = client.models.list()
    except Exception as e:
        self.skipTest(f"call failed: {e}")

which turns any outage, expired key, or rate limit into a silent pass —
the suite reported 3 skips on one run and 4 on the next, minutes apart,
with no code change in between. A gated skip is constant and intentional.

Run the live checks explicitly with:

    RUN_LIVE_INFRA_TESTS=1 python -m unittest discover -s tests

When opted in, these tests are expected to FAIL on a real problem rather
than skip — that is the whole point of running them.
"""
import os
import unittest

LIVE_INFRA_ENV_VAR = "RUN_LIVE_INFRA_TESTS"

_SKIP_REASON = (
    "live infrastructure test — contacts a real external service. "
    f"Set {LIVE_INFRA_ENV_VAR}=1 to run. Skipped by declaration, not by "
    "a swallowed connection error."
)


def live_infra_enabled() -> bool:
    return os.environ.get(LIVE_INFRA_ENV_VAR, "") == "1"


#: Decorator for a test method or TestCase class that needs live infra.
requires_live_infra = unittest.skipUnless(live_infra_enabled(), _SKIP_REASON)
