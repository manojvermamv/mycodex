# Workflow audit and fixes

Audited against the repository and the installed Codex CLI 0.160.0 on 2026-10-04.
The experimental schemas were generated using an empty temporary Codex home. Runtime
inspection used read-only database connections, journal reads, and read-only app-server
methods before the explicit backed-up repair and metadata/resume actions recorded below.
The historical quota figures supplied in the request were not treated as current.
This records the completed workflow changes, not a claim that the whole project is free
of defects. The subsequent [whole-project review](project-review.md) lists reproduced
open issues and additional recovery/concurrency risks. At the 2026-10-04 audit, package
version remained `0.2.0` and the additions were unreleased. See the dated publication and v0.4.0 follow-ups below.

| Finding | Evidence and disposition |
|---|---|
| Existing-thread project linking missing | Confirmed: the CLI only supported list/adopt. Added `threads link`, which resolves the project, saves its assignment, and hydrates the same thread without a turn or restart. |
| Hydration and mobile visibility | The existing thread was assigned but `notLoaded` on the server. That is a normal unloaded state, not proof of a mobile filtering failure. Resume loads it and emits lifecycle notifications; rendering on a specific phone cannot be established from host inspection alone. |
| Earned-reset consumption missing | Confirmed in CLI and installed protocol. Added account-specific `quota redeem`; no real credit was consumed during verification. Outcomes are handled explicitly and limits are read again before clearing quota pauses. |
| Project list/add missing | Confirmed. Added paginated project inspection and idempotent folder registration without a dummy thread. Counts cover non-archived threads across all providers/sources. |
| Legacy rollout references | Confirmed: four legacy references in one shared database, not independent per-profile copies. All four had matching replacement rollouts, including one archived rollout. Added guarded repair with SQLite backup and compare-and-update. Orphans and ambiguous paths are preserved and reported. |
| Proactive quota routing missing | Confirmed: the old proxy ranked fallback accounts but retained the current account until rejection. Added a configurable 5% default threshold for fresh model requests without an active turn binding, refreshing stale snapshots. Sticky turns and reactive fallbacks are retained. |
| Relay failover default off | Confirmed as a setting, rather than an observed outage. `remote start` defaults to failover on; `remote restart` preserves the saved choice and `--no-failover` opts out. Changing the relay account still requires the phone to sign in to that account. |
| UUID/profile shortcuts missing | Added full-UUID resume shorthand and top-level `use`. |
| Project cleanup missing | Added preview/confirmation and API archival only for verified empty or untouched built-in ready-check histories; active work and descendants are preserved. |
| Seed completion could match another turn | Confirmed in the client. Waits now match both thread and turn IDs and consume only the matching completion notification. |
| Launch dry-run prepared shared links | Confirmed. Home preparation now uses dry-run mode, so a launch preview does not write links. |

## Operational boundaries

- No running Codex or mycodex process is restarted, signaled, or replaced by this update.
- The narrow `doctor --fix-rollout-paths` command changes only verified legacy path
  references after a consistent SQLite backup; writer locks are checked. It performs no
  authentication or quota calls and deletes no thread rows or files.
  It checks existing lock files, so discovery is not an atomic exclusion of a newly
  starting writer. That race was not exercised against the live service.
- `threads link` uses the already-running official server. It starts no model turn.
- `quota redeem` starts an isolated temporary stdio server and supplies the selected
  account's access token through the experimental external-token login method. Refresh
  tokens are not copied. An account lock serializes redemption, and unresolved attempts
  retain their idempotency key. No phone relay login is changed.
- `rotation.auto_redeem` defaults to false. Enabling it explicitly allows spending an
  earned reset on an exhausted 5-hour window, before reactive/proactive fallback. Another
  exhausted window prevents automatic spending. Unknown backend usage permission does
  not count as confirmed recovery.
- The running supervisor/proxy retains its loaded code until a later restart. Existing
  explicit `remote.failover=false` is preserved by this update. A later new start
  uses the new failover default; restart preserves the saved choice. New terminal
  sessions use the new routing policy.
  Supported config changes may refresh in a proxy after its pool-cache delay; source
  changes do not reload Python modules. Periodic relay failover currently requires a
  child that remains alive until a check, and does not cover immediate child exit.
- Low-quota routing reduces avoidable failures; it cannot predict a turn's total cost or
  promise that no mid-turn usage error occurs.

## Validation

All 81 tests passed after installing the changes into the working tree. The focused
live repair updated all four verified paths, and a second read-only scan found no
remaining legacy references. Its consistent backup is retained in the private
`state/backups/` directory. The existing thread was resumed through the running server;
subsequent reads confirmed it was loaded, idle, and assigned to the expected project.
The new project listing returned the registered projects and thread counts successfully.
The remote service retained the same main PID, start time, and zero restart count.
No reset credit was spent and no project or thread was created or archived.
The fresh documentation review also left that service untouched and reran all 81 tests
with temporary homes, external HTTP blocked, subprocess creation blocked, and signals
blocked. New defect reproductions used temporary files and mocked lifecycle functions.

