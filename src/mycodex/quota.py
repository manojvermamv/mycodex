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

from . import __version__, auth, state
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


def parse(profile: str, payload: dict[str, Any]) -> Quota:
    rate = payload.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = rate.get(key)
        if not window:
            continue
        seconds = int(window.get("limit_window_seconds") or 0)
        windows.append(Window(
            name=WINDOW_NAMES.get(seconds, f"{seconds // 3600}h" if seconds else "window"),
            seconds=seconds,
            used_percent=float(window.get("used_percent") or 0),
            reset_at=window.get("reset_at"),
        ))
    windows.sort(key=lambda w: w.seconds)
    limited = bool(rate.get("limit_reached")) or rate.get("allowed") is False \
        or any(w.remaining <= 0 for w in windows)
    credits = (payload.get("rate_limit_reset_credits") or {}).get("available_count")
    return Quota(
        profile=profile, ok=True, status="limited" if limited else "ready",
        plan=payload.get("plan_type"), email=payload.get("email"),
        account_id=payload.get("account_id"), windows=windows, reset_credits=credits,
    )


def to_dict(q: Quota) -> dict[str, Any]:
    return asdict(q)


def from_dict(data: dict[str, Any]) -> Quota:
    windows = [Window(**w) for w in data.get("windows") or []]
    fields = {k: v for k, v in data.items() if k in Quota.__dataclass_fields__ and k != "windows"}
    return Quota(windows=windows, **fields)


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
        return Quota(name, False, "unknown", error=f"quota request failed: {getattr(exc, 'reason', exc)}")
    if status == 200:
        try:
            return _remember(parse(name, json.loads(body)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return Quota(name, False, "unknown", error=f"unparseable quota response: {exc}")
    if status == 401:
        return _remember(Quota(name, False, "auth invalid", error="the backend rejected a fresh token (HTTP 401)"))
    if status in (402, 403):
        return _remember(Quota(name, False, "unavailable", error=f"HTTP {status}: {body[:160].decode(errors='replace')}"))
    return Quota(name, False, "unknown", error=f"HTTP {status}")


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
    except TypeError:
        return None
