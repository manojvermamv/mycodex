"""Opt-in diagnosis and quota-window auto-resume for halted Codex threads.

Auto-resume is disabled by default (`auto_resume.enabled = false`, `threads = []`).
While disabled, commands only diagnose and report why threads halted. When enabled
globally or for specific thread IDs, only turns classified as `(a) 5-hour usage window
exhausted` are scheduled and retried once per failed turn after `reset_at + buffer`.
Threads blocked on approvals `(b)` or other failures `(c)` are never auto-retried.
"""

from __future__ import annotations

import datetime
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import appserver, codex, config, profiles, quota, remote_cmd, state, ui

QUOTA_5H = "quota_5h"
APPROVAL_BLOCKED = "approval"
OTHER = "other"

_USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "too many requests", "usage_limit_reached",
                        "usage_limit_exceeded", "429")
_QUOTA_ERROR_CODES = frozenset({
    "usage_limit_reached", "usage_limit_exceeded", "rate_limit_exceeded",
    "usageLimitExceeded", "rateLimitExceeded",
})
_MODEL_NOT_FOUND_CODES = ("model_not_found", "modelNotFound")
_TICK_INTERVAL = 30.0
_last_tick_at = 0.0
_tick_offset = 0


@dataclass
class Diagnosis:
    thread_id: str
    turn_id: str | None
    halted: bool
    classification: str  # quota_5h | approval | other
    reason: str
    evidence: list[str] = field(default_factory=list)
    turn_status: str | None = None
    cwd: str | None = None
    updated_at: int | None = None


@dataclass
class WindowResolution:
    ready_now: bool
    reset_at: int | None
    account: str | None
    all_capable_limited: bool
    accounts: list[dict[str, Any]] = field(default_factory=list)


def is_enabled(cfg: dict[str, Any], thread_id: str | None = None) -> bool:
    ar = cfg.get("auto_resume") or {}
    if ar.get("enabled"):
        return True
    if thread_id and thread_id in (ar.get("threads") or []):
        return True
    return False


def _is_subagent(row: dict[str, Any]) -> bool:
    if row.get("thread_source") == "subagent":
        return True
    source = str(row.get("source") or "").lstrip()
    return source.startswith("{") and "subagent" in source


def _read_rollout_tail(path: Path, max_bytes: int = 262144) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(size - max_bytes)
                stream.readline()  # discard partial first line
            raw_lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _capable_profiles(cfg: dict[str, Any]) -> list[profiles.Profile]:
    disabled = set(cfg["rotation"]["disabled"])
    all_p = [p for p in profiles.all(cfg) if p.logged_in and p.name not in disabled]
    paid = [p for p in all_p if (profiles.identity(p.home).get("plan") or "").lower() != "free"]
    return paid or all_p


def _capable_pool_all_limited(cfg: dict[str, Any], quotas: dict[str, quota.Quota] | None = None) -> bool:
    capable = _capable_profiles(cfg)
    if not capable:
        return False
    blocked = state.blocks(max_age=2.0)
    for p in capable:
        if quotas and p.name in quotas:
            if quotas[p.name].eligible:
                return False
            continue
        if p.name in blocked:
            continue
        snap = quota.cached(p.name, max_age=1800)
        if snap is None or snap.eligible:
            return False
    return True


