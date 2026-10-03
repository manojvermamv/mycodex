"""Read-only inspection of the official Codex installation and its shared state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


def version() -> str | None:
    try:
        result = subprocess.run([paths.codex_bin(), "--version"], capture_output=True, text=True,
                                timeout=20, env=paths.tool_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
    return match.group(1) if match else None


def resolved_binary() -> Path | None:
    found = paths.which("codex")
    return Path(found).resolve() if found else None


def package_info() -> dict[str, Any]:
    """Official standalone package metadata (installer layout under ~/.codex/packages)."""
    info: dict[str, Any] = {}
    binary = resolved_binary()
    if binary:
        info["binary"] = str(binary)
        manifest = binary.parent.parent / "codex-package.json"
        if manifest.exists():
            try:
                info["package"] = json.loads(manifest.read_text())
            except json.JSONDecodeError:
                pass
        info["standalone"] = "/packages/standalone/" in str(binary)
    try:
        info["update_check"] = json.loads((paths.SHARED_CODEX_HOME / "version.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return info


# ----------------------------------------------------------------------------- daemon state
@dataclass
class DaemonState:
    home: Path
    pid: int | None
    alive: bool
    remote_control_setting: bool | None
    socket: Path | None
    updater_pid: int | None
    updater_alive: bool


def _pid_alive(pid: int | None, needle: str = "codex") -> bool:
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return False
    return needle in cmdline


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            return int(json.loads(text).get("pid"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
    return int(text) if text.isdigit() else None


def daemon_state(home: Path) -> DaemonState:
    base = home / "app-server-daemon"
    pid = _read_pid(base / "daemon.pid")
    updater = _read_pid(base / "daemon-updater.pid")
    setting = None
    try:
        setting = bool(json.loads((base / "settings.json").read_text()).get("remoteControlEnabled"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    sock = home / "app-server-control" / "app-server-control.sock"
    return DaemonState(
        home=home, pid=pid, alive=_pid_alive(pid, "app-server"),
        remote_control_setting=setting, socket=sock if sock.exists() or sock.is_symlink() else None,
        updater_pid=updater, updater_alive=_pid_alive(updater, "pid-update-loop"),
    )


# ----------------------------------------------------------------------------- sqlite (read-only)
def _connect(name_prefix: str) -> sqlite3.Connection | None:
    home = paths.SHARED_CODEX_HOME
    candidates = sorted(home.glob(f"{name_prefix}_*.sqlite"))
    if not candidates:
        return None
    try:
        conn = sqlite3.connect(f"file:{candidates[-1]}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def threads(limit: int = 200) -> list[dict[str, Any]]:
    conn = _connect("state")
    if not conn:
        return []
    try:
        cols = {row[1] for row in conn.execute("pragma table_info(threads)")}
        wanted = [c for c in ("id", "created_at", "updated_at", "source", "model_provider", "cwd",
                              "name", "title", "project_id", "archived", "first_user_message")
                  if c in cols]
        rows = conn.execute(f"select {', '.join(wanted)} from threads order by updated_at desc limit ?",
                            (limit,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def projects() -> list[dict[str, Any]]:
    conn = _connect("state")
    if not conn:
        return []
    try:
        rows = conn.execute(
            "select p.id, p.name, group_concat(r.path, ';') as roots from projects p "
            "left join project_roots r on r.project_id = p.id group by p.id order by p.position").fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def enrollments() -> list[dict[str, Any]]:
    conn = _connect("state")
    if not conn:
        return []
    try:
        rows = conn.execute("select account_id, server_id, environment_id, server_name, updated_at "
                            "from remote_control_enrollments").fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def recent_log_errors(since_seconds: int = 3600, pattern: str = "%401%") -> list[dict[str, Any]]:
    conn = _connect("logs")
    if not conn:
        return []
    try:
        import time
        rows = conn.execute(
            "select ts, level, target, substr(feedback_log_body, 1, 200) as body from logs "
            "where ts > ? and level in ('ERROR','WARN') and feedback_log_body like ? "
            "order by id desc limit 20", (int(time.time()) - since_seconds, pattern)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def run_codex(args: list[str], home: Path | None = None, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    extra = {"NO_COLOR": "1"}
    if home:
        extra.update({"CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(paths.SHARED_CODEX_HOME)})
    return subprocess.run([paths.codex_bin(), *args], capture_output=True, text=True, timeout=timeout,
                          env=paths.tool_env(extra), stdin=subprocess.DEVNULL)


def env_for_profile(home: Path) -> dict[str, str]:
    return {"CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(paths.SHARED_CODEX_HOME)}


def umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
