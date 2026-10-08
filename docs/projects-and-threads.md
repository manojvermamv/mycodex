# Projects and new threads

The ChatGPT phone app groups Codex threads by **project**: a named set of folders on the
host. This guide shows how to start a new thread inside a project, with one mycodex
command or through the app-server proxy path for your own scripts.
Reviewed on 2026-10-04. See [project-review.md](project-review.md) for current lookup,
partial-operation, and concurrency limits.

Model selection remains user controlled for each conversation. Sharing the relay's
model catalogue and switching accounts preserve selected models and reasoning settings;
they do not apply one model to every project thread. See the [command guide](cli-guide.md)
and [v0.4.1 changes](../CHANGELOG.md).

## How Codex files threads under projects

- A project is created only through Codex's app-server API (`project/create`); there is no
  `codex` CLI command for it.
- A thread belongs to a project only when the client passes the project id: `thread/start`
  with `projectId`, or `thread/metadata/update` afterwards. Codex does not derive it from
  the working directory. This wrapper's TUI/exec launch does not supply a project ID, so
  starting in a project folder alone does not establish project membership.
- A thread is saved once its first turn runs, so a new project thread starts with a first
  message.
- Normal wrapper launches retain Codex's `openai` provider tag. Native provider overrides
  can differ. Provider compatibility, project assignment, loaded state, and phone
  rendering are separate checks (see [architecture.md](architecture.md#threads)).

All of the methods below are experimental app-server methods: a client must announce
`experimentalApi` when it connects.
These commands require a running remote-control-capable server; they do not silently
start/restart one. `projects` currently has no offline fallback, rename, or delete CLI.

## Register and inspect projects

```bash
mycodex projects
mycodex projects --json
mycodex projects add ~/MyProject --name "My Project"
```

Registration reuses the folder's existing project and creates no thread. Lists include
all pages and count non-archived threads across all providers and source types.
That count includes threads regardless of loaded/active state. `--name` applies when
creating a project; adding an already-registered folder does not rename it. Use an exact
ID when names or roots are ambiguous.

## Link an existing thread

```bash
mycodex threads link <thread id> --project ~/MyProject
mycodex threads link <thread id> --project "My Project"
```

The selector accepts a root directory, exact name, or ID. Ambiguous names require an ID.
Without `--project`, the command uses the thread's existing project, or the deepest
registered root containing its working directory. It assigns the project with
`thread/metadata/update`, then hydrates the same thread with `thread/resume`; no turn is
started and no service restart is needed. Archived threads must be unarchived first;
threads with another provider must be adopted for phone visibility. If hydration fails,
the saved assignment is retained and the command reports the failure so it can be retried.
`notLoaded` is a normal unloaded state, rather than proof of a missing mobile thread.
The command verifies the returned ID, project, and loaded status (`idle` or `active`).
The database lookup currently searches only the latest 5,000 threads, even for full IDs;
an older thread can therefore fail lookup. Investigate writer/resume errors rather than
restarting the service to force hydration.

## Clean empty or ready-check threads

```bash
mycodex projects clean "My Project" --dry-run
mycodex projects clean "My Project" --yes
```

Only empty histories or one completed built-in ready check with the exact `ready` answer
qualify. Threads with real prompts, tool executions, additional turns, active work, or
non-archived descendants are preserved. Cleanup confirms the candidates and rechecks
history before archiving. It archives through Codex's API and never deletes rollouts.
Without `--yes`, mutation requires a terminal confirmation. The subcommand's
`--dry-run` and global `mycodex --dry-run projects clean ...` both preview cleanup. Reads/archive are separate RPCs and do not provide an atomic
cross-client lock, so schedule cleanup when candidate threads are idle.

## One command: `mycodex remote seed`

```bash
mycodex remote seed ~/MyProject --name "Login page" \
  --message "Plan the login page. List the files you would change first."
```

With the remote-control service running, this:

1. lists the projects and reuses the one whose root is `~/MyProject`, or creates it
   (named after the folder; `--project NAME` chooses another name);
2. starts a thread there (`thread/start` with `cwd` and `projectId`);
3. names it (`thread/name/set`);
4. starts its first turn with your message (`turn/start`) and waits up to three minutes
   for it. The turn keeps running on the server after that, or if you pass `--no-wait`.

Without `--message`, the first turn is a one-word "ready" check, so the thread exists
with a minimal model interaction; it still consumes model quota and uses the server's
approval/sandbox policy. It requests no commands or file changes, but that instruction
is not a separate sandbox. Continue the thread on the phone, inside the project, or in the
terminal:

```bash
mycodex resume <thread id>
mycodex <full-thread-UUID>
```

The turn runs as the server's relay account; in rotating mode its model requests rotate
across your ready accounts like any other turn. The usual `~/.codex/config.toml` approval
and sandbox settings apply.
Seed may return while the turn is still running if the wait expires; exit zero alone
does not prove completion. Failure between project/thread/turn operations can leave a
partial result. Inspect it before retrying to avoid creating another thread.

## The app-server proxy path

`seed` is a thin client of the running server. You can do the same from a script.

### 1. Find the server's control socket

```bash
mycodex remote socket
```

This prints the control socket of the remote-control service's server (for example
`/tmp/codex-daemon-1000/9f3c…`). Always pass it explicitly: `codex app-server proxy`
without `--sock` connects to the default socket of its `CODEX_HOME`, which the service's
server does not publish.

### 2. Connect through the official relay

```bash
mycodex app-server proxy --sock "$(mycodex remote socket)"
```

`mycodex app-server …` runs the official `codex app-server …` (the relay needs no rotation
proxy, so mycodex execs it directly). The relay connects its stdin and stdout to the
socket, and the server speaks **JSON-RPC over WebSocket** on that stream:

1. Write an HTTP upgrade request and read the `HTTP/1.1 101 Switching Protocols` answer:

   ```text
   GET / HTTP/1.1
   Host: localhost
   Upgrade: websocket
   Connection: Upgrade
   Sec-WebSocket-Key: <16 random bytes, base64>
   Sec-WebSocket-Version: 13
   ```

2. Send each JSON-RPC message as one WebSocket text frame (client frames are masked) and
   read the server's frames back.

### 3. The JSON-RPC sequence

```json
{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "my-script", "version": "1.0"}, "capabilities": {"experimentalApi": true}}}
{"method": "initialized"}
{"id": 2, "method": "project/list", "params": {}}
{"id": 3, "method": "project/create", "params": {"name": "MyProject", "roots": [{"path": "/home/you/MyProject"}], "idempotencyKey": "my-script-myproject"}}
{"id": 4, "method": "thread/start", "params": {"cwd": "/home/you/MyProject", "projectId": "<project id>"}}
{"id": 5, "method": "thread/name/set", "params": {"threadId": "<thread id>", "name": "Login page"}}
{"id": 6, "method": "turn/start", "params": {"threadId": "<thread id>", "input": [{"type": "text", "text": "Plan the login page."}]}}
```

- `project/list` returns `{"data": [{"id", "name", "roots": [{"path"}], …}], "nextCursor"}`
  (pass `{"cursor": …}` for the next page). Create a project only when no project has your
  folder as a root.
- `project/create` returns `{"project": {…}}`. The `idempotencyKey` makes a retried
  request return the same project; derive it from the folder path, not just its name.
- `thread/start` returns `{"thread": {"id", "modelProvider", …}}`.
- After `turn/start`, the server streams notifications (`turn/started`, item events, …)
  and finally `turn/completed` with the turn's status.
- The server may send requests of its own (approvals, for example). Answer them, or decline
  with a JSON-RPC error; with approvals disabled in `config.toml` none arrive.

Other useful methods with the same connection:

| Method | Params | Use |
|---|---|---|
| `thread/metadata/update` | `{"threadId", "projectId"}` | file an existing thread (for example one started in the TUI) under a project; `""` removes it |
| `thread/resume` | `{"threadId": "<id>", "excludeTurns": true}` | load a stored thread without starting a model turn |
| `thread/read` | `{"threadId": "<id>", "includeTurns": false}` | inspect a stored thread without hydrating it |
| `thread/list` | `{"projectId": "<id>"}` | threads in one project (`null` for threads without one) |
| `project/import` | `{"name", "roots", "threads": [ids], "idempotencyKey"}` | create a project and file existing threads in one call |
| `thread/archive` | `{"threadId"}` | archive a thread |
| `project/delete` | `{"projectId"}` | delete a project (its threads stay, without a project) |

### 4. A ready-to-use script

mycodex's own client (`mycodex.appserver.AppServer`) performs the handshake, the framing
and the `initialize` exchange over the same relay, so a script needs only the calls:

```python
#!/usr/bin/env python3
"""new-project-thread DIR NAME PROMPT: start a named thread in DIR's project."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/mycodex/src"))
from mycodex import appserver, projects, remote_cmd

root = Path(sys.argv[1]).expanduser().resolve()
if not root.is_dir():
    raise SystemExit(f"Not a directory: {root}")
name, prompt = sys.argv[2], sys.argv[3]

sock, relay = remote_cmd.server_socket()           # the remote-control service's server
with appserver.AppServer(sock, timeout=120) as server:
    project, created = projects.ensure(server, root)  # paginated lookup, idempotent creation
    thread = server.result("thread/start", {"cwd": str(root), "projectId": project["id"]})["thread"]
    server.result("thread/name/set", {"threadId": thread["id"], "name": name})
    server.result("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": prompt}]})
    print(thread["id"])
```

To file a thread you started in the terminal under a project instead, replace the
thread start, naming, turn start, and final print statements with this sequence,
retaining project resolution. Prefer the
CLI's `threads link` for its selector/error/verification handling:

```python
server.result("thread/metadata/update", {"threadId": "<thread id>", "projectId": project["id"]})
loaded = server.result("thread/resume", {"threadId": "<thread id>", "excludeTurns": True})["thread"]
print(loaded["id"], loaded["projectId"], loaded["status"])
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no remote-control server is running` | Start the service: `mycodex --profile NAME remote-control` |
| `project/…` or `projectId` rejected as experimental | The client did not send `capabilities.experimentalApi: true` in `initialize` |
| The thread is missing on the phone | Check its tag with `mycodex threads`; threads not tagged `openai` need `mycodex threads adopt ID` |
| The thread is outside the project or unloaded | `mycodex threads link ID --project DIR_OR_NAME_OR_ID` updates metadata and resumes it |
| Linking saves assignment but hydration fails | Retry the reported command; inspect active writers/errors; the assignment was retained |
| Adding a folder ignores the supplied new name | The folder is already registered; `add` reuses it and is not a rename command |
| Correct provider/project still does not appear on the phone | Check loaded state, relay account, and client synchronization; host eligibility is not proof of rendering |
| `codex app-server proxy` fails to connect | Pass `--sock "$(mycodex remote socket)"`; the default socket is not the service's server |

## Command help and input checks

See the [complete command guide](cli-guide.md) for every project/conversation parameter.
`mycodex help projects clean` and `mycodex threads link --help` work without required IDs.
Names containing spaces should be quoted. `threads list -n N` requires a positive whole
number; zero, negative, and malformed values are rejected consistently for table/JSON
output. The 2,000-row listing and 5,000-row ID lookup ceilings remain open (R08).
The service lifecycle issues R01 and R06 are saved in [project-review.md](project-review.md).

## v0.3.0 release

These project/conversation commands ship in [v0.3.0](../CHANGELOG.md). The
[everyday commands](cheatsheet.md) provide short examples, and
`python3 -B tools/run_tests.py` runs the 114-test isolated suite. The corrected host
deployment retained conversation projects and phone pairing, and its observer completed
healthy monitoring. Publication does not reload active sessions; the known lookup and
concurrency limits remain documented in the review.
