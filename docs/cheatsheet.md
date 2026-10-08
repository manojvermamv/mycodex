# Everyday commands

These examples use `work`, `backup`, and `~/MyProject` as placeholders. Replace them
with your saved account names and folders. Quote names or text containing spaces.
The [complete guide](cli-guide.md) explains every parameter and its side effects.

## Start and choose an account

```bash
mycodex                              # open Codex with your usual account
mycodex use work                     # choose the usual account for new sessions
mycodex --profile backup             # choose an account just for this launch
mycodex --no-rotate                  # use one account for this session
mycodex resume --last                # reopen your latest conversation
mycodex FULL-CONVERSATION-UUID        # reopen a specific conversation
mycodex exec "Review this repository"
```

## Check accounts and usage

```bash
mycodex profile list
mycodex profile list --no-quota       # saved facts; skip online usage checks
mycodex profile add work             # sign in and save an account
mycodex profile reauth work          # repair an expired or damaged login
mycodex profile alias work daily     # create an account shortcut
mycodex quota                        # all accounts unless selected with --profile
mycodex --profile work quota
mycodex quota --watch 60             # repeat the check; Ctrl-C stops it
mycodex quota redeem work            # deliberately spend an earned reset credit
```

`not confirmed` means readiness was not established, not that an account is ready.
Online checks can renew expiring logins and save usage snapshots. Avoid deleting
account history to clear a cache; it also stores reset retry keys.

## Account switching

```bash
mycodex rotation status
mycodex rotation order work backup
mycodex rotation headroom 5          # fresh-request 5-hour threshold
mycodex rotation reset work          # clear a pause; adds no usage
mycodex rotation auto-redeem off     # preserve earned credits for manual spending
mycodex rotation log -n 20
```

Saved 5-hour/weekly reset times trigger live checks at the next fresh request when due.
Explicit readiness restores configured account priority; unknown or limited results keep
the pause. An active response retains its account.

## Projects and conversations

These commands require a running phone-capable app-server, except local thread listing.

```bash
mycodex projects
mycodex projects add ~/MyProject --name "My Project"
mycodex threads -n 40
mycodex threads link CONVERSATION-ID --project ~/MyProject
mycodex projects clean "My Project" --dry-run
mycodex projects clean "My Project"   # inspect candidates, then confirm archive
mycodex remote seed ~/MyProject --message "Plan the next change"
```

Linking creates no turn. Seeding sends a request and uses model quota. Cleanup archives
verified empty/ready-check histories and does not provide an atomic cross-client lock.

## Phone connection and problems

```bash
mycodex remote status
mycodex remote pair
mycodex remote clients
mycodex remote models share work     # share available models; keep each thread's model choice
mycodex remote logs -n 40
mycodex status --no-quota
mycodex processes
mycodex doctor
mycodex doctor --fix-rollout-paths    # backed-up path repair; no online usage check
```

Sharing models copies only a validated cache and leaves pairing and service lifecycle
intact. Without SOURCE it reuses the saved source, or the active profile if none is saved.
An explicit SOURCE overrides it. Later managed starts refresh the saved source;
an optional copy failure warns and continues startup with the existing cache.

Starting/stopping/restarting a phone service can interrupt active work. Use the
[redeployment guide](redeployment.md) to plan a host-side observer before replacing a
relay that hosts your conversation. It describes a private deployment procedure,
not a built-in restart command. Forced account removal and early-exit failover remain
open in the [review](project-review.md#deferred-service-lifecycle-issues).

## Learn an option

```bash
mycodex help
mycodex help quota redeem
mycodex projects clean --help
mycodex -- --help                    # official Codex's own commands and options
```

See [v0.4.0 changes](../CHANGELOG.md) and the [Quickstart](../QUICKSTART.md).
