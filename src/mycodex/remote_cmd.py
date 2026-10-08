"""`mycodex remote ...` and `mycodex [--profile P] remote-control`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import appserver, config, fmt, model_cache, paths, procs, profiles, proxy, quota, remote, resolve, state, ui

ACTIVE = ("active", "activating", "reloading")


def _active() -> bool:
    return remote.service_info().get("ActiveState") in ACTIVE


# ----------------------------------------------------------------------------- start
def start(profile: str | None = None, mode: str | None = None, cwd: str | None = None,
          failover: bool | None = None, force: bool = False, wait: bool = True,
          restart: bool = False) -> int:
    cfg = config.load()
    names = profiles.names(cfg)
    if not names:
        ui.error("no profiles yet", ["mycodex profile add", "mycodex migrate   # import accounts from prodex"])
        return 1
    chosen = profile or cfg["remote"].get("profile") or profiles.active_name(cfg) or names[0]
    chosen = resolve.resolve(chosen, names, cfg)
    mode = mode or cfg["remote"].get("mode") or "rotating"
    if mode not in config.REMOTE_MODES:
        ui.error(f"unknown mode '{mode}' (use rotating or pinned)")
        return 2
    workdir = str(Path(cwd or cfg["remote"].get("cwd") or paths.HOME).expanduser().resolve())
    if not Path(workdir).is_dir():
        ui.error(f"working directory {workdir} does not exist")
        return 1
    if failover is None:
        # A new start uses the CLI default; restart preserves the saved explicit choice.
        failover = cfg["remote"].get("failover", True) if restart else True

    box = ui.StatusBox("Mycodex Phone Connection")
    box.update("preflight", f"checking {chosen}")
    q = quota.fetch(profiles.get(chosen, cfg))
    if q.status == "auth invalid":
        box.clear()
        ui.error(f"{chosen} cannot authenticate ({q.error})", [f"mycodex profile reauth {chosen}"])
        return 1
    if mode == "pinned" and q.status == "limited":
        ui.warn(f"{chosen} is currently limited; pinned mode will not serve turns until it resets")

    # conflicts: codex-managed daemons with remote control, and stray foreground servers
    all_procs = procs.scan()
    for daemon in remote.official_remote_daemons(all_procs):
        home = Path(daemon.env.get("CODEX_HOME") or paths.SHARED_CODEX_HOME)
        box.update("conflicts", f"stopping codex daemon (pid {daemon.pid}, {daemon.profile})")
        ok, message = remote.stop_official_daemon(home)
        if not ok:
            box.clear()
            ui.error(f"could not stop the codex daemon for {daemon.profile}: {message}")
            return 1
    service_pids = set()
    main_pid = int(remote.service_info().get("MainPID") or 0)
    if main_pid:
        service_pids = {p.pid for p in procs.descendants(main_pid, all_procs)}
    strays = [p for p in all_procs if p.role == "remote-control server" and p.pid not in service_pids]
    for stray in strays:
        if not force:
            box.clear()
            ui.error(f"another remote-control server is running (pid {stray.pid}, {stray.profile})",
                     ["stop it, or rerun with --force to terminate it"])
            return 1
        os.kill(stray.pid, signal.SIGTERM)

    previous = remote.read_serve_state()
    changed = (previous.get("profile"), previous.get("mode")) != (chosen, mode) \
        or cfg["remote"].get("cwd") != workdir or cfg["remote"].get("failover") != failover
    with config.editing() as data:
        data["remote"].update({"profile": chosen, "mode": mode, "cwd": workdir, "failover": failover})
    reloaded = remote.install_unit(workdir)
    remote._systemctl("enable", paths.REMOTE_UNIT)
    running = _active()
    if running and (changed or reloaded or restart):
        box.update("restarting", f"switching to {chosen} ({mode})")
        remote._systemctl("restart", paths.REMOTE_UNIT)
    elif not running:
        remote._systemctl("reset-failed", paths.REMOTE_UNIT)
        box.update("starting", f"{chosen} ({mode})")
        result = remote._systemctl("start", paths.REMOTE_UNIT)
        if result.returncode != 0:
            box.clear()
            ui.error(f"systemctl start failed: {result.stderr.strip()}", ["mycodex remote logs"])
            return 1
    if not wait:
        box.clear()
        ui.success(f"remote-control service started for {chosen} ({mode})")
        return 0
    connected = remote.wait_until_connected(box=box)
    box.clear()
    if not connected:
        ui.warn("remote control did not report 'connected' yet")
        status()
        return 1
    status()
    return 0


def stop(quiet: bool = False) -> int:
    if not paths.REMOTE_UNIT_FILE.exists():
        if not quiet:
            ui.info("remote-control service is not installed")
        return 0
    remote._systemctl("stop", paths.REMOTE_UNIT)
    remote._systemctl("disable", paths.REMOTE_UNIT)
    if not quiet:
        ui.success("remote-control service stopped and disabled (phone link is offline)")
    return 0


def restart() -> int:
    if not paths.REMOTE_UNIT_FILE.exists():
        ui.error("remote-control service is not installed", ["mycodex remote start"])
        return 1
    return start(restart=True)


def share_models(source_name: str | None) -> int:
    """Copy one Plus profile's cache now and save it for future managed starts."""
    cfg = config.load()
    names = profiles.names(cfg)
    target_name = cfg["remote"].get("profile")
    if not target_name:
        ui.error("no phone account is configured", ["mycodex remote start --profile NAME"])
        return 1
    source_name = source_name or cfg["remote"].get("models_source") or profiles.active_name(cfg)
    if not source_name:
        ui.error("choose a source account", ["mycodex remote models share SOURCE"])
        return 1
    try:
        source = profiles.get(resolve.resolve(source_name, names, cfg), cfg)
        target = profiles.get(resolve.resolve(target_name, names, cfg), cfg)
    except SystemExit as exc:
        ui.error(str(exc))
        return 1
    if source is None or target is None:
        ui.error("the selected phone or source account no longer exists")
        return 1
    try:
        copied = model_cache.sync(source, target)
    except model_cache.ModelCacheError as exc:
        ui.error(str(exc))
        return 1
    if not copied:
        ui.info("the selected source is already the phone account; no model cache copy is needed")
        return 0
    with config.editing() as data:
        data["remote"]["models_source"] = source.name
    ui.success(f"shared {source.name}'s model cache with phone account {target.name}; future managed starts refresh it")
    return 0


