# Changelog

## v0.4.6 — 2026-11-26

- **Codex CLI 0.169.x Alignment & Reliability Hardening (`TESTED_CODEX_SERIES = "0.169."`)**:
  - **Model Cache & Sync (`model_cache.py`)**: `fetch_and_write()` / `_fetch_envelope()` now write full 5-field `ModelsCacheEntry` envelopes, derive `client_version` from `codex.version()`, send `ChatGPT-Account-Id` with `401` token refresh retry, validate RFC3339 `fetched_at` + `etag` + `client_version`, preserve target `identity`, and refuse to clobber a valid relay cache with an invalid/envelope-less source.
  - **Shared State & Locks (`state.py`, `auth.py`, `profiles.py`)**: Serialized `state.load()` reads/publication under `_cache_lock` with `stat()` signature validation (`_state_sig`) and `copy.deepcopy()` isolation; `auth._locked()` acquires `<CODEX_HOME>/auth.lock`; `profiles.prepare_home()` shares `.sqlite-maintenance.lock`, `.tmp/plugins.sync.lock`, and `session_index.jsonl.lock`, and unlinks mismatched `-wal`/`-shm` symlinks for private SQLite DBs.
  - **Auto-Resume & Redemption (`auto_resume.py`, `redemption.py`, `remote.py`)**: Aligned `classify()` and `_current_turn_span()` with `codex 0.169.0` events (`usageLimitExceeded`, `usage_limit_exceeded`, `rateLimitExceeded`, `task_started`, `turn_aborted`) and sub-second `ts_nanos` log scoping; atomically claimed due retries under `state.editing()` enforcing `max_retries_per_turn`; rotated global `tick()` batches; caught `SystemExit` in `remote.py`; cleared `reset_attempt` before post-consume `account/rateLimits/read` in `redemption.consume()`.
  - **App-Server, Proxy, Launcher & Maintenance (`appserver.py`, `proxy.py`, `launch.py`, `procs.py`, `maintenance.py`, `codex.py`, `threads.py`, `accounts.py`, `remote_cmd.py`)**: Replaced deprecated `includeTurns=True` with `thread/turns/list` pagination (`read_thread_with_turns`) and `"excludeTurns": True` on `thread/fork`; added masked WebSocket Pong (`0x8A`) and disconnect propagation; updated `launch.wants_proxy()` for `0.169.0` commands/flags (keeping `agents` proxied) and `procs.classify()` exact basename matching; matched curly-apostrophe quota phrases (`You\u2019ve`), excluded cached `auth invalid` profiles from `/models`, enforced 64 MiB `MAX_REQUEST_BODY`, and locked `RotationProxy.pool()`; added literal `PROFILES_ROOT` matching in `maintenance.audit()`, SQLite warning logs + `find_threads()` literal prefix lookup in `codex.py`, and pre-stop confirmation + `systemctl` failure propagation in `accounts.profile_remove()` / `remote_cmd.stop()`.
- **Test Suite & Documentation**: Consolidated audit regressions into `tests/test_codex_0169_audit.py` (**195 isolated tests pass**); refreshed `README.md`, `QUICKSTART.md`, `helptext.py`, and `docs/*.md`.

## v0.4.5 — 2026-11-25

- **Opt-In Auto-Resume Engine (`auto_resume.py`)**: Added thread halt diagnosis,
  classification (`quota_5h`, `approval`, `other`), and quota-window auto-resume
  (disabled by default: `auto_resume.enabled = false`, `threads = []`).
  - Only turns blocked by 5-hour quota exhaustion (`quota_5h`, including cascading
    Free-profile `model_not_found` fallback when all Plus profiles in rotation are
    5h-limited) are eligible when explicitly opted in globally or per-thread.
  - Approval blocks (`waitingOnApproval`) and unrelated failures are never auto-retried.
  - Persists idempotent schedule/resume tracking per `(thread_id, turn_id)` in
    `state/accounts.json`, waits until `reset_at + buffer_seconds`, re-verifies live
    quota at wake-up with configurable backoff, clears stale proxy quota pauses for
    confirmed ready profiles, and dispatches `"Try again"` cleanly via `thread/resume`
    + `turn/start` inside `mycodex-remote.service`.
