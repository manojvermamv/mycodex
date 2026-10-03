"""Remote-control lifecycle: a systemd user service around the official `codex remote-control`.

Both modes run the official foreground server as the relay profile (CODEX_HOME = its
home, so its installation id and phone pairing stay stable) with Codex's built-in
`openai` provider, so phone threads and terminal threads are one list in either mode:

  rotating  codex -c openai_base_url=<mycodex proxy> remote-control
            model turns rotate across ready profiles through an in-process proxy.
  pinned    codex remote-control
            every model turn uses the relay account. Optional failover moves the relay
            to the next ready profile when it is exhausted (pair the phone with it).

UMask 0022 is required: codex creates its private socket directory with tempfile's
default mode; under umask 0002 that directory is group-writable and codex refuses to bind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import appserver, codex, config, launch, paths, procs, profiles, quota, ui

EXIT_FAILOVER = 75


# ----------------------------------------------------------------------------- systemd
def _systemctl(*args: str, check: bool = False, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    env = paths.tool_env()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=timeout, env=env, check=check)


def unit_text(cwd: str) -> str:
    return f"""# Managed by mycodex. Edit with `mycodex remote start ...`, not by hand.
# Source: {paths.UNIT_SOURCE} (systemd reads it through {paths.REMOTE_UNIT_FILE}).
[Unit]
Description=mycodex remote-control (official Codex, multi-account)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStart={paths.LAUNCHER} __remote-serve
Restart=always
RestartSec=10s
UMask=0022
WorkingDirectory={cwd}
Environment=PATH={paths.LOCAL_BIN}:/usr/local/bin:/usr/bin:/bin
Environment=MYCODEX_ROLE=remote-serve
Environment=NO_COLOR=1
Environment=TERM=dumb
Environment=PYTHONUNBUFFERED=1
TimeoutStopSec=30s
KillMode=control-group
SyslogIdentifier=mycodex-remote
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install_unit(cwd: str) -> bool:
    """Keep the unit file inside MYCODEX_HOME and link it into systemd's search path.

    Returns True when anything changed (systemd was reloaded)."""
    changed = False
    text = unit_text(cwd)
    paths.UNIT_SOURCE.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = paths.UNIT_SOURCE.read_text()
    except FileNotFoundError:
        current = None
    if current != text:
        paths.UNIT_SOURCE.write_text(text)
        os.chmod(paths.UNIT_SOURCE, 0o644)
        changed = True
    link = paths.REMOTE_UNIT_FILE
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if Path(os.readlink(link)) != paths.UNIT_SOURCE:
            link.unlink()
            link.symlink_to(paths.UNIT_SOURCE)
            changed = True
    elif link.exists():
        if "Managed by mycodex" not in link.read_text():
            raise SystemExit(f"Error: {link} exists and is not managed by mycodex; move it away first")
        link.unlink()  # a regular file written by mycodex 0.1.0
        link.symlink_to(paths.UNIT_SOURCE)
        changed = True
    else:
        link.symlink_to(paths.UNIT_SOURCE)
        changed = True
    if changed:
        _systemctl("daemon-reload")
    return changed


def service_info() -> dict[str, str]:
    if not paths.REMOTE_UNIT_FILE.exists():
        return {"LoadState": "not-installed"}
    props = "LoadState,ActiveState,SubState,MainPID,NRestarts,UnitFileState,Result,ExecMainStartTimestamp,ActiveEnterTimestamp"
    result = _systemctl("show", paths.REMOTE_UNIT, "-p", props)
    info = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        info[key] = value
    return info


# ----------------------------------------------------------------------------- serve
def server_args(mode: str, base_url: str | None) -> list[str]:
    """codex arguments for the foreground remote-control server."""
    if mode == "rotating" and base_url:
        return [*launch.proxy_args(base_url), "remote-control"]
    return ["remote-control"]


def _write_serve_state(data: dict[str, Any]) -> None:
    paths.state_dir()
    tmp = paths.SERVE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, paths.SERVE_STATE)


