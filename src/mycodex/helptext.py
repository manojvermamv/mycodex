"""Plain-language help shared by the terminal and the full Markdown reference."""

from __future__ import annotations

from . import ui

JSON = ("--json", "Show structured data for scripts instead of the usual table.")
YES = ("-y, --yes", "Skip the confirmation question. Use after reviewing the changes.")
ACCOUNT = ("NAME", "An account name, shortcut, email address, or a unique part of its name.")
THREAD = ("ID", "The conversation ID from `mycodex threads`; a unique beginning also works.")
PROJECT = ("PROJECT", "A project name, folder, or ID. Use the ID if a name matches more than once.")
FOLLOW = ("-f, --follow", "Keep showing new log lines. Press Ctrl-C to stop watching.")

TOP_COMMANDS = [
    ("(no command)", "Open Codex and start working with your usual account."),
    ("profile", "Manage your saved accounts: add, choose, inspect, or remove one."),
    ("use NAME", "Choose your usual account for new terminal sessions."),
    ("quota", "Check usage left and when your limits reset."),
    ("rotation", "Choose how Codex switches accounts when usage runs low."),
    ("remote-control", "Make this computer available in the ChatGPT phone app."),
    ("remote", "Check or manage the phone connection."),
    ("projects", "List project folders, add one, or tidy empty conversations."),
    ("threads", "List conversations, copy an older one, or put one in a project."),
    ("status", "See accounts, phone connection, and running sessions together."),
    ("processes", "See which Codex sessions are running on this computer."),
    ("doctor", "Check for problems and explain what needs attention."),
    ("migrate", "Move saved accounts from the older prodex tool."),
    ("version", "Show the mycodex and Codex versions."),
    ("help [COMMAND]", "Explain a command, for example `mycodex help quota redeem`."),
    ("<Codex command or options>", "Use official Codex commands such as exec, resume, or -m."),
    ("<full conversation UUID>", "Resume that conversation; shortcut for `resume ID`."),
]
TOP_OPTIONS = [
    ("-P, --profile NAME", "Choose an account for this terminal launch, account check, or phone start."),
    ("--no-rotate", "Use only the chosen account for this terminal session."),
    ("--rotate", "Allow account switching for this terminal session."),
    ("--dry-run", "Preview a launch, project cleanup, or migration. Do not make those changes."),
    ("--", "Send everything after this directly to Codex, such as `mycodex -- --help`."),
    ("-h, --help", "Show instructions without starting a session."),
    ("-V, --version", "Show the installed versions."),
]

