"""`mycodex doctor`: verify the codex + mycodex stack and repair what is safe."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

from . import (TESTED_CODEX_SERIES, codex, config, fmt, migrate, paths, procs, profiles, proxy, quota, remote,
               remote_cmd, state, threads, ui)
from . import maintenance

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, check: str, result: str, detail: str) -> None:
        self.rows.append((check, result, detail))


def run(fix: bool = False, assume_yes: bool = False, as_json: bool = False) -> int:
    report = Report()
    cfg = config.load()

    for item in maintenance.audit(fix=fix):
        if item.get("error") or item["legacy"]:
            remaining = item["legacy"] - item["repaired"]
            report.add("rollout paths", WARN if remaining or item.get("error") else OK,
                       f"{item['database']}: legacy {item['legacy']}, repaired {item['repaired']}"
                       + (f"; backup {item['backup']}" if item["backup"] else "")
                       + (f"; unresolved {item['unresolved']}" if item["unresolved"] else "")
                       + (f"; {item['error']}" if item.get("error") else "")
                       + (" (mycodex doctor --fix-rollout-paths)" if remaining and not fix else ""))

    # -- codex ------------------------------------------------------------------------------
    codex_version = codex.version()
    pkg = codex.package_info()
    if not codex_version:
        report.add("codex", FAIL, "official codex CLI not found on PATH or ~/.local/bin")
    else:
        tested = codex_version.startswith(TESTED_CODEX_SERIES)
        latest = (pkg.get("update_check") or {}).get("latest_version")
        report.add("codex", OK if tested else WARN,
                   f"{codex_version} at {pkg.get('binary', '?')}"
                   + ("" if tested else f" (mycodex verified with {TESTED_CODEX_SERIES}x)")
                   + (f"; update {latest} available" if latest and latest != codex_version else ""))

    # -- mycodex's own files: everything in MYCODEX_HOME, only links outside --------------
    outside = []
    if not (paths.PATH_LINK.is_symlink() and Path(os.readlink(paths.PATH_LINK)) == paths.LAUNCHER):
        outside.append(f"{paths.PATH_LINK} should link to {paths.LAUNCHER} (run {paths.MYCODEX_HOME}/install.sh)")
    if paths.REMOTE_UNIT_FILE.exists() and not paths.REMOTE_UNIT_FILE.is_symlink():
        if fix:
            remote.install_unit(cfg["remote"].get("cwd") or str(paths.HOME))
        else:
            outside.append(f"{paths.REMOTE_UNIT_FILE} is a copy, not a link to {paths.UNIT_SOURCE}")
    legacy = [str(d) for d in (paths.LEGACY_CONFIG_DIR, paths.LEGACY_STATE_DIR) if d.exists()]
    if legacy and fix:
        paths.migrate_legacy()
        legacy = [str(d) for d in (paths.LEGACY_CONFIG_DIR, paths.LEGACY_STATE_DIR) if d.exists()]
    outside += [f"old location still present: {d}" for d in legacy]
    report.add("mycodex files", WARN if outside else OK,
               "; ".join(outside) + " (mycodex doctor --fix)" if outside
               else f"all in {paths.MYCODEX_HOME}; links: {paths.PATH_LINK}, {paths.REMOTE_UNIT_FILE}")

    # -- accounts ---------------------------------------------------------------------------
    all_profiles = profiles.all(cfg)
    root = paths.PROFILES_ROOT
    if root.exists() and stat.S_IMODE(root.stat().st_mode) & 0o077:
        if fix:
            os.chmod(root, 0o700)
        report.add("profiles dir", OK if fix else WARN,
                   f"{root} mode fixed to 0700" if fix else f"{root} is readable by others (mycodex doctor --fix)")
    report.add("profiles", OK if all_profiles else WARN,
               f"{len(all_profiles)} in {root}" if all_profiles else "none (mycodex profile add, or mycodex migrate)")
    leftovers = [i for i in migrate.plan() if i["action"] in ("move", "conflict")]
    if leftovers:
        report.add("prodex accounts", WARN, ", ".join(i["name"] for i in leftovers)
                   + " still live in ~/.prodex/profiles (mycodex migrate)")
    elif paths.PRODEX_PROFILES.is_dir():
        report.add("prodex accounts", INFO, "all migrated; ~/.prodex/profiles/<name> link to the new homes")

    quotas = quota.fetch_all([p for p in all_profiles if p.logged_in])
    paused = state.blocks(max_age=0)
    for p in all_profiles:
        claims = profiles.identity(p.home)
        if not claims.get("present"):
            report.add("account", FAIL, f"{p.name}: no auth.json (mycodex profile reauth {p.name})")
            continue
        q = quotas[p.name]
        level = {"ready": OK, "limited": WARN, "auth invalid": FAIL}.get(q.status, WARN)
        detail = f"{p.name}: {claims.get('plan') or q.plan or '?'} plan, {q.status}"
        expires = claims.get("access_expires")
        if expires:
            detail += f", token valid until {fmt.reset_time(expires)}"
        if not claims.get("has_refresh_token"):
            detail += ", NO refresh token"
            level = FAIL
        if p.name in paused:
            detail += f"; paused until {fmt.reset_time(paused[p.name][0])} ({paused[p.name][1]})"
        if q.error:
            detail += f"; {q.error}"
        if level == FAIL:
            detail += f" → mycodex profile reauth {p.name}"
        mode = stat.S_IMODE((p.home / "auth.json").stat().st_mode)
        if mode & 0o077:
            if fix:
                os.chmod(p.home / "auth.json", 0o600)
            detail += "; auth.json mode " + ("fixed to 0600" if fix else f"{mode:o} is too open (doctor --fix)")
            if not fix and level == OK:
                level = WARN
        report.add("account", level, detail)

    # -- shared storage ---------------------------------------------------------------------
    for p in all_profiles:
        result = profiles.prepare_home(p.home, adopt=fix, dry_run=not fix)
        parts = []
        if result.linked:
            parts.append(("linked " if fix else "missing links: ") + ", ".join(result.linked))
        if result.adopted:
            parts.append(("shared " if fix else "can share: ") + ", ".join(result.adopted))
        if result.private:
            parts.append("private (not shared): " + ", ".join(result.private))
        level = OK if not parts or (fix and not result.private) else WARN
        report.add("shared state", level, f"{p.name}: " + ("; ".join(parts) if parts else
                                                            "threads, history, config and SQLite linked to ~/.codex")
                   + ("" if fix or not (result.linked or result.adopted) else " (mycodex doctor --fix)"))

    all_procs = procs.scan()
    unshared = [pr for pr in all_procs if pr.env.get("CODEX_HOME") and "/profiles/" in pr.env["CODEX_HOME"]
                and pr.env.get("CODEX_SQLITE_HOME") != str(paths.SHARED_CODEX_HOME) and pr.role.startswith(("codex", "daemon"))]
    report.add("thread database", WARN if unshared else OK,
               (f"{len(unshared)} process(es) use a profile home without CODEX_SQLITE_HOME: "
                + ", ".join(str(pr.pid) for pr in unshared)) if unshared
               else "all profile processes share ~/.codex SQLite state")
    prodex_procs = [pr for pr in all_procs if pr.role.startswith("prodex") or pr.role == "rotation proxy"]
    if prodex_procs:
        report.add("prodex", INFO, "running but not used by mycodex: "
                   + ", ".join(f"{pr.pid} {pr.role}" for pr in prodex_procs))

    # -- sockets / permissions --------------------------------------------------------------
    sock_dir = paths.daemon_socket_dir()
    if sock_dir.exists():
        st = sock_dir.stat()
        good = st.st_uid == os.getuid() and not (st.st_mode & 0o022)
        report.add("socket dir", OK if good else FAIL, f"{sock_dir} mode {oct(stat.S_IMODE(st.st_mode))}")
    else:
        report.add("socket dir", INFO, f"{sock_dir} not created yet")
    mask = codex.umask()
    group_writable = not (mask & 0o020)
    report.add("umask", INFO if group_writable else OK,
               f"{mask:04o}" + (" — plain `codex remote-control` refuses its group-writable socket dir here; "
                                "the mycodex service and --foreground use umask 0022" if group_writable else ""))

    # -- remote -----------------------------------------------------------------------------
    info = remote.service_info()
    if info.get("LoadState") == "not-installed":
        report.add("remote service", INFO, "not installed (mycodex remote start)")
    else:
        active = info.get("ActiveState")
        report.add("remote service", OK if active == "active" else WARN,
                   f"{active}/{info.get('SubState')}, enabled={info.get('UnitFileState')}, restarts={info.get('NRestarts')}, "
                   f"mode={cfg['remote'].get('mode')}, relay={cfg['remote'].get('profile')}")
        if cfg["remote"].get("profile") not in {p.name for p in all_profiles}:
            report.add("remote relay", FAIL, f"relay {cfg['remote'].get('profile')!r} is not a profile")
        serve = remote.read_serve_state()
        if active == "active" and cfg["remote"].get("mode") == "rotating":
            port = serve.get("proxy_port")
            health = proxy.health(port) if port else None
            report.add("remote proxy", OK if health else WARN,
                       f"127.0.0.1:{port} serving {health.get('current')}, {health.get('requests', 0)} request(s)"
                       if health else "the service has no answering rotation proxy (mycodex remote restart)")
    try:
        data = remote_cmd.collect()
        for server in data["servers"]:
            st = (server.get("status") or {}).get("status", server.get("error", "unknown"))
            level = OK if st == "connected" and server["kind"] == "service" else WARN
            report.add("remote link" if server["kind"] == "service" else f"remote {server['kind']}", level,
                       f"pid {server['pid']} ({server['profile']}): {st}; clients {len(server.get('clients') or [])}"
                       + ("" if server["kind"] == "service" else " — conflicts with the mycodex service"))
    except Exception as exc:  # status is best effort
        report.add("remote status", WARN, f"could not query: {exc}")
    for p in all_profiles:
        ds = codex.daemon_state(p.home)
        if ds.remote_control_setting and not ds.alive:
            msg = f"{p.name}: daemon setting remoteControlEnabled=true while stopped (a plain `codex` run would re-enable it)"
            if fix and ui.confirm(f"Persist remote control off for {p.name}'s codex daemon?", assume_yes=assume_yes):
                result = codex.run_codex(["app-server", "daemon", "disable-remote-control"], home=p.home)
                report.add("stale daemon setting", OK if result.returncode == 0 else WARN,
                           f"{p.name}: fixed" if result.returncode == 0 else msg)
            else:
                report.add("stale daemon setting", WARN, msg + " (mycodex doctor --fix)")
    for home in [paths.SHARED_CODEX_HOME, *[p.home for p in all_profiles]]:
        ds = codex.daemon_state(home)
        if ds.updater_alive and not ds.alive and ds.updater_pid:
            label = home.name if home != paths.SHARED_CODEX_HOME else "~/.codex"
            if fix and ui.confirm(f"Stop orphan daemon updater {ds.updater_pid} ({label})?", assume_yes=assume_yes):
                os.kill(ds.updater_pid, signal.SIGTERM)
                report.add("orphan updater", OK, f"stopped {ds.updater_pid} ({label})")
            else:
                report.add("orphan updater", WARN, f"pid {ds.updater_pid} ({label}) runs without its daemon (mycodex doctor --fix)")

    # -- auth errors, linger, thread tags ---------------------------------------------------
    recent = codex.recent_log_errors(3600, "%token_invalidated%") + codex.recent_log_errors(3600, "%refresh_token_reused%")
    report.add("auth errors", WARN if recent else OK,
               f"{len(recent)} token invalidation(s) in the last hour (codex logs)" if recent else "none in the last hour")
    try:
        linger = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        linger = ""
    report.add("linger", OK if linger.endswith("yes") else WARN,
               "user services survive logout and reboot" if linger.endswith("yes")
               else "run `sudo loginctl enable-linger $USER` so the remote service survives logout")

    tags: dict[str, int] = {}
    rows = codex.threads(limit=5000)
    for thread in rows:
        if not thread.get("archived"):
            tag = thread.get("model_provider") or "?"
            tags[tag] = tags.get(tag, 0) + 1
    invisible = threads.hidden(rows)
    adopted = len(state.adoptions())
    report.add("thread tags", WARN if invisible else OK,
               (", ".join(f"{k}={v}" for k, v in sorted(tags.items())) or "no threads")
               + (f"; {len(invisible)} hidden from the phone (mycodex threads adopt <ID>)" if invisible else
                  "; every live thread is visible to the phone and every terminal")
               + (f"; {adopted} adopted" if adopted else ""))

    # -- render -----------------------------------------------------------------------------
    worst = FAIL if any(r == FAIL for _, r, _ in report.rows) else (WARN if any(r == WARN for _, r, _ in report.rows) else OK)
    if as_json:
        print(json.dumps({"result": worst, "checked_at": int(time.time()),
                          "checks": [{"check": c, "result": r, "detail": d} for c, r, d in report.rows]}, indent=2))
        return 1 if worst == FAIL else 0
    styles = {OK: ui.SUCCESS, WARN: ui.WARNING, FAIL: ui.ERROR, INFO: ui.SECONDARY}

    def styler(_label: str, value: str) -> ui.Style:
        return styles.get(value[1:value.find("]")] if value.startswith("[") else "", ui.PRIMARY)

    ui.panel("Mycodex Doctor", [(check, f"[{result}] {detail}") for check, result, detail in report.rows],
             subtitle=f"overall {worst}", styler=styler)
    return 1 if worst == FAIL else 0
