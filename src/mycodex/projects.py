"""Project discovery and maintenance through the running official app-server."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import appserver, ui

READY_MESSAGE = "Workspace ready check. Reply with exactly the word: ready. Do not run commands or modify files."
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]


def pages(server: appserver.AppServer, method: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    request = dict(params or {})
    cursors: set[str] = set()
    while True:
        page = server.result(method, request)
        result.extend(page.get("data", []))
        cursor = page.get("nextCursor")
        if not cursor:
            return result
        if cursor in cursors:
            raise appserver.AppServerError(f"{method}: server repeated a pagination cursor")
        cursors.add(cursor)
        request["cursor"] = cursor


def connection() -> appserver.AppServer:
    from .remote_cmd import server_socket
    sock, _ = server_socket()
    return appserver.AppServer(sock, timeout=60)


def resolve(rows: list[dict[str, Any]], value: str | None = None,
            cwd: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    if value:
        matches = [p for p in rows if p["id"] == value]
        if not matches:
            matches = [p for p in rows if p["name"] == value]
        if not matches:
            root = Path(value).expanduser().resolve()
            matches = [p for p in rows if any(Path(r["path"]).resolve() == root for r in p.get("roots", []))]
    elif project_id:
        matches = [p for p in rows if p["id"] == project_id]
    else:
        directory = Path(cwd or ".").expanduser().resolve()
        ranked = [(len(Path(r["path"]).parts), p) for p in rows for r in p.get("roots", [])
                  if directory == Path(r["path"]).resolve() or Path(r["path"]).resolve() in directory.parents]
        deepest = max((n for n, _ in ranked), default=0)
        matches = list({p["id"]: p for n, p in ranked if n == deepest}.values())
    if len(matches) != 1:
        label = value or project_id or cwd or "current directory"
        if matches:
            raise appserver.AppServerError(f"project '{label}' is ambiguous: " + ", ".join(p["id"] for p in matches))
        raise appserver.AppServerError(f"no registered project matches '{label}' (mycodex projects add DIR)")
    return matches[0]


def ensure(server: appserver.AppServer, directory: Path, name: str | None = None) -> tuple[dict[str, Any], bool]:
    root = str(directory.expanduser().resolve())
    rows = pages(server, "project/list")
    matches = [p for p in rows if any(str(Path(r["path"]).resolve()) == root for r in p.get("roots", []))]
    if matches:
        return resolve(matches, value=root), False
    key = "mycodex-" + hashlib.sha256(root.encode()).hexdigest()[:16]
    project = server.result("project/create", {"name": name or Path(root).name, "roots": [{"path": root}],
                                               "idempotencyKey": key})["project"]
    return project, True


def member_threads(server: appserver.AppServer, project_id: str) -> list[dict[str, Any]]:
    return pages(server, "thread/list", {"projectId": project_id, "archived": False,
                 "modelProviders": [], "sourceKinds": SOURCE_KINDS, "useStateDbOnly": True})


def list_projects(as_json: bool = False) -> int:
    with connection() as server:
        rows = pages(server, "project/list")
        data = [{**p, "thread_count": len(member_threads(server, p["id"]))} for p in rows]
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        ui.table("Mycodex Projects", [ui.Column("ID"), ui.Column("NAME"), ui.Column("ROOTS"), ui.Column("THREADS")],
                 [[p["id"], p["name"], "; ".join(r["path"] for r in p.get("roots", [])), str(p["thread_count"])]
                  for p in data], notes=["THREADS counts non-archived threads across all providers and sources."])
    return 0


def add(directory: str, name: str | None = None, as_json: bool = False) -> int:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        ui.error(f"{root} is not a directory")
        return 1
    if name is not None and not name.strip():
        ui.error("project name must not be empty")
        return 2
    with connection() as server:
        project, created = ensure(server, root, name)
    if as_json:
        print(json.dumps({"project": project, "created": created}, indent=2))
    else:
        ui.success(f"project {project['name']} ({project['id']}) {'created' if created else 'already registered'} at {root}")
    return 0


def disposable(thread: dict[str, Any]) -> bool:
    """Prove there is no work: no turns, or exactly the built-in ready check."""
    if thread.get("status", {}).get("type") in ("active", "systemError"):
        return False
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return False
    if not turns:
        return not thread.get("preview", "").strip()
    if len(turns) != 1 or turns[0].get("status") != "completed":
        return False
    items = turns[0].get("items", [])
    if any(i.get("type") not in ("userMessage", "agentMessage", "reasoning") for i in items):
        return False
    users = [i for i in items if i.get("type") == "userMessage"]
    agents = [i for i in items if i.get("type") == "agentMessage"]
    if len(users) != 1 or len(agents) != 1 or agents[0].get("text", "").strip() != "ready":
        return False
    content = users[0].get("content", [])
    return len(content) == 1 and content[0].get("type") == "text" and content[0].get("text") == READY_MESSAGE


def clean(value: str, dry_run: bool = False, assume_yes: bool = False) -> int:
    with connection() as server:
        project = resolve(pages(server, "project/list"), value)
        candidates = []
        for row in member_threads(server, project["id"]):
            try:
                thread = server.result("thread/read", {"threadId": row["id"], "includeTurns": True})["thread"]
            except appserver.AppServerError as exc:
                ui.warn(f"skipped {row['id']}: could not verify history ({exc})")
                continue
            if disposable(thread):
                children = pages(server, "thread/list", {"ancestorThreadId": row["id"], "modelProviders": [],
                                                        "sourceKinds": SOURCE_KINDS, "useStateDbOnly": True})
                if not children:
                    candidates.append(thread)
        ui.table("Mycodex Project Cleanup", [ui.Column("ID"), ui.Column("NAME")],
                 [[t["id"], t.get("name") or t.get("preview") or "(empty)"] for t in candidates],
                 subtitle=project["name"])
        if dry_run or not candidates:
            return 0
        if not ui.confirm(f"Archive these {len(candidates)} empty/ready-check threads?", assume_yes=assume_yes):
            return 1
        for row in candidates:
            # Recheck immediately before archive; never clean a thread that gained a turn.
            current = server.result("thread/read", {"threadId": row["id"], "includeTurns": True})["thread"]
            children = pages(server, "thread/list", {"ancestorThreadId": row["id"], "modelProviders": [],
                                                    "sourceKinds": SOURCE_KINDS, "useStateDbOnly": True})
            if current.get("projectId") != project["id"] or not disposable(current) or children:
                ui.warn(f"skipped {row['id']}: changed or has descendants")
                continue
            server.result("thread/archive", {"threadId": row["id"]})
            ui.success(f"archived {row['id']}")
    return 0
