# Command and option guide

Use this guide to choose a command and understand every mycodex option.
This guide targets mycodex v0.3.0; see the [changelog](../CHANGELOG.md) and
[everyday commands](cheatsheet.md).

Command names and flags stay compatible. A saved account is called a **profile**
in commands; a conversation is called a **thread**. The phone account is the
**relay account** in technical output. `[Brackets]` mean optional: do not type them.

Shortcuts: `profiles` means `profile list`; `ps` means `processes`;
`use NAME` means `profile use NAME`. A full conversation UUID resumes it.

This reference shares its descriptions with `mycodex help COMMAND SUBCOMMAND`.
Official Codex commands/options pass through and have their own help:
`mycodex -- --help`. See [README](../README.md) and [Quickstart](../QUICKSTART.md).

## Choose a command

| Command | What it does |
|---|---|
| `(no command)` | Open Codex and start working with your usual account. |
| `profile` | Manage your saved accounts: add, choose, inspect, or remove one. |
| `use NAME` | Choose your usual account for new terminal sessions. |
| `quota` | Check usage left and when your limits reset. |
| `rotation` | Choose how Codex switches accounts when usage runs low. |
| `remote-control` | Make this computer available in the ChatGPT phone app. |
| `remote` | Check or manage the phone connection. |
| `projects` | List project folders, add one, or tidy empty conversations. |
| `threads` | List conversations, copy an older one, or put one in a project. |
| `status` | See accounts, phone connection, and running sessions together. |
| `processes` | See which Codex sessions are running on this computer. |
| `doctor` | Check for problems and explain what needs attention. |
| `migrate` | Move saved accounts from the older prodex tool. |
| `version` | Show the mycodex and Codex versions. |
| `help [COMMAND]` | Explain a command, for example `mycodex help quota redeem`. |
| `<Codex command or options>` | Use official Codex commands such as exec, resume, or -m. |
| `<full conversation UUID>` | Resume that conversation; shortcut for `resume ID`. |

## Options before the command

| Option | Meaning |
|---|---|
| `-P, --profile NAME` | Choose an account for this terminal launch, account check, or phone start. |
| `--no-rotate` | Use only the chosen account for this terminal session. |
| `--rotate` | Allow account switching for this terminal session. |
| `--dry-run` | Preview a launch, project cleanup, or migration. Do not make those changes. |
| `--` | Send everything after this directly to Codex, such as `mycodex -- --help`. |
| `-h, --help` | Show instructions without starting a session. |
| `-V, --version` | Show the installed versions. |

Put these options before the command or any native Codex option. Explicit command
values win over a global account selection; `quota --all` checks every account.
Global preview works for launch, project cleanup, and migration; other management
commands reject it. Legacy state relocation at startup is still a separate open issue.

## profile list

List saved accounts. Usage checks may renew an expiring login.

