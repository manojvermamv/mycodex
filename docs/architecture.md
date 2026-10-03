# mycodex architecture

This document explains how mycodex works internally and why each mechanism exists. For
everyday use, read the [README](../README.md) and the [Quickstart](../QUICKSTART.md).
The investigation behind these choices is in [discovery-and-design.md](discovery-and-design.md).

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
- Codex's SQLite stores are read-only to mycodex. Threads and projects change only through
  Codex's official app-server API (`thread/fork`, `thread/name/set`, `project/create`, …).
- `auth.json` is written only to store a refreshed token: atomically, mode 0600, keeping
  every other field. Logins and logouts are the official `codex login` / `codex logout`.
- Anything mycodex does not own passes through unchanged: `mycodex <args>` runs
  `codex <args>` with the chosen account's environment.

| Module | Responsibility |
|---|---|
| `profiles.py` | account homes, shared links, adopting per-home data |
| `auth.py` | reading `auth.json`, non-secret claims, token refresh |
| `quota.py` | ChatGPT usage endpoint, quota snapshots |
| `state.py` | runtime facts shared by all processes: pauses, quota cache, adopted threads |
| `proxy.py` | the rotation proxy |
| `launch.py` | starting codex as a profile, with or without the proxy |
| `remote.py`, `remote_cmd.py` | the remote-control service and its commands |
| `threads.py`, `migrate.py` | thread list and adoption, prodex migration |
| `doctor.py`, `views.py`, `procs.py` | diagnostics, status, process discovery |

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
directory where a link belongs is reported, never overwritten. `doctor --fix` and
`migrate` *adopt* the entries that can be folded in safely: thread names and history are
merged by timestamp; idle lock files are replaced. Session directories are never moved
automatically.

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
4. every other ready account: first `rotation.order`, then the most quota left.

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
and the request goes to the next account. Otherwise the held events and the rest of the
stream are passed through as they arrive. After that point nothing is retried.

When every account fails, Codex receives the backend's own error; for usage limits, the
one with the earliest `resets_at`, so Codex's message names the earliest time a turn can
succeed. A turn-state token is never forwarded to an account other than its issuer.

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

### Shared state, logs and safety

- `state/accounts.json`: pauses (until, reason), the last quota snapshot per account and
  adopted threads. Every proxy and command reads it, so a limit found in one session is
  respected by all of them.
- `state/logs/proxy.log`: one line per request, switch, pause and refresh, without
  secrets, rotated at 5 MB (`mycodex rotation log`).
- `state/proxies/<pid>.json`: the port and owner of each running proxy;
  `GET /__mycodex/health` reports the current account and counters.
- The proxy binds to 127.0.0.1 and accepts a connection only if `/proc/net/tcp` shows the
  client socket belongs to the same Unix user.

## Quota

`GET https://chatgpt.com/backend-api/wham/usage` with the account's bearer,
`ChatGPT-Account-Id`, `originator: codex_cli_rs` and a Codex user agent: the endpoint
Codex's own `/status` uses. Windows of 18 000 s, 604 800 s and 2 592 000 s are shown as
`5h`, `weekly` and `30d`. An account is `limited` when the backend says so or a window is
at 100 %; it is usable again when every exhausted window has reset.

## Remote control

Remote control runs as the systemd user service `mycodex-remote`. Its unit lives in
`~/mycodex/systemd/` and is linked into `~/.config/systemd/user/`. The service runs
`mycodex __remote-serve`, a supervisor that starts the official foreground
`codex remote-control` as the relay account and exits with it, so systemd restarts it
(`Restart=always`, `RestartSec=10s`).

| Mode | Command inside the service | Phone pairs with | Model turns | Thread tag |
|---|---|---|---|---|
| `rotating` | `codex -c openai_base_url="http://127.0.0.1:<port>/backend-api/codex" remote-control` (proxy in the supervisor) | P | rotate across ready accounts | `openai` |
| `pinned` | `codex remote-control` | P | always P | `openai` |

Why each piece exists:

| Piece | Reason |
|---|---|
| `UMask=0022` | Codex creates its private socket directory with the process umask (tempfile 3.27 default mode). Under Debian's default `0002` the directory is group-writable and Codex refuses to bind ("socket parent must be owned by the user or root…"). |
| `CODEX_HOME` = relay account | Keeps `installation_id` and the account-keyed enrollment, so the host identity (server and environment ids) never changes and the phone stays paired. |
| proxy inside the supervisor | The service has its own proxy; stopping it never affects terminal sessions, and its events go to the journal. |
| `mycodex __remote-serve` parent | Forwards signals, records `state/remote-serve.json` (including the proxy port), and with `failover` checks the relay account every few minutes. |

**Conflicts.** Before starting, `mycodex remote start` stops codex-managed daemons that
have remote control enabled (`codex remote-control stop`, then
`codex app-server daemon disable-remote-control` so a later auto-start cannot re-enable
it) and stops their idle updater loops. Foreground `codex remote-control` processes that
mycodex did not start are left alone unless `--force` is given.

**Failover.** With `--failover`, the supervisor checks the relay account every
`failover_check_seconds` (default 300). If the account can no longer authenticate (both
modes) or is limited (pinned mode), it switches `remote.profile` to the next ready account
and exits with status 75; systemd restarts the service as that account. The phone must then
be signed in to the new account.

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
and filters `thread/list` by the server's provider. Since everything mycodex runs uses
`openai`, terminal sessions and phone threads are one list.

Projects are separate from tags. A thread belongs to a project only when a client passes
the project id (`thread/start` with `projectId`, or `thread/metadata/update`); Codex does
not derive it from the working directory, and the TUI and `codex exec` never pass one.
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

## Migration from prodex

`mycodex migrate` moves `~/.prodex/profiles/<name>` to `~/.codex/profiles/<name>` with one
`rename` per account (same filesystem, atomic): `auth.json`, the refresh token,
`installation_id`, caches and daemon state move together and never exist twice. A link is
left at the old path so a later `prodex` run still finds its homes. Then `prepare_home`
adopts the per-account thread names and writer locks into `~/.codex`. The command refuses
while any Codex or prodex process uses those homes, except the mycodex service, which it
stops before the move and starts afterwards; the phone reconnects with the same identity.

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

```bash
python3 -m unittest discover -s ~/mycodex/tests -v
```

The tests cover argument routing, account resolution and naming, quota parsing, process
classification, launch and service command lines, the unit file, the error policy and SSE
pre-commit scanning, shared-home linking and adoption, token refresh and reuse, and an
end-to-end proxy run against a local fake backend: a usage limit moves the request to the
next account, a failure inside the stream before output does too, a 401 refreshes once,
exhausted accounts return the earliest reset, turn-state tokens stay with their issuer,
and WebSocket upgrades get 426.
