"""Auto-imported by Python at interpreter startup whenever this directory is on `PYTHONPATH`.

`tests/conftest.py`'s session fixture puts it there - alongside the repo root, so `tests` itself
is importable - for the lifetime of the test session only, so it installs the same network guard
(`tests/_network_guard.py`) in every subprocess a test spawns (the `JobRunner` tests' fake CLI,
`tests/shell/test_cli.py`'s "never imports the web UI" probe). Without this, a test that spawns a
child to make a real request would slip past the guard the parent process installs on itself.

Only installs when the parent session says so (`LIKEARR_TEST_NETWORK_GUARD=1`): a plain `likearr`
invocation outside the test suite never has this directory on its path at all, but the flag is a
second line of defense against a stray `PYTHONPATH` surprising a production child.
"""

from __future__ import annotations

import os

if os.environ.get("LIKEARR_TEST_NETWORK_GUARD") == "1":
    from tests._network_guard import install

    install()