```text
mycodex profile list [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `--no-quota` | Skip online usage checks; show saved account information only. |
| `-h, --help` | Show instructions without needing required values. |

## profile show

Check one account, its login, usage left, and phone role. Default is --profile or your usual account.

```text
mycodex profile show [NAME] [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `NAME` | An account name, shortcut, email address, or a unique part of its name. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## profile current

Check the account currently chosen for terminal sessions.

```text
mycodex profile current [--json]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## profile use

Choose the default account for new terminal sessions. This does not switch the phone account.

```text
mycodex profile use NAME
```

| Parameter | Meaning |
|---|---|
| `NAME` | An account name, shortcut, email address, or a unique part of its name. |
| `-h, --help` | Show instructions without needing required values. |

## profile add

Sign in and save an account. By default, open the printed link on any device.

```text
mycodex profile add [NAME] [--browser]
```

| Parameter | Meaning |
|---|---|
| `NAME` | Optional name for the saved account; otherwise use its email. |
| `--browser` | Use browser sign-in instead of the link-and-code sign-in. |
| `-h, --help` | Show instructions without needing required values. |

## profile reauth

Sign in again to repair an expired, revoked, or damaged login. Give NAME or select it with --profile.

```text
mycodex profile reauth [NAME] [--browser]
```

| Parameter | Meaning |
|---|---|
| `NAME` | An account name, shortcut, email address, or a unique part of its name. |
| `--browser` | Use browser sign-in instead of a link and code. |
| `-h, --help` | Show instructions without needing required values. |

## profile remove

Remove a saved account. Forced removal of the phone account currently stops the connection before confirmation; this issue remains open.

```text
mycodex profile remove NAME [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `NAME` | An account name, shortcut, email address, or a unique part of its name. |
| `-y, --yes` | Skip the confirmation question. Use after reviewing the changes. |
| `--keep-home` | Keep the account folder, including its login, under a hidden name. Does not sign out. |
| `--force` | Allow removal while in use. Can stop the phone connection; close active work first. |
| `-h, --help` | Show instructions without needing required values. |

## profile alias

Create, list, or remove a short name for an account.

```text
mycodex profile alias NAME [SHORT_NAME] [--remove]
```

| Parameter | Meaning |
|---|---|
| `NAME` | An account name, shortcut, email address, or a unique part of its name. |
| `SHORT_NAME` | New shortcut; omit it to list shortcuts for NAME. |
| `--remove` | Remove the shortcut named by NAME; does not remove the account. |
| `-h, --help` | Show instructions without needing required values. |

## quota

Check usage left. “Not confirmed” (unknown in JSON) means the service did not say the account is ready.

```text
mycodex quota [NAME] [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `NAME` | Account to check; default is --profile, or every account if neither is given. |
| `--all` | Check every account, even if --profile or NAME selected one. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `--watch [SECONDS], --live [SECONDS]` | Refresh repeatedly; default 60 seconds. Use a positive number. Ctrl-C stops watching. With --json, show one result. |
| `-h, --help` | Show instructions without needing required values. |

## quota redeem

Spend one earned reset credit for the chosen account, then check usage again. This does not change your phone login.

```text
mycodex quota redeem [NAME] [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `NAME` | Account to reset; default is --profile or your usual account. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `--idempotency-key KEY` | Advanced: keep the same retry ID for an uncertain attempt. Normally chosen and saved automatically. |
| `--credit-id ID` | Advanced: choose a particular earned credit instead of letting the service choose. |
| `-h, --help` | Show instructions without needing required values. |

## rotation status

Check account switching settings, paused accounts, and available usage.

```text
mycodex rotation status [--json]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## rotation enable

Allow account switching for future terminal launches, or allow named accounts as backup choices.

```text
mycodex rotation enable [NAME ...]
```

| Parameter | Meaning |
|---|---|
| `NAME ...` | Optional account names. Without names, enable switching for new terminal launches. |
| `-h, --help` | Show instructions without needing required values. |

## rotation disable

Use one account for future terminal launches, or exclude named backup accounts. A session's starting account remains a choice.

```text
mycodex rotation disable [NAME ...]
```

| Parameter | Meaning |
|---|---|
| `NAME ...` | Optional accounts to exclude as backup choices. Without names, disable switching for new terminal launches. |
| `-h, --help` | Show instructions without needing required values. |

## rotation order

Set the order of backup accounts. This does not guarantee which account starts every request.

```text
mycodex rotation order [NAME ...] [--clear]
```

| Parameter | Meaning |
|---|---|
| `NAME ...` | Preferred backup order; omit names to show it. |
| `--clear` | Remove your preferred order; use available usage to rank backups. |
| `-h, --help` | Show instructions without needing required values. |

## rotation reset

Clear account-switching pauses after a limit has reset. This does not add usage or spend a reset credit.

```text
mycodex rotation reset [NAME ...]
```

| Parameter | Meaning |
|---|---|
| `NAME ...` | Accounts to unpause; omit names to clear every current pause. |
| `-h, --help` | Show instructions without needing required values. |

## rotation headroom

Switch fresh requests away from low 5-hour usage before they fail. Already-started sticky requests stay on their account.

```text
mycodex rotation headroom [PERCENT]
```

| Parameter | Meaning |
|---|---|
| `PERCENT` | 0 to 100; default 5. Omit to show the setting. 0 turns this feature off. |
| `-h, --help` | Show instructions without needing required values. |

## rotation auto-redeem

Choose whether to spend earned resets automatically when 5-hour usage reaches zero. Off by default.

```text
mycodex rotation auto-redeem on|off
```

| Parameter | Meaning |
|---|---|
| `on&#124;off` | on allows automatic spending; off keeps earned credits for manual use. |
| `-h, --help` | Show instructions without needing required values. |

## rotation log

Read account-switching events.

```text
mycodex rotation log [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `-n, --lines N` | Show the last N lines; default 40. Use a positive whole number. |
| `-f, --follow` | Keep showing new log lines. Press Ctrl-C to stop watching. |
| `-h, --help` | Show instructions without needing required values. |

## threads list

List recent conversations across your accounts. Provider is the service used to create a conversation.

```text
mycodex threads [list] [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--all` | Include archived conversations. |
| `-n, --limit N` | Number of conversations to show; default 40. Use a positive whole number. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## threads adopt

Copy a conversation created with another provider into an OpenAI conversation. The original stays unless you ask to archive it.

```text
mycodex threads adopt ID [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `ID` | The conversation ID from `mycodex threads`; a unique beginning also works. |
| `--name NAME` | Choose a name for the copy. |
| `--archive-original` | Archive the original after creating the copy; its history is kept. |
| `-h, --help` | Show instructions without needing required values. |

## threads link

Put an existing conversation in a project and load it for the phone server. Starts no new model turn and needs no restart.

```text
mycodex threads link ID [--project PROJECT]
```

| Parameter | Meaning |
|---|---|
| `ID` | The conversation ID from `mycodex threads`; a unique beginning also works. |
| `--project PROJECT` | Project folder, exact name, or ID. Omit to use its current project or the nearest registered parent folder. |
| `-h, --help` | Show instructions without needing required values. |

## projects list

List registered folders and conversation counts. Counts include non-archived conversations, not just running ones.

```text
mycodex projects [list] [--json]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## projects add

Register a folder as a project without starting a conversation. Existing registrations are reused, not renamed.

```text
mycodex projects add DIR [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `DIR` | An existing project folder. |
| `--name NAME` | Name for a newly registered project; default is the folder name. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## projects clean

Archive verified empty or untouched ready-check conversations. Preview the candidates first; history is not deleted.

```text
mycodex projects clean PROJECT [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `PROJECT` | A project name, folder, or ID. Use the ID if a name matches more than once. |
| `--dry-run` | Show candidates only; archive nothing. |
| `-y, --yes` | Skip the confirmation question. Use after reviewing the changes. |
| `-h, --help` | Show instructions without needing required values. |

## remote stop

Stop the phone connection and disable automatic startup. Can interrupt phone work.

```text
mycodex remote stop
```

| Parameter | Meaning |
|---|---|
| `-h, --help` | Show instructions without needing required values. |

## remote restart

Restart the phone connection using saved settings, including its saved failover choice. Can interrupt active work.

```text
mycodex remote restart
```

| Parameter | Meaning |
|---|---|
| `-h, --help` | Show instructions without needing required values. |

## remote status

Check the phone connection and paired devices. The usual display also checks usage; --json skips those extra usage checks.

```text
mycodex remote [status] [--json]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## remote pair

Create a short-lived code to connect your phone. Sign in to the same ChatGPT account as the computer's phone connection.

```text
mycodex remote pair [--no-wait]
```

| Parameter | Meaning |
|---|---|
| `--no-wait` | Print the code and return without waiting for the phone. |
| `-h, --help` | Show instructions without needing required values. |

## remote clients

List paired phones, or remove one phone's access.

```text
mycodex remote clients [--revoke ID]
```

| Parameter | Meaning |
|---|---|
| `--revoke ID` | Remove the paired device with this ID; get IDs by listing devices first. |
| `-h, --help` | Show instructions without needing required values. |

## remote logs

Read the phone connection's service log.

```text
mycodex remote logs [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `-n, --lines N` | Show the last N lines; default 100. Use a positive whole number. |
| `-f, --follow` | Keep showing new log lines. Press Ctrl-C to stop watching. |
| `-h, --help` | Show instructions without needing required values. |

## remote seed

Start a new project conversation and send its first message. This uses model quota even for the default ready check.

```text
mycodex remote seed [DIR] [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `DIR` | Project folder; default is your current folder. |
| `--name NAME` | Conversation name; default is the folder name. |
| `--message TEXT` | First request; quote text containing spaces. Default asks for the word ready. |
| `--project NAME` | Name used only if creating a new project; it does not select a different existing project. |
| `--no-wait` | Return after sending the request; the turn keeps running on the server. |
| `-h, --help` | Show instructions without needing required values. |

## remote socket

Advanced: print the running phone server's connection path for scripts.

```text
mycodex remote socket
```

| Parameter | Meaning |
|---|---|
| `-h, --help` | Show instructions without needing required values. |

## status

Show accounts, usage, phone connection, and running sessions together.

```text
mycodex status [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `--no-quota` | Skip online usage checks; still check local processes and the phone server. |
| `-h, --help` | Show instructions without needing required values. |

## processes

List running Codex sessions and their account names.

```text
mycodex processes [--json]
```

| Parameter | Meaning |
|---|---|
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## doctor

Check for problems. Read each warning even if the command succeeds. A full check can renew expiring logins.

```text
mycodex doctor [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--fix` | Repair supported problems, including backed-up old conversation paths. Some repairs ask first. |
| `--fix-rollout-paths` | Only repair verified old conversation-file paths, with a backup and no online usage/login checks. |
| `-y, --yes` | Skip the confirmation question. Use after reviewing the changes. |
| `--json` | Show structured data for scripts instead of the usual table. |
| `-h, --help` | Show instructions without needing required values. |

## migrate

Move saved accounts from prodex. May restart the phone service; preview and close active work first.

```text
mycodex migrate [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--dry-run` | Preview the move without changing account folders or restarting the service. |
| `-y, --yes` | Skip the confirmation question. Use after reviewing the changes. |
| `--no-compat-links` | Do not leave shortcuts from old prodex account folders to their new locations. |
| `-h, --help` | Show instructions without needing required values. |

## version

Show the installed mycodex and official Codex versions.

```text
mycodex version
```

| Parameter | Meaning |
|---|---|
| `-h, --help` | Show instructions without needing required values. |

## help

Explain a command or show all commands. Required values are not needed to read help.

```text
mycodex help [COMMAND [SUBCOMMAND]]
```

| Parameter | Meaning |
|---|---|
| `COMMAND` | Optional command name, such as quota or projects. Omit to see all commands. |
| `SUBCOMMAND` | Optional action, such as redeem or clean, to see every parameter. |
| `-h, --help` | Show instructions without needing required values. |

## remote start

Start or switch the phone service. Can stop conflicting servers or restart the existing connection.

```text
mycodex remote start [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | Account used for the phone connection; sign into this account on the phone. |
| `--rotating` | Allow model requests to use backup accounts. Without either mode flag, reuse the saved mode (initially rotating). |
| `--pinned` | Use only the phone account for model requests. |
| `--failover` | Allow the phone service to switch its login account if a health check fails; on for new starts. |
| `--no-failover` | Keep the phone account fixed; repair its login manually if it stops working. |
| `--cwd DIR` | Starting folder for new phone conversations; default is the saved folder or your home. |
| `--force` | Stop conflicting phone servers. Can interrupt their work. |
| `--no-wait` | Return after starting the service, without waiting for connection confirmation. |
| `-h, --help` | Show instructions without needing required values. |

## remote-control

Connect this computer to the ChatGPT phone app. Starts a managed background service by default.

```text
mycodex [--profile NAME] remote-control [OPTIONS]
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | Account used for the phone connection; sign into this account on the phone. |
| `--rotating` | Allow model requests to use backup accounts. Without either mode flag, reuse the saved mode (initially rotating). |
| `--pinned` | Use only the phone account for model requests. |
| `--failover` | Allow the phone service to switch its login account if a health check fails; on for new starts. |
| `--no-failover` | Keep the phone account fixed; repair its login manually if it stops working. |
| `--cwd DIR` | Starting folder for new phone conversations; default is the saved folder or your home. |
| `--force` | Stop conflicting phone servers. Can interrupt their work. |
| `--no-wait` | Return after starting the service, without waiting for connection confirmation. |
| `--foreground` | Advanced: run in this terminal, using profile/mode only. Other service options are not supported. |
| `-h, --help` | Show instructions without needing required values. |

## Behavior to remember

- `unknown` usage never counts as confirmed readiness. Online checks may renew logins.
- Reset spending is explicit unless you enable `rotation auto-redeem on`.
- Preserve `state/accounts.json`; unreadable state is reported without replacing its retry keys.
- Starting/stopping/restarting the phone service can interrupt active work.
- Forced phone-account removal and immediate-exit failover remain open:
  [see the review](project-review.md).
- Existing running processes keep their loaded source code until a planned restart.
