"""ChatGPT plan quota per account, read from the endpoint Codex's own /status uses.

GET https://chatgpt.com/backend-api/wham/usage with the account's bearer and
ChatGPT-Account-Id (headers as prodex sends them; see NOTICE). A 401 triggers one token
refresh. Every result is cached in the shared runtime state so the rotation proxy can
rank accounts without a network round trip.
"""

from __future__ import annotations

import concurrent.futures as futures
import functools
import json
import os
import platform
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__, auth, state, validation as check
from .profiles import Profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WINDOW_NAMES = {18000: "5h", 604800: "weekly", 2592000: "30d"}


@dataclass
class Window:
    name: str
    seconds: int
    used_percent: float
    reset_at: int | None

    @property
    def remaining(self) -> float:
        return max(0.0, 100.0 - float(self.used_percent))


@dataclass
class Quota:
    profile: str
    ok: bool
    status: str                       # ready | limited | auth invalid | unavailable | unknown
    plan: str | None = None
    email: str | None = None
    account_id: str | None = None
    windows: list[Window] = field(default_factory=list)
    reset_credits: int | None = None
    error: str | None = None
    fetched_at: float = field(default_factory=time.time)
    usage_allowed: bool | None = None  # explicit permission, retained in the cache

    @property
    def eligible(self) -> bool:
        return self.ok and self.status == "ready"

    @property
    def remaining(self) -> float | None:
        """Headroom of the tightest window, in percent."""
        return min((w.remaining for w in self.windows), default=None) if self.ok else None

    @property
    def usable_again_at(self) -> int | None:
        """When every exhausted window has reset (limited accounts only)."""
        resets = [w.reset_at for w in self.windows if w.remaining <= 0 and w.reset_at]
        return max(resets) if resets else None

    def summary(self) -> str:
        if not self.ok:
            return self.status
        return " | ".join(f"{w.name} {w.remaining:.0f}%" for w in self.windows) or "no windows"


def _window(raw: Any, field: str, duration: str, used: str, reset: str,
            multiplier: int = 1) -> Window:
    raw = check.object_value(raw, field)
    seconds = check.number(raw.get(duration), field + "." + duration, minimum=1, integer=True) * multiplier
    percent = check.number(raw.get(used), field + "." + used)
    reset_at = check.timestamp(raw.get(reset), field + "." + reset)
    return Window(WINDOW_NAMES.get(seconds, f"{seconds // 3600}h"), seconds, percent, reset_at)


def _facts(profile: str, payload: dict[str, Any], windows: list[Window], permission: bool | None,
           limited: bool, plan: str, account: str, credits: int | None) -> Quota:
    known = limited or permission is True
    return Quota(profile, known, "limited" if limited else ("ready" if permission is True else "unknown"),
                 plan=check.text_value(payload.get(plan), plan, optional=True),
                 account_id=check.text_value(payload.get(account), account, optional=True),
                 windows=sorted(windows, key=lambda w: w.seconds), reset_credits=credits,
                 error=None if known else "Usage permission was not provided. Try checking again later.",
                 usage_allowed=permission)


def parse(profile: str, payload: dict[str, Any]) -> Quota:
    payload = check.object_value(payload, "usage response")
    rate = check.object_value(payload.get("rate_limit"), "rate_limit", optional=True)
    permission = check.boolean(rate.get("allowed"), "rate_limit.allowed", optional=True)
    reached = check.boolean(rate.get("limit_reached"), "rate_limit.limit_reached", optional=True)
    windows = [_window(rate[key], key, "limit_window_seconds", "used_percent", "reset_at")
               for key in ("primary_window", "secondary_window") if rate.get(key) is not None]
    reset = check.object_value(payload.get("rate_limit_reset_credits"), "reset credits", optional=True)
    credits = check.number(reset.get("available_count"), "available_count", integer=True, optional=True)
    limited = permission is False or reached is True or any(w.remaining <= 0 for w in windows)
    q = _facts(profile, payload, windows, permission, limited, "plan_type", "account_id", credits)
    q.email = check.text_value(payload.get("email"), "email", optional=True)
    return q


def to_dict(q: Quota) -> dict[str, Any]:
    return asdict(q)