# ----------------------------------------------------------------------------- status
def collect() -> dict[str, Any]:
    cfg = config.load()
    info = remote.service_info()
    all_procs = procs.scan()
    servers = remote.find_servers(all_procs)
    entries = []
    for server in servers:
        proc = server["proc"]
        entry = {"kind": server["kind"], "pid": proc.pid, "profile": proc.profile, "socket": server["socket"],
                 "uptime": procs.human_uptime(proc.uptime)}
        if server["socket"]:
            try:
                details = remote.server_status(server["socket"])
                entry["status"] = details["status"]
                entry["clients"] = details["clients"]
            except appserver.AppServerError as exc:
                entry["error"] = str(exc)
        entries.append(entry)
    return {"config": cfg["remote"], "service": info, "servers": entries,
            "serve_state": remote.read_serve_state()}


def status(as_json: bool = False) -> int:
    data = collect()
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    cfg, info = data["config"], data["service"]
    service_server = next((s for s in data["servers"] if s["kind"] == "service"), None)
    service_state = info.get("ActiveState", "not-installed") if info.get("LoadState") != "not-installed" \
        else "not installed"
    fields = [
        ("Mode", f"{cfg.get('mode')}" + (" (model turns rotate across ready profiles)" if cfg.get("mode") == "rotating"
                                          else " (one account; no rotation)")),
        ("Phone account", cfg.get("profile") or "-"),
        ("Service", f"{service_state} ({info.get('SubState', '-')}), enabled={info.get('UnitFileState', '-')}, "
                    f"restarts={info.get('NRestarts', '0')}"),
    ]
    if service_server:
        st = service_server.get("status") or {}
        fields += [
            ("Connection", st.get("status", service_server.get("error", "unknown"))),
            ("Host name", st.get("serverName", "-")),
            ("Environment", st.get("environmentId") or "-"),
            ("Server", f"pid {service_server['pid']}, up {service_server['uptime']}"),
        ]
        clients = service_server.get("clients") or []
        if clients:
            for client in clients:
                seen = fmt.age(client.get("lastSeenAt"))
                fields.append(("Paired client", f"{client.get('displayName', client.get('clientId'))} "
                                                f"({client.get('platform', '?')} app {client.get('appVersion', '?')}), last seen {seen}"))
        else:
            fields.append(("Paired client", "none yet — run `mycodex remote pair`"))
    elif service_state in ACTIVE:
        fields.append(("Connection", "starting (server not up yet)"))
    if cfg.get("mode") == "rotating" and service_state in ACTIVE:
        port = data["serve_state"].get("proxy_port")
        health = proxy.health(port) if port else None
        if health:
            fields.append(("Rotation proxy", f"127.0.0.1:{port}, serving {health.get('current')}, "
                                             f"{health.get('requests', 0)} request(s), {health.get('switches', 0)} switch(es)"))
        else:
            fields.append(("Rotation proxy", "not answering" if port else "unknown (service predates this mycodex)"))
        settings = config.load()
        pool = [p for p in profiles.all(settings) if p.logged_in and p.name not in settings["rotation"]["disabled"]]
        quotas = quota.fetch_all(pool)
        paused = state.blocks(max_age=0)
        ready = [f"{p.name} ({fmt.windows(quotas[p.name])})" for p in pool
                 if quotas[p.name].eligible and p.name not in paused]
        fields.append(("Rotation pool", ", ".join(ready) or "no ready account — turns fail until a reset"))
    fields += [
        ("Working dir", cfg.get("cwd") or str(paths.HOME)),
        ("Switch phone account if unhealthy", "on" if cfg.get("failover") else "off"),
        ("Threads shown", "openai-tagged threads: the same list as every terminal session"),
    ]
    others = [s for s in data["servers"] if s["kind"] != "service"]
    for other in others:
        st = (other.get("status") or {}).get("status", other.get("error", "?"))
        fields.append(("Conflict", f"{other['kind']} pid {other['pid']} ({other['profile']}), {st}"))
    ui.panel("Mycodex Phone Connection", fields)
    return 0


