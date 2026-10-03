"""Command routing for mycodex.

Anything mycodex does not own goes, unchanged, to the official codex CLI (TUI by default),
started with the selected profile's CODEX_HOME and, when rotating, behind mycodex's proxy.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from . import __version__, accounts, codex, config, profiles, resolve, ui

OWN_COMMANDS = {"profile", "profiles", "quota", "rotation", "remote", "remote-control", "status",
                "processes", "ps", "doctor", "threads", "migrate", "help", "version", "login", "logout",
                "__remote-serve"}


@dataclass
class Globals:
    profile: str | None = None
    rotate: bool | None = None
    dry_run: bool = False
    rest: list[str] = field(default_factory=list)
    passthrough_only: bool = False   # everything after `--`
    help: bool = False
    version: bool = False


def parse_globals(argv: list[str]) -> Globals:
    g = Globals()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            g.rest = argv[i + 1:]
            g.passthrough_only = True
            return g
        if arg in ("--profile", "-P"):
            if i + 1 >= len(argv):
                raise SystemExit("Error: --profile needs a value")
            g.profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--profile="):
            g.profile = arg.split("=", 1)[1]
        elif arg == "--no-rotate":
            g.rotate = False
        elif arg == "--rotate":
            g.rotate = True
        elif arg == "--dry-run":
            g.dry_run = True
        elif arg in ("-h", "--help") and not g.rest:
            g.help = True
        elif arg in ("-V", "--version"):
            g.version = True
        else:
            g.rest = argv[i:]
            return g
        i += 1
    return g


# ----------------------------------------------------------------------------- help
TOP_COMMANDS = [
    ("(none)", "Start the official Codex TUI with the active profile (rotation on)."),
    ("<codex args>", "Any codex command or flag (exec, resume, fork, review, mcp, -m, -c ...) passes through."),
    ("profile", "List, show, add, remove, re-authenticate and alias accounts."),
    ("quota", "Quota and readiness for every profile in one table (--watch to keep it open)."),
    ("rotation", "Show or change rotation: enable/disable, order, paused accounts, event log."),
    ("remote-control", "Run persistent phone remote control for --profile (rotating or pinned)."),
    ("remote", "Manage the remote-control service: start, stop, restart, status, pair, clients, logs, seed."),
    ("threads", "The shared thread list; adopt threads that are hidden from the phone."),
    ("status", "One-screen summary: accounts, rotation, remote control, sessions, versions."),
    ("processes", "Every codex/mycodex process with its role and account."),
    ("doctor", "Check codex, accounts, tokens, shared threads, sockets, remote service, stale state."),
    ("migrate", "Take over accounts from prodex (~/.prodex/profiles) without logging in again."),
    ("help", "Show help for a command."),
]
TOP_OPTIONS = [
    ("--profile <NAME>", "Account to use (name, alias, email or unique prefix)."),
    ("--no-rotate", "Keep that account fixed for this launch (no rotation proxy)."),
    ("--rotate", "Allow rotation even if `mycodex rotation disable` turned it off."),
    ("--dry-run", "Show what would be launched, without starting codex."),
    ("--", "Pass everything after it to codex verbatim (e.g. `mycodex -- --help`)."),
    ("-h, --help", "Print help."),
    ("-V, --version", "Print mycodex and codex versions."),
]


def top_help() -> None:
    ui.help_text(
        "Multi-account Codex: the official codex TUI and remote control with persistent profiles, "
        "shared ~/.codex threads and automatic account rotation.",
        "mycodex [--profile <NAME>] [--no-rotate] [COMMAND | CODEX ARGS...]",
        TOP_COMMANDS, TOP_OPTIONS,
        tips=["Bare `mycodex` = `codex` as the active profile, with rotation across ready profiles.",
              "Shared threads: every profile links sessions, history and SQLite state to ~/.codex.",
              "Accounts live in ~/.codex/profiles/<name>, one CODEX_HOME per account.",
              "Use `mycodex help <command>` for command options."],
        examples=["mycodex", "mycodex --profile work", "mycodex resume --last",
                  "mycodex exec \"review this repo\"", "mycodex profile list", "mycodex quota",
                  "mycodex --profile work remote-control",
                  "mycodex remote-control --pinned", "mycodex remote status", "mycodex doctor"])


SUB_HELP = {
    "profile": ("Manage accounts.", "mycodex profile <COMMAND>", [
        ("list [--json] [--no-quota]", "All profiles with plan, status, quota, rotation and remote role."),
        ("show <NAME> [--json]", "Details: identity, plan expiry, token, quota windows, rotation, sharing, paths."),
        ("current", "The active profile (used when --profile is omitted)."),
        ("use <NAME>", "Make NAME the active profile."),
        ("add [NAME] [--browser]", "Log a new account in with `codex login` (device code by default)."),
        ("reauth <NAME> [--browser]", "Log an existing profile in again (expired/revoked token)."),
        ("remove <NAME> [--yes] [--keep-home] [--force]", "Stop its daemon, sign it out, delete (or keep) its home."),
        ("alias <NAME> [ALIAS] [--remove]", "Short names usable anywhere a profile is expected."),
    ]),
    "rotation": ("Rotation policy.", "mycodex rotation <COMMAND>", [
        ("status [--json]", "Global switch, order, disabled and paused profiles, who is ready now."),
        ("enable [NAME...]", "Turn rotation on (globally, or re-enable listed profiles)."),
        ("disable [NAME...]", "Turn rotation off (globally), or never switch to the listed profiles."),
        ("order [NAME...] [--clear]", "Preferred order when the proxy has to pick an account."),
        ("reset [NAME...]", "Clear pauses the proxy set after usage/rate limits (all if none named)."),
        ("log [-n N] [-f]", "Rotation proxy events: switches, pauses, token refreshes."),
    ]),
    "threads": ("Shared threads.", "mycodex threads [list|adopt] ...", [
        ("list [--all] [--json] [-n N]", "Recent threads with their provider tag (default command)."),
        ("adopt <ID> [--name N] [--archive-original]",
         "Fork a thread with another tag (e.g. prodex) into an openai-tagged copy the phone lists."),
    ]),
    "migrate": ("Take over prodex accounts.", "mycodex migrate [--dry-run] [--yes] [--no-compat-links]", [
        ("--dry-run", "Show what would move."),
        ("--yes", "Do not ask for confirmation."),
        ("--no-compat-links", "Do not leave links at ~/.prodex/profiles/<name>."),
    ]),
    "remote": ("Persistent remote control (systemd user service `mycodex-remote`).", "mycodex remote <COMMAND>", [
        ("start [--profile NAME] [--rotating|--pinned] [--failover|--no-failover] [--cwd DIR] [--force] [--no-wait]",
         "Start or switch the service; stops conflicting codex daemons first."),
        ("stop", "Stop and disable the service (phone link goes offline)."),
        ("restart", "Restart with the current settings."),
        ("status [--json]", "Mode, relay account, connection, host name, paired phones, conflicts."),
        ("pair [--no-wait]", "Create a short-lived manual pairing code for the ChatGPT app."),
        ("clients [--revoke ID]", "Paired devices for this host."),
        ("logs [-f] [-n N]", "Service logs (journald)."),
        ("seed [DIR] [--name N] [--message T]", "Register DIR as a project and start a thread so the phone lists it."),
    ]),
    "remote-control": ("Phone remote control for the selected profile.",
                       "mycodex [--profile NAME] remote-control [--rotating|--pinned] [--failover] [--cwd DIR] [--foreground]", [
        ("--rotating", "Default. Phone pairs with NAME; model turns rotate across ready profiles."),
        ("--pinned", "Everything runs as NAME, no rotation (official codex remote-control as that account)."),
        ("--failover", "If the relay account dies/exhausts, restart as the next ready profile (pair the phone with it)."),
        ("--cwd DIR", "Default working directory for new remote threads."),
        ("--foreground", "Run in this terminal instead of the service (debugging)."),
        ("--force", "Terminate other remote-control servers that would conflict."),
    ]),
    "quota": ("Quota for all profiles in one table.", "mycodex quota [NAME] [--all] [--json] [--watch [SECONDS]]", [
        ("NAME", "Only this profile."), ("--all", "All profiles (default)."), ("--json", "Machine-readable."),
        ("--watch [SECONDS]", "Refresh the table every SECONDS (default 60) until Ctrl-C."),
    ]),
    "status": ("Summary of the whole stack.", "mycodex status [--json] [--no-quota]", []),
    "processes": ("Processes with role and account.", "mycodex processes [--json]", []),
    "doctor": ("Diagnostics and safe repairs.", "mycodex doctor [--fix] [--yes] [--json]", []),
}


def sub_help(name: str) -> None:
    if name == "ps":
        name = "processes"
    if name == "profiles":
        name = "profile"
    if name not in SUB_HELP:
        top_help()
        return
    desc, usage, commands = SUB_HELP[name]
    ui.help_text(desc, usage, commands, [("-h, --help", "Print help.")])


# ----------------------------------------------------------------------------- parsers
class Parser(argparse.ArgumentParser):
    def __init__(self, command: str, **kw):
        super().__init__(prog=f"mycodex {command}", add_help=False, **kw)
        self.command = command
        self.add_argument("-h", "--help", action="store_true")

    def error(self, message: str) -> None:  # prodex-style error, exit 2
        ui.error(message, [f"mycodex help {self.command.split()[0]}"])
        raise SystemExit(2)

    def parse(self, args: list[str]) -> argparse.Namespace:
        ns = self.parse_args(args)
        if ns.help:
            sub_help(self.command.split()[0])
            raise SystemExit(0)
        return ns


def cmd_profile(args: list[str], g: Globals) -> int:
    sub = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if sub in ("-h", "--help", "help"):
        sub_help("profile")
        return 0
    p = Parser(f"profile {sub}")
    if sub == "list":
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-quota", action="store_true")
        ns = p.parse(rest)
        return accounts.profile_list(ns.json, not ns.no_quota)
    if sub in ("show", "current"):
        if sub == "show":
            p.add_argument("name", nargs="?")
        p.add_argument("--json", action="store_true")
        ns = p.parse(rest)
        target = getattr(ns, "name", None) or g.profile
        return accounts.profile_show(target, ns.json) if target else accounts.profile_current(ns.json)
    if sub == "use":
        p.add_argument("name")
        return accounts.profile_use(p.parse(rest).name)
    if sub == "add":
        p.add_argument("name", nargs="?")
        p.add_argument("--browser", action="store_true")
        ns = p.parse(rest)
        return accounts.profile_add(ns.name, ns.browser)
    if sub == "reauth":
        p.add_argument("name", nargs="?")
        p.add_argument("--browser", action="store_true")
        ns = p.parse(rest)
        target = ns.name or g.profile
        if not target:
            p.error("which profile? (mycodex profile reauth <NAME>)")
        return accounts.profile_reauth(target, ns.browser)
    if sub == "remove":
        p.add_argument("name")
        p.add_argument("-y", "--yes", action="store_true")
        p.add_argument("--keep-home", action="store_true")
        p.add_argument("--force", action="store_true")
        ns = p.parse(rest)
        return accounts.profile_remove(ns.name, ns.yes, ns.keep_home, ns.force)
    if sub == "alias":
        p.add_argument("name")
        p.add_argument("alias", nargs="?")
        p.add_argument("--remove", action="store_true")
        ns = p.parse(rest)
        return accounts.profile_alias(ns.name, ns.alias, ns.remove)
    ui.error(f"unknown profile command '{sub}'", ["mycodex help profile"])
    return 2


def cmd_quota(args: list[str], g: Globals) -> int:
    p = Parser("quota")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", "--live", nargs="?", type=float, const=60.0, default=None)
    ns = p.parse(args)
    return accounts.quota_cmd(None if ns.all else (ns.name or None), ns.json, ns.watch)


def cmd_rotation(args: list[str], g: Globals) -> int:
    sub = args[0] if args and not args[0].startswith("-") else "status"
    rest = args[1:] if args and not args[0].startswith("-") else args
    p = Parser(f"rotation {sub}")
    if sub == "status":
        p.add_argument("--json", action="store_true")
        return accounts.rotation_status(p.parse(rest).json)
    if sub in ("enable", "disable"):
        p.add_argument("names", nargs="*")
        return accounts.rotation_set(sub == "enable", p.parse(rest).names)
    if sub == "order":
        p.add_argument("names", nargs="*")
        p.add_argument("--clear", action="store_true")
        ns = p.parse(rest)
        return accounts.rotation_order(ns.names, ns.clear)
    if sub == "reset":
        p.add_argument("names", nargs="*")
        return accounts.rotation_reset(p.parse(rest).names)
    if sub == "log":
        p.add_argument("-n", "--lines", type=int, default=40)
        p.add_argument("-f", "--follow", action="store_true")
        ns = p.parse(rest)
        return accounts.rotation_log(ns.lines, ns.follow)
    if sub in ("help", "-h", "--help"):
        sub_help("rotation")
        return 0
    ui.error(f"unknown rotation command '{sub}'", ["mycodex help rotation"])
    return 2


def _remote_start_parser(command: str) -> Parser:
    p = Parser(command)
    p.add_argument("--profile")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--rotating", dest="mode", action="store_const", const="rotating")
    mode.add_argument("--pinned", dest="mode", action="store_const", const="pinned")
    fo = p.add_mutually_exclusive_group()
    fo.add_argument("--failover", dest="failover", action="store_const", const=True)
    fo.add_argument("--no-failover", dest="failover", action="store_const", const=False)
    p.add_argument("--cwd")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-wait", action="store_true")
    return p


def cmd_remote_control(args: list[str], g: Globals) -> int:
    from . import remote_cmd
    p = _remote_start_parser("remote-control")
    p.add_argument("--foreground", action="store_true")
    ns = p.parse(args)
    profile = ns.profile or g.profile
    if ns.foreground:
        return remote_cmd.foreground(profile, ns.mode)
    return remote_cmd.start(profile, ns.mode, ns.cwd, ns.failover, ns.force, not ns.no_wait)


def cmd_remote(args: list[str], g: Globals) -> int:
    from . import remote_cmd
    sub = args[0] if args and not args[0].startswith("-") else "status"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if sub in ("help", "-h", "--help"):
        sub_help("remote")
        return 0
    if sub == "start":
        ns = _remote_start_parser("remote start").parse(rest)
        return remote_cmd.start(ns.profile or g.profile, ns.mode, ns.cwd, ns.failover, ns.force, not ns.no_wait)
    p = Parser(f"remote {sub}")
    if sub == "stop":
        p.parse(rest)
        return remote_cmd.stop()
    if sub == "restart":
        p.parse(rest)
        return remote_cmd.restart()
    if sub == "status":
        p.add_argument("--json", action="store_true")
        return remote_cmd.status(p.parse(rest).json)
    if sub == "pair":
        p.add_argument("--no-wait", action="store_true")
        return remote_cmd.pair(not p.parse(rest).no_wait)
    if sub == "clients":
        p.add_argument("--revoke")
        return remote_cmd.clients(p.parse(rest).revoke)
    if sub == "logs":
        p.add_argument("-f", "--follow", action="store_true")
        p.add_argument("-n", "--lines", type=int, default=100)
        ns = p.parse(rest)
        return remote_cmd.logs(ns.follow, ns.lines)
    if sub == "seed":
        p.add_argument("dir", nargs="?")
        p.add_argument("--name")
        p.add_argument("--message")
        ns = p.parse(rest)
        return remote_cmd.seed(ns.dir, ns.name, ns.message)
    ui.error(f"unknown remote command '{sub}'", ["mycodex help remote"])
    return 2


def cmd_threads(args: list[str]) -> int:
    from . import threads
    sub = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if sub in ("help", "-h", "--help"):
        sub_help("threads")
        return 0
    p = Parser(f"threads {sub}")
    if sub == "list":
        p.add_argument("--all", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("-n", "--limit", type=int, default=40)
        ns = p.parse(rest)
        return threads.list_threads(ns.all, ns.json, ns.limit)
    if sub == "adopt":
        p.add_argument("thread")
        p.add_argument("--name")
        p.add_argument("--archive-original", action="store_true")
        ns = p.parse(rest)
        return threads.adopt(ns.thread, ns.name, ns.archive_original)
    ui.error(f"unknown threads command '{sub}'", ["mycodex help threads"])
    return 2


def cmd_migrate(args: list[str]) -> int:
    from . import migrate
    p = Parser("migrate")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--no-compat-links", action="store_true")
    ns = p.parse(args)
    return migrate.run(ns.dry_run, ns.yes, not ns.no_compat_links)


def cmd_simple(name: str, args: list[str]) -> int:
    from . import doctor, views
    p = Parser(name)
    p.add_argument("--json", action="store_true")
    if name == "status":
        p.add_argument("--no-quota", action="store_true")
        ns = p.parse(args)
        return views.status(ns.json, not ns.no_quota)
    if name in ("processes", "ps"):
        return views.processes(p.parse(args).json)
    if name == "doctor":
        p.add_argument("--fix", action="store_true")
        p.add_argument("-y", "--yes", action="store_true")
        ns = p.parse(args)
        return doctor.run(ns.fix, ns.yes, ns.json)
    return 2


def versions() -> int:
    from . import __author__, __github__, __url__
    ui.panel("Mycodex Version", [("mycodex", __version__), ("codex", codex.version() or "not found"),
                                  ("Author", f"{__author__} (@{__github__})"), ("Project", __url__)])
    return 0


# ----------------------------------------------------------------------------- launch
def launch(g: Globals, codex_args: list[str]) -> int:
    from . import launch as launcher
    cfg = config.load()
    names = profiles.names(cfg)
    if not names:
        ui.error("no profiles yet", ["mycodex profile add", "mycodex migrate   # import accounts from prodex"])
        return 1
    name = resolve.resolve(g.profile, names, cfg) if g.profile else accounts.pick_start_profile(cfg)
    profile = profiles.get(name, cfg)
    if profile is None or not profile.logged_in:
        ui.error(f"profile {name} has no login", [f"mycodex profile reauth {name}"])
        return 1
    rotate = cfg["rotation"]["enabled"] if g.rotate is None else g.rotate
    return launcher.run(profile, codex_args, rotate, g.dry_run)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import paths
    for change in paths.migrate_legacy():
        ui.out(f"mycodex: {change}", sys.stderr)
    g = parse_globals(argv)
    if g.version:
        return versions()
    if g.help and not g.rest:
        top_help()
        return 0
    if g.passthrough_only:
        return launch(g, g.rest)
    if not g.rest:
        return launch(g, [])
    command, args = g.rest[0], g.rest[1:]
    if command not in OWN_COMMANDS:
        return launch(g, g.rest)
    if command == "__remote-serve":
        from . import remote
        return remote.serve()
    if command == "help":
        if args:
            sub_help(args[0])
        else:
            top_help()
        return 0
    if command == "version":
        return versions()
    if command in ("login", "logout"):
        ui.error(f"`{command}` is managed per profile by mycodex",
                 ["mycodex profile add            # log in a new account",
                  "mycodex profile reauth <NAME>  # log an existing account in again",
                  "mycodex profile remove <NAME>  # sign an account out and delete it",
                  f"mycodex -- {command} ...       # raw codex {command} inside the active profile"])
        return 2
    if command in ("profile", "profiles"):
        return cmd_profile(["list", *args] if command == "profiles" else args, g)
    if command == "threads":
        return cmd_threads(args)
    if command == "migrate":
        return cmd_migrate(args)
    if command == "quota":
        return cmd_quota(args, g)
    if command == "rotation":
        return cmd_rotation(args, g)
    if command == "remote-control":
        return cmd_remote_control(args, g)
    if command == "remote":
        return cmd_remote(args, g)
    return cmd_simple(command, args)


def entry() -> None:
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 0
    sys.exit(code)
