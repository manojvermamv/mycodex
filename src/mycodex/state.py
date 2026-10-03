"""Runtime facts shared by every mycodex process: which accounts are blocked (and until
when, and why) and the last quota snapshot of each account.

One JSON file in STATE_DIR, edited under a lock. It is a cache: deleting it only makes
the next request rediscover limits the hard way.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Iterator

from . import paths

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def load(max_age: float = 0.0) -> dict[str, Any]:
    if max_age and _cache["data"] is not None and time.monotonic() - _cache["at"] < max_age:
        return _cache["data"]
    try:
        data = json.loads(paths.ACCOUNTS_STATE.read_text())
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    data.setdefault("profiles", {})
    _cache.update(at=time.monotonic(), data=data)
    return data


@contextlib.contextmanager
def editing() -> Iterator[dict[str, Any]]:
    import fcntl

    directory = paths.state_dir()
    with open(directory / "accounts.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        yield data
        tmp = directory / f".accounts.json.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, paths.ACCOUNTS_STATE)
        _cache.update(at=time.monotonic(), data=data)


def _entry(data: dict[str, Any], name: str) -> dict[str, Any]:
    return data["profiles"].setdefault(name, {})


def block(name: str, until: float, reason: str) -> None:
    with editing() as data:
        entry = _entry(data, name)
        entry.update(blocked_until=int(until), blocked_reason=reason[:200], blocked_at=int(time.time()))


def unblock(name: str) -> None:
    with editing() as data:
        entry = _entry(data, name)
        for key in ("blocked_until", "blocked_reason", "blocked_at"):
            entry.pop(key, None)


def blocks(max_age: float = 2.0) -> dict[str, tuple[int, str]]:
    """name -> (until, reason) for accounts blocked right now."""
    now = time.time()
    result = {}
    for name, entry in load(max_age)["profiles"].items():
        until = entry.get("blocked_until") or 0
        if until > now:
            result[name] = (int(until), entry.get("blocked_reason") or "")
    return result


def save_quota(name: str, snapshot: dict[str, Any]) -> None:
    with editing() as data:
        _entry(data, name)["quota"] = snapshot


def quota_snapshot(name: str, max_age: float) -> dict[str, Any] | None:
    snapshot = load(2.0)["profiles"].get(name, {}).get("quota")
    if snapshot and time.time() - float(snapshot.get("fetched_at") or 0) <= max_age:
        return snapshot
    return None


def forget(name: str) -> None:
    with editing() as data:
        data["profiles"].pop(name, None)


def record_adoption(original: str, copy: str) -> None:
    with editing() as data:
        data.setdefault("adopted", {})[original] = copy


def adoptions() -> dict[str, str]:
    """original thread id -> its openai-tagged copy (from `mycodex threads adopt`)."""
    return dict(load(2.0).get("adopted") or {})