- **Thread-Targeted `feed` Command (`remote_cmd.py`, `cli.py`)**: Extended `mycodex feed`
  and `mycodex remote feed` to detect UUID/thread-prefix targets and resume/feed existing
  threads via `thread/resume` + `turn/start` (supporting both `--message` and positional
  text with `--no-wait`), while preserving `seed` behavior for directory targets.
- **New `mycodex auto-resume` CLI Group**: Added `status`, `check`, `enable`,
  `disable` (which immediately cancels pending jobs), and `run`.
- **Test Suite & Documentation**: Added isolated unit tests in `tests/test_auto_resume.py`;
  all **178 tests pass**. Updated `docs/cli-guide.md` and `docs/cheatsheet.md`.

## v0.4.4 — 2026-11-22

- **Mobile Relay Thread Visibility (`threadSource: "user"`)**: Fixed seeded project conversations
  being invisible in the ChatGPT mobile app (`codex_chatgpt_android_remote`). The app derives
  project groupings client-side from `thread/list` filtered by `thread_source == "user"`. Seeding
  now explicitly passes `"threadSource": "user"` to the official daemon's `thread/start` RPC so
  new project conversations appear immediately under "Projects" and "Recent Projects" on the phone.
- **Built-in `feed` Command Alias**: Added `mycodex feed` and `mycodex remote feed` as top-level
  and remote command aliases for `mycodex remote seed`, providing direct message dispatch into
  project conversations while strictly respecting YAGNI principles.
- **Mobile Relay Forensic Diagnostic Record (`docs/mobile-relay-visibility-audit.md`)**: Included
  the comprehensive forensic audit detailing the mobile client's discovery protocol, timeline
  analysis, Bubblewrap loopback constraints on Linux/EC2 unprivileged namespaces, and verification.
- **Unit Test Coverage**: Added tests in `test_improvements.py` asserting `threadSource: "user"`
  propagation on thread start and `feed` CLI routing. The test suite now passes **169 isolated
  application tests**.

## v0.4.3 — 2026-11-17

- **Decoupled Model Catalogue Proxy Routing**: Model list requests (`GET /backend-api/codex/models`)
  are decoupled from turn/prompt quota pauses. The proxy continuously routes model queries
  through the configured Plus source (`models_source` or highest-tier member), ensuring
  connected mobile and terminal sessions retain access to the full model catalogue (including
  Astra and Sol) even when accounts temporarily reach their 5-hour rolling turn limits.
- **Live Backend Catalogue Sync (`model_cache.py`)**: Added `fetch_and_write` to retrieve
  the model list directly from the OpenAI backend using the Plus account's credentials on demand
  and during `mycodex remote models share`, eliminating errors when a source profile lacks a
  pre-existing local cache.
- **Plan-Tier Ranking & Model Error Failover (`proxy.py`)**: Profile ranking now prioritizes
  Plus accounts over Free accounts for turn execution. Added `MODEL_CODES` (`model_not_found`,
  `unsupported_model`, etc.) to HTTP 400 error classification so that backend model-unsupported
  rejections automatically rotate to an eligible account rather than failing the turn.
- **GPT-6.2 Model Family Integration**: Verified and integrated OpenAI's newly launched **GPT-6.2
  model family** (`gpt-6.2-sol`, `gpt-6.2-astra`), verified priority 1 and 2 in the live catalogue,
  and confirmed active serving to ChatGPT mobile clients and local CLI commands.
- **Test Suite Race Condition Hardening**: Resolved a multi-threaded timing race in
  `test_completed_response_releases_turn_affinity_but_keeps_token_origin` using bounded
  completion polling. **167 isolated application tests** pass.
- Successfully verified on host with connected remote relay, zero-downtime service restart,
  and active pairing preserved.

## v0.4.2 — 2026-10-12

- Automatic reset-credit redemption now requires confirmed **0% weekly quota and
  at least one available credit**. A normal exhausted 5-hour window never qualifies,
  including after a backend rejection. The existing setting remains opt-in.
