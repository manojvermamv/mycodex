# Whole-project review

Reviewed on 2026-10-04 against the complete working tree. The reviewed improvements
are included in **v0.3.0**, published on 2026-10-05; see the [changelog](../CHANGELOG.md). The original review recorded issues without changing code. The requested
follow-up fixes R07 and CLI clarity; this file records their current status. No
account/service lifecycle command was operated during the follow-up.

## Scope and evidence

The original review covered 26 Python modules, two test files, the launcher, installer,
service unit, runtime settings, and nine then-existing Markdown files. The 2026-10-05 follow-up
covered 28 modules, three test files, and all 11 Markdown files, including the ignored
personal cheat sheet and host notes. Public documentation
uses generic names; private host details remain in ignored files.
The static banner assets and MIT license/NOTICE were inspected as repository artifacts;
they were left unchanged. No upstream repository/release state was assumed from the
local version number or historical notes.

Evidence falls into three categories:

- **Reproduced:** current functions executed against temporary files, synthetic payloads,
  or mocked processes/RPCs. No real account deletion, logout, token refresh, credit
  consumption, service change, or model turn was used to demonstrate a defect.
- **Source risk:** a missing guard or incomplete recovery path was found in code, but the
  corresponding failure was not triggered against a running host or real backend.
- **Previously verified:** the earlier workflow audit repaired four stale rollout paths
  and loaded the existing project thread. Those outcomes are recorded in
  [workflow-audit.md](workflow-audit.md), rather than presented as new actions here.

The original 2026-10-04 review had 81 passing tests; the 2026-10-05 follow-up had 114 passing tests in temporary homes
with external HTTP, child process creation, and process signals blocked. Those tests validate their covered behavior;
passing them does not resolve the issues below. The remote service retained its main
process, start time, and zero restart count during this review.

## System model

The CLI resolves a profile and prepares shared-home links. Model-producing terminal
launches get an in-process HTTP proxy; other launches execute the official Codex binary
directly. Each proxy chooses credentials independently, while quota snapshots and pauses
are shared through `state/accounts.json`.

Remote control has a separate supervisor/proxy and a persistent relay account. Model
rotation does not switch that relay account. Relay failover changes the service's login
identity and can require phone sign-in/pairing with the replacement account.

Project commands require a running remote-control-capable server. They use its socket
for official RPCs. Thread listing and ID resolution read the shared database. Reset
redemption uses a different, temporary stdio server with the selected profile's access
token. Rollout repair is the deliberately narrow exception to read-only database access.

This design keeps the official TUI intact, but makes lifecycle handling, shared-file
concurrency, protocol validation, and accurate diagnostics especially important.

## Reproduced findings and current status

Priority is relative to this project: high can interrupt service or misstate account
readiness; medium affects correctness or recovery; low affects ergonomics or future
compatibility. None of these reproductions establishes a current outage on this host.

