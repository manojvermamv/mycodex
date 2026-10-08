# Restart and recovery plan

This plan prepares a host-side restart without ending an active model turn. The
current conversation is hosted inside `mycodex-remote.service`: restarting that
service necessarily replaces its supervisor, relay server, and code-execution helper.
Their exact PIDs cannot survive a restart. Conversation history, project assignment,
phone account, installation identity, and pairing records can be kept in place.

## Handoff sequence

1. Inspect service/process ownership and the control socket. Record the relay account,
   saved settings, installation/environment identity, paired-device IDs, loaded
   conversations/projects, and independent Codex process identities. Read-only RPCs
   establish a connected baseline; they start no model turn and consume no reset credit.
2. Freeze a separate copy of the probe code and an earlier tested recovery version.
   Keep the recovery copy independent of the application files being deployed. Validate
   the candidate, recovery copy, and deployment state machine before arming any restart.
3. Start a temporary observer as its own systemd **user service**, outside the relay's
   cgroup. It must have no `PartOf`/`BindsTo` dependency on the relay. A separate service
   survives the relay restart and does not depend on this conversation staying online.
4. Wait for every loaded conversation to be idle and for tool processes inside the
   relay cgroup to finish. Require a continuous ten-second quiet window and immediately
   recheck it before restarting. New work resets that window. Source/settings changes,
   another actor replacing/stopping the relay, or no safe window within fifteen minutes cancel
   the job without restarting it. Other Codex instances are never stopped.
5. Restart **only** `mycodex-remote.service`, directly through systemd. Avoid the broader
   `remote start` path, which includes daemon-conflict handling and new-start defaults.
   Preserve saved account, mode, working directory, and failover choice.
6. Require a new supervisor, responsive control socket and proxy, `connected` relay,
   unchanged installation/environment identity, and retained paired-device records.
   Resume saved conversations only when they are not loaded, using `excludeTurns: true`;
   validate their IDs/projects. Hydration starts no model turn. New active turns are
   accepted as healthy and are not interrupted for a successful deployment.
7. Allow ninety seconds for initial connection checks. Require six seconds of
   stable health, then observe for two minutes. Three consecutive failed probes or an
   unexpected restart initiate recovery. Monitoring itself leaves a healthy relay online.

The expected reconnection gap is a few seconds, but startup/network timing determines
the actual delay. Control-socket checks do not prove that the phone UI has refreshed.
The server offers no atomic admission/drain API, so there is a small race between the
last idle check and stopping it; this procedure does not promise zero downtime or that
an in-progress turn can migrate across server processes.

## Automatic recovery

The observer persists its phase before lifecycle changes and can restart independently
after an observer failure. It checks that both source and recovery snapshots retain
their recorded hashes. Account settings and independent process identities are guarded.

Recovery installs a **service-only override** pointing `ExecStart` to the verified
earlier code while keeping `MYCODEX_HOME` at the normal host directory. It then reloads
unit definitions and restarts the relay. Application source, credentials, account
history, SQLite, rollouts, and pairing data are not rolled back. The override persists
across reboot if recovery is needed; retain its snapshot until a later successful
deployment deliberately removes the override.

Recovery checks the same account/identity/pairing/thread/proxy criteria and has at most
two attempts. If a new active turn is visible, recovery waits for idle instead of
stopping that work. An unresponsive/dead relay is treated as a recovery failure path;
its turn state cannot be independently guaranteed. Expired/revoked credentials or an
upstream outage may require human action even after code recovery. The observer does
not change phone accounts or restore stale credentials to hide those failures.

The temporary observer removes its own unit after completion/cancellation/failure.
Durable status, events, and recovery artifacts remain private under
`state/deployments/<timestamp>/`. Do not delete a snapshot while the relay uses it.
The observer is not enabled at boot and is not a permanent service-health policy.

## Status and cancellation

The exact private job folder and unit name are reported when the job is armed.
Use its `status.json` for the result (`deployed`, `rolled_back`, `cancelled`, or `failed`)
and `events.jsonl` for the timeline. While the observer is running:

```bash
systemctl --user status mycodex-deploy-observer-TIMESTAMP.service
journalctl --user -u mycodex-deploy-observer-TIMESTAMP.service --no-pager
cat ~/mycodex/state/deployments/TIMESTAMP/status.json
```

