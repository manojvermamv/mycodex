# mycodex architecture

This document explains how mycodex works internally and why each mechanism exists. For
everyday use, read the [README](../README.md) and the [Quickstart](../QUICKSTART.md).
The investigation behind these choices is in [discovery-and-design.md](discovery-and-design.md).
Reviewed against the full working tree on 2026-10-04; these additions are published
in **v0.3.0** on 2026-10-05. See the [changelog](../CHANGELOG.md). [project-review.md](project-review.md) records open issues;
this document describes actual mechanisms rather than guarantees of every failure path.

## Layering

```text
mycodex ──► official codex (unmodified)
   │             │
   │             └─ model requests ──► rotation proxy (in the mycodex process) ──► chatgpt.com
   ├─ accounts: ~/.codex/profiles/<name> (one CODEX_HOME each), shared entries linked to ~/.codex
   ├─ credentials: reads auth.json, refreshes a token only when it is expiring or rejected
   └─ quota, rotation policy, remote-control service, threads, migration, diagnostics
```

Rules mycodex follows:

- It never reimplements Codex. Every Codex feature runs in the official binary.
- Codex's SQLite stores are read-only for ordinary inspection. The narrow exception is
  `doctor --fix` / `--fix-rollout-paths`: a backed-up, identity-verified update of legacy
  rollout paths, checking existing writer locks. It never deletes thread rows. Creation
  of a new lock during discovery remains a concurrency risk.
  Threads and projects otherwise change only through
  Codex's official app-server API (`thread/fork`, `thread/name/set`, `project/create`, …).
- Refresh writes `auth.json` atomically with mode 0600, preserving other fields.
  Login/reauth use official `codex login` in a temporary home and install those new
  credentials into the selected profile. Deletion attempts official logout; `--keep-home`
  retains the renamed home and credentials without logout.
- Anything mycodex does not own passes through unchanged: `mycodex <args>` runs
  `codex <args>` with the chosen account's environment.

| Module | Responsibility |
|---|---|
| `profiles.py` | account homes, shared links, adopting per-home data |
| `__init__.py`, `paths.py`, `config.py`, `resolve.py` | version/compatibility constants, locations/environment, settings/locking, profile selectors |
| `cli.py`, `accounts.py` | argument routing and profile/quota/rotation commands |
| `helptext.py`, `validation.py` | shared plain-language terminal/Markdown help and field-only boundary checks |
| `auth.py` | reading `auth.json`, non-secret claims, token refresh |
| `appserver.py` | WebSocket control-socket RPC and isolated stdio RPC transports |
| `quota.py`, `redemption.py` | usage snapshots and isolated, idempotent earned-reset redemption |
| `projects.py`, `maintenance.py` | project workflows and narrow legacy rollout-path repair |
| `state.py` | shared pauses/cache plus durable adoption mappings and pending reset retry keys |
| `proxy.py` | the rotation proxy |
| `launch.py` | starting codex as a profile, with or without the proxy |
| `remote.py`, `remote_cmd.py` | the remote-control service and its commands |
| `threads.py`, `migrate.py` | thread list and adoption, prodex migration |
| `doctor.py`, `views.py`, `procs.py` | diagnostics, status, process discovery |
| `codex.py` | official installation/daemon inspection and read-only database queries |
| `fmt.py`, `ui.py` | local-time formatting and terminal/plain rendering |

## Accounts and shared state

Each account is a real Codex home, `~/.codex/profiles/<name>` (mode 0700):

| Per account | Shared (symlinks into `~/.codex`) |
|---|---|
| `auth.json`, `installation_id`, `models_cache.json`, `version.json`, `cache/`, `log/`, `packages/`, `app-server-daemon/`, `app-server-control/` | `sessions/`, `archived_sessions/`, `history.jsonl`, `session_index.jsonl`, `config.toml`, `managed_config.toml`, `environments.toml`, `AGENTS*.md`, `.credentials.json`, `skills/`, `rules/`, `memories*/`, `plugins/`, `agents/`, `attachments/`, `image_attachments/`, `shell_snapshots/`, `thread-writer-locks/`, `.tmp/` plugin caches and the rollout maintenance lock, every `*_N.sqlite` (with `-wal`/`-shm`) and `*.config.toml` |

Every Codex child also gets `CODEX_SQLITE_HOME=~/.codex`, so the thread, project, goal,
memory, queue and log databases are the same for every account. That is what makes a
thread started on one account resumable on another.

