"""`mycodex status` and `mycodex processes`."""

from __future__ import annotations

import json

from . import __version__, codex, config, fmt, procs, profiles, proxy, quota, remote, remote_cmd, state, ui


def _who(profile: str | None) -> str:
    if not profile:
        return ""
    return profile if profile.startswith("(") else f"({profile})"


def processes(as_json: bool = False) -> int:
    all_procs = procs.scan()
    service_main = int(remote.service_info().get("MainPID") or 0)
    managed = {p.pid for p in procs.descendants(service_main, all_procs)} | ({service_main} if service_main else set())
    if as_json:
        print(json.dumps([{"pid": p.pid, "ppid": p.ppid, "role": p.role, "profile": p.profile,
                           "uptime_s": int(p.uptime), "tty": p.tty, "sockets": p.sockets,
                           "mycodex_service": p.pid in managed, "command": p.command[:300]}
                          for p in all_procs], indent=2))
        return 0
    rows = []
    for p in all_procs:
        detail = p.sockets[0] if p.sockets else " ".join(
            a for a in p.argv[1:] if a != "-c" and "openai_base_url" not in a and "model_provider" not in a)[:60]
        role = p.role + (" [service]" if p.pid in managed else "")
        rows.append([str(p.pid), role, p.profile or "-", procs.human_uptime(p.uptime), "yes" if p.tty else "-", detail])

    def style(i: int, row: list[str]) -> ui.Style:
        if i == 1:
            if "remote" in row[1] or "daemon (remote" in row[1]:
                return ui.ACCENT
            if row[1].startswith("codex"):
                return ui.SUCCESS
            return ui.PRIMARY
        if i == 2:
            return ui.Style(ui.CYAN)
        return ui.SECONDARY if i in (3, 4, 5) else ui.PRIMARY

    ui.table("Mycodex Processes", [ui.Column("PID", align="right"), ui.Column("ROLE"), ui.Column("PROFILE"),
                                   ui.Column("UP"), ui.Column("TTY"), ui.Column("DETAIL", max_width=60)],
             rows, subtitle=f"{len(all_procs)} process(es)", cell_style=style)
    return 0


def status(as_json: bool = False, with_quota: bool = True) -> int:
    cfg = config.load()
    all_profiles = profiles.all(cfg)
    quotas = quota.fetch_all(all_profiles) if with_quota else {}
    paused = state.blocks(max_age=0)
    remote_data = remote_cmd.collect()
    all_procs = procs.scan(with_sockets=False)
    tuis = [p for p in all_procs if p.role.startswith("codex ") and p.tty]
    daemons = [p for p in all_procs if p.role.startswith("daemon")]
    live = proxy.live_proxies()
    for entry in live:
        entry["health"] = proxy.health(entry["port"])
    service_server = next((s for s in remote_data["servers"] if s["kind"] == "service"), None)
    data = {
        "version": __version__, "codex": codex.version(),
        "active_profile": profiles.active_name(cfg),
        "profiles": {p.name: (quotas[p.name].status if p.name in quotas else None) for p in all_profiles},
        "paused": {n: {"until": u, "reason": r} for n, (u, r) in paused.items()},
        "rotation": cfg["rotation"], "remote": remote_data, "proxies": live,
        "terminal_sessions": [{"pid": p.pid, "profile": p.profile, "role": p.role} for p in tuis],
        "codex_daemons": [{"pid": p.pid, "role": p.role, "profile": p.profile} for p in daemons],
    }
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    ready = [n for n, q in quotas.items() if q.eligible and n not in paused]
    limited = [n for n, q in quotas.items() if q.status == "limited" or n in paused]
    broken = [n for n, q in quotas.items() if q.status == "auth invalid"]
    remote_state = remote_data["service"].get("ActiveState", "not installed") \
        if remote_data["service"].get("LoadState") != "not-installed" else "not installed"
    connection = (service_server or {}).get("status", {}).get("status", "-") if service_server else "-"
    clients = len((service_server or {}).get("clients") or [])
    others = [f"{s['kind']} {s['pid']} [{s['profile']}] {(s.get('status') or {}).get('status', '?')}, "
              f"clients {len(s.get('clients') or [])}" for s in remote_data["servers"] if s["kind"] != "service"]
    fields = [
        ("Active profile", data["active_profile"] or "none"),
        ("Profiles", f"{len(all_profiles)} total; ready {len(ready)}"
                     + (f"; limited {', '.join(limited)}" if limited else "")
                     + (f"; auth invalid {', '.join(broken)}" if broken else "")),
    ]
    for p in all_profiles:
        q = quotas.get(p.name)
        note = f" · paused until {fmt.reset_time(paused[p.name][0])}" if p.name in paused else ""
        fields.append((f"  {p.name}", (f"{q.status} · {fmt.windows(q)}" if q else "-") + note))
    sessions = []
    for entry in live:
        health = entry.get("health") or {}
        sessions.append(f"{entry['pid']} {entry.get('role')} (owner {entry.get('owner')}, "
                        f"now {health.get('current', '?')}, {health.get('switches', 0)} switch(es))")
    fields += [
        ("Rotation", ("enabled" if cfg["rotation"]["enabled"] else "disabled")
                     + (f"; order {' > '.join(cfg['rotation']['order'])}" if cfg["rotation"]["order"] else "")
                     + (f"; disabled {', '.join(cfg['rotation']['disabled'])}" if cfg["rotation"]["disabled"] else "")),
        ("Rotation proxies", "; ".join(sessions) or "none running"),
        ("Remote", f"mycodex service {remote_state}; mode {cfg['remote'].get('mode')}; "
                   f"relay {cfg['remote'].get('profile') or '-'}; connection {connection}; clients {clients}"),
        *([("Other remote", "; ".join(others))] if others else []),
        ("Terminal sessions", ", ".join(f"{p.pid} {_who(p.profile)}" for p in tuis) or "none"),
        ("Codex daemons", ", ".join(f"{p.pid} {p.role} {_who(p.profile)}" for p in daemons) or "none"),
        ("Versions", f"mycodex {__version__} · codex {data['codex'] or '?'}"),
    ]
    ui.panel("Mycodex Status", fields)
    return 0