Regression tests use temporary homes/databases, fake HTTP backends, and fake RPC servers.
They cover paging and ambiguity, link/hydration sequencing, cleanup preserving real work,
exact completion matching, proactive and sticky routing, opt-in reset retry/fallback,
idempotent redemption after uncertain failure, backend permission/account checks,
backups, active writer locks, and non-destructive orphan handling. An isolated stdio
smoke check against the installed binary initialized successfully and returned an
unauthenticated account without reading real account homes.

The installed schema confirms all new RPC request fields. The official
[app-server documentation](https://learn.chatgpt.com/docs/app-server) documents stored
versus loaded threads, earned-reset outcomes, and idempotency. Protocol support and
mocked redemption do not prove that a particular account's backend will grant a reset.

## Follow-up boundaries

Project counts are non-archived memberships, not counts of currently running turns.
`projects add --name` does not rename an existing registration. `projects clean` rechecks
history/descendants but read and archive are separate RPCs. Linking currently inherits
the 5,000-row thread lookup ceiling. Both global and cleanup-local `--dry-run` now
preview cleanup. Other management commands reject unsupported global preview. Account state includes durable reset retry keys and must not be discarded as if it
were only a cache. These limits and the fresh review's open lifecycle/input-validation
issues are tracked with their current status in the project review.

## Follow-up: quota validation and command clarity — 2026-10-05

R07 is fixed: HTTP/RPC readiness needs explicit permission; empty/malformed replies become
unknown and replace old healthy cache. Login/settings shapes and numeric values are
validated; failed refresh leaves the saved login unchanged. Damaged durable account
history is preserved, rather than silently discarding reset retry identities.

Detailed help explains every parameter and works without required values. Global account
selection now reaches quota checks, global preview reaches cleanup/migration and is
rejected elsewhere, and foreground service flags are rejected instead of ignored.
These follow-ups resolve R04/R05 and the zero-count ambiguity in R08.
The [command guide](cli-guide.md) shares terminal help descriptions. All 114 tests pass
with temporary homes and external HTTP/process/signal guards. Existing host settings,
account history, and three login files also passed read-only validation. No real quota query,
credit spending, account change, or service restart was needed.
R01 forced-removal confirmation ordering and R06 immediate-exit failover remain
deferred in [project-review.md](project-review.md#deferred-service-lifecycle-issues).

## Deployment completion and v0.3.0

The subsequent host handoff completed at 01:00:57 UTC on 2026-10-05 with
`outcome: deployed`. Its observer completed two-minute monitoring and removed its
temporary unit. Read-only rechecks confirmed project-source equality, connected relay
and proxy, retained pairing/projects, and independent Codex processes. The first
observer's false failure on normal idle eviction and the corrected 22-test observer are
recorded in [redeployment.md](redeployment.md). No reset credit was spent.

This workflow and validation work is published as [v0.3.0](../CHANGELOG.md). The v0.3.0 public
project suite contained 114 tests; `python3 -B tools/run_tests.py` runs the current suite.
Observer tests/scripts remain private host artifacts. Publishing version metadata and
docs does not restart or reload the running relay. R01/R06 stay open in the review.

## v0.4.0 follow-up — 2026-10-08

The quota-pause priority correction belongs to v0.4.0. Persisted 5-hour/weekly reset
times or pause deadlines trigger live checks at the next fresh request when due.
Explicit readiness restores configured account priority; unknown/still-limited checks
keep the pause even when a completed token header accompanies the next fresh request.
Active responses sharing tokens retain affinity until their final owner finishes;
completed tokens preserve issuer history only. Fresh requests follow explicit order,
or remaining quota with session/current affinity breaking ties when no order is set.
`remote models share [SOURCE]` copies a validated Plus catalogue into the configured
relay, saves the source, and refreshes it before later managed starts. Safe source and
destination checks and readable deeply nested JSON failures preserve other account data;
optional startup failures, including nonblocking rejection of FIFO sources, warn and
continue with the existing relay cache.

The model-choice audit confirms that cache sharing keeps user model defaults and saved
thread choices intact. The proxy forwards the requested model and reasoning settings
unchanged across account rotation. Two isolated regressions cover separate conversations
using different models and unchanged settings/history after a catalogue copy. No runtime
model override was found or introduced.

The package now has 29 modules and 145 isolated tests. The guarded restart is pending:
this conversation runs in the relay cgroup. An independent observer is armed and waiting
for all active threads/tools and ten seconds idle. The live cache copy retained the relay
PID, credentials, installation/environment identity, and paired client.
Updating CLI/source alone does not reload the proxy. R01/R06 and the other documented
open issues remain. See the [redeployment guide](redeployment.md).
