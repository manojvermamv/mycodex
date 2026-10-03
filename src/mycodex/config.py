"""mycodex's own settings (active profile, rotation policy, remote-control service, aliases).

Stored as JSON in MYCODEX_HOME/config.json, written atomically with mode 0600 under an
advisory lock. Credentials never live here: each account's auth.json stays in its own
CODEX_HOME under ~/.codex/profiles.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
from typing import Any, Iterator

from . import paths

REMOTE_MODES = ("rotating", "pinned")

DEFAULTS: dict[str, Any] = {
    "version": 2,
    "active": None,               # profile used when --profile is omitted
    "rotation": {
        "enabled": True,          # global switch: off => launches run one account, no proxy
        "order": [],              # preferred order when the proxy has to pick an account
        "disabled": [],           # profiles the proxy never switches to
    },
    "remote": {
        "mode": "rotating",       # rotating: model turns go through the rotation proxy; pinned: one account
        "profile": None,          # relay account the phone pairs with
        "cwd": None,              # working directory for the service (default: home)
        "failover": False,        # pinned mode: switch relay account when it is exhausted
        "failover_check_seconds": 300,
    },
    "aliases": {},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load(path: Path = paths.CONFIG_FILE) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Error: {path} is not valid JSON ({exc}); fix or delete it")
    if not isinstance(raw, dict):
        raise SystemExit(f"Error: {path} must contain a JSON object")
    data = _merge(DEFAULTS, raw)
    data["version"] = DEFAULTS["version"]
    return data


def save(data: dict[str, Any], path: Path = paths.CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def editing(path: Path = paths.CONFIG_FILE) -> Iterator[dict[str, Any]]:
    """Read-modify-write under an exclusive lock."""
    import fcntl  # POSIX only; imported lazily so unit tests can run elsewhere

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(path)
        yield data
        save(data, path)