- Fresh requests check weekly-paused accounts in configured priority order and stop
  at a usable account. Each needed reset consumes one credit and saves the updated
  balance; unavailable accounts fall back to the next candidate. A live check under
  the redemption lock prevents spending after weekly quota or credit availability changes.
- A successful weekly reset keeps its prompt on the recovered account even below
  the five-hour headroom threshold. Updated command help and usage documentation.
- The approved host deployment completed at **2026-10-12 08:42:13 UTC**, enabling
  weekly-only automatic redemption after stopping the old proxy. Its two-minute
  health monitor finished with no misses or rollback attempts. Fresh checks confirm
  the connected relay/proxy, retained phone identity/pairing and project threads,
  and preserved independent Codex processes. See the [deployment record](docs/redeployment.md#weekly-only-reset-deployment--2026-10-12).
- **166 isolated application tests** pass. The frozen recovery version passed 145
  tests; 30 private observer checks cover lifecycle and policy activation/recovery.
  Real credits were not spent for verification. Automatic spending remains off by
  default in new installations; this host's approved setting is on.
- Release publication updates version metadata and documentation. It does not
  restart the already deployed relay or change its loaded weekly-only behavior.

## v0.4.1 — 2026-10-08

- Clarified that relay cache sharing preserves user model defaults and saved thread
  choices. Account rotation forwards each thread's requested model and reasoning settings
  unchanged. Two additional isolated regressions verify these guarantees; 145 tests pass.
  This patch updates version metadata, help wording, tests, and documentation while preserving the
  v0.4.0 application behavior.
- The earlier waiting deployment job was cancelled before restarting. The replacement
  handoff includes independent recovery and final deployment verification before
  documentation, commit/tag push, and GitHub publication are completed automatically.

<!-- mycodex-deployment-status:start -->
The v0.4.1 host deployment completed at **2026-10-08 03:19:26 UTC**, with `outcome: deployed`
and 0 rollback attempts. The independent observer finished its
two-minute health monitor. Fresh checks confirmed the connected relay, control
socket and proxy, retained phone identity/pairing and conversation projects, and
preserved independent Codex processes. The service uses the verified v0.4.1
working-tree launcher; 145 isolated application tests pass. User model choices
remain unchanged. Private observer/recovery artifacts are excluded from Git.
<!-- mycodex-deployment-status:end -->

## v0.4.0 — 2026-10-08

### Quota recovery and relay models

- Quota-paused accounts are checked again at the next fresh model-request boundary
  when their persisted 5-hour/weekly reset time or pause deadline is due. Explicit
  readiness restores configured account priority; unknown or still-limited results
  retain the pause, including when a fresh request carries a completed turn-state header.
  Overlapping responses sharing a token retain affinity until the last response finishes;
  completed tokens preserve issuer history. Fresh unbound requests follow explicit order,
  or remaining quota with session/current affinity breaking ties when no order is set.
- `mycodex remote models share [SOURCE]` copies a selected Plus model catalogue to the
  configured phone relay without restarting it or changing pairing. The saved source is
  refreshed before later managed relay starts; an optional-cache failure warns and leaves
  the existing relay cache and phone service startup intact.
- Cache copying validates UTF-8 JSON, rejects symbolic-link sources and unsafe
  destinations, and replaces only `models_cache.json` atomically with private file
  permissions. Login, installation identity, and other account data are preserved.
  Malformed or deeply nested JSON produces a readable failure rather than a traceback.
  Non-regular sources, including FIFOs, are rejected without blocking optional startup.

### Upgrade behavior and validation

- At v0.4.0 publication, new CLI invocations loaded 0.4.0 while the running proxy
  awaited a guarded restart. That waiting job was later cancelled before restarting
  and superseded by the v0.4.1 handoff. See the [redeployment guide](docs/redeployment.md).
- The package contains 29 modules; 143 isolated tests pass with external HTTP, external
  subprocess launches, and service signals blocked. Generated help and public documentation
  links are checked.
- Existing R01/R06 service lifecycle defects and the other limitations in the
  [project review](docs/project-review.md) remain open. Real reset spending, phone
  rendering, and full concurrency/recovery behavior are not established by these tests.

## v0.3.0 — 2026-10-05

This release adds project management, existing-conversation linking, earned-reset
redemption, proactive usage routing, and complete plain-language command help.

### New commands

- `mycodex projects [list]`: registered folders and non-archived conversation counts.
- `mycodex projects add DIR [--name NAME]`: register a folder without a model turn.
- `mycodex projects clean PROJECT [--dry-run] [--yes]`: archive verified empty or
  untouched built-in ready checks; preserve real work and active conversations.
- `mycodex threads link ID [--project PROJECT]`: assign an existing conversation and
  load it through `thread/resume`, without a model turn or service restart.
- `mycodex quota redeem [NAME]`: deliberately spend an earned reset with a durable
  retry key and account-specific official app-server checks.
- `mycodex rotation headroom [PERCENT]`: configure the fresh-request 5-hour usage
  threshold, initially 5%; sticky requests retain their account.
- `mycodex rotation auto-redeem on|off`: automatic earned-reset spending is opt-in,
  off by default, and restricted to qualifying exhausted 5-hour windows.
- `mycodex use NAME` and a full conversation UUID are shortcuts for selecting an
  account and resuming a conversation.

### Fixes and usability

- Empty usage replies or missing permission never report readiness. HTTP and reset
  RPC snapshots require explicit usage permission; stated limits take precedence.
- Failed checks invalidate stale ready cache. Legacy snapshots without recorded
  permission need a fresh check. Invalid numeric values and data shapes are rejected.
- Damaged login/settings/state and malformed server replies receive readable errors.
  Malformed refresh replies leave saved login untouched. Unreadable durable account
  history is preserved, including pending reset keys and adoption mappings.
- Global `--profile NAME` selects quota checks; explicit names and `--all` take
  precedence. Global `--dry-run` reaches cleanup/migration and is rejected by other
  management commands instead of being ignored.
- Foreground phone mode rejects unsupported service options. Watch intervals and
  conversation/log counts require positive values; headroom accepts 0–100.
- Every mycodex subcommand has detailed help without required positional values.
  The [command guide](docs/cli-guide.md) shares the terminal descriptions. Existing
  command/flag names and JSON status codes remain compatible.
- `doctor --fix` and `--fix-rollout-paths` repair verified legacy rollout references
  with a SQLite backup. Ambiguous, busy, or orphaned references are reported rather
  than purged. Ordinary inspection remains read-only.
- RPC completion matching checks both conversation and turn and preserves unrelated
  notifications. Failed client initialization closes its relay child.

### Upgrade behavior and validation

- New phone-service starts enable relay failover by default. `--no-failover` keeps
  the phone account fixed; `remote restart` preserves the saved choice. A relay account
  switch can require phone sign-in/pairing with the replacement account.
- Preserve `state/accounts.json`: it contains durable retry identity, not only cache.
  Strict validation may expose damaged files that older versions silently accepted.
- Online usage/diagnostic checks may renew logins and save snapshots. Model requests,
  cleanup, and credit spending have the side effects described in detailed help.
- Updating source does not reload already-running processes. The host deployment
  completed on 2026-10-05 with a corrected independent observer and retained pairing.
  The [redeployment guide](docs/redeployment.md) documents this host-specific procedure;
  its temporary private observer scripts are not a reusable CLI feature in this release.
- 114 isolated project tests pass. The temporary deployment observer had 22 mocked
  regressions; these are separate host artifacts, not part of the public test suite.
  No real reset credit was consumed for release validation.

### Known open issues

- R01: forced removal of the active relay can stop the phone connection before the
  removal confirmation; cancellation does not undo that stop.
- R06: an immediate Codex child exit bypasses the periodic relay failover tick.
- Startup legacy relocation, thread lookup/listing ceilings, numeric SQLite schema
  selection, WebSocket Ping replies, and broader concurrency/recovery risks remain
  tracked in the [project review](docs/project-review.md). Do not treat the deployment
  observer as a permanent fix for R01/R06.

## v0.2.0 — 2026-10-03

First public release: persistent accounts, shared Codex state, reactive request rotation,
systemd phone relay, quota/diagnostics, project seeding, and migration from prodex.
See the [GitHub release](https://github.com/manojvermamv/mycodex/releases/tag/v0.2.0).