Two entries are shared that prodex keeps per account:

- `session_index.jsonl` holds thread names. Per account, a thread renamed on the phone
  would keep its old name in another account's terminal.
- `thread-writer-locks/` holds Codex's exclusive per-thread writer locks. With shared
  sessions, two accounts could otherwise write the same thread at once.

`prepare_home` runs before every launch and creates missing links. A real file or
directory where a link belongs is reported, never overwritten during normal preparation.
Launch currently continues despite reported private entries, so sharing must be verified
with doctor. `doctor --fix` and
`migrate` *adopt* the entries that can be folded in safely: thread names and history are
merged by timestamp; idle lock files are replaced. Session directories are never moved
automatically.
JSONL merge is read/replace without coordination with concurrent Codex appenders; an
atomic rename alone does not make adoption safe during writes. See the review.

`codex login` writes to the account home passed in `CODEX_HOME`; `profile add` points it
at a hidden `.pending-*` home, reads the email from the new token's claims and renames the
home to the profile name. `config.toml` edits by Codex go through the symlink (Codex
resolves symlinks before writing), so the shared config stays shared.

## Launching Codex

```text
CODEX_HOME=~/.codex/profiles/<account>   CODEX_SQLITE_HOME=~/.codex
MYCODEX_PROFILE=<account>                MYCODEX_ROLE=session
codex -c openai_base_url="http://127.0.0.1:<port>/backend-api/codex" <your args>
```

- Without rotation (`--no-rotate`, `rotation disable`, or a command that never calls the
  model such as `login` or `mcp`), mycodex simply `exec`s codex with that environment.
- With rotation, mycodex stays as a small parent: it starts the proxy on an ephemeral
  loopback port, runs codex as a child, ignores Ctrl-C (the terminal delivers it to codex
  directly), forwards TERM/HUP/QUIT, and exits with codex's status.
- `openai_base_url` is Codex's own setting for the built-in `openai` provider. Codex keeps
  the provider id `openai`, so threads are tagged `openai` exactly as with plain `codex` or
  the phone. Because a `-c` override is present, the TUI also never attaches to a shared
  background daemon, so its model traffic always goes through the proxy.

These are the normal provider settings; explicit native provider/base-URL overrides still
pass through. Proxy selection examines the first native argument, so leading native flags
can prevent recognition of a command that otherwise needs no proxy. Global `--dry-run`
suppresses launch/link preparation and forwards preview to cleanup/migration. Other
management commands reject preview. Startup legacy migration still happens before parsing (R03).

## The rotation proxy

### Transport

The built-in provider prefers WebSockets. The proxy answers every upgrade with
**426 Upgrade Required**; Codex treats that as "this endpoint has no WebSocket support" and
uses HTTPS (SSE) for the rest of the session. Over HTTPS Codex sends the full input with
every request (no `previous_response_id`), so any request can move to another account
without server-side continuation state.

Request bodies (zstd-compressed by Codex) are forwarded untouched. Only these headers
change: `Authorization` and `ChatGPT-Account-Id` are replaced with the chosen account's,
`Accept-Encoding` becomes `identity` so error bodies can be read, and hop-by-hop headers
are dropped.

### Choosing an account

For each request the proxy builds a plan:

1. the account that issued the request's `x-codex-turn-state` (Codex's sticky-routing token
   for one turn);
2. the account that last served the request's session (`session-id` / `thread-id`);
3. the proxy's current account (initially the launch profile);
4. other logged-in, unpaused candidates: first `rotation.order`, then cached headroom.

Candidates are not all proven ready: unknown quotas remain candidates for reactive
fallback, and limited snapshots are ranked lower rather than universally excluded.

For fresh model requests without a turn-state token, an account below the configured
5-hour headroom threshold (default 5%) moves behind healthy alternatives. Stale quota
snapshots older than 60 seconds are refreshed at this boundary, using a 3-second usage
HTTP timeout. Token-lock/refresh waits and batched accounts can take longer. A snapshot
must be no older than 300 seconds for proactive headroom decisions. No request with a
turn-state token is moved
proactively. Optional `auto_redeem` spends an earned reset at an exhausted 5-hour window
before fallback; it defaults to false. Redemption uses a temporary stdio app-server with
externally managed ChatGPT tokens and a persistent idempotency key, then reads backend
limits. It never changes the relay account or copies refresh tokens.