# ----------------------------------------------------------------------------- server access
def server_socket() -> tuple[str, str | None]:
    """Control socket of the running remote-control server (the service's first) and its account."""
    server = remote.service_server()
    if server and server["socket"]:
        return server["socket"], server["proc"].profile
    for candidate in remote.find_servers():
        if candidate["socket"]:
            return candidate["socket"], candidate["proc"].profile
    raise SystemExit("Error: no remote-control server is running (start one with `mycodex remote start`)")


def socket_path() -> int:
    """Print the control socket, for `mycodex app-server proxy --sock "$(mycodex remote socket)"`."""
    sock, _ = server_socket()
    print(sock)
    return 0


def pair(wait: bool = True) -> int:
    sock, profile = server_socket()
    with appserver.AppServer(sock, timeout=30) as server:
        result = server.result("remoteControl/pairing/start", {"manualCode": True})
        code = result.get("manualPairingCode") or result.get("pairingCode")
        expires = result.get("expiresAt")
        ui.panel("Mycodex Pairing", [
            ("Pairing code", code),
            ("Account", f"{profile} — open the ChatGPT app signed into this account"),
            ("Expires", time.strftime("%H:%M:%S", time.localtime(expires)) if expires else "-"),
        ], styler=lambda label, value: ui.Style(ui.LIGHT_CYAN, bold=True) if label == "Pairing code" else ui.PRIMARY)
        if not wait or not ui.is_tty():
            return 0
        box = ui.StatusBox("Mycodex Pairing")
        deadline = min(time.time() + 600, expires or time.time() + 600)
        while time.time() < deadline:
            box.update("waiting", "enter the code in the ChatGPT app (Ctrl-C to stop waiting)")
            try:
                claimed = server.result("remoteControl/pairing/status", {"manualPairingCode": code}).get("claimed")
            except appserver.AppServerError:
                claimed = False
            if claimed:
                box.clear()
                ui.success("paired")
                return 0
            time.sleep(3)
        box.clear()
        ui.warn("pairing code expired; run `mycodex remote pair` again")
        return 1


