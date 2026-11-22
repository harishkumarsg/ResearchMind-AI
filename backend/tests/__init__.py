"""Test package bootstrap.

Usage enforcement (E1 mechanisms A and B) is switched OFF by default for
the suite, and every E1 test switches it back on explicitly.

Why: charging a daily quota writes to `usage_counters` through
session_scope(). Most existing suites drive endpoints with the auth
dependency overridden and persistence functions patched, and never attach
a database — so with enforcement on they would try to open the real
connection from .env on every request. Tests must not touch the live
project, and their subject is retrieval, ownership or persistence rather
than limits.

Set here rather than in each suite so it is impossible to forget, and
with setdefault so an explicit environment value still wins:

    QUOTA_ENFORCEMENT=on python -m unittest ...

The trade-off is real and worth naming: a future endpoint test will NOT
exercise quota enforcement unless it opts in. The E1 suites
(test_e1_*.py) opt in for exactly that reason, and they are where
enforcement behaviour is asserted.

The suite is also made hermetic here, unless RUN_LIVE_INFRA_TESTS=1:

  - QDRANT_URL and QDRANT_API_KEY are blanked. app.rag.vector_store
    builds its client on import, and qdrant-client immediately starts a
    background version check against that URL; with a developer's
    backend/.env loaded, importing the app contacted the real cluster.
    load_dotenv() never overrides a variable that is already set, so an
    empty value here wins over .env and the client targets loopback.
  - tests/network_guard.py refuses every non-loopback DNS lookup and
    connection for the rest of the process.

ORDERING: `python -m unittest discover -s tests` (the CI command) imports
each test module as a TOP-LEVEL module, so this file runs only when
something imports `tests`. Every test module therefore starts with
`import tests`, before any `app`, `dotenv` or
`tests.<helper>` import — that is what makes a module safe however it is
selected (whole suite, `-p <file>`, `tests.<module>`, or run as a script).
test_00_hermetic_environment.TestEveryModuleBootstrapsFirst enforces the
rule statically, so a new file that forgets it fails the suite.
"""
import os

os.environ.setdefault("QUOTA_ENFORCEMENT", "off")

from tests.live_infra import live_infra_enabled  # noqa: E402

if not live_infra_enabled():
    os.environ["QDRANT_URL"] = ""
    os.environ["QDRANT_API_KEY"] = ""

    from tests import network_guard  # noqa: E402

    network_guard.install()
