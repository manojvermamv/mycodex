"""Process discovery from /proc: who runs codex (and mycodex), as which account, doing what."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# ~/.codex/profiles/<name> (mycodex) and ~/.prodex/profiles/<name> (older prodex homes)
PROFILE_PATH_RE = re.compile(r"/\.(?:codex|prodex)/profiles/([^/.][^/]*)/")


@dataclass
class Proc:
    pid: int
    ppid: int
    argv: list[str]
    env: dict[str, str]
    started: float               # epoch seconds
    tty: bool
    role: str = ""
    profile: str | None = None
    sockets: list[str] = field(default_factory=list)

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.started)

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return time.time()


def _read(pid: int) -> Proc | None:
    base = Path(f"/proc/{pid}")
    try:
        if base.stat().st_uid != os.getuid():
            return None
        argv = [a for a in (base / "cmdline").read_bytes().decode(errors="replace").split("\0") if a]
        stat = (base / "stat").read_text()
        env_raw = (base / "environ").read_bytes().decode(errors="replace").split("\0")
    except (OSError, PermissionError):
        return None
    if not argv:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    ppid = int(fields[1])
    tty_nr = int(fields[4])
    start_ticks = int(fields[19])
    env = {}
    for item in env_raw:
        key, sep, value = item.partition("=")
        if sep and key in ("CODEX_HOME", "CODEX_SQLITE_HOME", "PRODEX_HOME", "MYCODEX_ROLE", "MYCODEX_PROFILE"):
            env[key] = value
    started = _boot_time() + start_ticks / os.sysconf("SC_CLK_TCK")
    return Proc(pid=pid, ppid=ppid, argv=argv, env=env, started=started, tty=tty_nr != 0)


def _registered_proxies() -> dict[int, str]:
    """pid -> role for running mycodex rotation proxies (state/proxies/<pid>.json)."""
    found: dict[int, str] = {}
    try:
        entries = list(paths.PROXIES_DIR.glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        try:
            data = json.loads(entry.read_text())
            found[int(data["pid"])] = str(data.get("role") or "session")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return found


def classify(proc: Proc, proxies: dict[int, str] | None = None) -> str:
    argv = proc.argv
    exe = os.path.basename(argv[0])
    rest = argv[1:]
    joined = " ".join(argv)
    if exe == "codex" or "/bin/codex" in argv[0]:
        if "pid-update-loop" in rest:
            return "daemon updater"
        if "app-server" in rest and "--managed-daemon" in rest:
            return "daemon (remote control)" if "--remote-control" in rest else "daemon"
        if "app-server" in rest and "proxy" in rest:
            return "control-socket client"
        if "remote-control" in rest:
            sub = rest[rest.index("remote-control") + 1:]
            if not sub or sub[0].startswith("-"):
                return "remote-control server"
            return f"remote-control {sub[0]}"
        if "app-server" in rest:
            return "app-server"
        via = "openai_base_url=" in joined or "prodex-openai-governed-http" in joined
        mode = "exec" if "exec" in rest else ("resume" if "resume" in rest else "tui")
        return f"codex {mode}{' (rotating)' if via else ''}"
    if "mycodex" in joined:
        if "__remote-serve" in joined or proc.env.get("MYCODEX_ROLE") == "remote-serve":
            return "mycodex remote service"
        if proxies and proc.pid in proxies:
            return "mycodex session (proxy)"
        return ""
    if exe == "prodex":
        if rest[:1] == ["__runtime-broker"]:
            return "rotation proxy"
        if rest[:1] == ["__runtime-goal-session-notify"]:
            return "prodex hook"
        if "remote-control" in rest:
            return "prodex remote supervisor"
        return "prodex launcher"
    return ""


def _profile_of(proc: Proc) -> str | None:
    if proc.env.get("MYCODEX_PROFILE"):
        return proc.env["MYCODEX_PROFILE"]
    home = proc.env.get("CODEX_HOME")
    if home:
        match = PROFILE_PATH_RE.search(home.rstrip("/") + "/")
        return match.group(1) if match else ("(shared ~/.codex)" if home.rstrip("/").endswith("/.codex") else home)
    match = PROFILE_PATH_RE.search(proc.argv[0])
    if match:
        return match.group(1)
    if "/.codex/packages/" in proc.argv[0]:
        return "(shared ~/.codex)"
    return None


def listening_unix_sockets() -> dict[int, str]:
    """inode -> path for listening unix sockets."""
    result = {}
    try:
        lines = Path("/proc/net/unix").read_text().splitlines()[1:]
    except OSError:
        return result
    for line in lines:
        parts = line.split()
        if len(parts) >= 8 and int(parts[3], 16) & 0x10000:  # __SO_ACCEPTCON
            result[int(parts[6])] = parts[7]
    return result


def sockets_of(pid: int, listening: dict[int, str]) -> list[str]:
    found = []
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:["):
            inode = int(target[8:-1])
            if inode in listening:
                found.append(listening[inode])
    return sorted(set(found))


def scan(with_sockets: bool = True) -> list[Proc]:
    listening = listening_unix_sockets() if with_sockets else {}
    proxies = _registered_proxies()
    procs = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        proc = _read(int(name))
        if not proc:
            continue
        proc.role = classify(proc, proxies)
        if not proc.role:
            continue
        proc.profile = _profile_of(proc)
        if with_sockets:
            proc.sockets = sockets_of(proc.pid, listening)
        procs.append(proc)
    return sorted(procs, key=lambda p: p.started)


def children_map(procs: list[Proc]) -> dict[int, list[Proc]]:
    tree: dict[int, list[Proc]] = {}
    for proc in procs:
        tree.setdefault(proc.ppid, []).append(proc)
    return tree


def descendants(pid: int, procs: list[Proc]) -> list[Proc]:
    tree = children_map(procs)
    result, stack = [], [pid]
    while stack:
        current = stack.pop()
        for child in tree.get(current, []):
            result.append(child)
            stack.append(child.pid)
    return result


def human_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"
