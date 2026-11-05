"""Offline acceptance tests."""

import atexit
import os
from pathlib import Path
import shutil
import tempfile


# Every runner imports this package first, and child processes inherit the marker, so no test can reach the real
# state directory or the real ledger in it.
if "BRAINSTORM_TESTS_HOME" not in os.environ:
    _home = Path(tempfile.mkdtemp(prefix="brainstorm_tests_home_")).resolve()
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
    os.environ.update(BRAINSTORM_TESTS_HOME=str(_home), HOME=str(_home), XDG_STATE_HOME=str(_home / "state"))
