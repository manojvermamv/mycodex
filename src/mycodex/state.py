"""Runtime facts shared by every mycodex process: which accounts are blocked (and until
when, and why) and the last quota snapshot of each account.

One JSON file in STATE_DIR, edited under a lock. It also holds adoption mappings and
pending redemption keys. Damaged state is reported and preserved, never replaced with
empty data that would discard a pending operation's identity.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Iterator

from . import paths, validation as check


class StateError(OSError):
    pass

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def load(max_age: float = 0.0) -> dict[str, Any]:
    if max_age and _cache["data"] is not None and time.monotonic() - _cache["at"] < max_age:
        return _cache["data"]
    try:
        data = json.loads(paths.ACCOUNTS_STATE.read_text())
    except FileNotFoundError:
        data = {}
    except (ValueError, RecursionError):
        raise StateError(f"Account history file {paths.ACCOUNTS_STATE} is unreadable. Keep it for recovery; no data was replaced.") from None
    try:
        check.object_value(data, "account history")
        entries = check.object_value(data.get("profiles", {}), "saved accounts")
        for name, entry in entries.items():
            check.text_value(name, "saved account name")
            entry = check.object_value(entry, "saved account")
            for key in ("blocked_until", "blocked_at"):
                check.timestamp(entry.get(key), key)
            check.text_value(entry.get("blocked_reason"), "pause reason", optional=True)
            check.text_value(entry.get("reset_attempt"), "pending reset key", optional=True)
        adopted = check.object_value(data.get("adopted", {}), "conversation copy history")
        for original, copy in adopted.items():
            check.text_value(original, "original conversation")
            check.text_value(copy, "copied conversation")
    except check.InvalidData as exc:
        raise StateError(f"Account history file {paths.ACCOUNTS_STATE} needs repair: {exc}. No data was replaced.") from None
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


def quota_pauses(max_age: float = 2.0) -> dict[str, tuple[int, str]]:
    """name -> (until, reason) for quota pauses, including expired ones."""
    result = {}
    for name, entry in load(max_age)["profiles"].items():
        reason = entry.get("blocked_reason") or ""
        until = entry.get("blocked_until") or 0
        if reason.startswith("quota:"):
            result[name] = (int(until), reason)
    return result


def set_quota_pause(name: str, until: float, reason: str) -> None:
    """Update quota pause fields without replacing another kind of pause."""
    with editing() as data:
        entry = _entry(data, name)
        if entry.get("blocked_reason") and not str(entry["blocked_reason"]).startswith("quota:"):
            return
        entry.update(blocked_until=int(until), blocked_reason=reason[:200], blocked_at=int(time.time()))


def save_quota(name: str, snapshot: dict[str, Any]) -> None:
    with editing() as data:
        _entry(data, name)["quota"] = snapshot


def quota_snapshot(name: str, max_age: float) -> dict[str, Any] | None:
    snapshot = load(2.0)["profiles"].get(name, {}).get("quota")
    if isinstance(snapshot, dict):
        try:
            fetched = check.number(snapshot.get("fetched_at"), "saved usage time", maximum=253402214400)
        except check.InvalidData:
            return None
        if 0 <= time.time() - fetched <= max_age:
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


def reset_attempt(name: str) -> str:
    """Persist the key before consumption, so an ambiguous failure can be retried safely."""
    import uuid
    with editing() as data:
        entry = _entry(data, name)
        return entry.setdefault("reset_attempt", str(uuid.uuid4()))


def finish_reset(name: str) -> None:
    with editing() as data:
        _entry(data, name).pop("reset_attempt", None)


def clear_quota_pause(name: str) -> None:
    with editing() as data:
        entry = _entry(data, name)
        if str(entry.get("blocked_reason", "")).startswith("quota:"):
            for key in ("blocked_until", "blocked_reason", "blocked_at"):
                entry.pop(key, None)


def extend_pause(name: str, expected_until: int, until: int) -> None:
    """A late quota lookup must not recreate a pause that was reset or redeemed."""
    with editing() as data:
        entry = _entry(data, name)
        if entry.get("blocked_until") == expected_until and expected_until > time.time() and until > expected_until:
            entry["blocked_until"] = until
