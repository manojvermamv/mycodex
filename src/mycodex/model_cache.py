"""Copy a validated model catalogue into one relay profile only."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .profiles import Profile

CACHE_FILE = "models_cache.json"


class ModelCacheError(Exception):
    """The selected catalogue could not be safely copied."""


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _snapshot(source: Path) -> bytes:
    try:
        data = source.read_bytes()
        json.loads(data.decode("utf-8"), parse_constant=_invalid_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelCacheError("the selected profile has no readable valid model cache") from exc
    return data


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


def sync(source: Profile, target: Profile) -> bool:
    """Atomically copy SOURCE's valid cache into TARGET; return false for a no-op."""
    if source.name == target.name:
        return False
    try:
        if source.home.resolve(strict=True) == target.home.resolve(strict=True):
            return False
    except OSError:
        pass

    snapshot = _snapshot(source.home / CACHE_FILE)
    directory, destination = _destination(target)
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{CACHE_FILE}.", dir=directory)
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise ModelCacheError("could not replace the relay model cache") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return True