# Each entry contains description, usage, and every supported parameter.
DETAILS = {
    "profile list": ("List saved accounts. Usage checks may renew an expiring login.", "mycodex profile list [OPTIONS]", [JSON, ("--no-quota", "Skip online usage checks; show saved account information only.")]),
    "profile show": ("Check one account, its login, usage left, and phone role. Default is --profile or your usual account.", "mycodex profile show [NAME] [OPTIONS]", [ACCOUNT, JSON]),
    "profile current": ("Check the account currently chosen for terminal sessions.", "mycodex profile current [--json]", [JSON]),
    "profile use": ("Choose the default account for new terminal sessions. This does not switch the phone account.", "mycodex profile use NAME", [ACCOUNT]),
    "profile add": ("Sign in and save an account. By default, open the printed link on any device.", "mycodex profile add [NAME] [--browser]", [("NAME", "Optional name for the saved account; otherwise use its email."), ("--browser", "Use browser sign-in instead of the link-and-code sign-in.")]),
    "profile reauth": ("Sign in again to repair an expired, revoked, or damaged login. Give NAME or select it with --profile.", "mycodex profile reauth [NAME] [--browser]", [ACCOUNT, ("--browser", "Use browser sign-in instead of a link and code.")]),
    "profile remove": ("Remove a saved account. Forced removal of the phone account currently stops the connection before confirmation; this issue remains open.", "mycodex profile remove NAME [OPTIONS]", [ACCOUNT, YES, ("--keep-home", "Keep the account folder, including its login, under a hidden name. Does not sign out."), ("--force", "Allow removal while in use. Can stop the phone connection; close active work first.")]),
    "profile alias": ("Create, list, or remove a short name for an account.", "mycodex profile alias NAME [SHORT_NAME] [--remove]", [ACCOUNT, ("SHORT_NAME", "New shortcut; omit it to list shortcuts for NAME."), ("--remove", "Remove the shortcut named by NAME; does not remove the account.")]),
    "quota": ("Check usage left. “Not confirmed” (unknown in JSON) means the service did not say the account is ready.", "mycodex quota [NAME] [OPTIONS]", [("NAME", "Account to check; default is --profile, or every account if neither is given."), ("--all", "Check every account, even if --profile or NAME selected one."), JSON, ("--watch [SECONDS], --live [SECONDS]", "Refresh repeatedly; default 60 seconds. Use a positive number. Ctrl-C stops watching. With --json, show one result.")]),
    "quota redeem": ("Spend one earned reset credit for the chosen account, then check usage again. This does not change your phone login.", "mycodex quota redeem [NAME] [OPTIONS]", [("NAME", "Account to reset; default is --profile or your usual account."), JSON, ("--idempotency-key KEY", "Advanced: keep the same retry ID for an uncertain attempt. Normally chosen and saved automatically."), ("--credit-id ID", "Advanced: choose a particular earned credit instead of letting the service choose.")]),
    "rotation status": ("Check account switching settings, paused accounts, and available usage.", "mycodex rotation status [--json]", [JSON]),
    "rotation enable": ("Allow account switching for future terminal launches, or allow named accounts as backup choices.", "mycodex rotation enable [NAME ...]", [("NAME ...", "Optional account names. Without names, enable switching for new terminal launches.")]),
    "rotation disable": ("Use one account for future terminal launches, or exclude named backup accounts. A session's starting account remains a choice.", "mycodex rotation disable [NAME ...]", [("NAME ...", "Optional accounts to exclude as backup choices. Without names, disable switching for new terminal launches.")]),
    "rotation order": ("Set the order of backup accounts. This does not guarantee which account starts every request.", "mycodex rotation order [NAME ...] [--clear]", [("NAME ...", "Preferred backup order; omit names to show it."), ("--clear", "Remove your preferred order; use available usage to rank backups.")]),
    "rotation reset": ("Clear account-switching pauses after a limit has reset. This does not add usage or spend a reset credit.", "mycodex rotation reset [NAME ...]", [("NAME ...", "Accounts to unpause; omit names to clear every current pause.")]),
    "rotation headroom": ("Switch fresh requests away from low 5-hour usage before they fail. Already-started sticky requests stay on their account.", "mycodex rotation headroom [PERCENT]", [("PERCENT", "0 to 100; default 5. Omit to show the setting. 0 turns this feature off.")]),
    "rotation auto-redeem": ("Choose whether to spend earned resets automatically when 5-hour usage reaches zero. Off by default.", "mycodex rotation auto-redeem on|off", [("on|off", "on allows automatic spending; off keeps earned credits for manual use.")]),
    "rotation log": ("Read account-switching events.", "mycodex rotation log [OPTIONS]", [("-n, --lines N", "Show the last N lines; default 40. Use a positive whole number."), FOLLOW]),
    "threads list": ("List recent conversations across your accounts. Provider is the service used to create a conversation.", "mycodex threads [list] [OPTIONS]", [("--all", "Include archived conversations."), ("-n, --limit N", "Number of conversations to show; default 40. Use a positive whole number."), JSON]),
    "threads adopt": ("Copy a conversation created with another provider into an OpenAI conversation. The original stays unless you ask to archive it.", "mycodex threads adopt ID [OPTIONS]", [THREAD, ("--name NAME", "Choose a name for the copy."), ("--archive-original", "Archive the original after creating the copy; its history is kept.")]),
    "threads link": ("Put an existing conversation in a project and load it for the phone server. Starts no new model turn and needs no restart.", "mycodex threads link ID [--project PROJECT]", [THREAD, ("--project PROJECT", "Project folder, exact name, or ID. Omit to use its current project or the nearest registered parent folder.")]),
    "projects list": ("List registered folders and conversation counts. Counts include non-archived conversations, not just running ones.", "mycodex projects [list] [--json]", [JSON]),
    "projects add": ("Register a folder as a project without starting a conversation. Existing registrations are reused, not renamed.", "mycodex projects add DIR [OPTIONS]", [("DIR", "An existing project folder."), ("--name NAME", "Name for a newly registered project; default is the folder name."), JSON]),
    "projects clean": ("Archive verified empty or untouched ready-check conversations. Preview the candidates first; history is not deleted.", "mycodex projects clean PROJECT [OPTIONS]", [PROJECT, ("--dry-run", "Show candidates only; archive nothing."), YES]),
    "remote stop": ("Stop the phone connection and disable automatic startup. Can interrupt phone work.", "mycodex remote stop", []),
    "remote restart": ("Restart the phone connection using saved settings, including its saved failover choice. Can interrupt active work.", "mycodex remote restart", []),
    "remote status": ("Check the phone connection and paired devices. The usual display also checks usage; --json skips those extra usage checks.", "mycodex remote [status] [--json]", [JSON]),
    "remote models share": ("Copy one Plus account's model catalogue into the configured phone account without restarting or changing pairing. Saves that source so later managed phone-service starts refresh it before the server launches; a refresh warning never keeps the phone offline.", "mycodex remote models share [SOURCE]", [("SOURCE", "Optional Plus account; default is the saved source or your usual account.")]),
    "remote pair": ("Create a short-lived code to connect your phone. Sign in to the same ChatGPT account as the computer's phone connection.", "mycodex remote pair [--no-wait]", [("--no-wait", "Print the code and return without waiting for the phone.")]),
    "remote clients": ("List paired phones, or remove one phone's access.", "mycodex remote clients [--revoke ID]", [("--revoke ID", "Remove the paired device with this ID; get IDs by listing devices first.")]),
    "remote logs": ("Read the phone connection's service log.", "mycodex remote logs [OPTIONS]", [("-n, --lines N", "Show the last N lines; default 100. Use a positive whole number."), FOLLOW]),
    "remote seed": ("Start a new project conversation and send its first message. This uses model quota even for the default ready check.", "mycodex remote seed [DIR] [OPTIONS]", [("DIR", "Project folder; default is your current folder."), ("--name NAME", "Conversation name; default is the folder name."), ("--message TEXT", "First request; quote text containing spaces. Default asks for the word ready."), ("--project NAME", "Name used only if creating a new project; it does not select a different existing project."), ("--no-wait", "Return after sending the request; the turn keeps running on the server.")]),
    "remote socket": ("Advanced: print the running phone server's connection path for scripts.", "mycodex remote socket", []),
    "status": ("Show accounts, usage, phone connection, and running sessions together.", "mycodex status [OPTIONS]", [JSON, ("--no-quota", "Skip online usage checks; still check local processes and the phone server.")]),
    "processes": ("List running Codex sessions and their account names.", "mycodex processes [--json]", [JSON]),
    "doctor": ("Check for problems. Read each warning even if the command succeeds. A full check can renew expiring logins.", "mycodex doctor [OPTIONS]", [("--fix", "Repair supported problems, including backed-up old conversation paths. Some repairs ask first."), ("--fix-rollout-paths", "Only repair verified old conversation-file paths, with a backup and no online usage/login checks."), YES, JSON]),
    "migrate": ("Move saved accounts from prodex. May restart the phone service; preview and close active work first.", "mycodex migrate [OPTIONS]", [("--dry-run", "Preview the move without changing account folders or restarting the service."), YES, ("--no-compat-links", "Do not leave shortcuts from old prodex account folders to their new locations.")]),
    "version": ("Show the installed mycodex and official Codex versions.", "mycodex version", []),
    "help": ("Explain a command or show all commands. Required values are not needed to read help.", "mycodex help [COMMAND [SUBCOMMAND]]", [("COMMAND", "Optional command name, such as quota or projects. Omit to see all commands."), ("SUBCOMMAND", "Optional action, such as redeem or clean, to see every parameter.")]),
}

