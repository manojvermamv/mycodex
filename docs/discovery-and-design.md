# Discovery and design notes

mycodex was designed after inspecting a real host running the official Codex CLI, and the
exact sources of the installed versions. Its first version (0.1) was a layer over prodex;
0.2 removed that dependency. These notes record what was found, the decisions that
followed, and how the result was verified.

The original discovery and host observations below describe the 2026-10-03 baseline;
they are historical evidence, not a current quota/process snapshot or a complete contract
for every recovery path. Reviewed again on 2026-10-04: see
[workflow-audit.md](workflow-audit.md) for completed additions and
[project-review.md](project-review.md) for reproduced remaining issues. At that 2026-10-04 review, the package reported `0.2.0` while the working tree
included unreleased changes. See the dated release follow-ups below.

Inspected versions: Codex CLI 0.160.0 (official standalone package, source tag
`rust-v0.160.0`) and prodex 0.435.1 (the installed binary's SHA-256 matched the release
asset). Host: Debian 13, systemd 257 with a user instance and lingering, Python 3.13,
default umask `0002`.

## 1. How Codex 0.160 behaves

**Accounts and storage**

- Account isolation is `CODEX_HOME`. `auth.json` (`auth_mode`, `tokens` with ID, access and
  refresh token and account id, `last_refresh`) lives in it, using the file store.
  `FileAuthStorage::save` truncates and rewrites the file in place, without a lock.
- Shared databases follow `CODEX_SQLITE_HOME`: thread, project, remote-control enrollment,
  thread-history, log, goal, memory and queue stores resolve there when it is set.
- Rollouts live in `sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl`. Thread names are appended
  to `session_index.jsonl` in `CODEX_HOME`; per-thread writer locks live in
  `CODEX_HOME/thread-writer-locks/`.
- Config edits resolve symlinks before writing, so a symlinked `config.toml` stays shared.

**Tokens**

- Access tokens live 240 hours. Codex refreshes proactively when `last_refresh` is older
  than eight days, and on a 401 it reloads `auth.json` before refreshing.
- Refresh: `POST https://auth.openai.com/oauth/token`, JSON body `client_id` (Codex's public
  client id), `grant_type=refresh_token`, `refresh_token`. Permanent failures:
  `refresh_token_expired`, `refresh_token_reused`, `refresh_token_invalidated`,
  `invalid_grant`, HTTP 401.

**Model provider and transport**

- The built-in `openai` provider cannot be redefined in config, but its base URL can:
  the top-level `openai_base_url` setting (`-c openai_base_url=…`). Project-local config may
  not set it; the command line may.
- The provider supports WebSockets. If the WebSocket handshake answers **426 Upgrade
  Required**, Codex switches the session to HTTPS at once (no retries).
- Over HTTPS, Codex sends the full input with every request and never a
  `previous_response_id`; request bodies are zstd-compressed by default
  (`enable_request_compression`).
- The backend returns `x-codex-turn-state`, a sticky-routing token that Codex replays on
  every request of the same turn. Requests also carry `session-id`, `thread-id` and
  `x-client-request-id`.
- A usage limit arrives as HTTP 429 with
  `{"error": {"type": "usage_limit_reached", "plan_type", "resets_at", …}}`.

**Threads and remote control**

- Thread lists are filtered by provider: `thread/list` without `modelProviders` returns only
  threads of the server's provider. The provider is stamped at creation, in the rollout's
  `session_meta` and in SQLite, and re-derived from the rollout on re-index, so it cannot be
  changed by editing SQLite. `thread/fork` accepts a `modelProvider`;
  `thread/metadata/update` assigns a project.
- With `daemon_auto_start` on, a plain `codex` TUI starts or attaches to a daemon for its
  `CODEX_HOME`, except when command-line config overrides (`-c`) are passed.
- `codex remote-control` runs a foreground app-server and honours `-c`. `codex
  remote-control start|stop|pair` manage a separate daemon that receives only feature flags.
  Enrollments are keyed by ChatGPT account and the server identity by `installation_id`,
  so the host identity is stable for the same account and home.
- Foreground socket bug under umask 0002: the private socket directory is created with
  tempfile's default mode, which is group-writable under `0002`, and Codex refuses it.
- `codex app-server proxy --sock PATH` relays raw bytes to a server's control socket, which
  speaks WebSocket.
- The ChatGPT usage endpoint is `GET /backend-api/wham/usage` (bearer plus
  `ChatGPT-Account-Id`).

## 2. What prodex 0.435.1 contributed, and why mycodex left it

prodex ran each account as a managed `CODEX_HOME` with shared entries symlinked to
`~/.codex`, started Codex with its own model provider (`prodex-openai-governed-http`)
pointing at a local broker, and rotated before output was committed.

