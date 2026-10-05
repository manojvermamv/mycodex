"""Narrow, backed-up repair of legacy rollout references; never delete thread rows."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from . import paths


def databases() -> list[Path]:
    root = paths.SHARED_CODEX_HOME
    candidates = [*root.glob("state_*.sqlite"), *root.glob("profiles/*/state_*.sqlite")]
    return sorted({p.resolve() for p in candidates if p.is_file()})


def _matches(path: Path, thread_id: str) -> bool:
    try:
        with path.open() as stream:
            event = json.loads(stream.readline(1 << 20))
        return event.get("type") == "session_meta" and event.get("payload", {}).get("id") == thread_id
    except (OSError, ValueError, AttributeError):
        return False


def replacement(thread_id: str, old: str) -> Path | None:
    root = paths.SHARED_CODEX_HOME.resolve()
    allowed = [root / "sessions", root / "archived_sessions"]
    candidates: set[Path] = set()
    for marker in ("/sessions/", "/archived_sessions/"):
        if marker in old:
            relative = old.split(marker, 1)[1]
            for base in allowed:
                for item in (base / relative, base / Path(old).name):
                    resolved = item.resolve()
                    if base.resolve() in resolved.parents and _matches(resolved, thread_id):
                        candidates.add(resolved)
    if not candidates:
        for base in allowed:
            for item in base.rglob(f"rollout-*-{thread_id}.jsonl"):
                resolved = item.resolve()
                if base.resolve() in resolved.parents and _matches(resolved, thread_id):
                    candidates.add(resolved)
    return next(iter(candidates)) if len(candidates) == 1 else None


@contextlib.contextmanager
def _idle_lock(thread_id: str) -> Iterator[None]:
    import fcntl
    # Codex uses <uuid>.lock; check every existing matching writer lock without creating one.
    roots = [paths.SHARED_CODEX_HOME, *paths.PROFILES_ROOT.glob("*")]
    locks = {p.resolve() for root in roots for p in (root / "thread-writer-locks").glob(f"*{thread_id}*")}
    with contextlib.ExitStack() as stack:
        for path in sorted(locks):
            handle = stack.enter_context(path.open("rb"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def audit(fix: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for db in databases():
        report: dict[str, Any] = {"database": str(db), "legacy": 0, "repaired": 0, "unresolved": [], "backup": None}
        results.append(report)
        try:
            with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as reader:
                columns = {r[1] for r in reader.execute("pragma table_info(threads)")}
                if not {"id", "rollout_path"} <= columns:
                    continue
                rows = reader.execute("select id, rollout_path from threads where rollout_path like '%/.prodex/%'").fetchall()
                report["legacy"] = len(rows)
                changes = []
                for thread_id, old in rows:
                    # Reject malformed IDs before using them in a filesystem glob.
                    try:
                        uuid.UUID(thread_id)
                    except (ValueError, TypeError):
                        report["unresolved"].append({"thread": thread_id, "reason": "invalid thread id"})
                        continue
                    target = replacement(thread_id, old)
                    if target is None:
                        report["unresolved"].append({"thread": thread_id, "reason": "no unique rollout with matching session identity"})
                    else:
                        changes.append((thread_id, old, target))
                report["repairable"] = len(changes)
                if not fix or not changes:
                    continue
                digest = hashlib.sha256(str(db).encode()).hexdigest()[:8]
                backup = paths.state_dir("backups") / f"rollouts-{digest}-{uuid.uuid4().hex}.sqlite"
                backup.touch(mode=0o600)
                with contextlib.closing(sqlite3.connect(backup)) as saved:
                    deadline = time.monotonic() + 10
                    def progress(_status: int, _remaining: int, _total: int) -> None:
                        if time.monotonic() > deadline:
                            raise TimeoutError("database backup timed out; no repair was applied")
                    reader.backup(saved, pages=256, progress=progress, sleep=0.05)
                report["backup"] = str(backup)
            with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=2)) as writer:
                for thread_id, old, target in changes:
                    try:
                        with _idle_lock(thread_id), writer:
                            if not _matches(target, thread_id):
                                raise OSError("replacement changed during repair")
                            changed = writer.execute("update threads set rollout_path=? where id=? and rollout_path=?",
                                                     (str(target), thread_id, old)).rowcount
                            report["repaired"] += changed
                    except (OSError, sqlite3.Error) as exc:
                        report["unresolved"].append({"thread": thread_id, "reason": f"busy or changed: {exc}"})
        except (OSError, sqlite3.Error) as exc:
            report["error"] = str(exc)
    return results