def _current_turn_span(events: list[dict[str, Any]], turn_id: str | None = None) -> list[dict[str, Any]]:
    """Return events belonging to `turn_id` (or the latest turn from the last task/turn start onward)."""
    if turn_id is not None:
        # Locate the turn matching `turn_id` if present; stop at the next turn start if newer turns follow.
        matched_start: int | None = None
        matched_end: int | None = None
        saw_other_turn = False
        for idx, ev in enumerate(events):
            p = ev.get("payload") or {}
            if ev.get("type") == "event_msg" and p.get("type") in ("task_started", "turn_started", "task_complete", "turn_aborted"):
                ev_tid = p.get("turn_id")
                if ev_tid == turn_id:
                    if p.get("type") in ("task_started", "turn_started"):
                        matched_start = idx
                        matched_end = None
                elif ev_tid:
                    saw_other_turn = True
                    if matched_start is not None and matched_end is None and p.get("type") in ("task_started", "turn_started"):
                        matched_end = idx
        if matched_start is not None:
            return events[matched_start:matched_end]
        if saw_other_turn:
            return []
    last_start: int | None = None
    for idx, ev in enumerate(events):
        p = ev.get("payload") or {}
        if ev.get("type") == "event_msg" and p.get("type") in ("task_started", "turn_started"):
            last_start = idx
    if last_start is not None:
        return events[last_start:]
    return events


