"""Command routing for mycodex.

Anything mycodex does not own goes, unchanged, to the official codex CLI (TUI by default),
started with the selected profile's CODEX_HOME and, when rotating, behind mycodex's proxy.
"""

from __future__ import annotations

import argparse
import math
import sys
import uuid
from dataclasses import dataclass, field

from . import __version__, accounts, codex, config, helptext, profiles, resolve, ui

OWN_COMMANDS = {"profile", "profiles", "quota", "rotation", "remote", "remote-control", "status",
                "processes", "ps", "doctor", "threads", "migrate", "help", "version", "login", "logout",
                "__remote-serve", "projects", "use"}


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
            if i + 1 >= len(argv) or not argv[i + 1].strip() or argv[i + 1].startswith("-"):
                raise SystemExit("Choose an account after --profile, for example: mycodex --profile work quota")
            g.profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--profile="):
            g.profile = arg.split("=", 1)[1]
            if not g.profile.strip():
                raise SystemExit("Choose an account after --profile=, for example: --profile=work")
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
def top_help() -> None:
    helptext.show()


def sub_help(name: str) -> None:
    if not helptext.show(name):
        ui.error(f"No help found for '{name}'.", ["mycodex help"])
        raise SystemExit(2)


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a whole number greater than zero") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("enter a whole number greater than zero")
    return result


def positive_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a number of seconds greater than zero") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("enter a number of seconds greater than zero")
    return result


