"""`mycodex remote ...` and `mycodex [--profile P] remote-control`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import appserver, codex, config, fmt, model_cache, paths, procs, profiles, proxy, quota, remote, resolve, state, ui

def _active() -> bool:
    return remote.service_info().get("ActiveState") in remote.ACTIVE_STATES


def _validate_policy(value: str | None, valid: tuple[str, ...], flag: str, allow_default: bool = False) -> str | None:
    if value is None:
        return None
    if allow_default and value == "default":
        return "default"
    if value not in valid:
        choices = ", ".join((*valid, "default") if allow_default else valid)
        raise SystemExit(f"Error: unknown {flag} '{value}' (choose: {choices})")
    return value


def _warn_if_sandbox_unready(effective_sandbox: str | None) -> dict[str, Any]:
    sb = codex.sandbox_status()
    if not sb["ready"] and effective_sandbox != "danger-full-access":
        ui.warn(f"sandbox readiness: {sb['summary']}; pass `--sandbox danger-full-access --approval never` if mobile shell commands fail")
    return sb


def restart_if_active(wait: bool = True, reason: str = "project change", quiet: bool = False) -> tuple[bool, bool]:
    """Restart the managed remote-control service if it is running so phones see fresh state."""
    if not paths.REMOTE_UNIT_FILE.exists() or not _active():
        return False, False
    box = None if quiet else ui.StatusBox("Mycodex Phone Connection")
    if box:
        box.update("restarting", f"refreshing remote-control service ({reason})")
    res = remote._systemctl("restart", paths.REMOTE_UNIT)
    if res.returncode != 0:
        if box:
            box.clear()
        if not quiet:
            detail = (res.stderr or res.stdout).strip()
            ui.warn("could not automatically restart remote-control service" + (f": {detail}" if detail else ""))
        return False, False
    if not wait:
        if box:
            box.clear()
        return True, False
    connected = remote.wait_until_connected(box=box)
    if box:
        box.clear()
    return True, bool(connected)


# ----------------------------------------------------------------------------- start
def start(profile: str | None = None, mode: str | None = None, cwd: str | None = None,
          failover: bool | None = None, force: bool = False, wait: bool = True,
          restart: bool = False, sandbox: str | None = None, approval: str | None = None) -> int:
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
    sandbox_arg = _validate_policy(sandbox, config.SANDBOX_MODES, "--sandbox", allow_default=True)
    approval_arg = _validate_policy(approval, config.APPROVAL_POLICIES, "--approval", allow_default=True)
    effective_sandbox = (None if sandbox_arg == "default" else sandbox_arg) if sandbox_arg is not None else cfg["remote"].get("sandbox")
    effective_approval = (None if approval_arg == "default" else approval_arg) if approval_arg is not None else cfg["remote"].get("approval")
    workdir = str(Path(cwd or cfg["remote"].get("cwd") or paths.HOME).expanduser().resolve())
    if not Path(workdir).is_dir():
        ui.error(f"working directory {workdir} does not exist")
        return 1
    if failover is None:
        # A new start uses the CLI default; restart preserves the saved explicit choice.
        failover = cfg["remote"].get("failover", True) if restart else True

    _warn_if_sandbox_unready(effective_sandbox)

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
    changed = (previous.get("profile"), previous.get("mode"), previous.get("sandbox"), previous.get("approval")) \
        != (chosen, mode, effective_sandbox, effective_approval) \
        or cfg["remote"].get("cwd") != workdir or cfg["remote"].get("failover") != failover \
        or cfg["remote"].get("sandbox") != effective_sandbox or cfg["remote"].get("approval") != effective_approval
    with config.editing() as data:
        data["remote"].update({"profile": chosen, "mode": mode, "cwd": workdir, "failover": failover,
                               "sandbox": effective_sandbox, "approval": effective_approval})
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
    stop_res = remote._systemctl("stop", paths.REMOTE_UNIT)
    disable_res = remote._systemctl("disable", paths.REMOTE_UNIT)
    if stop_res.returncode != 0 or disable_res.returncode != 0:
        if not quiet:
            detail = (stop_res.stderr or disable_res.stderr or stop_res.stdout or disable_res.stdout).strip()
            ui.error("could not stop/disable the remote-control service" + (f": {detail}" if detail else ""))
        return 1
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
        copied = model_cache.sync(source, target, refresh=True)
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
    elif service_state in remote.ACTIVE_STATES:
        fields.append(("Connection", "starting (server not up yet)"))
    if cfg.get("mode") == "rotating" and service_state in remote.ACTIVE_STATES:
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
    sb = codex.sandbox_status()
    sb_detail = f"sandbox={cfg.get('sandbox') or 'default'}, approval={cfg.get('approval') or 'default'}; {sb['summary']}"
    fields += [
        ("Working dir", cfg.get("cwd") or str(paths.HOME)),
        ("Sandbox / approval", sb_detail),
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
            except appserver.AppServerClosedError as exc:
                box.clear()
                ui.error(f"remote-control server disconnected: {exc}")
                return 1
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


def _wait_for_turn(server: appserver.AppServer, title: str, thread_id: str, turn_id: str, label: str) -> str:
    status = "running on the server"
    box = ui.StatusBox(title)
    box.update("turn", f"waiting for {label} on {thread_id} (Ctrl-C stops waiting, not the turn)")
    try:
        done = server.wait_for("turn/completed", timeout=180,
                               predicate=lambda note: note.get("params", {}).get("threadId") == thread_id
                               and note.get("params", {}).get("turn", {}).get("id") == turn_id)
    except KeyboardInterrupt:
        done = None
    except appserver.AppServerError as exc:
        box.clear()
        return f"disconnected ({exc})"
    box.clear()
    turn = (done or {}).get("params", {}).get("turn", {})
    return turn.get("status") or status


def dispatch_turn(server: appserver.AppServer, thread_id: str, text: str) -> str:
    """Resume `thread_id` onto the running server and start a new text turn."""
    server.result("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    started = server.result("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})
    return started["turn"]["id"]


def feed_thread(thread_id: str, message: str | None, wait: bool = True) -> int:
    """Send a new user turn to an existing OpenAI-tagged conversation through the running server."""
    from . import threads

    text = (message or "").strip()
    if not text:
        ui.error("message text is required when feeding an existing thread",
                 [f'mycodex feed {thread_id} --message "Try again" --no-wait'])
        return 2
    thread = threads._find(thread_id)
    if thread.get("archived"):
        ui.error("the thread is archived; unarchive it in Codex before feeding it")
        return 1
    if (thread.get("model_provider") or threads.OPENAI) != threads.OPENAI:
        ui.error("this thread uses another provider; adopt it first (`mycodex threads adopt`)")
        return 1
    sock, profile = server_socket()
    with appserver.AppServer(sock, timeout=60) as server:
        turn_id = dispatch_turn(server, thread["id"], text)
        status = _wait_for_turn(server, "Mycodex Feed", thread["id"], turn_id, f"turn {turn_id}") if wait else "running on the server"
    ui.panel("Mycodex Feed", [
        ("Thread", thread["id"]),
        ("Turn", turn_id),
        ("Status", status),
        ("Phone account", profile or "-"),
        ("Continue", f"on the phone or `mycodex resume {thread['id']}`"),
    ])
    return 1 if status in ("failed", "interrupted") or status.startswith("disconnected") else 0


def _looks_like_thread_target(value: str | None) -> bool:
    if not value:
        return False
    candidate = value.strip()
    if Path(candidate).expanduser().exists():
        return False
    hex_chars = candidate.replace("-", "")
    return len(candidate) >= 8 and all(c in "0123456789abcdefABCDEF" for c in hex_chars)


def feed_or_seed(target: str | None, name: str | None, message: str | None, project_name: str | None = None,
                 wait: bool = True, sandbox: str | None = None, approval: str | None = None,
                 trust: bool = False, restart: bool = True) -> int:
    """Feed an existing thread when `target` is a thread ID/prefix, otherwise seed a project."""
    if _looks_like_thread_target(target):
        unsupported = [flag for flag, used in (("--name", name is not None), ("--project", project_name is not None),
                                               ("--sandbox", sandbox is not None), ("--approval", approval is not None),
                                               ("--trust", trust), ("--no-restart", not restart)) if used]
        if unsupported:
            ui.error(f"these options apply only when seeding a project directory, not an existing thread: {', '.join(unsupported)}",
                     [f'mycodex feed {target} --message "..." [--no-wait]'])
            return 2
        assert target is not None
        return feed_thread(target, message, wait=wait)
    return seed(target, name, message, project_name, wait=wait,
                sandbox=sandbox, approval=approval, trust=trust, restart=restart)


def seed(directory: str | None, name: str | None, message: str | None, project_name: str | None = None,
         wait: bool = True, sandbox: str | None = None, approval: str | None = None,
         trust: bool = False, restart: bool = True) -> int:
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
    cfg = config.load()
    sandbox_val = _validate_policy(sandbox, config.SANDBOX_MODES, "--sandbox")
    approval_val = _validate_policy(approval, config.APPROVAL_POLICIES, "--approval")
    effective_sandbox = sandbox_val or cfg["remote"].get("sandbox")
    effective_approval = approval_val or cfg["remote"].get("approval")
    sb = _warn_if_sandbox_unready(effective_sandbox)
    trusted = codex.trust_project(root_path) if trust else False

    root = str(root_path)
    sock, profile = server_socket()
    start_params: dict[str, Any] = {"cwd": root, "serviceName": "mycodex", "threadSource": "user"}
    if effective_sandbox:
        start_params["sandbox"] = effective_sandbox
    if effective_approval:
        start_params["approvalPolicy"] = effective_approval
    with appserver.AppServer(sock, timeout=60) as server:
        from . import projects
        project, created = projects.ensure(server, root_path, project_name)
        start_params["projectId"] = project["id"]
        thread = server.result("thread/start", start_params)["thread"]
        title = name or root_path.name
        server.result("thread/name/set", {"threadId": thread["id"], "name": title})
        text = message or projects.READY_MESSAGE
        started = server.result("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": text}]})
        turn_id = started["turn"]["id"]
        status = _wait_for_turn(server, "Mycodex Seed", thread["id"], turn_id, f"the first turn on {thread['id']}") if wait else "running on the server"

    restarted, connected = (False, False)
    if not restart:
        restart_detail = "disabled (--no-restart)"
    elif not wait or status == "running on the server" or status.startswith("disconnected"):
        restart_detail = "skipped while first turn is running/unresolved; run `mycodex remote restart` once finished"
    elif _active():
        restarted, connected = restart_if_active(wait=True, reason="project seeded")
        restart_detail = ("restarted (connected)" if connected else "restarted (connecting)") if restarted else "failed to restart"
    else:
        restart_detail = "not needed (no managed service)"

    sandbox_summary = f"{effective_sandbox or 'default'} / {effective_approval or 'default'} ({sb['summary']})"
    rows = [
        ("Project", f"{project['name']} ({project['id']})" + (", created" if created else "")),
        ("Thread", thread["id"]),
        ("Name", title),
        ("Provider tag", thread.get("modelProvider") or "-"),
        ("Sandbox", sandbox_summary),
        ("First turn", status),
        ("Phone account", profile or "-"),
        ("Remote service", restart_detail),
        ("Continue", f"on the phone (project {project['name']}) or `mycodex resume {thread['id']}`"),
    ]
    if trust:
        rows.insert(5, ("Workspace trust", "added to config.toml" if trusted else "already trusted"))
    ui.panel("Mycodex Seed", rows)
    return 1 if status in ("failed", "interrupted") or status.startswith("disconnected") else 0


def remote_doctor(as_json: bool = False) -> int:
    """Diagnose the remote-control relay, mobile visibility, sandboxing, trust, and error logs."""
    from . import doctor, projects, threads

    report = doctor.Report()
    data = collect()
    cfg, info = data["config"], data["service"]
    service_state = info.get("ActiveState", "not-installed") if info.get("LoadState") != "not-installed" else "not-installed"
    relay_name = cfg.get("profile")
    sb_cfg = cfg.get("sandbox") or "default"
    ap_cfg = cfg.get("approval") or "default"
    report.add("remote service",
               doctor.OK if service_state in remote.ACTIVE_STATES else doctor.WARN,
               f"{service_state}/{info.get('SubState', '-')}, enabled={info.get('UnitFileState', '-')}, "
               f"mode={cfg.get('mode')}, relay={relay_name or '-'}, sandbox={sb_cfg}, approval={ap_cfg}")

    all_profiles = profiles.all()
    relay_prof = next((p for p in all_profiles if p.name == relay_name), None) if relay_name else None
    if not relay_prof or not relay_prof.logged_in:
        report.add("relay account", doctor.FAIL, f"relay profile {relay_name!r} is missing or not logged in")
    else:
        q = quota.fetch(relay_prof)
        lvl = doctor.FAIL if q.status == "auth invalid" else (doctor.WARN if cfg.get("mode") == "pinned" and q.status != "ready" else doctor.OK)
        report.add("relay account", lvl, f"{relay_prof.name}: {q.status}" + (f" ({q.error})" if q.error else ""))

    if cfg.get("mode") == "rotating" and service_state in remote.ACTIVE_STATES:
        port = data["serve_state"].get("proxy_port")
        health = proxy.health(port) if port else None
        report.add("rotation proxy", doctor.OK if health else doctor.WARN,
                   f"127.0.0.1:{port} serving {health.get('current')}, {health.get('requests', 0)} req(s)"
                   if health else "rotation proxy not answering (mycodex remote restart)")

    service_server = next((s for s in data["servers"] if s["kind"] == "service"), None)
    socket_path = (service_server or {}).get("socket")
    proj_rows: list[dict[str, Any]] = []
    if not service_server or not socket_path:
        report.add("remote link", doctor.FAIL if service_state in remote.ACTIVE_STATES else doctor.WARN,
                   "no running remote-control server socket found (mycodex remote start)")
    else:
        st = (service_server.get("status") or {}).get("status", service_server.get("error", "unknown"))
        clients_list = service_server.get("clients") or []
        report.add("remote link", doctor.OK if st == "connected" else doctor.FAIL,
                   f"pid {service_server['pid']} ({service_server['profile']}): {st}, uptime {service_server['uptime']}")
        if clients_list:
            client_desc = ", ".join(
                f"{c.get('displayName', c.get('clientId'))} (app {c.get('appVersion', '?')}, seen {fmt.age(c.get('lastSeenAt'))})"
                for c in clients_list)
            report.add("paired clients", doctor.OK, client_desc)
        else:
            report.add("paired clients", doctor.WARN, "none paired yet (mycodex remote pair)")

        try:
            with appserver.AppServer(socket_path, timeout=20) as server:
                proj_rows = projects.pages(server, "project/list")
                root_paths = []
                missing_roots = []
                for p in proj_rows:
                    for r in p.get("roots", []):
                        raw = r.get("path")
                        if not raw:
                            continue
                        rp = Path(raw).expanduser().resolve()
                        root_paths.append(rp)
                        if not rp.is_dir():
                            missing_roots.append(f"{p.get('name')}:{raw}")
                report.add("projects", doctor.WARN if missing_roots else (doctor.OK if proj_rows else doctor.INFO),
                           f"{len(proj_rows)} registered"
                           + (f"; missing roots on disk: {', '.join(missing_roots)}" if missing_roots else ""))

                mobile_threads = projects.pages(server, "thread/list", {
                    "currentAccountOnly": True, "archived": False,
                    "sourceKinds": ["cli", "vscode", "exec", "appServer"],
                })
                loaded = projects.pages(server, "thread/loaded/list")
                no_project = []
                for t in mobile_threads:
                    if t.get("projectId") or not t.get("cwd"):
                        continue
                    cwd_path = Path(t["cwd"]).expanduser().resolve()
                    if any(cwd_path == rp or rp in cwd_path.parents for rp in root_paths):
                        no_project.append(t["id"][:8])
                report.add("mobile thread list", doctor.OK if mobile_threads else doctor.INFO,
                           f"{len(mobile_threads)} visible thread(s), {len(loaded)} loaded in memory"
                           + (f"; {len(no_project)} in project folders lack projectId (`mycodex threads link`)"
                              if no_project else ""))
        except appserver.AppServerError as exc:
            report.add("app-server probe", doctor.FAIL, f"RPC probe failed: {exc}")

    hidden_threads = threads.hidden()
    if hidden_threads:
        report.add("hidden threads", doctor.WARN,
                   f"{len(hidden_threads)} non-openai thread(s) invisible on mobile (`mycodex threads adopt <ID>`)")

    sb = codex.sandbox_status()
    sb_ok = sb["ready"] or cfg.get("sandbox") == "danger-full-access"
    report.add("sandbox / bwrap", doctor.OK if sb_ok else doctor.WARN,
               sb["summary"] + (f" (remote sandbox override: {cfg.get('sandbox')})" if cfg.get("sandbox")
                                else ("" if sb["ready"] else " — set `--sandbox danger-full-access --approval never` if mobile commands fail")))

    trusted = codex.trusted_projects()
    roots = [r.get("path") for p in proj_rows for r in p.get("roots", []) if r.get("path")]
    if not roots:
        roots = [r for p in codex.projects() for r in (p.get("roots") or "").split(";") if r]
    untrusted = [r for r in roots if trusted.get(codex._resolve_project_key(r), trusted.get(r)) != "trusted"]
    trust_warns = codex.recent_log_errors(86400, "%until the project is trusted%")
    global_state_exists = (paths.SHARED_CODEX_HOME / ".codex-global-state.json").is_file()
    report.add("workspace trust",
               doctor.WARN if (untrusted and trust_warns) else (doctor.INFO if untrusted else doctor.OK),
               (f"{len(untrusted)} project root(s) untrusted in config.toml (`--trust` on seed/add)"
                if untrusted else "all registered project roots trusted")
               + (f"; global-state={ 'present' if global_state_exists else 'absent' }"))

    auth_errs = codex.recent_log_errors(3600, "%token_invalidated%") + codex.recent_log_errors(3600, "%refresh_token_reused%")
    ws_errs = codex.recent_log_errors(3600, "%WebSocket protocol error%")
    proxy_errs = codex.recent_proxy_errors(since_seconds=3600)
    log_issues: list[str] = []
    if auth_errs:
        log_issues.append(f"{len(auth_errs)} auth error(s)")
    if proxy_errs:
        log_issues.append(f"{len(proxy_errs)} proxy error(s)")
    if ws_errs:
        log_issues.append(f"{len(ws_errs)} websocket reset(s)")
    report.add("logs / errors",
               doctor.WARN if (auth_errs or proxy_errs) else doctor.OK,
               ", ".join(log_issues) if log_issues else "no auth or proxy errors in recent Codex/proxy logs")

    return doctor.render(report, "Mycodex Remote Doctor", as_json)


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
