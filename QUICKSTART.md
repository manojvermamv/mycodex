# mycodex Quickstart

This path installs mycodex, logs in your ChatGPT accounts, launches the official Codex
TUI with automatic rotation, and connects the ChatGPT phone app through remote control.
This guide targets **v0.4.0**, dated 2026-10-08.
The [changelog](CHANGELOG.md) and [everyday commands](docs/cheatsheet.md) show the updates. See the [whole-project review](docs/project-review.md)
for remaining defects and [workflow audit](docs/workflow-audit.md) for verified fixes.

## Understand commands and messages

The [command guide](docs/cli-guide.md) explains every option in everyday language.
A saved account is a **profile** in command names, and a conversation is a **thread**.
Use `mycodex help profile add` or `mycodex threads link --help` for detailed instructions
without supplying an account or conversation ID. Existing commands and flags still work.

- **ready**: the service explicitly confirmed that usage is allowed.
- **limit reached**: wait for the displayed reset, switch accounts, or deliberately spend
  an earned credit with `mycodex quota redeem NAME`.
- **not confirmed**: try the usage check again later. Missing or malformed information
  does not count as readiness. JSON uses the status `unknown`.
- **sign in again**: run `mycodex profile reauth NAME`. Damaged login files can be repaired
  through the same command.

`mycodex --profile work quota` checks that account; `quota --all` checks every account.
Watch seconds and list/log counts must be greater than zero. Incorrect values give an
explanation before the command runs. Foreground phone mode rejects service-only options.
Damaged settings/history are preserved, so correct the reported file or recover it
from a backup. Never erase account history just to clear a quota cache.

