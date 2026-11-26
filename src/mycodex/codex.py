"""Read-only inspection of the official Codex installation and its shared state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
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
    version_files = [paths.SHARED_CODEX_HOME / "version.json"]
    if paths.PROFILES_ROOT.is_dir():
        profile_versions: list[tuple[float, Path]] = []
        for candidate in paths.PROFILES_ROOT.glob("*/version.json"):
            try:
                if candidate.is_file():
                    profile_versions.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        profile_versions.sort(key=lambda item: item[0], reverse=True)
        version_files.extend(p for _, p in profile_versions)
    for vf in version_files:
        try:
            info["update_check"] = json.loads(vf.read_text())
            break
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return info


# ----------------------------------------------------------------------------- daemon state
@dataclass
class DaemonState:
    home: Path
    pid: int | None
    alive: bool
    remote_control_setting: bool | None
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
    return DaemonState(
        home=home, pid=pid, alive=_pid_alive(pid, "app-server"),
        remote_control_setting=setting,
        updater_pid=updater, updater_alive=_pid_alive(updater, "pid-update-loop"),
    )


# ----------------------------------------------------------------------------- sqlite (read-only)
def _warn_sqlite(op: str, exc: sqlite3.Error) -> None:
    print(f"mycodex: sqlite error in {op}: {exc}", file=sys.stderr)


def _connect(name_prefix: str) -> sqlite3.Connection | None:
    home = paths.SHARED_CODEX_HOME
    candidates = sorted(home.glob(f"{name_prefix}_*.sqlite"))
    if not candidates:
        return None
    try:
        conn = sqlite3.connect(f"file:{candidates[-1]}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        _warn_sqlite(f"_connect({name_prefix})", exc)
        return None


_THREAD_COLS = (
    "id", "created_at", "updated_at", "source", "thread_source", "model_provider",
    "cwd", "name", "title", "project_id", "archived", "first_user_message", "rollout_path",
)


def _thread_columns(conn: sqlite3.Connection) -> str:
    cols = {row[1] for row in conn.execute("pragma table_info(threads)")}
    return ", ".join(c for c in _THREAD_COLS if c in cols)


def threads(limit: int | None = 200) -> list[dict[str, Any]]:
    conn = _connect("state")
    if not conn:
        return []
    try:
        sql = f"select {_thread_columns(conn)} from threads order by updated_at desc"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " limit ?"
            params = (limit,)
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        _warn_sqlite("threads", exc)
        return []
    finally:
        conn.close()


def find_threads(thread_id: str) -> list[dict[str, Any]]:
    """Query threads matching an exact ID or ID prefix directly in SQLite."""
    conn = _connect("state")
    if not conn:
        return []
    try:
        cols_sql = _thread_columns(conn)
        exact = conn.execute(f"select {cols_sql} from threads where id = ?", (thread_id,)).fetchall()
        if exact:
            return [dict(row) for row in exact]
        prefixed = conn.execute(
            f"select {cols_sql} from threads where substr(id, 1, ?) = ? order by updated_at desc",
            (len(thread_id), thread_id),
        ).fetchall()
        return [dict(row) for row in prefixed]
    except sqlite3.Error as exc:
        _warn_sqlite("find_threads", exc)
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
    except sqlite3.Error as exc:
        _warn_sqlite("projects", exc)
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
    except sqlite3.Error as exc:
        _warn_sqlite("enrollments", exc)
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
    except sqlite3.Error as exc:
        _warn_sqlite("recent_log_errors", exc)
        return []
    finally:
        conn.close()


def recent_thread_logs(thread_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """Return recent log lines associated with `thread_id` from logs_*.sqlite."""
    conn = _connect("logs")
    if not conn:
        return []
    try:
        rows = conn.execute(
            "select id, ts, ts_nanos, level, target, substr(feedback_log_body, 1, 500) as body from logs "
            "where thread_id = ? order by id desc limit ?", (thread_id, limit)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        _warn_sqlite("recent_thread_logs", exc)
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