START_OPTIONS = [
    ("--profile NAME", "Account used for the phone connection; sign into this account on the phone."),
    ("--rotating", "Allow model requests to use backup accounts. Without either mode flag, reuse the saved mode (initially rotating)."),
    ("--pinned", "Use only the phone account for model requests."),
    ("--failover", "Allow the phone service to switch its login account if a health check fails; on for new starts."),
    ("--no-failover", "Keep the phone account fixed; repair its login manually if it stops working."),
    ("--cwd DIR", "Starting folder for new phone conversations; default is the saved folder or your home."),
    ("--force", "Stop conflicting phone servers. Can interrupt their work."),
    ("--no-wait", "Return after starting the service, without waiting for connection confirmation."),
]
DETAILS["remote start"] = ("Start or switch the phone service. Can stop conflicting servers or restart the existing connection.", "mycodex remote start [OPTIONS]", START_OPTIONS)
DETAILS["remote-control"] = ("Connect this computer to the ChatGPT phone app. Starts a managed background service by default.", "mycodex [--profile NAME] remote-control [OPTIONS]", [*START_OPTIONS, ("--foreground", "Advanced: run in this terminal, using profile/mode only. Other service options are not supported.")])

GROUPS = {
    "profile": ("Saved accounts (called profiles in command names).", "mycodex profile COMMAND [OPTIONS]", [key for key in DETAILS if key.startswith("profile ")]),
    "rotation": ("Automatic account switching.", "mycodex rotation COMMAND [OPTIONS]", [key for key in DETAILS if key.startswith("rotation ")]),
    "threads": ("Conversations shared by your accounts.", "mycodex threads [COMMAND] [OPTIONS]", [key for key in DETAILS if key.startswith("threads ")]),
    "projects": ("Project folders on this computer. Requires the running phone server.", "mycodex projects [COMMAND] [OPTIONS]", [key for key in DETAILS if key.startswith("projects ")]),
    "remote": ("Your phone connection and paired devices.", "mycodex remote [COMMAND] [OPTIONS]", ["remote start", *[key for key in DETAILS if key.startswith("remote ") and key != "remote start"]]),
}