The two deferred service issues are [R01 and R06](docs/project-review.md#deferred-service-lifecycle-issues).
No running service is automatically restarted to load these source changes.

## 1. Check prerequisites

You need:

- a Linux host with a systemd user instance (Debian 13 tested);
- Python 3.10 or newer (`python3 --version`);
- the official [Codex CLI](https://developers.openai.com/codex/cli) 0.160.x;
- one or more ChatGPT accounts with Codex access.

Install Codex if it is missing:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

So that remote control keeps running after you log out or reboot, enable lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

## 2. Install mycodex

```bash
git clone https://github.com/manojvermamv/mycodex.git ~/mycodex
~/mycodex/install.sh
```

`~/.local/bin` must be on your `PATH`. Then check the stack:

```bash
mycodex --version
mycodex doctor
```

Inspect every doctor warning: its exit status is nonzero for failures, but warnings can
still return zero. Ordinary quota/doctor checks may refresh credentials and save quota
snapshots; they are not strictly offline. `mycodex profile list --no-quota` skips usage
queries. Help/preview currently may relocate legacy mycodex state on startup.

## 3. Add your accounts

Log in each account once. On a server, the default device-code flow prints a link and a
one-time code to open on any device:

```bash
mycodex profile add
```

Repeat for every account. Profiles are named after the account email
(`you_example.com`); pass a name to choose your own (`mycodex profile add work`). Each login
is stored in its own Codex home, `~/.codex/profiles/<name>`, and reused, so you do not log
in again. The first account you add becomes the active one.

Already using prodex? Move its accounts instead of logging in again:

```bash
mycodex migrate --dry-run
mycodex migrate
```

Migration can restart an active remote service. Schedule it around existing work and
inspect the result before removing compatibility paths. If doctor finds stale legacy
rollout references, use `mycodex doctor --fix-rollout-paths` and inspect unresolved entries.
The README's [Coming from prodex](README.md#coming-from-prodex) covers later cleanup.

Optional short names:

```bash
mycodex profile alias you_example.com work
```

## 4. Check quota and readiness

```bash
mycodex profile list
mycodex quota
```

`ROTATION READY` shows which accounts can take requests right now.
For one account use `mycodex quota work`; global `--profile work` currently does not
restrict ordinary quota inspection.

## 5. Launch Codex

```bash
mycodex
```

You are in the official Codex TUI, on the active account, with rotation on. Everything
Codex accepts works the same way:

```bash
mycodex --profile work              # start on a specific account
mycodex resume --last               # continue the latest thread (any account can resume it)
mycodex <full-thread-UUID>           # shorthand for resume with a full 36-character ID
mycodex exec "review this repository"
mycodex --no-rotate                 # this account only for this session
```

All accounts share `~/.codex`, so threads, history, skills and settings are the same
whichever account you start on.

## 6. Tune rotation (optional)

```bash
mycodex rotation status
mycodex rotation order work personal    # which account is tried next
mycodex rotation disable personal       # never switch to it
mycodex rotation disable                # turn rotation off for every launch
mycodex rotation log -f                 # watch switches as they happen
mycodex rotation headroom 5             # default fresh-request 5h threshold
mycodex rotation auto-redeem off        # credits preserved by default
mycodex quota redeem work               # explicitly consume an earned reset, if needed
```

Fresh model requests without a sticky turn token can prefer a healthier account when
the current account's 5h headroom is below 5%. Recent usage snapshots are required.
Eligible failures before response commitment retry another account in the same thread;
failures after commitment are returned to Codex. Clearing a pause does not reset quota.
When a persisted 5-hour or weekly quota reset date and time is due, the next fresh
prompt checks that profile again. It regains its configured priority only when the service explicitly confirms it is
ready. Unknown or still-limited results keep it paused. An in-progress turn stays with its
current account; the next fresh prompt can use the restored priority.
`rotation disable personal` excludes it as a switch target, but a proxy launched as that
profile still includes its owner. The global switch applies to later terminal launches.

Reset redemption reuses a persistent idempotency key after an uncertain failure. Keep
`state/accounts.json` intact for retries. Automatic spending requires explicitly enabling
`rotation auto-redeem on`; no credit is spent merely by checking quota.

## 7. Connect your phone

Choose the account your ChatGPT phone app is signed in to and start remote control:

```bash
mycodex --profile work remote-control
```

mycodex starts the `mycodex-remote` service, waits until the host is connected, and shows
the host name, paired devices and the rotation proxy. In the ChatGPT app, open Codex and
pick this host. If it does not appear, create a pairing code and enter it in the app:

```bash
mycodex remote pair
```

Starting remote control is a lifecycle action: it can stop conflicting official remote
daemons or restart the existing service. New starts enable relay failover by default;
`--no-failover` opts out. `remote restart` preserves the saved choice. Periodic failover
currently needs the child to stay alive; it does not cover immediate child exit. A relay
account change can require phone sign-in/pairing with the replacement account.

If the relay needs a Plus account's model catalogue, copy it while the phone service keeps
running:

```bash
mycodex remote models share work
```

This saves `work` as the source and updates only the relay's model cache; it does not
restart the service or change pairing. Later managed service starts refresh the saved cache
before the server launches. An unavailable or invalid cache logs a warning and still starts
the phone relay with its previous cache.

Register a project without a model turn, then link an existing thread:

```bash
mycodex projects add ~/your-project --name "My Project"
mycodex projects
mycodex threads link <thread-id> --project ~/your-project
```

To start a new thread inside a project (the project is created if it does not exist yet):

```bash
mycodex remote seed ~/your-project --name "First task" --message "Describe the first task here"
```

The wrapper's terminal launch does not assign a project. Linking updates metadata and
resumes the existing thread on the server without a turn or restart. Correct provider
tags and loaded state help establish eligibility; phone rendering still depends on the
client. See [docs/projects-and-threads.md](docs/projects-and-threads.md).

Prefer one account with no rotation for the phone?

```bash
mycodex --profile work remote-control --pinned --no-failover
```

The phone and your terminal sessions share one thread list. The service restarts on its
own after child exits, subject to systemd restart limits, and keeps running after you
close SSH with lingering enabled. Foreground debugging lacks service failover and
rejects cwd/failover/force/no-wait flags instead of ignoring them.

## 8. Everyday checks

```bash
mycodex status            # accounts, rotation, phone link, sessions, versions
mycodex remote status     # connection, relay account, paired phones, proxy
mycodex threads           # recent threads and their provider tag
mycodex processes         # every codex / mycodex process and its account
mycodex remote logs -f    # follow the remote-control service
```

## 9. Diagnose a problem

```bash
mycodex doctor
mycodex doctor --fix
```

Common fixes:

- `auth invalid` on an account: `mycodex profile reauth NAME`
- an account still paused after its reset: check the saved reset date and time in
  `mycodex status` or `mycodex rotation status`. The next fresh prompt rechecks it after
  that time; a live ready result restores its configured priority. If the service still
  reports a limit or cannot confirm readiness, it stays paused. `mycodex rotation reset
  NAME` clears the pause manually.
- a thread with another provider: `mycodex threads adopt ID` creates an `openai` copy
- an existing thread missing its project/loading: `mycodex threads link ID --project DIR`
- phone shows no project: `mycodex projects add ~/your-project`, then link or seed
- stale legacy rollout paths: `mycodex doctor --fix-rollout-paths` (backed up, no usage query)
- another remote-control server is running: stop it, or `mycodex remote-control --force`
- remote service restarting: `mycodex remote logs`

More in the README's [Troubleshooting](README.md#troubleshooting).

Preview cleanup with `mycodex projects clean "My Project" --dry-run`. After inspecting
the candidates, omit that flag to confirm archival. Only empty or untouched built-in
ready-check histories qualify; the client rechecks them, but does not provide an atomic
cross-client cleanup lock. Global `mycodex --dry-run projects clean ...` also previews
cleanup; other management commands reject unsupported preview.

## 10. Update or uninstall

Inspect uncommitted changes before updating:

```bash
git -C ~/mycodex status --short
mycodex remote status
```

Preserve your changes, then update from the desired revision. In a clean checkout:

```bash
git -C ~/mycodex pull --ff-only
~/mycodex/install.sh
```

New invocations load the updated code. Running processes keep their loaded modules;
plan `mycodex remote restart` separately when active turns can tolerate interruption.
No service restart is needed simply to update documentation.

Uninstall. Your accounts in `~/.codex/profiles` and your threads in `~/.codex` stay where
they are, so plain `codex` with `CODEX_HOME=~/.codex/profiles/<name>` still works:

```bash
mycodex remote stop
rm ~/.local/bin/mycodex ~/.config/systemd/user/mycodex-remote.service
systemctl --user daemon-reload
rm -rf ~/mycodex
```

## Next references

- [README](README.md): features, commands and troubleshooting
- [docs/projects-and-threads.md](docs/projects-and-threads.md): project threads, with one command or through the app-server proxy
- [docs/architecture.md](docs/architecture.md): how accounts, the rotation proxy and the remote service work
- [docs/discovery-and-design.md](docs/discovery-and-design.md): the investigation behind the design
- [docs/workflow-audit.md](docs/workflow-audit.md): completed workflow fixes and live validation limits
- [docs/project-review.md](docs/project-review.md): remaining issues from the fresh whole-project review

## v0.4.0 validation and deployment — 2026-10-08

For local regression checks, run `python3 -B tools/run_tests.py`; the suite contains
132 isolated tests. The package has 29 modules. The successful 2026-10-05 restart was
for v0.3.0; the v0.4.0 guarded restart is pending. This conversation runs in the relay
cgroup. After release validation, an independent observer will be armed and wait for
all active threads/tools to finish and ten seconds idle. See
[redeployment.md](docs/redeployment.md) for the procedure and its limits.

New CLI commands report v0.4.0. Updating source alone does not reload the running proxy;
already-running processes retain their loaded modules until a guarded restart.
