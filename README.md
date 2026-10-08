<p align="center">
  <img src="assets/mycodex-banner.svg" alt="mycodex: multi-account Codex for Linux hosts" width="100%">
</p>

# mycodex

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%2B%20systemd-0f172a?logo=linux&logoColor=white)](#requirements)
[![Codex CLI](https://img.shields.io/badge/codex%20cli-0.160-111827)](https://developers.openai.com/codex/cli)
[![Dependencies](https://img.shields.io/badge/dependencies-none-16a34a)](#installation)
[![License: MIT](https://img.shields.io/badge/license-MIT-0e7490)](LICENSE)

`mycodex` runs the **official Codex CLI** with several ChatGPT accounts on one Linux host.
Each account keeps its own persistent login, every account shares one `~/.codex` (same
threads, history and settings), requests move to another ready account when one runs out,
and a supervised service keeps **Codex remote control** available to the ChatGPT phone app.

The Codex you get is the unmodified original. mycodex is a small, dependency-free Python
tool around it: it prepares each account's Codex home, runs a local rotation proxy in
front of the ChatGPT backend, and manages the remote-control service.

The current release is **v0.3.0** (2026-10-05). See the [changelog](CHANGELOG.md),
[GitHub release](https://github.com/manojvermamv/mycodex/releases/tag/v0.3.0), and
[everyday commands](docs/cheatsheet.md). The complete source, tests, and public docs
are versioned together.
The [restart and recovery plan](docs/redeployment.md) explains a guarded deployment
with an independent temporary host observer.
See the [whole-project review](docs/project-review.md) for reproduced open issues and
the [workflow audit](docs/workflow-audit.md) for fixes already applied and verified.

## Contents

- [Why mycodex](#why-mycodex)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Coming from prodex](#coming-from-prodex)
- [Daily use](#daily-use)
- [Commands](#commands)
- [Remote control](#remote-control)
- [Projects and new threads](#projects-and-new-threads)
- [Rotation](#rotation)
- [Accounts and shared threads](#accounts-and-shared-threads)
- [Files and layout](#files-and-layout)
- [Troubleshooting](#troubleshooting)
- [Security and responsible use](#security-and-responsible-use)
- [Compatibility](#compatibility)
- [Documentation](#documentation)
- [Author](#author)
- [License](#license)

## Why mycodex

Use `mycodex` if you want to:

- use several ChatGPT/Codex accounts from one terminal, without logging in again
- keep one set of threads: every account sees the same sessions, history, skills and config
- keep working when an account hits its usage limit, because the next request goes to the
  next ready account
- drive the host from the ChatGPT phone app, with remote control that survives SSH
  disconnects and reboots, and the same thread list as your terminal
- see every account's quota, the phone link and all Codex processes at a glance

If you use a single account and never use remote control, plain `codex` is enough.

## How it works

```text
mycodex ──► official codex (TUI, exec, resume, fork, remote-control: unchanged)
   │              │
   │              └─ model requests ──► mycodex rotation proxy (127.0.0.1) ──► chatgpt.com
   │                                        picks a ready account per request
   ├─ accounts: one CODEX_HOME per account in ~/.codex/profiles/<name>, linked to ~/.codex
   └─ quota, rotation policy, remote-control service, threads, diagnostics
```

- **Accounts**: each account is a real Codex home, `~/.codex/profiles/<name>`, with its own
  `auth.json`. You log in with the official `codex login`, once per account.
- **Shared threads**: sessions, history, thread names, config, skills and Codex's databases
  are shared through `~/.codex`, so a thread started on one account continues on another.
- **Rotation**: Codex keeps its built-in `openai` provider and only its base URL points at a
  local proxy. The proxy sends each request with the credentials of a ready account and
  retries eligible usage/rate failures on another account before the response is committed.
  Fresh requests can also prefer a healthier account using recent quota snapshots.
  When a saved quota reset time becomes due, the proxy checks that account again at the
  next fresh model-request boundary. An explicitly ready result restores its configured
  priority; an unknown or still-limited result keeps it paused. An in-progress turn keeps
  its account, and status continues to show the saved reset date and time while paused.
- **Remote control**: a systemd user service runs the official `codex remote-control`,
  behind the same kind of proxy, and restarts it if it stops.

See [docs/architecture.md](docs/architecture.md) for the details.

## Requirements

| Requirement | Notes |
|---|---|
| Linux with a systemd user instance | Debian 13 tested. Enable lingering so remote control survives logout and reboot: `sudo loginctl enable-linger $USER` |
| Python 3.10 or newer | standard library only; nothing to `pip install` |
| [Codex CLI](https://developers.openai.com/codex/cli) 0.160.x | the official standalone CLI, `codex --version` |
| ChatGPT accounts with Codex access | any plan that can use Codex |
| ChatGPT mobile app (optional) | for remote control, signed in to the relay account |

## Installation

1. Install the official Codex CLI (skip if you have it):

   ```bash
   curl -fsSL https://chatgpt.com/codex/install.sh | sh
   ```

2. Install mycodex:

   ```bash
   git clone https://github.com/manojvermamv/mycodex.git ~/mycodex
   ~/mycodex/install.sh
   ```

   `install.sh` links `~/.local/bin/mycodex` to the launcher and creates the private
   `state/` directory. Make sure `~/.local/bin` is on your `PATH`.

3. Check the stack:

   ```bash
   mycodex --version
   mycodex doctor
   ```

## Quick start

```bash
mycodex profile add                         # log in an account (device code); repeat per account
mycodex quota                               # every account's plan, quota and rotation readiness
mycodex                                     # the official Codex TUI, rotation on
mycodex --profile <name> remote-control     # keep the host available to the ChatGPT phone app
```

The step-by-step guide is in [QUICKSTART.md](QUICKSTART.md).

## Coming from prodex

If your accounts live in prodex (`~/.prodex/profiles`), take them over without logging in
again:

```bash
mycodex migrate --dry-run     # what would move
mycodex migrate               # move them into ~/.codex/profiles
```

Each account home is moved with one rename, so its login, refresh token and
`installation_id` (the identity a paired phone knows) stay exactly as they were, and a link
is left at the old path. If the remote-control service is running, it restarts on the new
homes and the phone reconnects by itself.

Threads that prodex created carry its provider tag, which the phone does not list. Make a
copy the phone can see with `mycodex threads adopt <ID>` (see [Accounts and shared
threads](#accounts-and-shared-threads)).

Before removing prodex compatibility paths, inspect `mycodex doctor` and repair verified
legacy rollout references with `mycodex doctor --fix-rollout-paths`. Unresolved references
need investigation; a successful diagnostic exit alone does not establish that none remain.
Once accounts and rollout paths are verified, prodex is no longer needed.
Close any session you started through prodex (`pgrep -a prodex` shows none), then delete
its binary, its state directory (by now it holds only links to the new homes), the empty
lock files it left in `~/.codex` and its temporary files:

```bash
rm ~/.local/bin/prodex
rm -rf ~/.prodex
rm -f ~/.codex/.prodex-session-repair.lock ~/.codex/sessions/.prodex-maintenance.lock
rm -rf /tmp/prodex-*
```

Keep the `export PATH="$HOME/.local/bin:$PATH"` line its installer may have added to your
shell profile: `codex` and `mycodex` live there too.

## Daily use

The commands you will use most, shown with two example accounts, `work` and `personal`. A
profile can be named by its full name, an alias, the account email or any unique prefix
(`work` for `work_example.com`).

```bash
# ── Start Codex ─────────────────────────────────────────────────────────────
mycodex                                    # Codex TUI as the active account, rotation on
mycodex --profile personal                 # start on another account
mycodex resume --last                      # continue your latest thread, on any account
mycodex resume <thread id>                 # continue a specific thread (ids: mycodex threads)
mycodex <full-thread-UUID>                 # shorthand for resume; full 36-character ID
mycodex exec "summarise the last 10 commits"   # one-shot run, no TUI (inside a git repo)
mycodex -m <model>                         # any codex flag or command passes straight through
mycodex --no-rotate                        # keep this session on one account (no proxy)
mycodex --dry-run                          # show what would be launched, start nothing

# ── Quota and rotation ──────────────────────────────────────────────────────
mycodex quota                              # 5h / weekly quota left and reset times, every account
mycodex quota --watch                      # same, refreshed every 60 s (Ctrl-C to quit)
mycodex rotation status                    # order, disabled and paused accounts, who is ready
mycodex rotation log -f                    # watch switches and pauses live (Ctrl-C to quit)
mycodex rotation reset                     # clear pauses once a limit has really reset
mycodex rotation headroom 5                # fresh-request threshold on the 5h window
mycodex quota redeem personal              # explicitly spend an earned reset for this account
mycodex rotation auto-redeem off           # preserve earned reset credits (default)
mycodex rotation order work personal       # which account is tried first
mycodex rotation disable personal          # never switch to it (`rotation enable personal` undoes)

# ── Phone (remote control) ──────────────────────────────────────────────────
mycodex remote status                      # connected? paired phone? proxy and rotation pool
mycodex remote logs -f                     # follow the service log (Ctrl-C to quit)
mycodex remote restart                     # restart the phone link with the same settings
mycodex --profile work remote-control      # (re)start it as work, rotating mode
mycodex remote-control --pinned            # one account only, no rotation
mycodex remote pair                        # pairing code for the ChatGPT app (new phone)
mycodex remote clients                     # paired devices (add --revoke ID to remove one)
mycodex remote stop                        # take the phone link offline

# ── Projects and threads ────────────────────────────────────────────────────
# new thread inside the project, visible on the phone:
mycodex remote seed ~/my-project --name "First task" --message "Describe the first task here"
mycodex threads                            # recent threads with their tag and folder
mycodex threads adopt <ID>                 # copy a hidden (non-openai) thread so the phone shows it
mycodex threads link <ID> --project ~/my-project  # link and hydrate an existing thread
mycodex projects                           # registered projects, roots, and non-archived thread counts
mycodex projects add ~/my-project          # register a project without creating a thread
mycodex projects clean ~/my-project --dry-run  # preview empty/ready-check candidates

# ── Accounts (when needed) ──────────────────────────────────────────────────
mycodex profile list                       # all accounts: plan, status, quota, rotation, phone role
mycodex profile show work                  # one account: token expiry, quota windows, paths
mycodex profile use personal               # change the default account
mycodex use personal                       # same shortcut
mycodex profile add                        # log in another ChatGPT account (device code)
mycodex profile reauth personal            # log in again when it shows "auth invalid"
mycodex profile alias work_example.com phone   # then use --profile phone

# ── Health ──────────────────────────────────────────────────────────────────
mycodex status                             # one-screen overview of everything
mycodex doctor                             # full check; `mycodex doctor --fix` repairs what is safe
mycodex processes                          # every codex / mycodex process and its account
mycodex --version                          # mycodex and codex versions
mycodex help remote                        # options of any command

# ── Before an update ────────────────────────────────────────────────────────
git -C ~/mycodex status --short             # preserve/review uncommitted changes first
mycodex remote status                      # plan any restart around active work
```

The [plain-language command guide](docs/cli-guide.md) explains every parameter, default,
and side effect. Use `mycodex help quota redeem` or `mycodex projects clean --help`
without filling in required values. Native Codex options continue to pass through.

## Usage checks and readable errors

Usage is **ready** only when the service explicitly allows it. An empty reply or missing
permission shows **not confirmed** (`unknown` in JSON). A stated limit or exhausted
window shows **limit reached** (`limited` in JSON). A failed check replaces the old
cached result; legacy cached readiness without recorded permission needs a fresh check.

Damaged replies, login files, and settings produce a readable message instead of a
traceback. Invalid percentages, reset times, flags, or settings are rejected before use.
Malformed token-refresh replies leave the saved login unchanged. An unreadable
`state/accounts.json` is preserved for recovery, including pending reset retry keys;
do not delete it to clear a usage cache. `mycodex profile reauth NAME` can repair a
damaged login.

`mycodex --profile work quota` checks work; an explicit `quota home` takes precedence,
and `quota --all` checks all accounts. Watch intervals must be positive; conversation
and log counts must be positive whole numbers. Foreground phone mode accepts only an
account and mode: service-only flags are rejected with instructions.

Forced phone-account removal before confirmation (R01) and failover after an immediate
child exit (R06) remain documented in the [project review](docs/project-review.md).
Running instances keep their loaded code until a planned restart; updating source does
not reload their modules.

## Commands

<details>
<summary>Launch Codex</summary>

```bash
mycodex                               # TUI on the active account
mycodex --profile NAME                # start on NAME
mycodex resume --last                 # every codex command passes through unchanged
mycodex exec "review this repo"
mycodex --no-rotate …                 # this account only, no proxy
mycodex --rotate …                    # allow rotation even if it is disabled globally
mycodex --dry-run …                   # show what would be launched, without starting codex
mycodex -- --help                     # Codex's own help: everything after -- is verbatim
```

`codex login` and `codex logout` are replaced by the profile commands below, so a login
always lands in the right account home.

Put wrapper options before the first command or native Codex option. Global `--dry-run`
previews terminal launches, project cleanup, and migration. Both
`mycodex --dry-run projects clean NAME` and `mycodex projects clean NAME --dry-run` work.
Other management commands reject global preview instead of ignoring it. Startup may
still relocate legacy mycodex files, even for help/preview (R03 in the review).

</details>

<details>
<summary>Accounts (profiles)</summary>

```bash
mycodex profile list [--json] [--no-quota]       # plan, status, quota, rotation, remote role
mycodex profile show NAME [--json]               # identity, plan expiry, token, quota, sharing, paths
mycodex profile current                          # the active account
mycodex profile use NAME                         # make NAME the active account
mycodex profile add [NAME] [--browser]           # log in a new account (device code by default)
mycodex profile reauth NAME [--browser]          # log in again after a token expires or is revoked
mycodex profile remove NAME [--yes] [--keep-home] [--force]
mycodex profile alias NAME SHORT                 # use SHORT anywhere an account is expected
mycodex profile alias SHORT --remove
```

`profile add` runs the official `codex login` in a fresh account home and names the
profile after the account email. Logging in to an account that already has a profile just
renews that profile's login. `profile reauth` refuses a login for a different account.
`profile remove` stops the account's Codex daemon, attempts logout and deletes its home.
`--keep-home` moves the home aside **with its credentials retained**, without logout.
It refuses to remove the running relay without `--force`. There is a confirmed ordering
defect: forced relay removal stops the service before the removal confirmation. Avoid
that path until R01 in the [review](docs/project-review.md) is fixed.

</details>

<details>
<summary>Quota</summary>

```bash
mycodex quota                 # all accounts: plan, status, remaining quota, resets, rotation readiness
mycodex quota NAME            # one account
mycodex quota --json          # machine-readable
mycodex quota --watch [SEC]   # keep the table open, refreshing every SEC seconds (default 60)
mycodex quota redeem [NAME]  # consume one earned reset for NAME (default: active account)
# Optional retry controls: --idempotency-key KEY, --credit-id ID, --json
```

Use `quota NAME` to restrict ordinary quota inspection; global `--profile NAME` currently
does not restrict it. Inspection can refresh expiring credentials and write cached
snapshots. Reset consumption returns `reset`, `alreadyRedeemed`, `nothingToReset`, or
`noCredit`; the first two are successful CLI outcomes. Confirmed backend usage permission
is required before a quota pause is cleared. Preserve `state/accounts.json` when retrying
an uncertain redemption: it contains the pending idempotency key.

</details>

<details>
<summary>Rotation</summary>

```bash
mycodex rotation status               # global switch, order, disabled and paused accounts, who is ready
mycodex rotation disable              # launches use one account, no proxy
mycodex rotation enable
mycodex rotation disable NAME         # never switch to NAME
mycodex rotation enable NAME
mycodex rotation order A B C          # preferred order when an account has to be picked
mycodex rotation order --clear        # most quota left goes first
mycodex rotation reset [NAME…]        # clear pauses set after usage or rate limits
mycodex rotation headroom [PERCENT]  # fresh-turn threshold (default 5%; 0 disables it)
mycodex rotation auto-redeem on|off   # earned-reset spending, off by default
mycodex rotation log [-n N] [-f]      # switches, pauses and token refreshes
```

</details>

<details>
<summary>Remote control</summary>

```bash
mycodex --profile NAME remote-control            # start or switch the phone link (rotating mode)
mycodex --profile NAME remote-control --pinned   # one account, no rotation
mycodex remote-control --no-failover              # opt out of the new-start failover default
mycodex remote-control --cwd ~/project           # default working directory for new phone threads
mycodex remote-control --foreground              # run in this terminal instead of the service

mycodex remote status [--json]       # mode, relay, connection, host, paired phones, proxy
mycodex remote pair [--no-wait]      # short-lived pairing code for the ChatGPT app
mycodex remote clients [--revoke ID] # paired devices
mycodex remote logs [-f] [-n N]      # service logs
mycodex remote seed [DIR] [--name N] [--message T] [--project P] [--no-wait]
                                     # new thread inside DIR's project (created if missing)
mycodex remote socket                # the server's control socket, for app-server proxy scripts
mycodex remote restart
mycodex remote stop                  # stop and disable the service (phone link offline)
```

`mycodex remote start` takes the same options as `remote-control`.
Foreground mode currently honors profile/mode only; `--cwd`, failover, force, and
no-wait are accepted but not applied there. Service start can stop conflicting official
remote daemons and restart the existing service when settings change. It is a lifecycle
operation, not a status check.

</details>

<details>
<summary>Threads</summary>

```bash
mycodex threads [--all] [-n N] [--json]          # recent threads with their provider tag
mycodex threads adopt ID [--name N] [--archive-original]
mycodex threads link ID [--project DIR_OR_NAME_OR_ID]
mycodex projects [list] [--json]
mycodex projects add DIR [--name NAME] [--json]
mycodex projects clean NAME_OR_DIR_OR_ID [--dry-run] [--yes]
```

</details>

<details>
<summary>Diagnostics and migration</summary>

```bash
mycodex status [--json] [--no-quota]   # one-screen summary
mycodex processes [--json]             # every codex / mycodex process with role and account
mycodex doctor [--fix] [--yes] [--json]
mycodex doctor --fix-rollout-paths [--json]  # narrow offline repair, with a database backup
mycodex migrate [--dry-run] [--yes] [--no-compat-links]
mycodex --version
mycodex help [COMMAND [SUBCOMMAND]]
```

`doctor` checks the Codex install, every account's login, token, plan and pauses, the
shared `~/.codex` links, the socket directory, the remote service, its rotation proxy and
connection, conflicting remote servers, stale daemon settings, orphaned daemon updaters,
recent token invalidations, lingering and threads hidden from the phone. `--fix` also repairs verified legacy rollout references with a backup.
`--fix-rollout-paths` performs only that repair, without quota/auth checks. Missing,
ambiguous, or busy rollouts are reported and preserved; thread rows are never purged.

</details>

## Remote control

Remote control lets the ChatGPT phone app start and continue Codex threads on this host.
mycodex runs it as the systemd user service `mycodex-remote`, which restarts automatically
and keeps running after you close SSH.

```bash
mycodex --profile NAME remote-control
```

| Mode | Phone pairs with | Model turns | Choose it when |
|---|---|---|---|
| `rotating` (default) | NAME | move to the next ready account when one runs out | you want the phone to keep working across accounts |
| `pinned` | NAME | always NAME | you want one account only; use `--no-failover` to keep the relay fixed |

- **Pairing**: the phone must be signed in to the relay account (NAME). The host keeps the
  same installation identity for the same account/home across normal restarts and mode
  switches. Use
  `mycodex remote pair` for a manual pairing code.
- **One thread list**: in both modes the server runs as Codex's built-in `openai` provider,
  matching normal terminal launches. Project assignment, loaded state, and client
  synchronization still determine how a thread is presented.
- **One server per host**: starting the service stops Codex-managed remote daemons and
  turns their remote-control setting off to reduce conflicting registrations. New/manual
  servers can still be started outside that command; inspect status if conflicts recur.
- **Projects**: see [Projects and new threads](#projects-and-new-threads).
- **Failover**: `remote start` enables failover by default; `remote restart` preserves the
  saved choice. While the service's Codex child remains alive, periodic checks can switch
  an unauthenticated relay in either mode, or an exhausted relay in pinned mode. An
  immediate child exit currently bypasses those checks. Sign the phone into the replacement
  account and verify pairing afterwards. `--failover` / `--no-failover` select the policy.

## Projects and new threads

The phone groups threads by project. Codex files a thread under a project when the
client asks for it, which this wrapper's TUI/exec launch path does not supply. A terminal
thread can have the correct provider without a project assignment. Register a folder
and link an existing thread through the running server:

```bash
mycodex projects add ~/MyProject
mycodex threads link <thread-id> --project ~/MyProject
```

To start a new thread and first model turn inside that project:

```bash
mycodex remote seed ~/MyProject --name "Login page" --message "Plan the login page first."
```

mycodex finds the project whose root is `~/MyProject` (or creates it), starts a thread
there, names it and sends your message as the first turn, all through the server's
app-server API. Continue on the phone or with `mycodex resume <thread id>`.

Scripts can make the same calls over the official relay,
`mycodex app-server proxy --sock "$(mycodex remote socket)"`. The protocol, the JSON-RPC
sequence, filing an existing thread under a project and a ready-to-use Python script are
in [docs/projects-and-threads.md](docs/projects-and-threads.md).

## Rotation

- **Fresh-turn headroom**: the default threshold is 5% on the 5-hour window. A fresh
  model request without a turn-state token prefers a healthy alternative when the
  current account is below it. Quota snapshots older than 60 seconds are refreshed
  with a 3-second usage HTTP timeout; token refresh/lock waits can take longer. Unknown
  quotas retain reactive fallback behavior.
  Existing sticky turns retain account affinity; this cannot guarantee that a long
  turn will never exhaust its remaining quota. `rotation headroom 0` disables it.
- **Earned resets**: `quota redeem PROFILE` uses an isolated account-specific official
  app-server, never switches the phone relay, and reuses a saved idempotency key after
  an uncertain failure. It re-reads limits before clearing a quota pause. Automatic
  redemption is off by default; `rotation auto-redeem on` enables it for exhausted
  5-hour windows with a credit, before fallback to another account. Credits are
  preserved when another quota window is also exhausted.
- **Per request**: the launch profile is initially preferred, subject to pauses and
  proactive routing. When the backend answers with a usage
  limit, a rate limit or a deactivated workspace before any output, the request is sent
  again with the next ready account and the exhausted account is paused until its reset.
  At the saved reset date and time, the next fresh prompt checks the paused account again.
  A live response must explicitly confirm readiness before its configured priority is
  restored; otherwise it stays paused. The thread does not change; the turn simply
  completes on another account.
- **After commit**: once the proxy commits the response, nothing is moved automatically.
  Its four-second/256 KiB buffering limit can cause commitment before visible answer text.
  When every account is exhausted, Codex shows the usage-limit message with the earliest
  reset time.
- **Stickiness**: a session stays on the account that served it while that account works,
  subject to fresh-request headroom policy. Tokens with a known issuer are stripped when
  retrying a different account; unknown tokens cannot be attributed with that certainty.
- **Your policy applies in the session**: `rotation order` sets which account is tried next,
  `rotation disable NAME` excludes switch targets (the launch owner remains eligible),
  and `rotation disable` disables proxies for subsequent terminal launches.
  `mycodex rotation log -f` shows each switch as it happens.

Full UUIDs can also be resumed as `mycodex <UUID>`; native Codex arguments continue
to pass through.

## Accounts and shared threads

| What | Where |
|---|---|
| Account homes (login, installation id, caches) | `~/.codex/profiles/<name>`, one real Codex home each |
| Shared threads, history, thread names, config, skills | `~/.codex`, linked from every account home |
| Codex databases (threads, projects, goals) | `~/.codex/*.sqlite`, shared by every account |

Add accounts with `mycodex profile add`; each login is stored once and reused until it is
revoked or expires (`mycodex profile reauth NAME` then logs it in again). mycodex refreshes
an account's token itself only when it is about to expire or the backend rejects it.

Codex stamps every thread with the model provider it was created under and lists threads
per provider. The normal wrapper configuration uses `openai`, preserving provider
compatibility. Native provider overrides still pass through; provider tags alone do not
establish phone rendering, project membership, hydration, or client synchronization.
Threads created under another provider id (for example by prodex) are hidden from the
phone; `mycodex threads` marks them and `mycodex threads adopt ID` forks one into an
`openai` copy with the same history, name and project. The original is kept.

## Files and layout

Everything mycodex owns lives in its own directory (`~/mycodex`):

| Path | Contents |
|---|---|
| `bin/`, `src/`, `tests/`, `install.sh` | the CLI |
| `assets/`, `docs/` | banner, architecture and design notes |
| `config.json` | active account, rotation policy, remote settings, aliases (mode 0600) |
| `state/accounts.json` | pauses, quota snapshots, adoption mappings, pending reset idempotency keys; preserve for retries |
| `state/backups/` | private consistent SQLite backups made before verified path repair |
| `state/logs/proxy.log` | rotation events: switches, pauses, token refreshes (no secrets) |
| `state/proxies/`, `state/locks/`, `state/remote-serve.json` | running proxies, refresh locks, service state |
| `systemd/mycodex-remote.service` | the generated service unit |

Outside it there are only the account homes in `~/.codex/profiles` and links the system
needs: `~/.local/bin/mycodex` and `~/.config/systemd/user/mycodex-remote.service`.
`install.sh` restores the launcher link. `doctor --fix` repairs missing shared-home links
and can replace an existing managed unit copy; it does not install an absent launcher/unit.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mycodex exec` prints `failed to connect to websocket: HTTP error: 426 Upgrade Required` | Expected. The proxy speaks HTTPS only; Codex switches to HTTPS for the session and continues. |
| `socket parent must be owned by the user or root …` when running `codex remote-control` yourself | Your umask makes Codex's socket directory group-writable. Use `mycodex remote-control`, which runs with umask 0022. |
| The phone shows the host but no project | `mycodex projects add ~/your-project`; link an existing thread or seed a new one |
| A thread is missing on the phone | `mycodex threads`; if its tag is not `openai`, `mycodex threads adopt ID` |
| An existing thread is outside its project or unloaded | `mycodex threads link ID --project DIR_OR_NAME_OR_ID` ([guide](docs/projects-and-threads.md)); then verify phone synchronization |
| Logs report a stale `.prodex` rollout path | `mycodex doctor --fix-rollout-paths`; inspect unresolved entries and retain its backup |
| A known older thread cannot be linked by UUID | The current lookup scans only the latest 5,000 rows; see R08 in the review |
| An account shows `auth invalid`, or logs mention `token_invalidated` | `mycodex profile reauth NAME`. Avoid running other long-lived Codex servers on the same account. |
| An account stays `paused` after its limit reset | Check the saved reset date and time with `mycodex status` or `mycodex rotation status`. The next fresh prompt rechecks the account once that time is due; it returns to configured priority only after a live ready response. If usage is still limited or not confirmed, it remains paused. `mycodex rotation reset NAME` clears the pause manually. |
| `another remote-control server is running` | Stop it, or rerun with `--force`. |
| The remote service keeps restarting | `mycodex remote logs` and `mycodex doctor` |
| `doctor` warns about an untested Codex version | mycodex was verified on Codex 0.160.x; check `mycodex remote status` and a short `mycodex exec` after updates. |

## Security and responsible use

- Normal UI/event fields omit token values and display non-secret claims such as email,
  plan and expiry. Login/reauth flows copy new credentials from a temporary login home
  into the selected profile; refresh writes updated credentials atomically with mode 0600.
  Redemption supplies an access token to a temporary official server without copying
  the refresh token.
- The rotation proxy listens on 127.0.0.1 only and accepts connections only from processes
  of your own user on Linux when `/proc/net/tcp` is available. The development override
  `MYCODEX_PROXY_ANY_UID=1` disables that check; missing `/proc/net/tcp` also bypasses it.
  The proxy is not an isolation boundary between processes of the same user.
- A paired phone can do everything a Codex session on the host can do. If
  `~/.codex/config.toml` disables approvals and the sandbox, phone threads run commands
  without asking; turn approvals on if that is not what you want.
- Pairing codes are short-lived. Review paired devices with `mycodex remote clients` and
  remove one with `--revoke`.
- Make sure your use of several accounts complies with the terms of your ChatGPT and
  OpenAI plans.

## Compatibility

| Component | Verified |
|---|---|
| OS | Debian 13 (trixie), systemd 257 |
| Python | 3.13 (3.10+ supported) |
| Codex CLI | 0.160.0 |

`mycodex doctor` warns when Codex moves outside the verified series.
The installed experimental schema and isolated initialization were checked against
0.160.0. A real reset redemption and pinned relay failover were not exercised. The
whole-project review records further gaps; a passing doctor result is not an end-to-end
phone, recovery, or concurrency guarantee.

## Documentation

- [QUICKSTART.md](QUICKSTART.md): from install to a paired phone, step by step
- [docs/workflow-audit.md](docs/workflow-audit.md): audited workflow gaps, fixes, and validation limits
- [docs/project-review.md](docs/project-review.md): whole-project review, reproduced open defects, and priorities
- [docs/projects-and-threads.md](docs/projects-and-threads.md): new project threads, with one command or through the app-server proxy
- [docs/architecture.md](docs/architecture.md): accounts, the rotation proxy, the remote service and why each piece exists
- [docs/discovery-and-design.md](docs/discovery-and-design.md): how Codex was inspected and the decisions that followed

## Author

Created and maintained by **Manoj Verma** ([@manojvermamv](https://github.com/manojvermamv)).

mycodex drives the official [Codex CLI](https://github.com/openai/codex) by OpenAI. Its
shared-home layout, error classification and terminal style are adapted from
[prodex](https://github.com/christiandoxa/prodex) by
[@christiandoxa](https://github.com/christiandoxa) (Apache-2.0); see [NOTICE](NOTICE).
mycodex is an independent project and is not affiliated with OpenAI.

## License

[MIT](LICENSE) © 2026 Manoj Verma

## Release verification

Run `python3 -B tools/run_tests.py` from a clone to execute the 114-test suite in
temporary homes with external HTTP, child processes, and signals blocked. The tests
use local fake backends; they do not consume real reset credits or operate live services.

The 2026-10-05 host deployment completed successfully and retained its phone identity,
pairing, and conversation projects. Its corrected temporary observer passed two-minute
monitoring and removed its own unit. A later read-only check confirmed the PATH launcher,
service launcher, source snapshot, proxy, and protected processes. The version metadata
change to v0.3.0 does not restart or reload an already-running Python process.

R01/R06 and the other open review items are explicitly retained in the
[project review](docs/project-review.md). Personal host notes, runtime state, credentials,
and temporary recovery scripts are excluded from the public repository and archives.