| ID | Priority | Status | Original trigger and observed behavior | Code location | Correction or remaining work |
|---|---|---|---|---|---|
| R01 | High | Open; deferred | Removing the active relay with `--force`, then declining confirmation, called the relay stop function once and returned cancellation. The stop occurs before confirmation. | [accounts.py](../src/mycodex/accounts.py), `profile_remove` | Complete inspection/confirmation before lifecycle side effects; avoid stopping the relay if removal is cancelled. |
| R02 | Medium | Help corrected | `profile_remove(..., keep_home=True)` called no logout command. The home is renamed with credentials intact. Previous help/docs described sign-out. | [accounts.py](../src/mycodex/accounts.py), `profile_remove` | Help and confirmation now explicitly describe retention without logout. Retention behavior is unchanged. |
| R03 | Medium | Open | Calling `cli.main(['help'])` with a temporary legacy config moved it into the new location and removed the old file. Migration runs before help/version/dry-run parsing. | [cli.py](../src/mycodex/cli.py), `main`; [paths.py](../src/mycodex/paths.py), `migrate_legacy` | Defer legacy migration until an operation needs writable state, or provide an explicit migration entry point. |
| R04 | Medium | Fixed | `--profile example quota` forwarded no profile selector to `quota_cmd`. Global `--dry-run` also does not reach project cleanup; only the cleanup subcommand's own flag does. | [cli.py](../src/mycodex/cli.py), `cmd_quota`, `main`, `cmd_projects` | Global account selection reaches quota; explicit name/all wins. Global preview reaches cleanup/migrate, and other management commands reject it. |
| R05 | Medium | Fixed | `remote-control --foreground --cwd DIR --failover --force --no-wait` forwarded only profile and mode. The other accepted flags are ignored; foreground supervision has no failover tick. | [cli.py](../src/mycodex/cli.py), `cmd_remote_control`; [remote_cmd.py](../src/mycodex/remote_cmd.py), `foreground` | Foreground now rejects unsupported service flags before any server operation. Use service mode for managed failover. |
| R06 | High | Open; deferred | A mocked Codex child exiting immediately caused `supervise` to return its status with zero tick calls. Relay failover depends on the child remaining alive until a periodic check. | [launch.py](../src/mycodex/launch.py), `supervise`; [remote.py](../src/mycodex/remote.py), `run_server` | Add a bounded post-exit health/replacement decision, with restart/backoff safeguards. Do not interpret every child exit as an account failure. |
| R07 | High | Fixed | `quota.parse('example', {})` reported eligible readiness. A 200 quota body containing `[]` raised `AttributeError`; an auth object with `tokens: [1]` also raised `AttributeError`; `remote: []` was accepted by config loading. | [quota.py](../src/mycodex/quota.py), `parse`, `fetch`; [auth.py](../src/mycodex/auth.py), `claims`; [config.py](../src/mycodex/config.py), `load` | Readiness needs explicit permission. Boundary validation handles malformed replies/auth/settings and numeric values with readable errors. Failed checks invalidate cached readiness. Regressions added. |
| R08 | Medium | Partly fixed | Exact thread-ID lookup requested only the latest 5,000 rows and failed if the target was outside that slice. Listing independently caps its database read at 2,000 rows; `-n 0` shows all fetched rows in JSON but none in the table. | [threads.py](../src/mycodex/threads.py), `_find`, `list_threads` | Positive whole-number counts fix zero/negative ambiguity. Exact lookup/listing ceilings remain open; query exact IDs and implement complete prefix/pagination handling. |
| R09 | Low | Open | Temporary `state_9.sqlite` and `state_10.sqlite` files caused `_connect('state')` to select version 9 because filenames are sorted lexicographically. Installed version 5 is unaffected by this reproduction. | [codex.py](../src/mycodex/codex.py), `_connect` | Parse and compare numeric schema suffixes, and report unsupported schemas rather than silently returning empty lists. |
| R10 | Medium | Open | A synthetic WebSocket Ping followed by a JSON frame produced no reply write. The reader skips Ping and Pong opcodes alike. No live disconnect was observed. | [appserver.py](../src/mycodex/appserver.py), `_receive` | JSON message/error shapes now receive readable errors. Ping replies, upgrade/frame validation, and broader fragmentation tests remain open. |

Before the fix, `mycodex --dry-run projects clean Example --yes` routed `dry_run=False`.
It now routes `dry_run=True`, as does `mycodex projects clean Example --dry-run`.
Regressions verify forwarding and unsupported global-preview rejection.

## Additional risks found in source

These are implementation gaps to test before claiming a failure or a guarantee.