Excluded: accounts without a login, accounts in `rotation.disabled` (except the launch
profile), and accounts paused in `state/accounts.json`. If everything is paused, the
account whose pause ends first is asked anyway, so Codex receives a real answer.

### When a request moves

The rules are adapted from prodex's runtime error policy:

| Backend answer (before any output reached Codex) | Class | Action |
|---|---|---|
| 429/402/403 with `usage_limit_reached`, `insufficient_quota`, … or "you've hit your usage limit" | quota | pause until `resets_at` (refined from the usage endpoint), next account |
| 429 with `rate_limit_exceeded` / `slow_down`, or any other 429 | rate | pause 90 s, next account |
| 402/403 with `deactivated_workspace` | profile | pause 6 h, next account |
| 401 | auth | refresh that account's token once and retry; if it fails again, pause and move on |
| 5xx, overload, 400, anything else | other | pass through (Codex has its own retries) |

For SSE responses the proxy holds back the first events (`response.created`,
`response.in_progress`) for up to four seconds. If the first real event is a
`response.failed` or `error` with a quota, rate or workspace code, the stream is dropped
and the request goes to the next account. The buffer is also limited to approximately
256 KiB. Otherwise held events and the rest of the stream are passed through. Commitment
can occur on bookkeeping timeout/size limits before visible answer text; after commit
the proxy does not move the request to another account.

When every account fails, Codex receives the backend's own error; for usage limits, the
one with the earliest `resets_at`, so Codex's message names the earliest time a turn can
succeed. Tokens with a known issuer are stripped when retrying a different account.
Issuer tracking is in-process and bounded; unknown tokens cannot be attributed with that
certainty. It is a routing aid rather than a durable cross-proxy guarantee.

### Credentials

`auth.credentials()` reads `auth.json` (retrying briefly, because Codex rewrites it in
place) and returns the access token if it is valid for more than ten minutes. Otherwise,
or after a 401, `auth.refresh()`:

1. takes a per-account lock in `state/locks/`,
2. re-reads `auth.json` and stops if another process already stored a newer valid token,
3. calls `POST https://auth.openai.com/oauth/token` with Codex's client id and JSON body,
4. writes the new tokens and `last_refresh` atomically (temp file and rename, mode 0600).

Access tokens live about ten days and Codex refreshes its own account after eight, so the
proxy refreshes mostly for accounts no Codex process runs as. A refresh failure that
cannot be retried (`refresh_token_expired`, `refresh_token_reused`, `invalid_grant`, 401)
pauses the account for an hour and asks for `mycodex profile reauth`.
The lock coordinates mycodex refreshers only. Official Codex token writers and
login/reauth installation do not acquire it, leaving a shared-account refresh race.

### Shared state, logs and safety

- `state/accounts.json`: pauses, quota snapshots, adoption mappings, and pending reset
  idempotency keys. Reads may be cached for two seconds; writes use an advisory lock and
  atomic replacement. This file is not wholly disposable: deleting it loses durable
  adoption/retry identity. Corrupt JSON currently becomes empty state, an open issue.
- `state/logs/proxy.log`: one line per request, switch, pause and refresh, without
  secrets, rotated at 5 MB (`mycodex rotation log`).
- `state/proxies/<pid>.json`: the port and owner of each running proxy;
  `GET /__mycodex/health` reports the current account and counters.
- The proxy binds to 127.0.0.1 and accepts a connection only if `/proc/net/tcp` shows the
  client socket belongs to the same Unix user on Linux. `MYCODEX_PROXY_ANY_UID=1` or
  absence of `/proc/net/tcp` bypasses the check. Requests, SSE reader queues, and queued
  RPC notifications currently lack explicit memory bounds. Log rotation is serialized
  within a process, not across all proxies.

## Configuration and reload boundaries

`config.json` is separate from Codex's shared `config.toml` and contains no tokens.
The settings schema version is 2. Its defaults include:

| Setting | Default | Effect |
|---|---|---|
| `active` | `null` | selected terminal profile; first/only profile resolution can supply one |
| `rotation.enabled` | `true` | proxy use for later terminal launches |
| `rotation.order`, `rotation.disabled` | `[]` | fallback preference/exclusion; proxy owner remains a candidate |
| `rotation.min_quota_headroom` | `5.0` | fresh model-request 5h threshold; 0 disables proactive routing |
| `rotation.auto_redeem` | `false` | explicit opt-in to earned-reset spending |
| `remote.mode` | `rotating` | separate service mode; global terminal rotation switch does not pin it |
| `remote.failover` | `true` | new-start default; restart preserves the stored value |
| `remote.failover_check_seconds` | `300` | periodic service check, with a 60-second minimum |

