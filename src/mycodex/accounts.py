"""Profile, quota and rotation commands."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import Any

from . import auth, codex, config, fmt, paths, procs, profiles, quota, remote, resolve, state, ui
from .profiles import Profile


def _names(cfg: dict[str, Any] | None = None) -> list[str]:
    return profiles.names(cfg)


def _resolve(name: str, cfg: dict[str, Any]) -> Profile:
    target = resolve.resolve(name, _names(cfg), cfg)
    profile = profiles.get(target, cfg)
    assert profile is not None
    return profile


def remote_roles(cfg: dict[str, Any] | None = None, all_procs: list[procs.Proc] | None = None) -> dict[str, str]:
    """profile -> remote role (service relay or codex daemon)."""
    cfg = cfg or config.load()
    roles: dict[str, str] = {}
    all_procs = all_procs if all_procs is not None else procs.scan(with_sockets=False)
    service_state = remote.service_info().get("ActiveState")
    relay = cfg["remote"].get("profile")
    if relay and service_state in ("active", "activating", "reloading"):
        roles[relay] = f"relay ({cfg['remote'].get('mode')})"
    for proc in all_procs:
        if proc.role == "daemon (remote control)" and proc.profile:
            roles.setdefault(proc.profile, "codex daemon")
        if proc.role == "remote-control server" and proc.profile and proc.profile not in roles:
            roles[proc.profile] = "foreground server"
    return roles


def rotation_label(name: str, cfg: dict[str, Any], blocked: dict[str, tuple[int, str]] | None = None) -> str:
    blocked = state.blocks() if blocked is None else blocked
    if name in cfg["rotation"]["disabled"]:
        label = "disabled"
    elif name in blocked:
        label = f"paused until {fmt.reset_time(blocked[name][0])}"
    else:
        label = "enabled"
    if not cfg["rotation"]["enabled"]:
        label += " (global off)"
    return label


# ----------------------------------------------------------------------------- profile list
def profile_list(as_json: bool = False, with_quota: bool = True) -> int:
    cfg = config.load()
    all_profiles = profiles.all(cfg)
    quotas = quota.fetch_all(all_profiles) if with_quota else {}
    roles = remote_roles(cfg)
    blocked = state.blocks(max_age=0)
    if as_json:
        print(json.dumps([{
            "profile": p.name, "active": p.active, "home": str(p.home),
            "email": profiles.identity(p.home).get("email"),
            "plan": quotas[p.name].plan if p.name in quotas else None,
            "status": quotas[p.name].status if p.name in quotas else None,
            "quota": {w.name: {"remaining": w.remaining, "reset_at": w.reset_at}
                      for w in (quotas[p.name].windows if p.name in quotas else [])},
            "rotation": rotation_label(p.name, cfg, blocked),
            "remote": roles.get(p.name), "aliases": resolve.aliases_for(p.name, cfg),
        } for p in all_profiles], indent=2))
        return 0
    if not all_profiles:
        hints = [("Profiles", "none"), ("Next", "mycodex profile add")]
        if paths.PRODEX_PROFILES.is_dir():
            hints.append(("prodex accounts", "found in ~/.prodex/profiles: import them with `mycodex migrate`"))
        ui.panel("Mycodex Accounts", hints)
        return 0
    rows = []
    for p in all_profiles:
        q = quotas.get(p.name)
        plan = (q.plan if q and q.plan else None) or profiles.identity(p.home).get("plan") or "-"
        rows.append([
            ("* " if p.active else "  ") + p.name,
            plan,
            (fmt.status_label(q.status) if q else ("logged in" if p.logged_in else "no login")),
            fmt.windows(q) if q else "-",
            rotation_label(p.name, cfg, blocked),
            roles.get(p.name, "-"),
        ])
    columns = [ui.Column("ACCOUNT"), ui.Column("PLAN"), ui.Column("STATUS"), ui.Column("USAGE LEFT"),
               ui.Column("SWITCHING"), ui.Column("PHONE ROLE")]

    def style(i: int, row: list[str]) -> ui.Style:
        if i == 0 and row[0].startswith("*"):
            return ui.SUCCESS
        if i in (2, 4, 5):
            return ui.value_style("", row[i]) if row[i] != "-" else ui.SECONDARY
        if i == 1:
            return ui.Style(ui.CYAN)
        return ui.PRIMARY

    ui.table("Mycodex Accounts", columns, rows, subtitle=f"{len(all_profiles)} account(s)", cell_style=style,
             notes=["* usual account for new terminal sessions"])
    return 0


# ----------------------------------------------------------------------------- profile show
def profile_show(name: str, as_json: bool = False) -> int:
    cfg = config.load()
    profile = _resolve(name, cfg)
    claims = profiles.identity(profile.home)
    q = quota.fetch(profile)
    daemon = codex.daemon_state(profile.home)
    roles = remote_roles(cfg)
    order = cfg["rotation"]["order"]
    blocked = state.blocks(max_age=0)
    sharing = profiles.prepare_home(profile.home, dry_run=True)
    data = {
        "profile": profile.name, "active": profile.active, "home": str(profile.home),
        "auth": claims, "quota": {"status": q.status, "plan": q.plan, "error": q.error,
                                  "windows": [w.__dict__ for w in q.windows], "reset_credits": q.reset_credits},
        "rotation": rotation_label(profile.name, cfg, blocked),
        "paused": blocked.get(profile.name),
        "order_position": order.index(profile.name) + 1 if profile.name in order else None,
        "remote": roles.get(profile.name), "daemon": {"pid": daemon.pid, "alive": daemon.alive,
                                                      "remote_control_setting": daemon.remote_control_setting},
        "shared": {"missing": sharing.linked, "private": sharing.private},
        "aliases": resolve.aliases_for(profile.name, cfg),
    }
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    expires = claims.get("access_expires")
    fields = [
        ("Account", profile.name + ("  (active)" if profile.active else "")),
        ("Identity", claims.get("email") or "-"),
        ("Plan", f"{claims.get('plan') or q.plan or '-'}"
                 + (f" until {str(claims.get('subscription_until'))[:10]}" if claims.get("subscription_until") else "")),
        ("Login", "no login (mycodex profile reauth)" if not claims.get("present") else
                 ("api key" if claims.get("api_key") and not claims.get("has_refresh_token") else "chatgpt")
                 + (f", token valid until {fmt.reset_time(expires)}" if expires else "")),
        ("Status", fmt.status_label(q.status) + (f" — {q.error}" if q.error else "")),
        ("Usage left", fmt.windows(q)),
        ("Resets", fmt.resets(q)),
        ("Reset credits", str(q.reset_credits) if q.reset_credits is not None else "-"),
        ("Account switching", data["rotation"] + (f", order #{data['order_position']}" if data["order_position"] else "")
                     + (f" ({blocked[profile.name][1]})" if profile.name in blocked else "")),
        ("Phone role", roles.get(profile.name, "-")),
        ("Codex daemon", ("running" if daemon.alive else "not running")
                         + (", remote control on" if daemon.remote_control_setting else "")),
        ("Shared state", "all linked to ~/.codex" if sharing.ok and not sharing.linked else
                         "; ".join(filter(None, [f"missing: {', '.join(sharing.linked)}" if sharing.linked else "",
                                                 f"private: {', '.join(sharing.private)}" if sharing.private else ""]))),
        ("Short names", ", ".join(data["aliases"]) or "-"),
        ("Last refresh", str(claims.get("last_refresh") or "-")[:19]),
        ("Account folder", str(profile.home)),
    ]
    ui.panel(f"Mycodex Account {profile.name}", fields)
    return 0


def profile_current(as_json: bool = False) -> int:
    active = profiles.active_name()
    if not active:
        ui.error("no active profile", ["mycodex profile use <profile>"])
        return 1
    return profile_show(active, as_json)


def profile_use(name: str) -> int:
    with config.editing() as data:
        target = resolve.resolve(name, _names(data), data)
        data["active"] = target
    ui.success(f"New terminal sessions will use account {target}")
    return 0


# ----------------------------------------------------------------------------- login flows
def _codex_login(home: os.PathLike[str] | str, browser: bool) -> int:
    """The official `codex login` against a given CODEX_HOME, attached to this terminal."""
    argv = [paths.codex_bin(), "-c", 'cli_auth_credentials_store="file"', "login"]
    if not browser:
        argv.append("--device-auth")
    env = paths.tool_env({"CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(paths.SHARED_CODEX_HOME)})
    try:
        return subprocess.call(argv, env=env)
    except KeyboardInterrupt:
        return 130


def _same_account(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("account_id") and b.get("account_id"):
        return a["account_id"] == b["account_id"] and (not a.get("email") or a.get("email") == b.get("email"))
    return bool(a.get("email")) and a.get("email") == b.get("email")


def profile_add(name: str | None, browser: bool) -> int:
    cfg = config.load()
    if name and not profiles.valid_name(name):
        ui.error(f"'{name}' is not a valid profile name (letters, digits, . _ @ + -)")
        return 2
    if name and name in _names(cfg):
        ui.error(f"profile '{name}' already exists", [f"mycodex profile reauth {name}"])
        return 1
    pending = profiles.new_pending()
    try:
        code = _codex_login(pending, browser)
        who = profiles.identity(pending)
        if code != 0 or not who.get("present") or who.get("corrupt"):
            ui.error("login did not finish; nothing was added")
            return code or 1
        for existing in profiles.all(cfg):
            if _same_account(who, profiles.identity(existing.home)) and (not name or name == existing.name):
                auth.write(existing.home, auth.read(pending) or {})
                state.unblock(existing.name)
                ui.success(f"{who.get('email')} is already profile {existing.name}; its login was renewed")
                return 0
        target = name or profiles.slug(who.get("email") or "account")
        base, suffix = target, 2
        while target in _names(cfg):
            target, suffix = f"{base}-{suffix}", suffix + 1
        home = profiles.install(pending, target)
        pending = None
    finally:
        if pending is not None:
            profiles.discard(pending)
    with config.editing() as data:
        if not data.get("active") or data["active"] not in _names(data):
            data["active"] = target
    ui.success(f"profile {target} added ({who.get('email') or 'unknown email'}, {who.get('plan') or '?'} plan) at {home}")
    return 0


def profile_reauth(name: str, browser: bool) -> int:
    cfg = config.load()
    profile = _resolve(name, cfg)
    before = profiles.identity(profile.home)
    pending = profiles.new_pending()
    try:
        code = _codex_login(pending, browser)
        after = profiles.identity(pending)
        if code != 0 or not after.get("present") or after.get("corrupt"):
            ui.error("login did not finish; the existing login was left unchanged")
            return code or 1
        if before.get("present") and not before.get("corrupt") and not _same_account(before, after):
            ui.error(f"you logged in as {after.get('email')}, but {profile.name} belongs to {before.get('email')}; "
                     "nothing changed", [f"mycodex profile add            # add {after.get('email')} as a new profile"])
            return 1
        auth.write(profile.home, auth.read(pending) or {})
    finally:
        profiles.discard(pending)
    state.unblock(profile.name)
    ui.success(f"{profile.name} is logged in again ({after.get('email')})")
    return 0


def _stop_profile_daemon(profile: Profile) -> None:
    daemon = codex.daemon_state(profile.home)
    if daemon.alive:
        ok, message = remote.stop_official_daemon(profile.home)
        (ui.success if ok else ui.warn)(f"codex daemon for {profile.name}: {message or 'stopped'}")
    daemon = codex.daemon_state(profile.home)
    if daemon.updater_alive and not daemon.alive and daemon.updater_pid:
        os.kill(daemon.updater_pid, signal.SIGTERM)
        ui.success(f"stopped orphan daemon updater {daemon.updater_pid} for {profile.name}")


def profile_remove(name: str, assume_yes: bool, keep_home: bool, force: bool) -> int:
    cfg = config.load()
    profile = _resolve(name, cfg)
    service = remote.service_info().get("ActiveState")
    if cfg["remote"].get("profile") == profile.name and service in ("active", "activating"):
        if not force:
            ui.error(f"'{profile.name}' is the relay account of the running remote-control service",
                     ["mycodex remote start --profile <other>", "or pass --force to stop the service"])
            return 1
        remote_stop()
    users = [p for p in procs.scan(with_sockets=False)
             if p.profile == profile.name and p.role.startswith(("codex", "remote-control", "mycodex"))]
    if users and not force:
        ui.error(f"{profile.name} is in use by: " + ", ".join(f"{p.pid} {p.role}" for p in users),
                 ["close those sessions, or pass --force"])
        return 1
    what = "keep its folder and saved login under a hidden name (no sign-out)" if keep_home else "sign it out and delete its home"
    if not ui.confirm(f"Remove account {profile.name}: {what}?", assume_yes=assume_yes):
        ui.info("nothing changed")
        return 1
    _stop_profile_daemon(profile)
    if keep_home:
        moved = profiles.retire(profile.home)
        ui.info(f"kept its home at {moved}")
    else:
        logout = codex.run_codex(["logout"], home=profile.home, timeout=60)
        if logout.returncode != 0:
            ui.warn("codex logout failed (tokens were not revoked); deleting the home anyway")
        profiles.discard(profile.home)
    with config.editing() as data:
        data["rotation"]["order"] = [n for n in data["rotation"]["order"] if n != profile.name]
        data["rotation"]["disabled"] = [n for n in data["rotation"]["disabled"] if n != profile.name]
        data["aliases"] = {a: t for a, t in data["aliases"].items() if t != profile.name}
        if data["remote"].get("profile") == profile.name:
            data["remote"]["profile"] = None
        if data.get("active") == profile.name:
            remaining = _names(data)
            data["active"] = remaining[0] if remaining else None
    state.forget(profile.name)
    ui.success(f"removed profile {profile.name}")
    return 0


def profile_alias(name: str, alias: str | None, remove: bool) -> int:
    with config.editing() as data:
        if remove:
            target = data["aliases"].pop(name, None)
            if not target:
                ui.error(f"no alias '{name}'")
                return 1
            ui.success(f"removed alias {name} -> {target}")
            return 0
        target = resolve.resolve(name, _names(data), data)
        if not alias:
            ui.out(", ".join(resolve.aliases_for(target, data)) or "-")
            return 0
        if alias in _names(data):
            ui.error(f"'{alias}' is already a profile name")
            return 1
        data["aliases"][alias] = target
    ui.success(f"alias {alias} -> {target}")
    return 0


# ----------------------------------------------------------------------------- quota
def quota_table(name: str | None, as_json: bool) -> int:
    cfg = config.load()
    all_profiles = profiles.all(cfg)
    if name:
        all_profiles = [_resolve(name, cfg)]
    quotas = quota.fetch_all(all_profiles)
    blocked = state.blocks(max_age=0)
    if as_json:
        print(json.dumps({n: {"status": q.status, "plan": q.plan, "eligible": q.eligible, "error": q.error,
                              "reset_credits": q.reset_credits, "paused_until": (blocked.get(n) or (None,))[0],
                              "windows": [{"name": w.name, "remaining": w.remaining, "reset_at": w.reset_at}
                                          for w in q.windows]} for n, q in quotas.items()}, indent=2))
        return 0
    active = profiles.active_name(cfg)
    rows = []
    for p in all_profiles:
        q = quotas[p.name]
        usable = q.eligible and p.name not in cfg["rotation"]["disabled"] and p.name not in blocked
        rows.append([("* " if p.name == active else "  ") + p.name, q.plan or "-", fmt.status_label(q.status), fmt.windows(q),
                     fmt.resets(q), "yes" if usable else "no"])
    ready = sum(1 for row in rows if row[5] == "yes")

    def style(i: int, row: list[str]) -> ui.Style:
        if i in (2, 5):
            return ui.value_style("", row[i]) if row[i] != "no" else ui.WARNING
        if i == 1:
            return ui.Style(ui.CYAN)
        return ui.SECONDARY if i == 4 else ui.PRIMARY

    notes = [f"{n}: {q.error}" for n, q in quotas.items() if q.error]
    notes += [f"{n}: paused by the proxy until {fmt.reset_time(until)} ({why})" for n, (until, why) in blocked.items()
              if n in quotas]
    ui.table("Mycodex Usage", [ui.Column("ACCOUNT"), ui.Column("PLAN"), ui.Column("STATUS"),
                               ui.Column("LEFT"), ui.Column("RESETS"), ui.Column("CAN SWITCH TO")],
             rows, subtitle=f"{ready}/{len(rows)} ready", cell_style=style, notes=notes)
    return 0


def quota_cmd(name: str | None, as_json: bool, watch: float | None = None) -> int:
    if not watch or as_json:
        return quota_table(name, as_json)
    try:
        while True:
            if ui.is_tty():
                print("\x1b[2J\x1b[H", end="")
            quota_table(name, False)
            ui.info(f"refreshing every {watch:g}s (Ctrl-C to stop)")
            time.sleep(watch)
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------------- rotation
def rotation_status(as_json: bool) -> int:
    cfg = config.load()
    rot = cfg["rotation"]
    blocked = state.blocks(max_age=0)
    if as_json:
        print(json.dumps({**rot, "paused": {n: {"until": u, "reason": r} for n, (u, r) in blocked.items()}}, indent=2))
        return 0
    names = _names(cfg)
    order = [n for n in rot["order"] if n in names] + [n for n in names if n not in rot["order"]]
    quotas = quota.fetch_all([p for p in profiles.all(cfg) if p.name in order])
    ready = [n for n in order if quotas[n].eligible and n not in rot["disabled"] and n not in blocked]
    fields = [
        ("Account switching", "enabled" if rot["enabled"] else "disabled (launches run one account, no proxy)"),
        ("Switch below", f"{rot.get('min_quota_headroom', 0):g}% (fresh turns only; recent quota required)"),
        ("Spend resets automatically", "on" if rot.get("auto_redeem") else "off (earned reset credits preserved)"),
        ("Order", " > ".join(rot["order"]) if rot["order"] else "not set (most quota left first)"),
        ("Disabled", ", ".join(rot["disabled"]) or "none"),
        ("Ready now", ", ".join(ready) or "none"),
    ]
    for name, (until, reason) in sorted(blocked.items()):
        fields.append(("Paused", f"{name} until {fmt.reset_time(until)} — {reason}"))
    fields.append(("How", "new turns can move to an account with more usage left; a usage-limit error can also "
                          "trigger a retry before any output is sent to Codex"))
    ui.panel("Mycodex Account Switching", fields)
    return 0


def rotation_headroom(value: float | None) -> int:
    if value is None:
        ui.out(f"{config.load()['rotation']['min_quota_headroom']:g}%")
        return 0
    if not 0 <= value <= 100:
        ui.error("headroom must be a percentage from 0 to 100")
        return 2
    with config.editing() as data:
        data["rotation"]["min_quota_headroom"] = value
    ui.success(f"fresh-turn low-quota threshold is {value:g}% (0 disables proactive routing)")
    return 0


def rotation_auto_redeem(enable: bool) -> int:
    with config.editing() as data:
        data["rotation"]["auto_redeem"] = enable
    ui.success(f"automatic earned-reset redemption {'enabled' if enable else 'disabled'}")
    return 0


def rotation_set(enable: bool, targets: list[str]) -> int:
    with config.editing() as data:
        if not targets:
            data["rotation"]["enabled"] = enable
            ui.success(f"rotation {'enabled' if enable else 'disabled'} for mycodex launches")
            return 0
        names = _names(data)
        for target in targets:
            name = resolve.resolve(target, names, data)
            disabled = [n for n in data["rotation"]["disabled"] if n != name]
            if not enable:
                disabled.append(name)
            data["rotation"]["disabled"] = disabled
            ui.success(f"{name}: rotation {'enabled' if enable else 'disabled'}")
    return 0


def rotation_order(targets: list[str], clear: bool) -> int:
    with config.editing() as data:
        if clear:
            data["rotation"]["order"] = []
            ui.success("rotation order cleared (most quota left goes first)")
            return 0
        if not targets:
            ui.out(" > ".join(data["rotation"]["order"]) or "(not set)")
            return 0
        names = _names(data)
        order = []
        for target in targets:
            name = resolve.resolve(target, names, data)
            if name not in order:
                order.append(name)
        data["rotation"]["order"] = order
    ui.success("rotation order: " + " > ".join(order))
    return 0


def rotation_reset(targets: list[str]) -> int:
    cfg = config.load()
    names = [resolve.resolve(t, _names(cfg), cfg) for t in targets] or list(state.blocks(max_age=0))
    for name in names:
        state.unblock(name)
        ui.success(f"{name}: pause cleared")
    if not names:
        ui.info("no profile is paused")
    return 0


def rotation_log(lines: int, follow: bool) -> int:
    path = paths.PROXY_LOG
    if not path.exists():
        ui.info(f"no rotation events yet ({path})")
        return 0
    args = ["tail", "-n", str(lines)] + (["-F"] if follow else []) + [str(path)]
    try:
        return subprocess.call(args)
    except KeyboardInterrupt:
        return 0


def pick_start_profile(cfg: dict[str, Any]) -> str | None:
    """The active profile, else the first one in rotation order that is not paused."""
    names = _names(cfg)
    if not names:
        return None
    active = profiles.active_name(cfg)
    if active:
        return active
    order = [n for n in cfg["rotation"]["order"] if n in names] + [n for n in names if n not in cfg["rotation"]["order"]]
    blocked = state.blocks()
    return next((n for n in order if n not in blocked and n not in cfg["rotation"]["disabled"]), order[0])


def remote_stop() -> None:
    from .remote_cmd import stop  # local import to avoid a cycle
    stop(quiet=True)