def percentage(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a percentage from 0 to 100") from None
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("enter a percentage from 0 to 100")
    return result


# ----------------------------------------------------------------------------- parsers
class Parser(argparse.ArgumentParser):
    def __init__(self, command: str, **kw):
        super().__init__(prog=f"mycodex {command}", add_help=False, **kw)
        self.command = command
        self.add_argument("-h", "--help", action="store_true")

    def error(self, message: str) -> None:
        message = message.replace("the following arguments are required:", "Please provide:")
        message = message.replace("unrecognized arguments:", "These options were not recognized:")
        ui.error(message, [f"mycodex help {self.command}"])
        raise SystemExit(2)

    def parse(self, args: list[str]) -> argparse.Namespace:
        options = args[:args.index("--")] if "--" in args else args
        if any(arg in ("-h", "--help") for arg in options):
            sub_help(self.command)
            raise SystemExit(0)
        ns = self.parse_args(args)
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
            p.error("Choose the account to sign in again: mycodex profile reauth NAME")
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
    if args[:1] == ["redeem"]:
        from . import redemption
        p = Parser("quota redeem")
        p.add_argument("name", nargs="?")
        p.add_argument("--json", action="store_true")
        p.add_argument("--idempotency-key")
        p.add_argument("--credit-id")
        ns = p.parse(args[1:])
        return redemption.redeem(ns.name or g.profile, ns.json, ns.idempotency_key, ns.credit_id)
    p = Parser("quota")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", "--live", nargs="?", type=positive_seconds, const=60.0, default=None)
    ns = p.parse(args)
    return accounts.quota_cmd(None if ns.all else (ns.name or g.profile), ns.json, ns.watch)


def cmd_rotation(args: list[str], g: Globals) -> int:
    sub = args[0] if args and not args[0].startswith("-") else "status"
    rest = args[1:] if args and not args[0].startswith("-") else args
    p = Parser(f"rotation {sub}")
    if sub == "headroom":
        p.add_argument("percent", nargs="?", type=percentage)
        return accounts.rotation_headroom(p.parse(rest).percent)
    if sub == "auto-redeem":
        p.add_argument("setting", choices=("on", "off"))
        return accounts.rotation_auto_redeem(p.parse(rest).setting == "on")
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
        p.add_argument("-n", "--lines", type=positive_integer, default=40)
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
        if ns.cwd is not None or ns.failover is not None or ns.force or ns.no_wait:
            p.error("With --foreground, use only --profile, --rotating, or --pinned. Remove --foreground to use service options.")
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
    if sub == "models":
        action = rest[0] if rest and not rest[0].startswith("-") else ""
        if action == "share":
            p = Parser("remote models share")
            p.add_argument("source", nargs="?")
            return remote_cmd.share_models(p.parse(rest[1:]).source)
        ui.error("unknown remote models command", ["mycodex help remote models share"])
        return 2
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
        p.add_argument("-n", "--lines", type=positive_integer, default=100)
        ns = p.parse(rest)
        return remote_cmd.logs(ns.follow, ns.lines)
    if sub == "seed":
        p.add_argument("dir", nargs="?")
        p.add_argument("--name")
        p.add_argument("--message")
        p.add_argument("--project")
        p.add_argument("--no-wait", action="store_true")
        ns = p.parse(rest)
        return remote_cmd.seed(ns.dir, ns.name, ns.message, ns.project, not ns.no_wait)
    if sub == "socket":
        p.parse(rest)
        return remote_cmd.socket_path()
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
        p.add_argument("-n", "--limit", type=positive_integer, default=40)
        ns = p.parse(rest)
        return threads.list_threads(ns.all, ns.json, ns.limit)
    if sub == "adopt":
        p.add_argument("thread")
        p.add_argument("--name")
        p.add_argument("--archive-original", action="store_true")
        ns = p.parse(rest)
        return threads.adopt(ns.thread, ns.name, ns.archive_original)
    if sub == "link":
        p.add_argument("thread")
        p.add_argument("--project")
        ns = p.parse(rest)
        return threads.link(ns.thread, ns.project)
    ui.error(f"unknown threads command '{sub}'", ["mycodex help threads"])
    return 2


def cmd_projects(args: list[str], dry_run: bool = False) -> int:
    from . import projects
    sub = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    if sub in ("help", "-h", "--help"):
        sub_help("projects")
        return 0
    p = Parser(f"projects {sub}")
    if sub == "list":
        p.add_argument("--json", action="store_true")
        return projects.list_projects(p.parse(rest).json)
    if sub == "add":
        p.add_argument("directory")
        p.add_argument("--name")
        p.add_argument("--json", action="store_true")
        ns = p.parse(rest)
        return projects.add(ns.directory, ns.name, ns.json)
    if sub == "clean":
        p.add_argument("project")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("-y", "--yes", action="store_true")
        ns = p.parse(rest)
        return projects.clean(ns.project, ns.dry_run or dry_run, ns.yes)
    ui.error(f"unknown projects command '{sub}'", ["mycodex help projects"])
    return 2


def cmd_migrate(args: list[str], dry_run: bool = False) -> int:
    from . import migrate
    p = Parser("migrate")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--no-compat-links", action="store_true")
    ns = p.parse(args)
    return migrate.run(ns.dry_run or dry_run, ns.yes, not ns.no_compat_links)


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
        p.add_argument("--fix-rollout-paths", action="store_true")
        p.add_argument("-y", "--yes", action="store_true")
        ns = p.parse(args)
        if ns.fix_rollout_paths:
            from . import maintenance
            import json
            results = maintenance.audit(fix=True)
            if ns.json:
                print(json.dumps(results, indent=2))
            else:
                for item in results:
                    ui.info(f"{item['database']}: repaired {item['repaired']}/{item['legacy']} legacy paths"
                            + (f"; backup {item['backup']}" if item['backup'] else ""))
                    if item.get("error") or item["unresolved"]:
                        ui.warn(str(item.get("error") or item["unresolved"]))
            return int(any(item.get("error") or item["unresolved"] for item in results))
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
        ui.error("No saved accounts yet", ["mycodex profile add", "mycodex migrate   # import accounts from prodex"])
        return 1
    name = resolve.resolve(g.profile, names, cfg) if g.profile else accounts.pick_start_profile(cfg)
    profile = profiles.get(name, cfg)
    if profile is None or not profile.logged_in:
        ui.error(f"Account {name} needs sign-in", [f"mycodex profile reauth {name}"])
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
    try:
        uuid.UUID(command)
    except ValueError:
        pass
    else:
        if len(command) == 36:
            return launch(g, ["resume", command, *args])
    if command not in OWN_COMMANDS:
        return launch(g, g.rest)
    if command == "__remote-serve":
        from . import remote
        return remote.serve()
    if command == "help":
        if any(arg in ("-h", "--help") for arg in args):
            Parser("help").parse(args)
        if args:
            sub_help(" ".join(args))
        else:
            top_help()
        return 0
    if command == "version":
        Parser("version").parse(args)
        return versions()
    if g.dry_run and not any(arg in ("-h", "--help") for arg in args):
        if command != "migrate" and not (command == "projects" and args[:1] == ["clean"]):
            ui.error("Preview (--dry-run) works for terminal launches, projects clean, and migrate. This command does not support preview.",
                     [f"mycodex help {command}"])
            return 2
    if command in ("login", "logout"):
        ui.error(f"`{command}` is managed per profile by mycodex",
                 ["mycodex profile add            # log in a new account",
                  "mycodex profile reauth <NAME>  # log an existing account in again",
                  "mycodex profile remove <NAME>  # sign an account out and delete it",
                  f"mycodex -- {command} ...       # raw codex {command} inside the active profile"])
        return 2
    if command in helptext.GROUPS and args in (["--help"], ["-h"]):
        sub_help(command)
        return 0
    if command in ("profile", "profiles"):
        return cmd_profile(["list", *args] if command == "profiles" else args, g)
    if command == "threads":
        return cmd_threads(args)
    if command == "projects":
        return cmd_projects(args, g.dry_run)
    if command == "use":
        return cmd_profile(["use", *args], g)
    if command == "migrate":
        return cmd_migrate(args, g.dry_run)
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
    from .appserver import AppServerError
    from .auth import AuthError
    from .validation import InvalidData
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 0
    except (AppServerError, AuthError, InvalidData, OSError) as exc:
        ui.error(str(exc))
        code = 1
    sys.exit(code)
