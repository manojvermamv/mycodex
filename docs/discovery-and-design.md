# Discovery and design notes

mycodex was designed after inspecting a real host running the official Codex CLI, and the
exact sources of the installed versions. Its first version (0.1) was a layer over prodex;
0.2 removed that dependency. These notes record what was found, the decisions that
followed, and how the result was verified.

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
| One shared `~/.codex`, same threads | shared links plus `CODEX_SQLITE_HOME`; everything uses the `openai` tag, so one thread list everywhere |
| `~/.codex/profiles/<name>` layout | real account homes there |
| Official TUI unchanged, native arguments pass through | `mycodex` runs `codex` with only `-c openai_base_url=…` added |
| Automatic rotation | per-request proxy with pre-commit rotation, pauses until reset |
| Rotation enable, disable and order | global switch, per-account disable and order all apply inside sessions |
| Remote control with rotation | systemd user service: `codex remote-control` behind an in-process proxy, umask 0022 |
| Remote control without rotation | the same service in pinned mode |
| Keep alive, restart, survive SSH | `Restart=always`, lingering |
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

## 5. Known limits

1. A turn that fails after output started streaming is not moved; Codex shows the error and
   the next turn goes to a ready account.
2. The phone pairs with one relay account; if that account can no longer log in, pinned or
   rotating failover moves the relay and the phone must sign in to the new account.
3. Two long-lived Codex processes on the same account can still race a token refresh;
   `doctor` reports token invalidations.
4. The proxy depends on Codex 0.160 behaviour (`openai_base_url`, 426 fallback, auth layout);
   `doctor` warns on other versions.
