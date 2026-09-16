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
"""
import os

os.environ.setdefault("QUOTA_ENFORCEMENT", "off")
