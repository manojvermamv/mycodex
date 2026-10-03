"""ChatGPT credentials in a profile's auth.json: read, inspect, refresh.

Codex writes auth.json in place (truncate + write, no lock), so reads retry on a torn
file. mycodex refreshes only when an access token is about to expire or the backend
rejected it, under a per-profile lock, after re-reading the file (another process may
have refreshed already). It writes atomically (temp file + rename, mode 0600) so Codex
never reads half a file, and it uses Codex's own endpoint, client id and JSON body.
Token values never leave this module except as request headers.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import paths

REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_MARGIN = 600            # refresh when an access token has less than this many seconds left
PERMANENT_CODES = {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated", "invalid_grant"}


class AuthError(RuntimeError):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True)
class Credentials:
    access_token: str = field(repr=False)
    account_id: str | None
    expires_at: int | None


def jwt_claims(token: str | None) -> dict[str, Any]:
    try:
        payload = (token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


def read(home: Path, attempts: int = 6) -> dict[str, Any] | None:
    path = home / "auth.json"
    for attempt in range(attempts):
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError):
            if attempt == attempts - 1:
                raise AuthError(f"{path} is not valid JSON")
            time.sleep(0.05)  # Codex may be rewriting it
    return None


def claims(home: Path) -> dict[str, Any]:
    """Non-secret facts about a profile's login (never returns tokens)."""
    try:
        data = read(home)
    except AuthError:
        return {"present": True, "corrupt": True}
    if data is None:
        return {"present": False}
    tokens = data.get("tokens") or {}
    id_claims = jwt_claims(tokens.get("id_token"))
    chatgpt = id_claims.get("https://api.openai.com/auth") or {}
    access = jwt_claims(tokens.get("access_token"))
    return {
        "present": True,
        "mode": data.get("auth_mode") or ("chatgpt" if tokens else ("apikey" if data.get("OPENAI_API_KEY") else None)),
        "email": id_claims.get("email"),
        "plan": chatgpt.get("chatgpt_plan_type"),
        "subscription_until": chatgpt.get("chatgpt_subscription_active_until"),
        "account_id": tokens.get("account_id") or chatgpt.get("chatgpt_account_id"),
        "user_id": chatgpt.get("chatgpt_user_id") or chatgpt.get("user_id"),
        "last_refresh": data.get("last_refresh"),
        "access_expires": access.get("exp"),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "api_key": bool(data.get("OPENAI_API_KEY")),
    }


def credentials(home: Path, margin: int = REFRESH_MARGIN) -> Credentials:
    """A usable bearer for the ChatGPT backend, refreshed first when it is about to expire."""
    data = read(home)
    if data is None:
        raise AuthError("not logged in (no auth.json)", permanent=True)
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        detail = "an API-key login cannot use ChatGPT plan quota" if data.get("OPENAI_API_KEY") else "no access token"
        raise AuthError(detail, permanent=True)
    expires = jwt_claims(access).get("exp")
    if isinstance(expires, (int, float)) and expires - time.time() < margin:
        return refresh(home, stale=access)
    return Credentials(access, tokens.get("account_id"), int(expires) if expires else None)


def _lock_path(home: Path) -> Path:
    digest = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:12]
    return paths.state_dir("locks") / f"auth-{home.name}-{digest}.lock"


@contextlib.contextmanager
def _locked(home: Path, timeout: float = 60) -> Iterator[None]:
    import fcntl

    with open(_lock_path(home), "a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise AuthError("timed out waiting for another token refresh")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh(home: Path, stale: str | None = None, timeout: float = 30) -> Credentials:
    """Exchange the refresh token for new tokens and persist them.

    `stale` is the access token that just failed or expired: if auth.json already holds a
    different, still valid token, that one is returned without a new exchange."""
    with _locked(home):
        data = read(home)
        if data is None:
            raise AuthError("not logged in (no auth.json)", permanent=True)
        tokens = dict(data.get("tokens") or {})
        current = tokens.get("access_token")
        expires = jwt_claims(current).get("exp")
        if stale is not None and current and current != stale and (not expires or expires - time.time() > 60):
            return Credentials(current, tokens.get("account_id"), int(expires) if expires else None)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise AuthError("no refresh token; log in again", permanent=True)
        payload = _exchange(refresh_token, timeout)
        for key in ("id_token", "access_token", "refresh_token"):
            if payload.get(key):
                tokens[key] = payload[key]
        updated = dict(data)
        updated["tokens"] = tokens
        updated["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        write(home, updated)
        expires = jwt_claims(tokens.get("access_token")).get("exp")
        return Credentials(tokens["access_token"], tokens.get("account_id"), int(expires) if expires else None)


def _exchange(refresh_token: str, timeout: float) -> dict[str, Any]:
    url = os.environ.get("CODEX_REFRESH_TOKEN_URL_OVERRIDE") or REFRESH_URL
    client_id = (os.environ.get("CODEX_APP_SERVER_LOGIN_CLIENT_ID") or "").strip() or CLIENT_ID
    body = json.dumps({"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token},
                      sort_keys=True).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read(1 << 16)
        code = _error_code(raw)
        permanent = exc.code == 401 or (code or "").lower() in PERMANENT_CODES
        raise AuthError(f"token refresh rejected: HTTP {exc.code}" + (f" ({code})" if code else ""),
                        permanent=permanent) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthError(f"token refresh failed: {exc}") from None
    except json.JSONDecodeError:
        raise AuthError("token refresh returned invalid JSON") from None


def _error_code(raw: bytes) -> str | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("code") or error.get("type")
    return error if isinstance(error, str) else data.get("code")


def write(home: Path, data: dict[str, Any]) -> None:
    path = home / "auth.json"
    tmp = home / f".auth.json.mycodex-{os.getpid()}-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
