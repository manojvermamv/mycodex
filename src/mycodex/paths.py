"""Filesystem locations used by codex and mycodex."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME = Path.home()


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


# The official Codex home: shared threads, history, config and SQLite state.
SHARED_CODEX_HOME = _env_path("MYCODEX_SHARED_CODEX_HOME") or HOME / ".codex"
# One real CODEX_HOME per account; each links its shared entries back to SHARED_CODEX_HOME.
PROFILES_ROOT = SHARED_CODEX_HOME / "profiles"

# Everything mycodex owns lives in its own directory (the source tree: <root>/src/mycodex/paths.py).
MYCODEX_HOME = _env_path("MYCODEX_HOME") or Path(__file__).resolve().parents[2]
LAUNCHER = MYCODEX_HOME / "bin" / "mycodex"
CONFIG_FILE = MYCODEX_HOME / "config.json"
STATE_DIR = MYCODEX_HOME / "state"
SERVE_STATE = STATE_DIR / "remote-serve.json"
ACCOUNTS_STATE = STATE_DIR / "accounts.json"
PROXIES_DIR = STATE_DIR / "proxies"
LOCKS_DIR = STATE_DIR / "locks"
LOG_DIR = STATE_DIR / "logs"
PROXY_LOG = LOG_DIR / "proxy.log"
REMOTE_UNIT = "mycodex-remote.service"
UNIT_SOURCE = MYCODEX_HOME / "systemd" / REMOTE_UNIT

# The only paths outside MYCODEX_HOME are links the OS requires (PATH entry, systemd search path).
LOCAL_BIN = HOME / ".local" / "bin"
PATH_LINK = LOCAL_BIN / "mycodex"
SYSTEMD_USER_DIR = (_env_path("XDG_CONFIG_HOME") or HOME / ".config") / "systemd" / "user"
REMOTE_UNIT_FILE = SYSTEMD_USER_DIR / REMOTE_UNIT

# Where accounts lived while mycodex ran on top of prodex (read only by `mycodex migrate`).
PRODEX_HOME = _env_path("PRODEX_HOME") or HOME / ".prodex"
PRODEX_STATE = PRODEX_HOME / "state.json"
PRODEX_PROFILES = PRODEX_HOME / "profiles"

# Locations used by mycodex 0.1.0 before everything moved into MYCODEX_HOME.
LEGACY_CONFIG_DIR = (_env_path("XDG_CONFIG_HOME") or HOME / ".config") / "mycodex"
LEGACY_STATE_DIR = (_env_path("XDG_STATE_HOME") or HOME / ".local" / "state") / "mycodex"


def migrate_legacy() -> list[str]:
    """Move files from the old ~/.config/mycodex and ~/.local/state/mycodex into MYCODEX_HOME."""
    moved: list[str] = []
    for old, new in ((LEGACY_CONFIG_DIR / "config.json", CONFIG_FILE),
                     (LEGACY_STATE_DIR / "remote-serve.json", SERVE_STATE)):
        if old.is_file() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            moved.append(f"moved {old} -> {new}")
    for directory in (LEGACY_CONFIG_DIR, LEGACY_STATE_DIR):
        if not directory.is_dir():
            continue
        for leftover in ("config.lock", "config.json.tmp", "remote-serve.tmp"):
            try:
                (directory / leftover).unlink()
            except FileNotFoundError:
                pass
        try:
            directory.rmdir()
            moved.append(f"removed {directory}")
        except OSError:
            pass  # not empty: something else lives there, leave it
    return moved


def state_dir(*parts: str) -> Path:
    """A private directory under STATE_DIR, created on first use."""
    path = STATE_DIR.joinpath(*parts)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def daemon_socket_dir() -> Path:
    return Path(f"/tmp/codex-daemon-{os.getuid()}")


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = LOCAL_BIN / name
    return str(candidate) if candidate.exists() else None


def codex_bin() -> str:
    return os.environ.get("MYCODEX_CODEX_BIN") or which("codex") or "codex"


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child tools: make sure ~/.local/bin is on PATH (ssh/systemd lack it)."""
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    if str(LOCAL_BIN) not in parts:
        env["PATH"] = os.pathsep.join([str(LOCAL_BIN), *parts]) if parts else str(LOCAL_BIN)
    if extra:
        env.update(extra)
    return env
