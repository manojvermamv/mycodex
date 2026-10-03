# Projects and new threads

The ChatGPT phone app groups Codex threads by **project**: a named set of folders on the
host. This guide shows how to start a new thread inside a project, with one mycodex
command or through the app-server proxy path for your own scripts.

## How Codex files threads under projects

- A project is created only through Codex's app-server API (`project/create`); there is no
  `codex` CLI command for it.
- A thread belongs to a project only when the client passes the project id: `thread/start`
  with `projectId`, or `thread/metadata/update` afterwards. Codex does not derive it from
  the working directory, and the TUI and `codex exec` never pass one. A thread you start
  with `mycodex` or `mycodex exec` inside a project folder is therefore visible on the
  phone, in the thread list, but not inside the project.
- A thread is saved once its first turn runs, so a new project thread starts with a first
  message.
- Everything mycodex starts uses Codex's `openai` provider tag, so these threads appear on
  the phone and in every terminal (see [architecture.md](architecture.md#threads)).

All of the methods below are experimental app-server methods: a client must announce
`experimentalApi` when it connects.

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
without doing any work. Continue the thread on the phone, inside the project, or in the
terminal:

```bash
mycodex resume <thread id>
```

The turn runs as the server's relay account; in rotating mode its model requests rotate
across your ready accounts like any other turn. The usual `~/.codex/config.toml` approval
and sandbox settings apply.

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
import hashlib
import os
import sys

sys.path.insert(0, os.path.expanduser("~/mycodex/src"))
from mycodex import appserver, remote_cmd

root = os.path.realpath(os.path.expanduser(sys.argv[1]))
name, prompt = sys.argv[2], sys.argv[3]

sock, relay = remote_cmd.server_socket()           # the remote-control service's server
with appserver.AppServer(sock, timeout=120) as server:
    projects = server.result("project/list", {})["data"]
    project = next((p for p in projects if any(r["path"] == root for r in p["roots"])), None)
    if project is None:
        key = "my-script-" + hashlib.sha256(root.encode()).hexdigest()[:16]
        project = server.result("project/create", {
            "name": os.path.basename(root), "roots": [{"path": root}], "idempotencyKey": key})["project"]
    thread = server.result("thread/start", {"cwd": root, "projectId": project["id"]})["thread"]
    server.result("thread/name/set", {"threadId": thread["id"], "name": name})
    server.result("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": prompt}]})
    print(thread["id"])
```

To file a thread you started in the terminal under a project instead, replace the last
three calls with:

```python
server.result("thread/metadata/update", {"threadId": "<thread id>", "projectId": project["id"]})
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no remote-control server is running` | Start the service: `mycodex --profile NAME remote-control` |
| `project/…` or `projectId` rejected as experimental | The client did not send `capabilities.experimentalApi: true` in `initialize` |
| The thread is missing on the phone | Check its tag with `mycodex threads`; threads not tagged `openai` need `mycodex threads adopt ID` |
| The thread is on the phone but not inside the project | It has no project id; use `thread/metadata/update` as shown above |
| `codex app-server proxy` fails to connect | Pass `--sock "$(mycodex remote socket)"`; the default socket is not the service's server |