def from_dict(data: dict[str, Any]) -> Quota:
    data = check.object_value(data, "saved usage")
    rows = data.get("windows", [])
    if not isinstance(rows, list):
        raise check.InvalidData("saved usage windows must be a list")
    windows = []
    for raw in rows:
        window = _window(raw, "saved window", "seconds", "used_percent", "reset_at")
        window.name = check.text_value(raw.get("name"), "window name")
        windows.append(window)
    fields = {k: v for k, v in data.items() if k in Quota.__dataclass_fields__ and k != "windows"}
    check.text_value(fields.get("profile"), "saved usage account")
    check.boolean(fields.get("ok"), "saved usage permission")
    if fields.get("status") not in ("ready", "limited", "auth invalid", "unavailable", "unknown"):
        raise check.InvalidData("saved usage status is not recognized")
    for name in ("plan", "email", "account_id", "error"):
        check.text_value(fields.get(name), "saved usage " + name, optional=True)
    check.number(fields.get("reset_credits"), "saved reset credits", integer=True, optional=True)
    if "fetched_at" in fields:
        check.number(fields["fetched_at"], "saved usage time", maximum=253402214400)
    permission = check.boolean(fields.get("usage_allowed"), "saved usage permission", optional=True)
    if fields["status"] == "ready" and permission is not True:
        fields.update(ok=False, status="unknown", error="Saved usage has no confirmed permission. Check usage again.")
    if fields["status"] == "ready" and any(w.remaining <= 0 for w in windows):
        fields.update(status="limited")
    return Quota(windows=windows, **fields)


def from_rpc(profile: str, payload: dict[str, Any]) -> Quota:
    """Translate an account snapshot; missing permission never proves recovery."""
    payload = check.object_value(payload, "account usage response")
    by_id = check.object_value(payload.get("rateLimitsByLimitId"), "rateLimitsByLimitId", optional=True)
    rate = by_id.get("codex")
    if rate is None:
        rate = payload.get("rateLimits")
    rate = check.object_value(rate, "rateLimits", optional=True)
    permission = check.boolean(payload.get("ordinaryUsageAllowed"), "ordinaryUsageAllowed", optional=True)
    spend = check.boolean(rate.get("spendControlReached"), "spendControlReached", optional=True)
    reached = check.text_value(rate.get("rateLimitReachedType"), "rateLimitReachedType", optional=True)
    windows = [_window(rate[key], key, "windowDurationMins", "usedPercent", "resetsAt", multiplier=60)
               for key in ("primary", "secondary") if rate.get(key) is not None]
    reset = check.object_value(payload.get("rateLimitResetCredits"), "reset credits", optional=True)
    credits = check.number(reset.get("availableCount"), "availableCount", integer=True, optional=True)
    limited = permission is False or bool(reached) or spend is True or any(w.remaining <= 0 for w in windows)
    combined = {**payload, "planType": rate.get("planType")}
    return _facts(profile, combined, windows, permission, limited, "planType", "accountId", credits)


@functools.lru_cache(maxsize=1)
def _user_agent() -> str:
    from . import codex
    return f"codex_cli_rs/{codex.version() or '0.0.0'} (Linux; {platform.machine() or 'unknown'}) mycodex/{__version__}"


def _get(creds: auth.Credentials, timeout: float) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "originator": "codex_cli_rs",
        "User-Agent": _user_agent(),
        "x-openai-codex-luna-reserve": "1",
        "Accept": "application/json",
    }
    if creds.account_id:
        headers["ChatGPT-Account-Id"] = creds.account_id
    request = urllib.request.Request(os.environ.get("MYCODEX_USAGE_URL") or USAGE_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1 << 16)


def fetch(profile: Profile, timeout: float = 20) -> Quota:
    name = profile.name
    try:
        creds = auth.credentials(profile.home)
        status, body = _get(creds, timeout)
        if status == 401:
            creds = auth.refresh(profile.home, stale=creds.access_token)
            status, body = _get(creds, timeout)
    except auth.AuthError as exc:
        return _remember(Quota(name, False, "auth invalid" if exc.permanent else "unknown", error=str(exc)))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _remember(Quota(name, False, "unknown", error="Could not check usage. Check your connection and try again."))
    if status == 200:
        try:
            return _remember(parse(name, json.loads(body)))
        except (TypeError, ValueError, RecursionError) as exc:
            detail = str(exc) if isinstance(exc, check.InvalidData) else "the response was not valid JSON"
            return _remember(Quota(name, False, "unknown", error=f"Could not read usage: {detail}. Try again later."))
    if status == 401:
        return _remember(Quota(name, False, "auth invalid", error="the backend rejected a fresh token (HTTP 401)"))
    if status in (402, 403):
        return _remember(Quota(name, False, "unavailable", error=f"Usage is unavailable for this account (HTTP {status})."))
    return _remember(Quota(name, False, "unknown", error=f"Could not check usage (HTTP {status}). Try again later."))


def _remember(q: Quota) -> Quota:
    try:
        state.save_quota(q.profile, to_dict(q))
    except OSError:
        pass
    return q


def fetch_all(profile_list: list[Profile], timeout: float = 20) -> dict[str, Quota]:
    if not profile_list:
        return {}
    with futures.ThreadPoolExecutor(max_workers=min(6, len(profile_list))) as pool:
        return dict(zip([p.name for p in profile_list], pool.map(lambda p: fetch(p, timeout), profile_list)))


def cached(name: str, max_age: float = 900) -> Quota | None:
    snapshot = state.quota_snapshot(name, max_age)
    if not snapshot:
        return None
    try:
        return from_dict(snapshot)
    except (TypeError, ValueError):
        return None
