"""Copy a validated model catalogue into one relay profile only."""

from __future__ import annotations

import json
import os
import platform
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .profiles import Profile

CACHE_FILE = "models_cache.json"


class ModelCacheError(Exception):
    """The selected catalogue could not be safely copied."""


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _is_envelope(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    fetched_at = obj.get("fetched_at")
    if not isinstance(fetched_at, str) or "T" not in fetched_at:
        return False
    try:
        dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        return False
    etag = obj.get("etag")
    if etag is not None and not isinstance(etag, str):
        return False
    client_version = obj.get("client_version")
    if not isinstance(client_version, str) or not client_version:
        return False
    identity = obj.get("identity")
    if identity is not None and not isinstance(identity, str):
        return False
    return isinstance(obj.get("models"), list)


def _atomic_write(directory: Path, data: bytes) -> None:
    """Replace `<directory>/models_cache.json` atomically with mode 0600."""
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{CACHE_FILE}.", dir=directory)
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / CACHE_FILE)
        temporary = None
    except OSError as exc:
        raise ModelCacheError("could not replace the relay model cache") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_cache(source: Path) -> tuple[bytes, Any]:
    if source.is_symlink():
        raise ModelCacheError("the selected model cache must not be a symbolic link")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(source, flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("model cache is not a regular file")
            data = handle.read()
        parsed = json.loads(data.decode("utf-8"), parse_constant=_invalid_constant)
    except (OSError, RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise ModelCacheError("the selected profile has no readable valid model cache") from exc
    return data, parsed


def _read_existing_dict(path: Path) -> dict[str, Any] | None:
    try:
        _, parsed = _read_cache(path)
    except ModelCacheError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _destination(profile: Profile) -> tuple[Path, Path]:
    logical_home = profile.home
    logical_target = logical_home / CACHE_FILE
    try:
        home = logical_home.resolve(strict=True)
        parent = logical_target.parent.resolve(strict=True)
    except OSError as exc:
        raise ModelCacheError("the relay profile directory is unavailable") from exc
    if not home.is_dir() or parent != home:
        raise ModelCacheError("the relay model cache path is outside its profile directory")
    if logical_target.is_symlink():
        raise ModelCacheError("the relay model cache path must not be a symbolic link")
    target = home / CACHE_FILE
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ModelCacheError("the relay model cache path is unsafe")
    return home, target


def _request_models(creds: Any, ver: str, timeout: int) -> tuple[bytes, str | None]:
    import urllib.request

    url = f"https://chatgpt.com/backend-api/codex/models?client_version={ver}"
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "User-Agent": f"codex_cli_rs/{ver} (Linux; {platform.machine() or 'unknown'}) mycodex/{__version__}",
        "Accept": "application/json",
    }
    if creds.account_id:
        headers["ChatGPT-Account-Id"] = creds.account_id
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("ETag")


def _fetch_envelope(profile: Profile, timeout: int = 10) -> tuple[bytes, dict[str, Any]]:
    """Fetch the latest model catalogue from backend and save a ModelsCacheEntry envelope."""
    import urllib.error
    from . import auth, codex

    ver = codex.version() or "0.169.0"
    creds = auth.credentials(profile.home)
    try:
        raw, etag = _request_models(creds, ver, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise ModelCacheError(f"failed to fetch model catalogue (HTTP {exc.code})") from exc
        creds = auth.refresh(profile.home, stale=creds.access_token)
        try:
            raw, etag = _request_models(creds, ver, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as retry_exc:
            raise ModelCacheError("failed to fetch model catalogue after token refresh") from retry_exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelCacheError("failed to fetch model catalogue from backend") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise ModelCacheError("backend returned invalid model catalogue JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("models"), list):
        raise ModelCacheError("backend model catalogue response omitted models list")

    home, target = _destination(profile)
    existing = _read_existing_dict(target)
    existing_identity = existing.get("identity") if existing and isinstance(existing.get("identity"), str) else None
    envelope = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "etag": etag,
        "client_version": ver,
        "identity": existing_identity,
        "models": parsed["models"],
    }
    encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    _atomic_write(home, encoded)
    return encoded, envelope


def sync(source: Profile, target: Profile, refresh: bool = False) -> bool:
    """Atomically copy SOURCE's valid cache into TARGET; return false for a no-op."""
    if source.name == target.name and not refresh:
        return False
    try:
        if source.home.resolve(strict=True) == target.home.resolve(strict=True) and not refresh:
            return False
    except OSError:
        pass

    source_cache = source.home / CACHE_FILE
    if refresh or not source_cache.exists():
        try:
            snapshot, source_obj = _fetch_envelope(source)
        except Exception:
            if source_cache.exists():
                snapshot, source_obj = _read_cache(source_cache)
            else:
                raise ModelCacheError("the selected profile has no readable valid model cache")
    else:
        snapshot, source_obj = _read_cache(source_cache)

    directory, destination = _destination(target)
    target_obj = _read_existing_dict(destination)
    if _is_envelope(target_obj) and target_obj.get("identity") and not _is_envelope(source_obj):
        raise ModelCacheError("source model cache lacks a ModelsCacheEntry envelope")
    if _is_envelope(source_obj):
        target_id = target_obj.get("identity") if isinstance(target_obj, dict) else None
        if source_obj.get("identity") != target_id:
            updated = dict(source_obj)
            updated["identity"] = target_id
            snapshot = json.dumps(updated, separators=(",", ":")).encode("utf-8")

    _atomic_write(directory, snapshot)
    return True