To cancel further deployment actions, create `cancel` in that job folder. This leaves
the current relay alone; it does not undo a restart already performed. Cancellation
during recovery can prevent automatic restoration, so inspect the recorded phase first.

On this host the systemd user message bus is absent, so `systemd-run --user` is
unavailable. A temporary unit file managed through the working `systemctl --user`
connection provides the independent observer without starting another host service.

## Validation and scope

For the 2026-10-05 v0.3.0 handoff, the candidate passed 114 isolated tests.
The frozen earlier recovery copy passed its 81 tests. Twenty mocked observer tests cover active/tool gating, quiet-window
checks, source/config guards, operator replacement, pairing/project/identity checks,
bounded rollback, new active work, cancellation, monitoring, and private state files.
The corrected observer adds two regressions for ordinary idle-thread eviction (22 total).
An unarmed observer run verifies actual systemd isolation without restarting the relay.
No intentional deployment failure is injected against live work.

The general lifecycle defects [R01/R06](project-review.md#deferred-service-lifecycle-issues)
remain open. This bounded deployment observer adds recovery for this handoff; it does
not implement permanent child-exit failover or removal-confirmation fixes in mycodex.
See the [command guide](cli-guide.md) for everyday options.

## 2026-10-05 recheck and correction

The first observer had ended and removed its unit. It had deployed the candidate, then
rolled back after incorrectly treating unloaded idle conversations as failed relay
health. The final recovered service was connected with the original phone identity and
pairing. Its active source was the recovery snapshot, rather than the updated working
tree. Earlier status reported failure although the relay remained connected.

A fresh read-only probe reproduced the false check: every original health requirement
passed when the saved idle conversation's normal `notLoaded` state was excluded from
the loaded-memory requirement. Server logs also recorded temporary connection errors
during the repeated recovery restarts. These observations do not establish a candidate
code defect. The first job artifacts remain preserved for inspection.

The new job requires saved conversations to load during initial verification, but
accepts `notLoaded` during later monitoring while still validating stored IDs/projects,
RPC, connection, proxy, pairing, supervisor identity, and independent processes. It
allows ninety seconds for initial reconnect and logs details of failed probes.
Before restarting, its isolated observer selects the updated working-tree launcher by
changing only the existing recovery service override. Automatic recovery points that
override to a frozen copy identical to the currently running, previously tested code.
It preserves credentials, account settings, thread history, and phone identity.

The candidate passes 114 isolated tests and the corrected observer passes 22 mocked
tests. An unarmed run again confirms a separate systemd cgroup. The observer is then
armed to restart automatically after the current turn/tools finish and ten seconds of
quiet. Until its status records completion, do not claim the candidate is redeployed.

## Confirmed result and release

The corrected observer recorded `phase: completed`, `outcome: deployed` at
**2026-10-05 01:00:57 UTC**, with no rollback attempts. Its two-minute monitor finished
with zero misses, and it removed its temporary unit. Subsequent read-only checks
confirmed a connected relay/proxy, original pairing and projects, the verified
supervisor, and independent Codex processes. The PATH command and service both use
the project's launcher. Systemd recorded one automatic startup retry before the
successful supervisor; that counter did not increase during the later verification.

[v0.3.0](../CHANGELOG.md) publishes the tested application changes and this procedure.
The observer implementation/tests/snapshots remain private host runtime artifacts;
clones receive this guide, not a built-in deployment observer command. Version metadata
publication leaves existing processes running with their loaded metadata.

## v0.4.0 handoff pending — 2026-10-08

The current candidate contains 29 package modules and 143 isolated tests. The completed
2026-10-05 deployment and 81/114-test evidence above belong to earlier snapshots.
The v0.4.0 guarded restart has not completed: this conversation is in the relay cgroup.
After release validation an independent observer will be armed, waiting for all active
threads/tools to finish and ten seconds idle before restarting. New work resets the
idle window; the observer must remain outside the relay cgroup.

Updating the CLI/source alone does not reload the running proxy. The observer must
validate the candidate and recovery snapshot before acting, then verify relay/proxy
health and retained identity/pairing/projects using the guarded procedure above.
A completed observer result is required before claiming deployment success. R01/R06
and the other documented lifecycle/concurrency limitations remain open.