mycodex 0.1 used it as is. Running it daily showed four costs:

1. **Two thread lists.** prodex's provider id tagged terminal threads differently from
   phone threads on a plain server, so each side hid the other's threads.
2. **Supervisor surprises.** prodex's session supervisor rewrote a failing child's command
   to `codex resume`; under `remote-control` that would have replaced the phone server with
   a TUI. Avoiding it needed preset `hooks.SessionStart` / `notify` flags.
3. **Policy out of reach.** prodex had no setting for in-session order or exclusion, so
   mycodex's rotation order and per-account disable only steered the starting account.
4. **A second tool to install, update and keep compatible**, including a version-specific
   set of guard flags.

mycodex 0.2 keeps what was good, reimplemented in Python (see `NOTICE`):

- the list of entries shared between account homes (plus thread names and writer locks,
  which prodex left per account);
- the error classes and the codes and phrases that identify them (quota, rate limit,
  deactivated workspace, overload, transient), and the rule that only pre-commit failures
  move;
- the usage-endpoint headers;
- the terminal panel and table style.

It drops the separate broker, the custom provider id, the session supervisor and response
replay, which HTTPS transport makes unnecessary.

## 3. Requirements versus outcome

| Requirement | Outcome |
|---|---|
| Persistent per-account login | one `CODEX_HOME` per account in `~/.codex/profiles/<name>`, logged in with `codex login`; mycodex refreshes tokens only when expiring or rejected |
| One shared `~/.codex`, same threads | shared links plus `CODEX_SQLITE_HOME`; normal launches retain `openai`, while provider overrides and mobile loading/filtering still matter |
| `~/.codex/profiles/<name>` layout | real account homes there |
| Official TUI unchanged, native arguments pass through | `mycodex` runs `codex` with only `-c openai_base_url=…` added |
| Automatic rotation | per-request proxy with pre-commit rotation, pauses until reset |
| Rotation enable, disable and order | exclusions/order refresh in running proxies; the owner remains eligible; global enable applies to later terminal launches |
| Remote control with rotation | systemd user service: `codex remote-control` behind an in-process proxy, umask 0022 |
| Remote control without rotation | the same service in pinned mode |
| Keep alive, restart, survive SSH | `Restart=always`, lingering, subject to systemd restart limits and lifecycle recovery gaps |
| Single remote instance | codex-managed remote daemons are stopped and their remote-control setting turned off |
| No dependency on prodex | `mycodex migrate` moves existing accounts; prodex was then uninstalled from the host |

## 4. Verification on the host

mycodex 0.1 (on prodex): takeover from a codex-managed remote daemon with the same host
identity, rotating/pinned switching with the phone staying paired, crash recovery in about
18 seconds.

mycodex 0.2 (no prodex):

- Unit and integration tests (38) pass on the host, including the proxy against a local
  fake backend: rotation on usage limits and on in-stream failures, token refresh on 401,
  earliest-reset pass-through, turn-state affinity, 426 on WebSocket upgrades.
- The usage client returned both accounts' plans and windows directly from ChatGPT.
- `mycodex exec` through the proxy completed a real turn: Codex logged the 426, switched to
  HTTPS, and the new thread was tagged `openai`.
- `mycodex migrate` moved both accounts with one rename each, merged the per-account thread
  names and writer locks, restarted the service, and the phone reconnected to the **same**
  environment id with its pairing intact. No prodex process remained.
- `mycodex threads adopt` forked the one prodex-tagged working thread into an `openai` copy
  with its name and project.
- prodex was then removed from the host: its binary, `~/.prodex` (state and the
  compatibility links), its empty lock files in `~/.codex` and its `/tmp` files. Nothing
  else referenced it (no shell profile line, systemd unit, cron entry or Codex config).
  Afterwards `mycodex doctor` still reported every check as passing, the phone link stayed
  connected on the same environment, and phone turns kept flowing through the proxy.

Not exercised live: an actual mid-session account switch on a real usage limit (requires an
exhausted account; covered by the integration test) and pinned-mode failover.
That statement belongs to the original discovery pass. Later user-supplied reports
describe account rotation, but this fresh review did not independently recreate that
live event. It did reproduce the immediate-child-exit gap in periodic relay failover.

## 2026-10-04 workflow additions and fresh review

- Added thread linking with metadata update plus resume, project list/add/guarded cleanup,
  account-specific earned-reset consumption, and UUID/profile shortcuts.
- Added 5% fresh-request headroom routing, opt-in automatic redemption, and a new-start
  failover default while preserving saved restart settings.
- Verified and repaired four legacy rollout paths with a private SQLite backup, then
  confirmed the existing thread was loaded/idle in its project through read-only RPCs.
