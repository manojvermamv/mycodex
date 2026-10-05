"""`mycodex threads`: the shared thread list, and adopting threads that carry another provider tag.

Codex stamps a thread with its model provider when the thread is created and filters
thread lists by it, so a thread started under another provider id (for example prodex's
`prodex-openai-governed-http`) stays hidden from the phone and from pickers that run as
`openai`. `adopt` forks such a thread through the running server's official API into an
`openai`-tagged copy with the same history, name and project; the original is kept.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import appserver, codex, fmt, state, ui

OPENAI = "openai"


def hidden(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Live threads the phone cannot list: another provider tag and no adopted copy yet."""
    rows = codex.threads(limit=5000) if rows is None else rows
    adopted = state.adoptions()
    return [t for t in rows if (t.get("model_provider") or OPENAI) != OPENAI
            and not t.get("archived") and t["id"] not in adopted]


def _when(value: Any) -> str:
    if not value:
        return "-"
    seconds = float(value) / 1000 if float(value) > 1e11 else float(value)
    return fmt.reset_time(seconds) if time.time() - seconds < 6 * 86400 else time.strftime("%Y-%m-%d", time.localtime(seconds))


def _title(thread: dict[str, Any]) -> str:
    return (thread.get("name") or thread.get("title") or (thread.get("first_user_message") or "").strip()
            or "-").splitlines()[0]


def list_threads(show_all: bool, as_json: bool, limit: int = 40) -> int:
    rows = codex.threads(limit=2000)
    if not show_all:
        rows = [t for t in rows if not t.get("archived")]
    if as_json:
        print(json.dumps(rows[:limit] if limit else rows, indent=2, default=str))
        return 0
    adopted = state.adoptions()
    invisible = hidden(rows)

    def tag(t: dict[str, Any]) -> str:
        value = t.get("model_provider") or "-"
        return value if t["id"] not in adopted else f"{value} (adopted)"

    table = [[t["id"], tag(t), _when(t.get("updated_at")), _title(t), t.get("cwd") or "-"] for t in rows[:limit]]

    def style(i: int, row: list[str]) -> ui.Style:
        if i == 1:
            return ui.SUCCESS if row[1] == OPENAI else (ui.SECONDARY if row[1].endswith("(adopted)") else ui.WARNING)
        return ui.SECONDARY if i in (2, 4) else ui.PRIMARY

    notes = [f"{original[:13]}… was adopted as {copy}" for original, copy in adopted.items()
             if any(t["id"] == original for t in rows[:limit])]
    if invisible:
        notes.append(f"{len(invisible)} thread(s) carry another provider tag and are hidden from the phone; "
                     "`mycodex threads adopt <ID>` makes an openai-tagged copy")
    ui.table("Mycodex Threads", [ui.Column("ID", min_width=36), ui.Column("TAG", max_width=18),
                                 ui.Column("UPDATED"), ui.Column("NAME", max_width=40), ui.Column("CWD", max_width=40)],
             table, subtitle=f"{len(rows)} thread(s)" + ("" if show_all else ", archived hidden"),
             cell_style=style, notes=notes)
    return 0


def _find(thread_id: str) -> dict[str, Any]:
    rows = codex.threads(limit=5000)
    matches = [t for t in rows if t["id"] == thread_id] or [t for t in rows if t["id"].startswith(thread_id)]
    if not matches:
        raise SystemExit(f"Error: no thread matches '{thread_id}' (mycodex threads --all)")
    if len(matches) > 1:
        raise SystemExit(f"Error: '{thread_id}' matches {len(matches)} threads; use more of the id")
    return matches[0]


def _project_for(thread: dict[str, Any]) -> str | None:
    if thread.get("project_id"):
        return thread["project_id"]
    cwd = Path(thread.get("cwd") or "/nonexistent")
    best: tuple[int, str] | None = None
    for project in codex.projects():
        for root in (project.get("roots") or "").split(";"):
            if root and (cwd == Path(root) or Path(root) in cwd.parents):
                if best is None or len(root) > best[0]:
                    best = (len(root), project["id"])
    return best[1] if best else None


def adopt(thread_id: str, name: str | None, archive_original: bool) -> int:
    from .remote_cmd import server_socket

    thread = _find(thread_id)
    if (thread.get("model_provider") or OPENAI) == OPENAI:
        ui.info(f"{thread['id']} is already tagged openai; nothing to do")
        return 0
    previous = state.adoptions().get(thread["id"])
    if previous:
        ui.info(f"{thread['id']} was already adopted as {previous}; continue there (mycodex resume {previous})")
        return 0
    sock, relay = server_socket()
    title = name or _title(thread)
    project = _project_for(thread)
    box = ui.StatusBox("Mycodex Adopt")
    box.update("forking", f"{thread['id']} ({thread.get('model_provider')}) -> openai")
    with appserver.AppServer(sock, timeout=180) as server:
        result = server.result("thread/fork", {"threadId": thread["id"], "modelProvider": OPENAI,
                                               "cwd": thread.get("cwd")})
        copy = result["thread"]
        box.update("naming", copy["id"])
        server.result("thread/name/set", {"threadId": copy["id"], "name": title})
        if project:
            server.result("thread/metadata/update", {"threadId": copy["id"], "projectId": project})
        if archive_original:
            server.result("thread/archive", {"threadId": thread["id"]})
    box.clear()
    state.record_adoption(thread["id"], copy["id"])
    ui.panel("Mycodex Adopt", [
        ("Original", f"{thread['id']} ({thread.get('model_provider')})" + (", archived" if archive_original else ", kept")),
        ("Copy", copy["id"]),
        ("Tag", result.get("modelProvider") or OPENAI),
        ("Name", title),
        ("Project", project or "-"),
        ("Server", relay or "-"),
        ("Continue", f"mycodex resume {copy['id']}   (or pick it on the phone)"),
    ])
    return 0


def link(thread_id: str, project: str | None = None) -> int:
    from . import projects

    thread = _find(thread_id)
    if thread.get("archived"):
        ui.error("the thread is archived; unarchive it in Codex before linking it")
        return 1
    if (thread.get("model_provider") or OPENAI) != OPENAI:
        ui.error("this thread uses another provider; adopt it first for phone visibility")
        return 1
    with projects.connection() as server:
        target = projects.resolve(projects.pages(server, "project/list"), project,
                                  cwd=thread.get("cwd"), project_id=thread.get("project_id") if not project else None)
        server.result("thread/metadata/update", {"threadId": thread["id"], "projectId": target["id"]})
        try:
            hydrated = server.result("thread/resume", {"threadId": thread["id"], "excludeTurns": True})["thread"]
        except appserver.AppServerError as exc:
            ui.error(f"project assignment saved, but hydration failed: {exc}",
                     [f"retry: mycodex threads link {thread['id']} --project {target['id']}"])
            return 1
        if hydrated.get("id") != thread["id"] or hydrated.get("projectId") != target["id"] \
                or (hydrated.get("status") or {}).get("type") not in ("idle", "active"):
            raise appserver.AppServerError("project assignment was saved, but thread/resume did not confirm a loaded thread in that project")
    ui.success(f"linked and hydrated {thread['id']} in {target['name']} ({target['id']})")
    return 0
