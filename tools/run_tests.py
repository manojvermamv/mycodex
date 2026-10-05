"""Run project regressions in temporary homes with external actions blocked."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    os.chdir(project)
    with tempfile.TemporaryDirectory(prefix="mycodex-tests-") as scratch:
        root = Path(scratch)
        for key, value in {
            "MYCODEX_HOME": root / "tool",
            "MYCODEX_SHARED_CODEX_HOME": root / "codex",
            "MYCODEX_CODEX_BIN": root / "missing",
            "PRODEX_HOME": root / "prodex",
            "XDG_CONFIG_HOME": root / "config",
            "XDG_STATE_HOME": root / "state",
        }.items():
            os.environ[key] = str(value)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("external HTTP blocked")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("child processes blocked")), \
             mock.patch("os.kill", side_effect=AssertionError("signals blocked")):
            result = unittest.TextTestRunner(verbosity=1).run(
                unittest.defaultTestLoader.discover("tests"))
        return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