- Generated experimental schemas and initialized a temporary unauthenticated stdio server
  against installed Codex 0.160.0; no real reset credit was consumed.
- Expanded verification from 38 to 81 tests, passing with temporary homes and external
  HTTP/process/signal guards. Coverage is described in [architecture.md](architecture.md#tests).
- A fresh pass through all modules/docs reproduced ten groups of remaining issues,
  including confirmation ordering, CLI flags, quota parsing, thread lookup, numeric schema
  selection, and WebSocket Ping handling. Other risks are labeled as source findings.

## 5. Known limits

1. A turn that fails after output started streaming is not moved; Codex shows the error and
   the next turn goes to a ready account.
2. The phone pairs with one relay account; if that account can no longer log in, pinned or
   rotating failover moves the relay and the phone must sign in to the new account.
3. Two long-lived Codex processes on the same account can still race a token refresh;
   `doctor` reports token invalidations.
4. The proxy depends on Codex 0.160 behaviour (`openai_base_url`, 426 fallback, auth layout);
   `doctor` warns on other versions.
5. Project commands require a running remote-control-capable server. Provider tags,
   metadata, loaded state, and phone rendering are separate checks.
6. Periodic service failover requires the child to stay alive until a health check;
   immediate exits currently restart the same relay without that decision.
7. Pending reset keys/adoption mappings make account state partly durable; deleting it
   loses retry identity. Shared-file adoption/maintenance are not fully atomic across
   concurrent Codex writers.

The original discovery remains useful, but the current [whole-project review](project-review.md)
takes precedence for open defects, claim limits, and implementation priorities.

## Later follow-up: validation and easier commands — 2026-10-05

The historical 81-test snapshot above is superseded by 114 passing isolated tests.
R07 quota/auth/config validation is fixed; missing permission is unknown, failed checks
invalidate stale readiness, malformed refresh does not overwrite login, and corrupt
durable history is preserved. R04 argument routing and R05 foreground flag handling
are corrected. Account/usage/phone wording and complete subcommand help are shared
with the new [command guide](cli-guide.md). R01/R06 remain open and documented in the
[current review](project-review.md#deferred-service-lifecycle-issues). Running instances
were left in place; no real reset credit or model turn was used for these tests.

## v0.3.0 publication — 2026-10-05

The additions/fixes described above are now versioned as v0.3.0; see
[CHANGELOG.md](../CHANGELOG.md). Historical version numbers and test counts describe
their original discovery passes. At that release, the committed runner `tools/run_tests.py`
reproduced 114 passing isolated regressions. The [public cheat sheet](cheatsheet.md) uses generic
account/folder examples. Personal host notes and temporary recovery scripts stay private.

The corrected guarded deployment completed with the updated source and retained pairing;
it passed monitoring and later read-only rechecks. The [redeployment guide](redeployment.md)
records the first observer's false idle-eviction failure and its correction. Release
publication changes metadata/docs and does not restart active sessions. R01/R06 remain open.

## v0.4.0 follow-up — 2026-10-08

The current package has 29 modules and 145 isolated tests; the 81/114-test results above
remain historical. Due persisted 5-hour/weekly resets now trigger checks at the next
fresh request, restoring configured priority only on explicit readiness and preserving
active response affinity until the last overlapping owner completes. Completed token
headers still trigger fresh-request checks. Explicit order takes priority for fresh
requests; without an order, quota ranks before session/current tie-breakers.
Relay model-cache sharing saves a Plus source for managed
starts and safely copies only its catalogue; invalid sources, unsafe paths, and deeply
nested JSON fail readably. Optional startup cache failures warn and continue.

Model choices remain user controlled for each conversation. Catalogue copying preserves
configured defaults and saved thread settings; routing preserves the selected model and
reasoning settings in each request. The follow-up adds two regressions without changing
runtime behavior.

The v0.4.0 waiting deployment job was cancelled before restarting and superseded by
the v0.4.1 deployment/publication handoff. Updating CLI/source alone does not reload
the running proxy. R01/R06 and the other open review items remain unresolved.

<!-- mycodex-deployment-status:start -->
The v0.4.1 host deployment completed at **2026-10-08 03:19:26 UTC**, with `outcome: deployed`
and 0 rollback attempts. The independent observer finished its
two-minute health monitor. Fresh checks confirmed the connected relay, control
socket and proxy, retained phone identity/pairing and conversation projects, and
preserved independent Codex processes. The service uses the verified v0.4.1
working-tree launcher; 145 isolated application tests pass. User model choices
remain unchanged. Private observer/recovery artifacts are excluded from Git.
<!-- mycodex-deployment-status:end -->