Proxy pool/policy refresh is cached for up to 20 seconds. An updated config can therefore
affect supported policy in a running proxy after that delay, but changing source files
does not reload its Python modules. Supervisor failover settings are captured at startup.
Changing the active terminal profile does not switch the phone relay. Use explicit service
lifecycle commands when a relay change/restart is intended.

`MYCODEX_HOME`, `MYCODEX_SHARED_CODEX_HOME`, and `MYCODEX_CODEX_BIN` override tool storage,
shared storage, and binary resolution. XDG overrides affect systemd/legacy locations.
`MYCODEX_UPSTREAM` and `MYCODEX_USAGE_URL` are development endpoint overrides;
`MYCODEX_WIDTH`, `MYCODEX_COLOR`, and `NO_COLOR` control rendering. Keep test overrides
out of service environments.

## Quota

`GET https://chatgpt.com/backend-api/wham/usage` with the account's bearer,
`ChatGPT-Account-Id`, `originator: codex_cli_rs` and a Codex user agent: the endpoint
Codex's own `/status` uses. Windows of 18 000 s, 604 800 s and 2 592 000 s are shown as
`5h`, `weekly` and `30d`. An account is `limited` when the backend says so or a window is
at 100 %; it is usable again when every exhausted window has reset.
Both HTTP and reset RPC parsers require explicit backend permission for readiness.
Missing permission yields unknown; explicit denial, reached limits, or exhausted windows
yield limited. Object shapes, strict booleans, finite numeric values, positive window
durations, integer reset times, and credit counts are checked at boundaries. Failed
HTTP/auth checks replace old quota snapshots with non-ready results. `usage_allowed`
records permission in the saved snapshot; legacy ready cache without that field is
downgraded until checked again. This resolves R07. Quota commands,
ordinary doctor, and text remote status may refresh credentials and save cached snapshots.
Reset times are rendered in the process's local timezone.

## Remote control

Remote control runs as the systemd user service `mycodex-remote`. Its unit lives in
`~/mycodex/systemd/` and is linked into `~/.config/systemd/user/`. The service runs
`mycodex __remote-serve`, a supervisor that starts the official foreground
`codex remote-control` as the relay account and exits with it, so systemd restarts it
(`Restart=always`, `RestartSec=10s`), subject to the unit's 10-start/300-second limit.

| Mode | Command inside the service | Phone pairs with | Model turns | Thread tag |
|---|---|---|---|---|
| `rotating` | `codex -c openai_base_url="http://127.0.0.1:<port>/backend-api/codex" remote-control` (proxy in the supervisor) | P | rotate across ready accounts | `openai` |
| `pinned` | `codex remote-control` | P | always P | `openai` |

Why each piece exists:

| Piece | Reason |
|---|---|
| `UMask=0022` | Codex creates its private socket directory with the process umask (tempfile 3.27 default mode). Under Debian's default `0002` the directory is group-writable and Codex refuses to bind ("socket parent must be owned by the user or root…"). |
| `CODEX_HOME` = relay account | Preserves `installation_id` for that home and supports the existing account-keyed enrollment; a different relay account can change enrollment/pairing. |
| proxy inside the supervisor | The service has its own proxy; stopping it never affects terminal sessions, and its events go to the journal. |
| `mycodex __remote-serve` parent | Forwards signals, records `state/remote-serve.json` (including the proxy port), and with `failover` checks the relay account every few minutes. |

**Conflicts.** Before starting, `mycodex remote start` stops codex-managed daemons that
have remote control enabled (`codex remote-control stop`, then
`codex app-server daemon disable-remote-control` so a later auto-start cannot re-enable
it) and stops their idle updater loops. Foreground `codex remote-control` processes that
mycodex did not start are left alone unless `--force` is given.

