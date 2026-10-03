"""`mycodex migrate`: take over accounts from prodex without logging in again.

For each prodex profile home ~/.prodex/profiles/<name> (a real directory):
  1. the old view link ~/.codex/profiles/<name> is removed,
  2. the home is renamed to ~/.codex/profiles/<name> - one atomic rename on the same
     filesystem that keeps auth.json, installation_id (the identity the phone paired
     with), caches and daemon state; nothing is copied, so a refresh token never exists twice,
  3. a link is left at the old path so a later `prodex` run still finds its homes,
  4. shared links are completed (thread names and writer locks become shared too).
The remote-control service is stopped for the move and started again afterwards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config, paths, procs, profiles, remote, ui


def _prodex_state() -> dict[str, Any]:
    try:
        data = json.loads(paths.PRODEX_STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def plan() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = paths.PRODEX_PROFILES
    if not root.is_dir():
        return items
    for source in sorted(root.iterdir(), key=lambda p: p.name):
        if source.name.startswith("."):
            continue
        target = paths.PROFILES_ROOT / source.name
        item = {"name": source.name, "source": source, "target": target}
        if source.is_symlink():
            item["action"] = "done" if Path(os.readlink(source)) == target else "skip"
            item["why"] = f"already a link to {os.readlink(source)}"
        elif not source.is_dir():
            item["action"], item["why"] = "skip", "not a directory"
        elif not profiles.valid_name(source.name):
            item["action"], item["why"] = "skip", "name is not usable as a mycodex profile"
        elif target.is_symlink() or not target.exists():
            item["action"] = "move"
        else:
            item["action"], item["why"] = "conflict", f"{target} already exists as a real directory"
        items.append(item)
    return items


def _users(sources: list[Path], all_procs: list[procs.Proc]) -> list[procs.Proc]:
    """Processes that run from, or with CODEX_HOME in, one of the homes about to move."""
    roots = [str(s) for s in sources] + [str(s.resolve()) for s in sources]
    busy = []
    for proc in all_procs:
        home = proc.env.get("CODEX_HOME") or ""
        if any(home.rstrip("/") == r or home.startswith(r + "/") or proc.argv[0].startswith(r + "/") for r in roots):
            busy.append(proc)
        elif proc.role.startswith("prodex") or proc.role == "rotation proxy":
            busy.append(proc)
    return busy


def run(dry_run: bool = False, assume_yes: bool = False, compat_links: bool = True) -> int:
    items = plan()
    moves = [i for i in items if i["action"] == "move"]
    if not items:
        ui.info(f"nothing to migrate: {paths.PRODEX_PROFILES} has no profiles")
        return 0
    fields = []
    for item in items:
        label = {"move": "move", "done": "already migrated", "skip": "skip", "conflict": "CONFLICT"}[item["action"]]
        fields.append((item["name"], f"{label}: {item['source']} -> {item['target']}"
                                     + (f" ({item['why']})" if item.get("why") else "")))
    ui.panel("Mycodex Migrate", fields, subtitle=f"{len(moves)} account(s) to move")
    if not moves:
        ui.info("nothing to move")
        return 1 if any(i["action"] == "conflict" for i in items) else 0

    service_active = remote.service_info().get("ActiveState") in ("active", "activating", "reloading")
    service_main = int(remote.service_info().get("MainPID") or 0)
    all_procs = procs.scan(with_sockets=False)
    service_pids = {p.pid for p in procs.descendants(service_main, all_procs)} | {service_main}
    busy = [p for p in _users([i["source"] for i in moves], all_procs) if p.pid not in service_pids]
    if busy:
        ui.error("these processes still use the prodex homes; close them first:",
                 [f"{p.pid} {p.role} ({p.profile or '-'})" for p in busy])
        return 1
    if dry_run:
        ui.info("dry run: nothing changed" + ("; the remote-control service would restart" if service_active else ""))
        return 0
    question = f"Move {len(moves)} account(s) into {paths.PROFILES_ROOT}"
    question += " (the remote-control service restarts; the phone reconnects by itself)?" if service_active else "?"
    if not ui.confirm(question, assume_yes=assume_yes):
        ui.info("nothing changed")
        return 1

    if service_active:
        ui.info("stopping the remote-control service")
        remote._systemctl("stop", paths.REMOTE_UNIT)
        leftovers = _users([i["source"] for i in moves], procs.scan(with_sockets=False))
        if leftovers:
            ui.error("processes are still using the prodex homes after stopping the service:",
                     [f"{p.pid} {p.role}" for p in leftovers])
            remote._systemctl("start", paths.REMOTE_UNIT)
            return 1

    profiles.ensure_root()
    moved = []
    for item in moves:
        source, target = item["source"], item["target"]
        if target.is_symlink():
            target.unlink()
        try:
            os.rename(source, target)
        except OSError as exc:
            ui.error(f"could not move {source}: {exc}")
            if not target.exists():
                target.symlink_to(source, target_is_directory=True)
            continue
        if compat_links:
            source.symlink_to(target, target_is_directory=True)
        report = profiles.prepare_home(target, adopt=True)
        detail = []
        if report.linked:
            detail.append("linked " + ", ".join(report.linked))
        if report.adopted:
            detail.append("shared " + ", ".join(report.adopted))
        if report.private:
            detail.append("still private: " + ", ".join(report.private))
        moved.append(item["name"])
        ui.success(f"{item['name']}: moved" + (f" ({'; '.join(detail)})" if detail else ""))

    prodex_active = _prodex_state().get("active_profile")
    with config.editing() as data:
        if not data.get("active") and prodex_active in profiles.names(data):
            data["active"] = prodex_active

    if service_active:
        ui.info("starting the remote-control service")
        remote._systemctl("start", paths.REMOTE_UNIT)
        box = ui.StatusBox("Mycodex Migrate")
        connected = remote.wait_until_connected(box=box)
        box.clear()
        (ui.success if connected else ui.warn)(
            "remote control is connected" if connected else "remote control is not connected yet (mycodex remote status)")

    from .threads import hidden as hidden_threads
    hidden = hidden_threads()
    notes = [("Moved", ", ".join(moved) or "none"),
             ("Accounts", str(paths.PROFILES_ROOT)),
             ("prodex", "no longer used by mycodex" + ("; its old paths link to the new homes" if compat_links else ""))]
    if hidden:
        notes.append(("Threads", f"{len(hidden)} thread(s) carry a prodex tag and are hidden from the phone; "
                                 "list them with `mycodex threads` and copy one with `mycodex threads adopt <ID>`"))
    ui.panel("Mycodex Migrate", notes)
    return 0