| Area | Evidence and practical limit | Next verification |
|---|---|---|
| Migration recovery | `migrate.run` stops an active service, then performs moves, linking, and config updates without an outer recovery `finally`. An unexpected exception can bypass its final start. Rename failures are handled locally; other failures are not. | Inject link/config failures in a fully mocked migration and verify service recovery and partial-move reporting. |
| Shared JSONL adoption | `merge_jsonl` reads then atomically replaces the destination, without coordinating with Codex appenders. `_adoptable` checks idle locks and releases them before replacement. Atomic replacement does not prevent losing a concurrent append. | Coordinate with writer/maintenance locks or require quiescence; test concurrent append preservation. |
| Cleanup and repair races | Cleanup reads history and then archives in separate RPCs. Repair locks only writer-lock files already present. Another client can act between these operations or create a new lock. Backend archive behavior under that race was not tested. | Verify backend rejection semantics and atomic/conditional maintenance options; avoid claiming complete concurrency protection. |
| Partial thread operations | Adoption records its mapping only after fork/name/project/archive complete. A failure after fork can leave a copy without a mapping, so retry may fork again. Seeding also has no recovery record for partial completion. | Inject each RPC failure and ensure retries find/report existing partial results. |
| Durable redemption state | Fixed corruption handling: malformed JSON/shapes raise `StateError` and preserve the file; bad quota cache is ignored without losing retry keys. Missing files initialize state; deleting the file still loses identity. | Recover unreadable history from a trusted backup. Regressions verify no replacement and preserved pending keys. |
| Refresh coordination | mycodex's lock serializes mycodex refreshers. Official Codex login/refresh writers do not take that lock; shared-account native processes can still race. | Controlled concurrent refresh tests; keep account ownership boundaries explicit. |
| Duplicate account profiles | `profile_add` can install an explicitly different profile name for an already-known account. Pool membership/state is keyed by profile name and redemption locks by home, so duplicate names can represent the same underlying quota/credits independently. No duplicate account identity was established on this host. | Test duplicate-login naming, deduplicate by backend account identity, and coordinate account-level redemption. |
| Resource bounds | Proxy request bodies, SSE reader queues, and RPC notification queues lack explicit size bounds. Proxy log rotation uses a thread lock, not a cross-process lock. Quota HTTP timeout does not bound token-lock waits, refresh, or total batches. | Test slow readers/large input/multiple log writers; measure total latency and add bounds appropriate to real Codex requests. |
| Diagnostics | `status` readiness counts can include rotation-disabled profiles; some DB read errors become empty lists; `doctor` infers phone visibility from provider tags alone. `remote status --json` omits the extra quota/health work done by text output. | Test each output contract and represent unknown/error separately from empty/ready/visible. |
| Service-unit paths | `unit_text` interpolates launcher, working directory, and PATH without systemd escaping. The normal no-space install path works, but paths containing spaces or specifier characters are not covered. | Verify generated units using escaped temporary paths and a local unit parser before supporting those paths. |

## Documentation corrections completed

- Updated the command surface everywhere: project registration/linking/cleanup, quota
  redemption, headroom controls, opt-in automatic spending, UUID shorthand, and `use`.
- Removed promises that a provider tag or host inspection proves phone rendering.
- Distinguished new-start failover defaults from saved restart settings and from code
  already loaded by a running process.
- Explained lifecycle side effects, foreground limitations, diagnostic quota refresh,
  preview flag scope, and retained credentials with `--keep-home`.
- Corrected the claim that doctor reinstalls a missing launcher link; the installer does.
- Replaced the one-page project example with the paginated helper and added hydration to
  the existing-thread example.
- Marked discovery notes as historical, updated validation coverage, and kept host
  snapshots/private identifiers out of public docs.
- Recorded safe update steps that inspect uncommitted work and defer service restart
  until active work can tolerate it. No update/restart command was run during this pass.

## Suggested implementation order

1. R01, R06, and exception-safe migration recovery: lifecycle safety and trustworthy
   readiness before more automatic behavior.
2. Durable redemption recovery tooling and shared-file concurrency: protect retry identity/history.
3. Remaining R08 lookup/listing ceilings: complete direct-ID and prefix/pagination handling.
4. R09/R10, partial-operation recovery, resource bounds, and diagnostic contracts.