**Failover.** `remote start` defaults to failover on; `remote restart` preserves the saved choice.
With `--failover`, the supervisor checks the relay account every
`failover_check_seconds` (default 300). If the account can no longer authenticate (both
modes) or is limited (pinned mode), it switches `remote.profile` to the next ready account
and exits with status 75; systemd restarts the service as that account. The phone must then
be signed in to the new account.
This path runs only while the child remains alive through a check. An immediate child
exit returns without a failover decision (R06). Foreground mode has no failover tick and
now rejects cwd/failover/force/no-wait flags with a readable error (R05 resolved). Start/restart/stop and
daemon conflict handling can interrupt active work; none are used just to refresh docs.

**Control socket.** Status, pairing, client lists, seeding and thread adoption talk to the
running server with JSON-RPC over WebSocket through the official relay
`codex app-server proxy --sock <socket>`. mycodex finds the socket from the server process
(`/proc/<pid>/fd` and `/proc/net/unix`); `mycodex remote socket` prints it for scripts.
The foreground server does not publish the default socket of its `CODEX_HOME`, so the
relay always needs `--sock`. `mycodex app-server proxy|daemon|generate-*` exec codex
directly, without a rotation proxy.

## Threads

Codex records a thread's provider when the thread is created, in the rollout's
`session_meta` and in SQLite, re-derives it from the rollout when it indexes the thread,
and filters `thread/list` by the server's provider. Normal mycodex launches retain
`openai`, making terminal/phone providers compatible. Explicit provider overrides and
client loading/filtering still matter; doctor cannot prove phone visibility from tags.

Projects are separate from tags. A thread belongs to a project only when a client passes
the project id (`thread/start` with `projectId`, or `thread/metadata/update`); Codex does
not derive it from the working directory, and the wrapper's TUI/exec launch does not
supply one.
`mycodex remote seed DIR` therefore runs on the server: it reuses or creates the project
whose root is DIR (the idempotency key is derived from the path), starts a thread with that
project, names it and starts the first turn, which is what saves a thread. See
[projects-and-threads.md](projects-and-threads.md).

A thread created under another provider id (prodex used `prodex-openai-governed-http`)
cannot be re-tagged safely: editing SQLite alone is undone by the next re-index. `mycodex
threads adopt ID` instead calls `thread/fork` with `modelProvider: "openai"` on the running
server, copies the name with `thread/name/set`, keeps the project with
`thread/metadata/update`, optionally archives the original, and records the pair in
`state/accounts.json` so the original is no longer reported as hidden.
The mapping is written at the end; failure after fork can leave a partial copy and retry
is not fully idempotent. Direct thread lookup scans only the latest 5,000 rows, while
display reads at most 2,000. Database errors can be represented as empty results.

`threads link ID --project DIR_OR_NAME_OR_ID` assigns an existing thread and calls
`thread/resume` to load it on the running server, without starting a turn. A `notLoaded`
thread is normally just unloaded; loading it emits the server lifecycle notifications,
but mobile rendering remains client-dependent. Project discovery handles every page
and rejects ambiguous names/roots. `projects add` creates no thread. `projects clean`
only archives verified empty histories or a single untouched built-in ready-check turn,
with confirmation and a second history check; it skips active work/descendants observed
at those checks. Read and archive are separate RPCs, so cross-client races require backend
verification. Counts are non-archived membership counts, not loaded/idle/active counts.
Seed completion waits match the exact thread and turn IDs.

## Migration from prodex

`mycodex migrate` moves `~/.prodex/profiles/<name>` to `~/.codex/profiles/<name>` with one
`rename` per account (same filesystem, atomic): `auth.json`, the refresh token,
`installation_id`, caches and daemon state move together and never exist twice. A link is
left at the old path so a later `prodex` run still finds its homes. Then `prepare_home`
adopts the per-account thread names and writer locks into `~/.codex`. The command refuses
while any Codex or prodex process uses those homes, except the mycodex service, which it
stops before the move and starts afterwards on its normal completion path. Unexpected
exceptions after stop are not covered by an outer recovery guard; migration is not a
transaction across all accounts or guaranteed to recover every partial failure.

Nothing in mycodex runs or reads prodex after that; `migrate` and `doctor` only look for
`~/.prodex/profiles` to offer the move. Uninstalling prodex (its binary, `~/.prodex`, the
empty `.prodex-*` lock files it created in `~/.codex` and `~/.codex/sessions`, and
`/tmp/prodex-*`) removes the compatibility links together with prodex's own state and
leaves the account homes untouched. Threads prodex created keep their provider tag; their
`openai` copies come from `mycodex threads adopt`.

## Processes mycodex recognises

