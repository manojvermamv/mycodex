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

from . import paths, validation as check

REMOTE_MODES = ("rotating", "pinned")

DEFAULTS: dict[str, Any] = {
    "version": 2,
    "active": None,               # profile used when --profile is omitted
    "rotation": {
        "enabled": True,          # global switch: off => launches run one account, no proxy
        "order": [],              # preferred order when the proxy has to pick an account
        "disabled": [],           # profiles the proxy never switches to
        "min_quota_headroom": 5.0, # percent remaining on 5h window; 0 disables proactive routing
        "auto_redeem": False,     # earned credits are preserved unless explicitly enabled
    },
    "remote": {
        "mode": "rotating",       # rotating: model turns go through the rotation proxy; pinned: one account
        "profile": None,          # relay account the phone pairs with
        "models_source": None,    # Plus profile whose model cache is copied to the relay profile
        "cwd": None,              # working directory for the service (default: home)
        "failover": True,         # auth invalidation in either mode; quota exhaustion only in pinned mode
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
    except (ValueError, RecursionError):
        raise SystemExit(f"Error: settings file {path} cannot be read. Correct its JSON format; your accounts were left unchanged.") from None
    if not isinstance(raw, dict):
        raise SystemExit(f"Error: {path} must contain a JSON object")
    try:
        data = _merge(DEFAULTS, raw)
    except RecursionError:
        raise SystemExit(f"Error: settings in {path} are nested too deeply. Simplify the file; no data was changed.") from None
    data["version"] = DEFAULTS["version"]
    try:
        check.text_value(data.get("active"), "active account", optional=True)
        rotation = check.object_value(data.get("rotation"), "rotation settings")
        check.boolean(rotation.get("enabled"), "rotation.enabled")
        check.boolean(rotation.get("auto_redeem"), "rotation.auto_redeem")
        check.number(rotation.get("min_quota_headroom"), "rotation.min_quota_headroom", maximum=100)
        for key in ("order", "disabled"):
            check.text_list(rotation.get(key), "rotation." + key)
        remote = check.object_value(data.get("remote"), "phone settings")
        if remote.get("mode") not in REMOTE_MODES:
            raise check.InvalidData("remote.mode must be rotating (share accounts) or pinned (one account)")
        for key in ("profile", "models_source", "cwd"):
            check.text_value(remote.get(key), "remote." + key, optional=True)
        check.boolean(remote.get("failover"), "remote.failover")
        check.number(remote.get("failover_check_seconds"), "remote.failover_check_seconds", minimum=1, integer=True)
        aliases = check.object_value(data.get("aliases"), "account shortcuts")
        for key, value in aliases.items():
            check.text_value(key, "shortcut name")
            check.text_value(value, "shortcut account")
    except check.InvalidData as exc:
        raise SystemExit(f"Error: {exc}. Correct {path}; your settings were left unchanged.") from None
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