def _extract_tool_call_approval(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Detect an unanswered escalated tool call in the latest rollout turn."""
    span = _current_turn_span(events)
    start_idx = 0
    for idx, ev in enumerate(span):
        p = ev.get("payload") or {}
        if ev.get("type") == "event_msg" and p.get("type") in ("task_complete", "turn_aborted"):
            start_idx = idx
    tail = span[start_idx:]
    answered: set[str] = set()
    pending: list[tuple[str, str]] = []
    for ev in tail:
        p = ev.get("payload") or {}
        if ev.get("type") != "response_item" or not isinstance(p, dict):
            continue
        ptype = p.get("type")
        cid = p.get("call_id")
        if ptype in ("custom_tool_call_output", "function_call_output") and cid:
            answered.add(str(cid))
        elif ptype in ("custom_tool_call", "function_call") and cid:
            text = str(p.get("input") or p.get("arguments") or "")
            if "require_escalated" in text or "approval" in text:
                pending.append((str(cid), text.strip()[:220]))
    for cid, text in reversed(pending):
        if cid not in answered:
            return cid, text
    return None


def _event_ts(ev: dict[str, Any]) -> float | None:
    raw = ev.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _has_429_evidence(events: list[dict[str, Any]], log_rows: list[dict[str, Any]],
                      turn_id: str | None = None) -> bool:
    span = _current_turn_span(events, turn_id=turn_id)
    start_ts = _event_ts(span[0]) if span else None
    end_ts: float | None = None
    if span:
        last_p = span[-1].get("payload") or {}
        if span[-1].get("type") == "event_msg" and last_p.get("type") in ("task_complete", "turn_aborted"):
            end_ts = _event_ts(span[-1])
    for row in log_rows:
        if start_ts is not None and row.get("ts") is not None:
            row_ts = float(row["ts"]) + float(row.get("ts_nanos") or 0) / 1_000_000_000.0
            if row_ts < start_ts or (end_ts is not None and row_ts > end_ts):
                continue
        elif turn_id is not None:
            continue
        body = str(row.get("body") or "").lower()
        if "429" in body or "usage limit" in body:
            return True
    for ev in span[-25:]:
        p = ev.get("payload") or {}
        if ev.get("type") == "response_item" and p.get("type") == "agent_message":
            content = p.get("content") or []
            for item in content:
                if isinstance(item, dict) and any(m in str(item.get("text") or "").lower() for m in _USAGE_LIMIT_MARKERS):
                    return True
    return False


def classify(thread: dict[str, Any], rollout_events: list[dict[str, Any]], log_rows: list[dict[str, Any]],
             live_thread: dict[str, Any] | None = None, *, pool_all_limited: bool = False) -> Diagnosis:
    tid = thread["id"]
    cwd = thread.get("cwd")
    updated_at = thread.get("updated_at")
    evidence: list[str] = []

    live_status = (live_thread or {}).get("status") or {}
    live_turns = (live_thread or {}).get("turns") or []
    last_live_turn = live_turns[-1] if live_turns else None

    # 1. Check (b) approval block first: active thread waitingOnApproval or unanswered escalation call.
    if live_status.get("type") == "active" and "waitingOnApproval" in (live_status.get("activeFlags") or []):
        turn_id = (last_live_turn or {}).get("id")
        evidence.append(f"app-server thread/read: status={json.dumps(live_status)}")
        pending_call = _extract_tool_call_approval(rollout_events)
        if pending_call:
            evidence.append(f"rollout pending tool call {pending_call[0]}: {pending_call[1]}")
        return Diagnosis(tid, turn_id, True, APPROVAL_BLOCKED,
                         "Turn paused waiting on user approval/permission prompt",
                         evidence, turn_status=(last_live_turn or {}).get("status") or "inProgress",
                         cwd=cwd, updated_at=updated_at)

    # 2. Extract final turn outcome from live thread or current rollout turn span.
    span = _current_turn_span(rollout_events)
    last_task_started: dict[str, Any] | None = None
    last_task_complete: dict[str, Any] | None = None
    last_turn_aborted: dict[str, Any] | None = None
    for ev in reversed(span):
        p = ev.get("payload") or {}
        if ev.get("type") == "event_msg":
            ptype = p.get("type")
            if ptype == "task_started" and last_task_started is None:
                last_task_started = ev
            elif ptype == "task_complete" and last_task_complete is None:
                last_task_complete = ev
            elif ptype == "turn_aborted" and last_turn_aborted is None:
                last_turn_aborted = ev

    turn_id = ((last_live_turn or {}).get("id")
               or ((last_turn_aborted or {}).get("payload") or {}).get("turn_id")
               or ((last_task_complete or {}).get("payload") or {}).get("turn_id")
               or ((last_task_started or {}).get("payload") or {}).get("turn_id"))
    turn_status = (last_live_turn or {}).get("status")
    # M2: Only fall back to rollout error when live_thread is unavailable or the live turn itself has an error.
    # This prevents stale rollout errors from overriding a healthy live turn.
    if last_live_turn is not None:
        err_obj = last_live_turn.get("error")
    elif last_turn_aborted is not None:
        err_obj = None
    else:
        err_obj = ((last_task_complete or {}).get("payload") or {}).get("error")

    if not err_obj:
        if turn_status == "interrupted" or (last_live_turn is None and last_turn_aborted is not None):
            reason = ((last_turn_aborted or {}).get("payload") or {}).get("reason") or "interrupted"
            evidence.append(f"turn {turn_id} ended with status={reason}")
            return Diagnosis(tid, turn_id, True, OTHER, "Turn was interrupted", evidence,
                             turn_status=turn_status or "interrupted", cwd=cwd, updated_at=updated_at)
        if live_thread is None:
            pending_call = _extract_tool_call_approval(rollout_events)
            if pending_call and last_task_complete is None:
                evidence.append(f"rollout unanswered escalated call {pending_call[0]}: {pending_call[1]}")
                return Diagnosis(tid, turn_id, True, APPROVAL_BLOCKED,
                                 "Turn halted on unanswered approval/permission request",
                                 evidence, turn_status="inProgress", cwd=cwd, updated_at=updated_at)
        return Diagnosis(tid, turn_id, False, OTHER, "Latest turn completed without error", evidence,
                         turn_status=turn_status or "completed", cwd=cwd, updated_at=updated_at)

    err_msg = str(err_obj.get("message") or "")
    # M2: codexErrorInfo / codex_error_info may be a dict (e.g. {"httpConnectionFailed": {...}});
    # extract the variant name as the code string in that case.
    raw_code = err_obj.get("codexErrorInfo") or err_obj.get("codex_error_info") or ""
    if isinstance(raw_code, dict):
        err_code = next(iter(raw_code), "")
    else:
        err_code = str(raw_code)
    evidence.append(f"turn {turn_id} error ({err_code or 'error'}): {err_msg}")
    for row in log_rows:
        body = str(row.get("body") or "")
        if "Turn error:" in body or "status=429" in body or "status=400" in body or row.get("level") == "ERROR":
            evidence.append(f"logs_2.sqlite [{row.get('id')}] ts={row.get('ts')}: {body[:240]}")
            if len(evidence) >= 4:
                break

    lowered = err_msg.lower()
    if any(m in lowered for m in _USAGE_LIMIT_MARKERS) or err_code in _QUOTA_ERROR_CODES:
        return Diagnosis(tid, turn_id, True, QUOTA_5H, "5-hour usage limit reached", evidence,
                         turn_status=turn_status or "failed", cwd=cwd, updated_at=updated_at)

    is_model_fallback = err_code in _MODEL_NOT_FOUND_CODES or "not supported when using codex with a chatgpt account" in lowered
    if is_model_fallback and (pool_all_limited or _has_429_evidence(rollout_events, log_rows, turn_id=turn_id)):
        evidence.append("all model-capable Plus accounts in rotation were 5h-limited, causing Free-profile model fallback")
        return Diagnosis(tid, turn_id, True, QUOTA_5H,
                         "5-hour usage window exhausted across Plus pool (cascaded to Free-account model_not_found)",
                         evidence, turn_status=turn_status or "failed", cwd=cwd, updated_at=updated_at)

    return Diagnosis(tid, turn_id, True, OTHER, f"Turn failed: {err_msg or err_code or 'unknown error'}",
                     evidence, turn_status=turn_status or "failed", cwd=cwd, updated_at=updated_at)


def _live_thread_map(thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        sock, _ = remote_cmd.server_socket()
    except SystemExit:
        return result
    try:
        with appserver.AppServer(sock, timeout=15) as server:
            for tid in thread_ids:
                try:
                    result[tid] = appserver.read_thread_with_turns(server, tid, sort_direction="desc",
                                                                   limit=1, items_view="summary")
                except appserver.AppServerError:
                    pass
    except (appserver.AppServerError, OSError):
        pass
    return result


def diagnose(cfg: dict[str, Any] | None = None, *, all_threads: bool = False,
             limit: int | None = None,
             offset: int = 0,
             thread_ids: list[str] | None = None,
             quotas: dict[str, quota.Quota] | None = None,
             live_threads: dict[str, dict[str, Any]] | None = None) -> list[Diagnosis]:
    cfg = cfg or config.load()
    rows = [r for r in codex.threads(limit=None, light=True) if not r.get("archived") and not _is_subagent(r)]
    if thread_ids is not None:
        wanted = set(thread_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not rows:
        return []
    if all_threads or thread_ids is not None:
        if limit is None:
            targets = rows[offset:]
        elif limit <= 0:
            targets = []
        else:
            start = offset % len(rows)
            targets = [rows[(start + i) % len(rows)] for i in range(min(limit, len(rows)))]
    else:
        targets = rows[:1]
    if not targets:
        return []
    pool_limited = _capable_pool_all_limited(cfg, quotas)
    live_map = live_threads if live_threads is not None else _live_thread_map([r["id"] for r in targets])

    results: list[Diagnosis] = []
    for row in targets:
        rpath = row.get("rollout_path")
        events = _read_rollout_tail(Path(rpath)) if rpath else []
        logs = codex.recent_thread_logs(row["id"], limit=30)
        diag = classify(row, events, logs, live_map.get(row["id"]), pool_all_limited=pool_limited)
        if all_threads and not diag.halted:
            continue
        results.append(diag)
    return results


def resolve_window(cfg: dict[str, Any] | None = None, quotas: dict[str, quota.Quota] | None = None) -> WindowResolution:
    cfg = cfg or config.load()
    capable = _capable_profiles(cfg)
    fetched = quotas if quotas is not None else quota.fetch_all(capable)
    accounts_info: list[dict[str, Any]] = []
    ready_candidates: list[tuple[float, str]] = []
    reset_candidates: list[tuple[int, str]] = []

    for p in capable:
        q = fetched.get(p.name)
        if not q:
            continue
        w5 = next((w for w in q.windows if w.seconds == 18000), None)
        rem_5h = w5.remaining if w5 else q.remaining
        reset_5h = w5.reset_at if w5 else q.usable_again_at
        accounts_info.append({
            "profile": p.name,
            "plan": q.plan or profiles.identity(p.home).get("plan"),
            "status": q.status,
            "remaining_5h": rem_5h,
            "reset_at": reset_5h,
        })
        if q.eligible and (rem_5h is None or rem_5h > 0):
            ready_candidates.append((rem_5h if rem_5h is not None else 100.0, p.name))
        elif reset_5h:
            reset_candidates.append((int(reset_5h), p.name))

    if ready_candidates:
        ready_candidates.sort(key=lambda item: (-item[0], item[1]))
        return WindowResolution(True, int(time.time()), ready_candidates[0][1], False, accounts_info)
    reset_candidates.sort(key=lambda item: (item[0], item[1]))
    earliest_ts, earliest_acct = reset_candidates[0] if reset_candidates else (None, None)
    return WindowResolution(False, earliest_ts, earliest_acct, bool(accounts_info), accounts_info)


def reconcile_schedule(cfg: dict[str, Any], diagnoses: list[Diagnosis], resolution: WindowResolution | None = None,
                       now: float | None = None) -> dict[str, dict[str, Any]]:
    """Sync pending auto-resume entries for enabled quota-halted threads idempotently."""
    now_ts = int(time.time() if now is None else now)
    ar_cfg = cfg["auto_resume"]
    buffer_s = int(ar_cfg.get("buffer_seconds", 60))
    entries = state.auto_resume_entries()

    # Cancel any pending entries whose thread is no longer opted in.
    to_cancel = [tid for tid, e in entries.items() if e.get("status") == "pending" and not is_enabled(cfg, tid)]
    if to_cancel:
        state.cancel_auto_resume(to_cancel)
        entries = state.auto_resume_entries()

    stagger = 0
    for diag in diagnoses:
        tid = diag.thread_id
        existing = entries.get(tid)
        if not diag.halted:
            if existing and existing.get("status") == "pending":
                state.save_auto_resume(tid, {**existing, "status": "superseded", "updated_at": now_ts})
            continue
        if not is_enabled(cfg, tid):
            continue
        if diag.classification != QUOTA_5H or not diag.turn_id:
            if existing and existing.get("status") == "pending":
                state.save_auto_resume(tid, {**existing, "status": "cancelled", "updated_at": now_ts})
            continue
        if existing and existing.get("turn_id") == diag.turn_id and existing.get("status") in ("pending", "resumed", "failed"):
            continue
        if resolution is None or (not resolution.ready_now and resolution.reset_at is None):
            continue
        fire_at = now_ts if resolution.ready_now else int(resolution.reset_at) + buffer_s + stagger
        stagger += 3
        state.save_auto_resume(tid, {
            "turn_id": diag.turn_id,
            "classification": diag.classification,
            "scheduled_for": fire_at,
            "status": "pending",
            "attempts": 0,
            "resumed_turn_id": None,
            "updated_at": now_ts,
        })
    return state.auto_resume_entries()


def execute_due(cfg: dict[str, Any] | None = None, *, now: float | None = None,
                diagnose_all: bool = False, quotas: dict[str, quota.Quota] | None = None,
                live_threads: dict[str, dict[str, Any]] | None = None,
                limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    """Diagnose enabled threads, reconcile schedules, and feed due retries once quota is open."""
    cfg = cfg or config.load()
    now_ts = int(time.time() if now is None else now)
    entries = state.auto_resume_entries()
    has_enabled = bool(cfg["auto_resume"]["enabled"] or cfg["auto_resume"]["threads"])

    if not has_enabled:
        if any(e.get("status") == "pending" for e in entries.values()):
            state.cancel_auto_resume()
        return []

    target_ids = None if (diagnose_all or cfg["auto_resume"]["enabled"]) else list(cfg["auto_resume"]["threads"])
    diags = diagnose(cfg, all_threads=diagnose_all or bool(cfg["auto_resume"]["threads"]),
                     limit=limit, offset=offset, thread_ids=target_ids, quotas=quotas, live_threads=live_threads)
    resolution = resolve_window(cfg, quotas=quotas) if any(
        d.halted and d.classification == QUOTA_5H and is_enabled(cfg, d.thread_id) for d in diags
    ) else None
    entries = reconcile_schedule(cfg, diags, resolution, now=now_ts)

    due = sorted((tid, e) for tid, e in entries.items()
                 if e.get("status") == "pending" and (e.get("scheduled_for") or 0) <= now_ts and is_enabled(cfg, tid))
    if not due:
        return []

    outcomes: list[dict[str, Any]] = []
    max_retries = int(cfg["auto_resume"].get("max_retries_per_turn", 1))
    backoff_s = int(cfg["auto_resume"].get("backoff_seconds", 30))
    message = str(cfg["auto_resume"].get("message") or "Try again")

    # #5: Fail entries that have already exhausted their retry budget before attempting anything.
    still_due: list[tuple[str, dict[str, Any]]] = []
    for tid, e in due:
        if int(e.get("attempts") or 0) >= max_retries:
            state.save_auto_resume(tid, {**e, "status": "failed", "updated_at": now_ts})
            outcomes.append({"thread_id": tid, "action": "failed", "reason": "max_retries_per_turn exceeded"})
        else:
            still_due.append((tid, e))
    if not still_due:
        return outcomes

    # Re-verify live quota at wake-up before dispatching any feed.
    live_res = resolution if (resolution is not None and quotas is not None) else resolve_window(cfg)

    if not live_res.ready_now:
        # Quota API still lagging: reschedule without consuming a turn retry attempt.
        next_try = now_ts + backoff_s
        for tid, e in still_due:
            updated = {**e, "scheduled_for": next_try, "updated_at": now_ts}
            state.save_auto_resume(tid, updated)
            outcomes.append({"thread_id": tid, "action": "backoff", "scheduled_for": next_try})
        return outcomes

    # Claim due jobs atomically under state lock before dispatching so concurrent callers
    # cannot both attempt the same turn.
    claimed: list[tuple[str, dict[str, Any]]] = []
    with state.editing() as data:
        ar_map = data.setdefault("auto_resume", {})
        for tid, _ in still_due:
            cur = ar_map.get(tid)
            if not cur or cur.get("status") != "pending" or (cur.get("scheduled_for") or 0) > now_ts:
                continue
            attempts = int(cur.get("attempts") or 0) + 1
            cur["attempts"] = attempts
            cur["scheduled_for"] = now_ts + backoff_s
            cur["updated_at"] = now_ts
            claimed.append((tid, dict(cur)))
    if not claimed:
        return outcomes

    # Clear any stale proxy quota block on the confirmed ready account so the retry is accepted immediately.
    if live_res.account:
        state.clear_quota_pause(live_res.account)

    handled: set[str] = set()
    try:
        sock, _ = remote_cmd.server_socket()
        with appserver.AppServer(sock, timeout=60) as server:
            for tid, e in claimed:
                handled.add(tid)
                latest_cfg = config.load()
                if not is_enabled(latest_cfg, tid):
                    state.save_auto_resume(tid, {**e, "status": "cancelled", "updated_at": now_ts})
                    outcomes.append({"thread_id": tid, "action": "cancelled"})
                    continue
                try:
                    thread = appserver.read_thread_with_turns(server, tid, sort_direction="desc",
                                                              limit=1, items_view="summary")
                except appserver.AppServerError as exc:
                    outcomes.append({"thread_id": tid, "action": "error", "error": str(exc)})
                    continue
                turns = thread.get("turns") or []
                last_turn = turns[-1] if turns else {}
                thread_status = (thread.get("status") or {}).get("type")
                if thread_status == "active" or last_turn.get("id") != e.get("turn_id") or last_turn.get("status") != "failed":
                    state.save_auto_resume(tid, {**e, "status": "superseded", "updated_at": now_ts})
                    outcomes.append({"thread_id": tid, "action": "superseded"})
                    continue
                try:
                    new_turn_id = remote_cmd.dispatch_turn(server, tid, message)
                except appserver.AppServerError as exc:
                    outcomes.append({"thread_id": tid, "action": "error", "error": str(exc)})
                    continue
                state.save_auto_resume(tid, {
                    **e,
                    "status": "resumed",
                    "resumed_turn_id": new_turn_id,
                    "updated_at": now_ts,
                })
                outcomes.append({"thread_id": tid, "action": "resumed", "turn_id": new_turn_id})
    except (SystemExit, OSError, appserver.AppServerError) as exc:
        # The server was unreachable or dropped: only threads not yet handled get an error, and a
        # connection failure is not the turn's fault, so it does not spend their retry attempt.
        unhandled = [tid for tid, _ in claimed if tid not in handled]
        with state.editing() as data:
            for tid in unhandled:
                cur = data.get("auto_resume", {}).get(tid)
                if cur and cur.get("status") == "pending":
                    cur["attempts"] = max(0, int(cur.get("attempts") or 0) - 1)
        outcomes.extend({"thread_id": tid, "action": "error", "error": str(exc)} for tid in unhandled)
    return outcomes


def tick() -> None:
    """Periodic hook run by `mycodex-remote`; skips all network work while disabled."""
    global _last_tick_at, _tick_offset
    now = time.time()
    cfg = config.load()
    entries = state.auto_resume_entries(max_age=2.0)
    enabled = bool(cfg["auto_resume"]["enabled"] or cfg["auto_resume"]["threads"])
    if not enabled:
        if any(e.get("status") == "pending" for e in entries.values()):
            state.cancel_auto_resume()
        return
    has_due = any(e.get("status") == "pending" and (e.get("scheduled_for") or 0) <= now for e in entries.values())
    if not has_due and now - _last_tick_at < _TICK_INTERVAL:
        return
    _last_tick_at = now
    offset = _tick_offset if cfg["auto_resume"]["enabled"] else 0
    if cfg["auto_resume"]["enabled"]:
        _tick_offset += 25
    for item in execute_due(cfg, now=now, diagnose_all=bool(cfg["auto_resume"]["enabled"]),
                            limit=25 if cfg["auto_resume"]["enabled"] else None, offset=offset):
        print(f"mycodex-remote: auto-resume thread={item['thread_id']} action={item['action']}"
              + (f" turn={item['turn_id']}" if item.get("turn_id") else ""), flush=True)


def _format_local(ts: int | None) -> str:
    if not ts:
        return "-"
    dt = datetime.datetime.fromtimestamp(ts).astimezone()
    rem = int(ts - time.time())
    if rem <= 0:
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S %Z')} (now)"
    h, m, s = rem // 3600, (rem % 3600) // 60, rem % 60
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {h}h {m}m {s}s)"


def _payload(cfg: dict[str, Any], diags: list[Diagnosis], res: WindowResolution | None,
             entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "auto_resume": cfg["auto_resume"],
        "local_timezone": datetime.datetime.now().astimezone().tzname() or "local",
        "window": asdict(res) if res else None,
        "threads": [{**asdict(d), "job": entries.get(d.thread_id)} for d in diags],
    }


def status_cmd(as_json: bool = False, all_threads: bool = False) -> int:
    cfg = config.load()
    diags = diagnose(cfg, all_threads=all_threads)
    res = resolve_window(cfg) if any(d.halted and d.classification == QUOTA_5H for d in diags) else None
    entries = state.auto_resume_entries()
    if as_json:
        print(json.dumps(_payload(cfg, diags, res, entries), indent=2))
        return 0
    ar = cfg["auto_resume"]
    mode = "enabled (global)" if ar["enabled"] else (
        f"enabled for {len(ar['threads'])} thread(s)" if ar["threads"] else "disabled (default)")
    fields: list[tuple[str, str]] = [("Auto-resume", mode)]
    if not ar["enabled"] and not ar["threads"]:
        fields.append(("How to enable", "mycodex auto-resume enable [THREAD_ID ...]  # or auto_resume.enabled=true in config.json"))
    if res:
        fields.append(("5h reset", "ready now (" + (res.account or "-") + ")" if res.ready_now
                       else f"{_format_local(res.reset_at)} ({res.account or '-'})"))
    ui.panel("Mycodex Auto-Resume", fields)
    rows: list[list[str]] = []
    for d in diags:
        job = entries.get(d.thread_id)
        if not is_enabled(cfg, d.thread_id):
            job_str = "not scheduled (disabled)"
        elif d.classification != QUOTA_5H or not d.halted:
            job_str = f"never ({d.classification})"
        elif job and job.get("turn_id") == d.turn_id:
            extra = f" -> {job['resumed_turn_id']}" if job.get("resumed_turn_id") else f" at {_format_local(job.get('scheduled_for'))}"
            job_str = f"{job.get('status')}{extra}"
        elif res:
            fire_ts = int(time.time()) if res.ready_now else (
                (res.reset_at + int(ar.get("buffer_seconds", 60))) if res.reset_at else None)
            job_str = f"awaiting service tick ({_format_local(fire_ts)})"
        else:
            job_str = "awaiting service tick"
        rows.append([d.thread_id, d.classification if d.halted else "ok", d.reason, job_str])
    if rows:
        ui.table("Diagnosed Threads", [ui.Column("THREAD", min_width=36), ui.Column("CLASS"),
                                       ui.Column("REASON", max_width=44), ui.Column("JOB")], rows)
        for d in diags:
            if d.evidence:
                ui.out(f"\nEvidence for {d.thread_id}:")
                for line in d.evidence:
                    ui.out(f"  - {line}")
    return 0


def set_enabled(enable: bool, thread_ids: list[str]) -> int:
    with config.editing() as cfg:
        ar = cfg["auto_resume"]
        if not thread_ids:
            ar["enabled"] = enable
        else:
            current = list(ar.get("threads") or [])
            if enable:
                for tid in thread_ids:
                    if tid not in current:
                        current.append(tid)
            else:
                current = [tid for tid in current if tid not in set(thread_ids)]
            ar["threads"] = current
    if not enable:
        cancelled = state.cancel_auto_resume(thread_ids or None)
        if cancelled:
            ui.info(f"cancelled pending job(s): {', '.join(cancelled)}")
    target = "all quota-blocked threads" if not thread_ids else ", ".join(thread_ids)
    ui.success(f"auto-resume {'enabled' if enable else 'disabled'} for {target}")
    return 0


def run_cmd(as_json: bool = False, all_threads: bool = False) -> int:
    cfg = config.load()
    if not cfg["auto_resume"]["enabled"] and not cfg["auto_resume"]["threads"]:
        if as_json:
            print(json.dumps({"skipped": True, "reason": "auto_resume is disabled", "outcomes": []}, indent=2))
        else:
            ui.info("auto-resume is disabled; nothing scheduled or fed (`mycodex auto-resume enable`)")
        return 0
    outcomes = execute_due(cfg, diagnose_all=all_threads)
    if as_json:
        print(json.dumps({"outcomes": outcomes}, indent=2))
    elif not outcomes:
        ui.info("no due auto-resume jobs ready to run")
    else:
        for item in outcomes:
            ui.info(f"{item['thread_id']}: {item['action']}" + (f" ({item.get('turn_id')})" if item.get("turn_id") else ""))
    return 0