| Role | Example |
|---|---|
| `codex tui` / `codex resume (rotating)` / `codex exec …` | a terminal Codex session (rotating when started behind the proxy) |
| `mycodex session (proxy)` | the mycodex parent of a rotating session |
| `mycodex remote service` | `mycodex __remote-serve` |
| `remote-control server` | foreground `codex remote-control` (the service's or a manual one) |
| `daemon` / `daemon (remote control)` / `daemon updater` | codex-managed app-server daemon and its updater |
| `rotation proxy`, `prodex launcher`, `prodex remote supervisor` | leftover prodex processes, reported by `doctor` |

## Compatibility guards

The proxy relies on Codex 0.160 behaviour: the `openai_base_url` setting, the immediate
HTTPS fallback after a 426, the `auth.json` layout and refresh endpoint, and the
app-server methods above. `mycodex doctor` reports other Codex versions as a warning; after
a Codex update, check `mycodex remote status` and a short `mycodex exec`.

## Tests

Run from the repository root with isolated locations and guards against external HTTP,
child processes, and process signals. Tests still use local fake HTTP servers:

```bash
python3 -B tools/run_tests.py
```

The tests cover argument routing, account resolution and naming, quota parsing, process
classification, launch and service command lines, the unit file, the error policy and SSE
pre-commit scanning, shared-home linking and adoption, token refresh and reuse, and an
end-to-end proxy run against a local fake backend: a usage limit moves the request to the
next account, a failure inside the stream before output does too, a 401 refreshes once,
exhausted accounts return the earliest reset, turn-state tokens stay with their issuer,
and WebSocket upgrades get 426.
The suite contains 114 tests across three files and passed again on 2026-10-05 with these guards. Added workflow
coverage includes pagination/ambiguity, link/hydration, guarded cleanup, exact completion
matching, fresh/sticky headroom routing, reset retries/permission/account checks, and
SQLite backups/active-lock handling. New boundary/CLI coverage checks empty and malformed
usage replies, failed-check cache invalidation, legacy readiness, login expiry/refresh
validation, settings/history preservation, every subcommand help page, numeric options,
account selection, preview forwarding/rejection, and foreground flag rejection. Real credit consumption, phone rendering, and full
lifecycle/concurrency failure recovery are not established by the suite. See
[project-review.md](project-review.md) for the uncovered defects and suggested tests.

## Validation and help contracts

`validation.py` reports field names and expected types without echoing credential values.
`auth.read` validates login fields and decoded claim types; display callers report corrupt
login facts, and credential callers receive `AuthError`. Refresh validates new tokens
before atomic replacement. Reauthentication can replace a damaged saved login.
`config.load` validates known settings while preserving unknown future keys.

`state.load` initializes missing files but raises `StateError` for malformed durable
history, without replacing it. Invalid quota-cache entries alone are ignored while
reset keys remain available. CLI entry renders known boundary/file errors without a
traceback; the proxy returns a local settings error when policy/state cannot be read.
These checks do not fix all backend payload or concurrency failures.

`helptext.py` owns descriptions for every user-facing mycodex command and parameter.
`cli.Parser` handles help before required-argument validation. `help COMMAND SUBCOMMAND`
and `COMMAND SUBCOMMAND --help` share those descriptions. To regenerate the reference:

```bash
PYTHONPATH=src python3 -B - <<'PYCODE'
from pathlib import Path
from mycodex.helptext import markdown
Path('docs/cli-guide.md').write_text(markdown())
PYCODE
```

[cli-guide.md](cli-guide.md) documents defaults, scopes, compatibility aliases, and side
effects. Human panels use accounts/usage/phone wording; JSON status codes and existing
command/flag names remain compatible. R01/R06 remain open and no service restart is
part of source/help updates.

## Published release and host verification

[Changelog](../CHANGELOG.md) records v0.3.0 behavior and compatibility changes.
The test runner is committed at `tools/run_tests.py`; deployment observer scripts and
artifacts remain private runtime files. They are described in
[redeployment.md](redeployment.md), not exposed as a new CLI lifecycle guarantee.

The corrected host observer completed with `outcome: deployed` on 2026-10-05 after
normal idle eviction was accepted during monitoring. The relay uses the working-tree
launcher and keeps its account/pairing. Its supervisor and source matched the successful
deployment during read-only rechecks. Release metadata now reports 0.3.0 for new
processes; already-running processes retain earlier loaded metadata until a planned
restart. Publication does not restart them.