def clients(revoke: str | None = None) -> int:
    sock, _ = server_socket()
    with appserver.AppServer(sock, timeout=30) as server:
        status_ = server.result("remoteControl/status/read")
        env = status_.get("environmentId")
        if revoke:
            server.result("remoteControl/client/revoke", {"environmentId": env, "clientId": revoke})
            ui.success(f"revoked {revoke}")
            return 0
        data = server.result("remoteControl/client/list", {"environmentId": env}).get("data", [])
    rows = [[c.get("clientId", "-"), c.get("displayName", "-"), c.get("platform", "-"), c.get("appVersion", "-"),
             fmt.age(c.get("lastSeenAt"))] for c in data]
    ui.table("Mycodex Paired Devices", [ui.Column("DEVICE ID"), ui.Column("NAME"), ui.Column("PLATFORM"),
                                        ui.Column("APP"), ui.Column("LAST SEEN")], rows,
             subtitle=status_.get("serverName"))
    return 0


def logs(follow: bool, lines: int) -> int:
    args = ["journalctl", "--user", "-u", paths.REMOTE_UNIT, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    if follow:
        args.append("-f")
    return subprocess.call(args, env=paths.tool_env())


def seed(directory: str | None, name: str | None, message: str | None, project_name: str | None = None,
         wait: bool = True) -> int:
    """Start a new thread inside a project, through the running server's app-server API.

    Codex files a thread under a project only when the client passes `projectId` (the TUI
    and `codex exec` never do), and saves a thread once its first turn runs. So: find the
    project whose root is DIR (or create it), `thread/start` there with that project,
    name the thread, and start its first turn with MESSAGE. The turn keeps running on the
    server if we stop waiting."""
    root_path = Path(directory or os.getcwd()).expanduser().resolve()
    if not root_path.is_dir():
        ui.error(f"{root_path} is not a directory")
        return 1
    root = str(root_path)
    sock, profile = server_socket()
    with appserver.AppServer(sock, timeout=60) as server:
        from . import projects
        project, created = projects.ensure(server, root_path, project_name)
        thread = server.result("thread/start", {"cwd": root, "projectId": project["id"],
                                                "serviceName": "mycodex"})["thread"]
        title = name or root_path.name
        server.result("thread/name/set", {"threadId": thread["id"], "name": title})
        text = message or projects.READY_MESSAGE
        started = server.result("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": text}]})
        turn_id = started["turn"]["id"]
        status = "running on the server"
        if wait:
            box = ui.StatusBox("Mycodex Seed")
            box.update("turn", f"waiting for the first turn on {thread['id']} (Ctrl-C stops waiting, not the turn)")
            try:
                done = server.wait_for("turn/completed", timeout=180,
                                       predicate=lambda note: note.get("params", {}).get("threadId") == thread["id"]
                                       and note.get("params", {}).get("turn", {}).get("id") == turn_id)
            except KeyboardInterrupt:
                done = None
            box.clear()
            turn = (done or {}).get("params", {}).get("turn", {})
            status = turn.get("status") or status
    ui.panel("Mycodex Seed", [
        ("Project", f"{project['name']} ({project['id']})" + (", created" if created else "")),
        ("Thread", thread["id"]),
        ("Name", title),
        ("Provider tag", thread.get("modelProvider") or "-"),
        ("First turn", status),
        ("Phone account", profile or "-"),
        ("Continue", f"on the phone (project {project['name']}) or `mycodex resume {thread['id']}`"),
    ])
    return 1 if status in ("failed", "interrupted") else 0


def foreground(profile: str | None, mode: str | None) -> int:
    """Run the server attached to this terminal (debugging); refuses while the service runs."""
    if _active():
        ui.error("the remote-control service is running", ["mycodex remote stop", "then retry --foreground"])
        return 1
    cfg = config.load()
    names = profiles.names(cfg)
    if not names:
        ui.error("no profiles yet", ["mycodex profile add"])
        return 1
    chosen = resolve.resolve(profile or cfg["remote"].get("profile") or profiles.active_name(cfg) or names[0], names, cfg)
    mode = mode or cfg["remote"].get("mode") or "rotating"
    return remote.run_server(profiles.get(chosen, cfg), mode, cfg, service=False)
