# Changelog

## Unreleased

- `mycodex remote models share [SOURCE]` copies a selected Plus model catalogue to the
  configured phone relay without restarting it or changing pairing. The saved source is
  refreshed before later managed relay starts; an optional-cache failure warns and leaves
  the phone service able to start.

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
- Quota-paused accounts are checked again at a fresh request boundary when their saved
  reset time is due. Only an explicit ready result clears the pause and restores priority;
  in-progress turns keep their account, and saved reset times remain visible in status.
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