def canonical(name: str) -> str:
    parts = name.split()
    if not parts:
        return ""
    parts[0] = {"profiles": "profile", "ps": "processes", "use": "profile use"}.get(parts[0], parts[0])
    return " ".join(parts)


def show(name: str = "") -> bool:
    name = canonical(name)
    if not name:
        ui.help_text("Use Codex with saved accounts and a phone connection.",
                     "mycodex [OPTIONS] [COMMAND]", TOP_COMMANDS, TOP_OPTIONS,
                     tips=["NAME means a saved account; ID means a conversation ID; DIR means a folder.",
                           "Put global options before the command. [Brackets] mean optional; do not type them.",
                           "Try `mycodex help profile add` or `mycodex projects clean --help`.",
                           "Account switching does not change the phone login; relay failover can."],
                     examples=["mycodex", "mycodex quota", "mycodex use work", "mycodex help quota redeem"])
        return True
    if name in GROUPS:
        description, usage, keys = GROUPS[name]
        ui.help_text(description, usage,
                     [(key.split(" ", 1)[1], DETAILS[key][0]) for key in keys],
                     [("-h, --help", "Show instructions for this command.")],
                     tips=[f"Use `mycodex help {keys[0]}` for all options and their meaning.",
                           "Account names, folder names, and conversation IDs are explained in detailed help."])
        return True
    if name in DETAILS:
        description, usage, options = DETAILS[name]
        ui.help_text(description, usage, [], [*options, ("-h, --help", "Show these instructions.")])
        return True
    return False


def markdown() -> str:
    """Complete parameter reference generated from the same descriptions as CLI help."""
    lines = ["# Command and option guide", "", "Use this guide to choose a command and understand every mycodex option.",
             "This guide targets mycodex v0.3.0; see the [changelog](../CHANGELOG.md) and",
             "[everyday commands](cheatsheet.md).", "",
             "Command names and flags stay compatible. A saved account is called a **profile**",
             "in commands; a conversation is called a **thread**. The phone account is the",
             "**relay account** in technical output. `[Brackets]` mean optional: do not type them.", "",
             "Shortcuts: `profiles` means `profile list`; `ps` means `processes`;",
             "`use NAME` means `profile use NAME`. A full conversation UUID resumes it.", "",
             "This reference shares its descriptions with `mycodex help COMMAND SUBCOMMAND`.",
             "Official Codex commands/options pass through and have their own help:",
             "`mycodex -- --help`. See [README](../README.md) and [Quickstart](../QUICKSTART.md).", "",
             "## Choose a command", "", "| Command | What it does |", "|---|---|"]
    def row(name: str, meaning: str) -> str:
        return f"| `{name.replace('|', '&#124;')}` | {meaning.replace('|', '&#124;')} |"
    lines += [row(name, meaning) for name, meaning in TOP_COMMANDS]
    lines += ["", "## Options before the command", "", "| Option | Meaning |", "|---|---|"]
    lines += [row(name, meaning) for name, meaning in TOP_OPTIONS]
    lines += ["", "Put these options before the command or any native Codex option. Explicit command",
              "values win over a global account selection; `quota --all` checks every account.",
              "Global preview works for launch, project cleanup, and migration; other management",
              "commands reject it. Legacy state relocation at startup is still a separate open issue.", ""]
    for name, (description, usage, options) in DETAILS.items():
        lines += [f"## {name}", "", description, "", "```text", usage, "```", "",
                  "| Parameter | Meaning |", "|---|---|"]
        lines += [row(option, meaning) for option, meaning in options]
        lines += [row("-h, --help", "Show instructions without needing required values."), ""]
    lines += ["## Behavior to remember", "",
              "- `unknown` usage never counts as confirmed readiness. Online checks may renew logins.",
              "- Reset spending is explicit unless you enable `rotation auto-redeem on`.",
              "- Preserve `state/accounts.json`; unreadable state is reported without replacing its retry keys.",
              "- Starting/stopping/restarting the phone service can interrupt active work.",
              "- Forced phone-account removal and immediate-exit failover remain open:",
              "  [see the review](project-review.md).", "- Existing running processes keep their loaded source code until a planned restart.", ""]
    return "\n".join(lines)