Keep real reset spending, phone account switching, destructive maintenance, and service
restarts out of automated tests. Use the isolated test command in
[architecture.md](architecture.md#tests). Future fixes should add regressions for desired behavior. Only findings explicitly
marked fixed above are resolved; source changes do not reload running modules.

## Completed validation and wording fix

- HTTP/RPC readiness requires `allowed: true` / `ordinaryUsageAllowed: true`. Empty
  replies or missing permission are unknown; denial/reached limits/exhaustion takes precedence.
- Malformed containers, booleans, numbers, timestamps, login claims, settings, cache, and
  reset replies receive readable field-only diagnostics. Invalid refresh replies leave
  login files unchanged. Reauthentication can replace a damaged saved login.
- Failed checks replace cached readiness; old ready cache without recorded permission
  needs rechecking. Unreadable durable account history is preserved for recovery.
- Every mycodex subcommand has detailed help before required-value parsing. The
  [command guide](cli-guide.md) shares descriptions and documents every parameter.
- Human panels use accounts, usage left, and phone wording. Existing command names,
  flags, aliases, and JSON status codes remain compatible.
- R04 account/preview routing and R05 foreground rejection are corrected. Positive
  watch/count values and 0–100 headroom are checked; count validation partly fixes R08.
- All 114 tests pass with isolated file/network/process/signal guards. Source changes
  do not reload modules already loaded by running Codex/mycodex instances. Existing
  host settings, durable history, and all three login files passed read-only validation.

## Deferred service lifecycle issues

These are the two remaining high-priority issues saved for separate work, as requested.
They were reproduced with mocked lifecycle functions; no running relay was stopped.

### R01: forced account removal stops the phone connection before confirmation

Trigger: removal targets the active phone relay, uses `--force`, and the user then
declines confirmation. `profile_remove` stops the service before asking. Cancelling
can therefore interrupt the connection while keeping the saved account. Until fixed,
avoid forced removal during active work, or switch the relay deliberately beforehand.
Help now states the actual behavior.

Required fix: gather facts and confirm removal before any stop/logout/move/delete,
then recheck ownership/state before side effects. A declining-confirmation regression
must call no stop function and preserve the account. Confirmed removal must still
enforce in-use rules. This lifecycle fix is not included here.

### R06: immediate child exit bypasses the relay failover check

Trigger: the official Codex child exits before the periodic tick. `supervise` returns
the exit status without that check. Systemd can restart the same account repeatedly;
periodic failover needs the child to survive until a health check. This reproduction
does not establish a current host outage.

Required fix: after an unexpected exit, make a bounded health/replacement decision with
backoff and a restart cap. Intentional shutdown and non-account errors need distinct
handling. Mock early exit, valid/invalid auth, available/exhausted backups, operator
stop, and repeated failure in regressions. Switching the phone account can require
sign-in or re-pairing. This fix is deferred and no service settings were changed.

## v0.3.0 release status

The later guarded restart completed successfully on 2026-10-05. The corrected observer
accepted ordinary idle eviction after initial hydration, verified connection/proxy/
pairing/projects, passed two-minute monitoring, and removed its unit. Read-only rechecks
confirmed the running launcher points to this project and protected independent Codex
processes are intact. This supersedes earlier pending deployment statements without
changing the open R01/R06 findings. See [redeployment.md](redeployment.md).

The release contains 28 package modules, three test files (114 passing tests), a
committed isolated runner, a public [cheat sheet](cheatsheet.md), and updated public
documentation. Historical file counts and 0.2.0 snapshots above describe their original
passes. Private notes/credentials/state/recovery snapshots are excluded. Version-only
publication does not reload modules or restart the host service.

## v0.4.0 release follow-up — 2026-10-08

The current package contains 29 modules and three test files (145 isolated tests).
The dated 26/28-module and 81/114-test counts above describe earlier reviews/releases.
New coverage verifies quota-pause recovery at the next fresh request after persisted
5-hour/weekly resets become due, configured priority restoration on explicit readiness,
completed-header live checks, overlapping response affinity, and no-order quota ranking.
Relay model-cache coverage checks saved sources, atomic
catalogue-only copying, source/destination safety, deeply nested JSON failures, and
bounded FIFO rejection and warning/continued startup on optional cache failure.

The additional model-choice audit found no runtime override. Its two regressions verify
unchanged user defaults and saved thread settings after sharing, plus distinct models and
reasoning settings forwarded unchanged for separate conversations during rotation.
Both checks detect an injected model override in isolated tests. This follow-up changes
tests and documentation only.

The v0.4.0 waiting job was cancelled before restarting and superseded by v0.4.1.
Updating CLI/source alone does not reload the running proxy. R01/R06 and every other
finding still marked open remain unresolved.

<!-- mycodex-deployment-status:start -->
The v0.4.1 host deployment completed at **2026-10-08 03:19:26 UTC**, with `outcome: deployed`
and 0 rollback attempts. The independent observer finished its
two-minute health monitor. Fresh checks confirmed the connected relay, control
socket and proxy, retained phone identity/pairing and conversation projects, and
preserved independent Codex processes. The service uses the verified v0.4.1
working-tree launcher; 145 isolated application tests pass. User model choices
remain unchanged. Private observer/recovery artifacts are excluded from Git.
<!-- mycodex-deployment-status:end -->