def read_serve_state() -> dict[str, Any]:
    try:
        return json.loads(paths.SERVE_STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def next_relay(current: str, cfg: dict[str, Any]) -> str | None:
    pool = [p for p in profiles.all(cfg) if p.logged_in and p.name != current
            and p.name not in cfg["rotation"]["disabled"]]
    order = cfg["rotation"]["order"]
    pool.sort(key=lambda p: order.index(p.name) if p.name in order else len(order))
    quotas = quota.fetch_all(pool)
    for p in pool:
        if quotas[p.name].eligible:
            return p.name
    return None


def _say(message: str) -> None:
    print(f"mycodex-remote: {message}", file=sys.stderr, flush=True)


def serve() -> int:
    """Service entry point (ExecStart)."""
    cfg = config.load()
    name = cfg["remote"].get("profile")
    profile = profiles.get(name, cfg) if name else None
    if not profile or not profile.logged_in:
        _say(f"relay profile {name!r} is not a logged-in mycodex profile; refusing to start")
        return 3
    return run_server(profile, cfg["remote"].get("mode") or "rotating", cfg, service=True)


def run_server(profile: profiles.Profile, mode: str, cfg: dict[str, Any], service: bool) -> int:
    """Run the official remote-control server as `profile` (rotating: behind the proxy)."""
    from .proxy import RotationProxy

    os.umask(0o022)
    profiles.prepare_home(profile.home)
    env = launch.codex_env(profile, "remote-serve")
    proxy = RotationProxy(profile.name, role="remote", echo=service).start() if mode == "rotating" else None
    argv = [paths.codex_bin(), *server_args(mode, proxy.base_url if proxy else None)]
    _say(f"starting mode={mode} relay={profile.name}" + (f" proxy=127.0.0.1:{proxy.port}" if proxy else ""))

    def started(child: subprocess.Popen[bytes]) -> None:
        if service:
            _write_serve_state({"pid": os.getpid(), "child_pid": child.pid, "mode": mode, "profile": profile.name,
                                "proxy_port": proxy.port if proxy else None, "started_at": time.time()})

    remote_cfg = cfg["remote"]
    interval = max(60, int(remote_cfg.get("failover_check_seconds") or 300))
    next_check = [time.time() + interval]

    def tick(_child: subprocess.Popen[bytes]) -> int | None:
        if not remote_cfg.get("failover") or time.time() < next_check[0]:
            return None
        next_check[0] = time.time() + interval
        q = quota.fetch(profile)
        exhausted = q.status == "auth invalid" or (mode == "pinned" and q.status == "limited")
        if not exhausted:
            return None
        replacement = next_relay(profile.name, cfg)
        if not replacement:
            _say(f"relay {profile.name} is {q.status}; no ready replacement")
            return None
        _say(f"relay {profile.name} is {q.status}; failing over to {replacement} (pair the phone with that account)")
        with config.editing() as data:
            data["remote"]["profile"] = replacement
        return EXIT_FAILOVER

    code = launch.supervise(argv, env, proxy, tick=tick if service else None, on_start=started,
                            interactive=not service)
    _say(f"server exited with status {code}")
    return code


# ----------------------------------------------------------------------------- discovery
def find_servers(all_procs: list[procs.Proc] | None = None) -> list[dict[str, Any]]:
    """Every remote-control capable server: our service's foreground server and codex daemons."""
    all_procs = all_procs if all_procs is not None else procs.scan()
    info = service_info()
    main_pid = int(info.get("MainPID") or 0)
    service_pids = {p.pid for p in procs.descendants(main_pid, all_procs)} if main_pid else set()
    servers = []
    for proc in all_procs:
        if proc.role == "remote-control server":
            servers.append({"kind": "service" if proc.pid in service_pids else "foreground",
                            "proc": proc, "socket": next(iter(proc.sockets), None)})
        elif proc.role == "daemon (remote control)":
            sock = next((s for s in proc.sockets if "codex-daemon" in s), None)
            servers.append({"kind": "daemon", "proc": proc, "socket": sock})
    return servers


def server_status(socket_path: str, home: Path | None = None, with_clients: bool = True) -> dict[str, Any]:
    with appserver.AppServer(socket_path, home=home, timeout=20) as server:
        status = server.result("remoteControl/status/read")
        clients = []
        if with_clients and status.get("environmentId"):
            try:
                clients = server.result("remoteControl/client/list",
                                        {"environmentId": status["environmentId"]}).get("data", [])
            except appserver.AppServerError:
                clients = []
        return {"status": status, "clients": clients}


def service_server(all_procs: list[procs.Proc] | None = None) -> dict[str, Any] | None:
    for server in find_servers(all_procs):
        if server["kind"] == "service":
            return server
    return None


def wait_until_connected(timeout: float = 90, box: ui.StatusBox | None = None) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        info = service_info()
        state = info.get("ActiveState", "?")
        if state in ("failed",):
            raise SystemExit(f"Error: {paths.REMOTE_UNIT} failed to start (see `mycodex remote logs`)")
        server = service_server()
        if server and server["socket"]:
            try:
                result = server_status(server["socket"], with_clients=False)
                status = result["status"].get("status")
                if box:
                    box.update("connecting", f"remote control is {status}")
                if status == "connected":
                    return result
            except appserver.AppServerError as exc:
                last_error = str(exc)
        elif box:
            box.update("starting", f"service {state}; waiting for the remote-control server")
        time.sleep(2)
    if last_error:
        ui.warn(f"last status error: {last_error}")
    return None


# ----------------------------------------------------------------------------- conflicts
def official_remote_daemons(all_procs: list[procs.Proc]) -> list[procs.Proc]:
    return [p for p in all_procs if p.role == "daemon (remote control)"]


def stop_official_daemon(home: Path) -> tuple[bool, str]:
    """Stop a codex-managed daemon and persist remote control off for that home."""
    stop = codex.run_codex(["remote-control", "stop"], home=home, timeout=90)
    disable = codex.run_codex(["app-server", "daemon", "disable-remote-control"], home=home, timeout=90)
    ok = stop.returncode == 0
    message = (stop.stdout + stop.stderr).strip().splitlines()[-1:] or [""]
    if disable.returncode != 0:
        message.append("could not persist remote control off: " + (disable.stderr.strip() or disable.stdout.strip())[:200])
    state = codex.daemon_state(home)
    if not state.alive and state.updater_alive and state.updater_pid:
        try:
            os.kill(state.updater_pid, signal.SIGTERM)
            message.append(f"stopped its idle updater {state.updater_pid}")
        except OSError:
            pass
    return ok, "; ".join(m for m in message if m)
