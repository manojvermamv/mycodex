"""Accounts: one real CODEX_HOME per profile under ~/.codex/profiles/<name>.

A profile home keeps only what belongs to one account: auth.json, installation_id
(the host identity the phone pairs with), caches and daemon state. Everything
conversational is a symlink into ~/.codex, so every account reads and writes the same
threads, history, config, skills and SQLite state.

The shared-entry list follows prodex's (Apache-2.0, see NOTICE). mycodex also shares
thread names (session_index.jsonl) and per-thread writer locks (thread-writer-locks/):
once sessions are shared, those must be shared too, or two accounts can name and write
the same thread independently.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import auth, config, paths

SHARED_DIRS = (
    "sessions", "archived_sessions", "attachments", "image_attachments", "shell_snapshots",
    "memories", "memories_extensions", "rules", "skills", "agents", "plugins",
    "thread-writer-locks", ".tmp/plugins", ".tmp/marketplaces",
)
SHARED_FILES = (
    "history.jsonl", "session_index.jsonl", "config.toml", "managed_config.toml", "environments.toml",
    "AGENTS.md", "AGENTS.override.md", ".credentials.json",
    ".tmp/plugins.sha", ".tmp/known_marketplaces.json", ".tmp/app-server-remote-plugin-sync-v1",
    ".tmp/rollout-maintenance.lock",
)
SQLITE_RE = re.compile(r"^[a-z][a-z0-9_]*_\d+\.sqlite$")
PROFILE_CONFIG_SUFFIX = ".config.toml"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
# Per-home data that can be folded into ~/.codex when a home is adopted (migration, doctor --fix).
MERGE_JSONL = {"session_index.jsonl": "updated_at", "history.jsonl": "ts"}
LOCK_DIRS = {"thread-writer-locks"}
LOCK_FILES = {".tmp/rollout-maintenance.lock"}


@dataclass
class Profile:
    name: str
    home: Path
    active: bool = False

    @property
    def logged_in(self) -> bool:
        return (self.home / "auth.json").is_file()


def all(cfg: dict[str, Any] | None = None) -> list[Profile]:  # noqa: A001 - module-level API name
    cfg = config.load() if cfg is None else cfg
    root = paths.PROFILES_ROOT
    result: list[Profile] = []
    if not root.is_dir():
        return result
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        result.append(Profile(entry.name, entry, entry.name == cfg.get("active")))
    return result


def names(cfg: dict[str, Any] | None = None) -> list[str]:
    return [p.name for p in all(cfg)]


def get(name: str, cfg: dict[str, Any] | None = None) -> Profile | None:
    return next((p for p in all(cfg) if p.name == name), None)


def active_name(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = config.load() if cfg is None else cfg
    known = names(cfg)
    if cfg.get("active") in known:
        return cfg["active"]
    return known[0] if len(known) == 1 else None


def slug(email: str) -> str:
    """prodex-compatible profile name for an email address: user@host -> user_host."""
    name = re.sub(r"[^A-Za-z0-9._@+-]", "-", email.strip().lower().replace("@", "_"))
    return name.strip(".-") or "account"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and ".." not in name


def by_home(home: str | Path | None, profiles: list[Profile]) -> str | None:
    if not home:
        return None
    try:
        resolved = Path(home).resolve()
    except OSError:
        return None
    for profile in profiles:
        with contextlib.suppress(OSError):
            if profile.home.resolve() == resolved:
                return profile.name
    if resolved == paths.SHARED_CODEX_HOME.resolve():
        return "(shared ~/.codex)"
    return None


# ----------------------------------------------------------------------------- shared entries
def shared_entries(shared: Path | None = None) -> list[tuple[str, bool]]:
    """(relative path, is_dir) for everything a profile home links into ~/.codex."""
    shared = shared or paths.SHARED_CODEX_HOME
    entries = [(name, True) for name in SHARED_DIRS] + [(name, False) for name in SHARED_FILES]
    if shared.is_dir():
        for item in sorted(shared.iterdir(), key=lambda p: p.name):
            if SQLITE_RE.match(item.name):
                entries += [(item.name + suffix, False) for suffix in ("", "-wal", "-shm")]
            elif item.name.endswith(PROFILE_CONFIG_SUFFIX) and item.is_file():
                entries.append((item.name, False))
    return entries


@dataclass
class HomeReport:
    linked: list[str] = field(default_factory=list)     # links created or corrected
    adopted: list[str] = field(default_factory=list)    # per-home data folded into ~/.codex
    private: list[str] = field(default_factory=list)    # real entries that stay private (problems)

    @property
    def ok(self) -> bool:
        return not self.private


def prepare_home(home: Path, adopt: bool = False, dry_run: bool = False,
                 shared: Path | None = None) -> HomeReport:
    """Create every missing shared link in `home`.

    Real files or directories where a link belongs are left alone and reported, unless
    `adopt` is set and the entry can be folded in safely: thread names and history are
    merged by timestamp, idle lock files are replaced. Session directories with content
    are never moved automatically."""
    shared = shared or paths.SHARED_CODEX_HOME
    report = HomeReport()
    for rel, is_dir in shared_entries(shared):
        link, target = home / rel, shared / rel
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
            report.linked.append(f"{rel} (was -> {os.readlink(link)})")
            if not dry_run:
                link.unlink()
                _link(link, target, is_dir)
            continue
        if link.exists():
            if not adopt or not _adoptable(rel, link):
                report.private.append(rel)
                continue
            report.adopted.append(rel)
            if not dry_run:
                _adopt(rel, link, target)
                _link(link, target, is_dir)
            continue
        report.linked.append(rel)
        if not dry_run:
            _link(link, target, is_dir)
    return report


def _link(link: Path, target: Path, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)  # codex create_dir_all fails on a dangling link
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=is_dir)


def _adoptable(rel: str, path: Path) -> bool:
    if rel in MERGE_JSONL:
        return path.is_file()
    if rel in LOCK_FILES:
        return path.is_file() and not _held(path)
    if rel in LOCK_DIRS:
        return path.is_dir() and builtins.all(child.is_file() and not _held(child) for child in path.iterdir())
    return False


def _held(path: Path) -> bool:
    import fcntl

    try:
        with open(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def _adopt(rel: str, path: Path, target: Path) -> None:
    if rel in MERGE_JSONL:
        merge_jsonl(path, target, MERGE_JSONL[rel])
        path.unlink()
    elif rel in LOCK_FILES:
        path.unlink()
    elif rel in LOCK_DIRS:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()


def merge_jsonl(source: Path, destination: Path, key: str) -> int:
    """Union of two JSON-lines files ordered by `key` (stable); returns lines added."""
    def lines(path: Path) -> list[str]:
        try:
            return [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    existing = lines(destination)
    seen = set(existing)
    added = [line for line in lines(source) if line not in seen and not seen.add(line)]
    if not added:
        return 0

    def order(item: tuple[int, str]) -> tuple[int, Any, int]:
        index, line = item
        try:
            value = json.loads(line).get(key)
        except (json.JSONDecodeError, AttributeError):
            return (0, 0, index)
        if isinstance(value, (int, float)):
            return (1, value, index)
        return (2, str(value), index) if value is not None else (0, 0, index)

    merged = [line for _, line in sorted(enumerate(existing + added), key=order)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.mycodex-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(merged) + "\n")
    os.replace(tmp, destination)
    return len(added)


# ----------------------------------------------------------------------------- lifecycle
def ensure_root() -> Path:
    paths.PROFILES_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    return paths.PROFILES_ROOT


def new_pending() -> Path:
    """A private scratch home for a login in progress (hidden from profile listings)."""
    root = ensure_root()
    pending = root / f".pending-{os.getpid()}-{secrets.token_hex(3)}"
    pending.mkdir(mode=0o700)
    prepare_home(pending)
    return pending


def install(pending: Path, name: str) -> Path:
    destination = paths.PROFILES_ROOT / name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"{destination} already exists")
    os.rename(pending, destination)
    return destination


def discard(home: Path) -> None:
    """Delete a profile home. Shared entries are links and are unlinked, never followed."""
    if home.is_symlink():
        home.unlink()
    elif home.exists():
        shutil.rmtree(home)


def retire(home: Path) -> Path:
    """Move a home aside (hidden from listings) instead of deleting it."""
    destination = home.with_name(f".removed-{home.name}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.rename(home, destination)
    return destination


def identity(home: Path) -> dict[str, Any]:
    """Email / account / plan of the login in `home` (no secrets)."""
    return auth.claims(home)
