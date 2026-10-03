"""Start the official codex CLI as one profile, behind the rotation proxy when rotating.

Without rotation mycodex simply execs codex with the profile's CODEX_HOME. With rotation it
stays as a small parent process: it runs the proxy, starts codex as a child pointed at it
(`-c openai_base_url=...`), passes signals on and exits with codex's status.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from typing import Any, Callable

from . import paths, profiles, ui
from .profiles import Profile

NO_MODEL_COMMANDS = frozenset({"login", "logout", "completion", "mcp", "features", "sandbox", "debug", "apply", "a",
                               "help", "update", "cloud", "plugin", "plugins", "doctor"})
INFO_FLAGS = frozenset({"-V", "--version", "-h", "--help"})


def codex_env(profile: Profile, role: str) -> dict[str, str]:
    return paths.tool_env({
        "CODEX_HOME": str(profile.home),
        "CODEX_SQLITE_HOME": str(paths.SHARED_CODEX_HOME),
        "MYCODEX_PROFILE": profile.name,
        "MYCODEX_ROLE": role,
    })


def wants_proxy(codex_args: list[str]) -> bool:
    """Does this codex invocation talk to the model (and so benefit from rotation)?"""
    if not codex_args:
        return True
    first = codex_args[0]
    if first in NO_MODEL_COMMANDS or first in INFO_FLAGS:
        return False
    if first in ("remote-control", "app-server") and len(codex_args) > 1 and not codex_args[1].startswith("-"):
        return False  # remote-control start|stop|pair, app-server proxy|daemon|generate-*: no model client
    return True


def proxy_args(base_url: str) -> list[str]:
    return ["-c", f'openai_base_url="{base_url}"']


def run(profile: Profile, codex_args: list[str], rotate: bool, dry_run: bool = False) -> int:
    profiles.prepare_home(profile.home)
    env = codex_env(profile, "session")
    use_proxy = rotate and wants_proxy(codex_args)
    binary = paths.codex_bin()
    if dry_run:
        argv = [binary, *(proxy_args("http://127.0.0.1:<port>/backend-api/codex") if use_proxy else []), *codex_args]
        ui.panel("Mycodex Launch", [
            ("Profile", profile.name),
            ("Rotation", "on (model requests go through a local mycodex proxy)" if use_proxy else "off (this account only)"),
            ("CODEX_HOME", str(profile.home)),
            ("CODEX_SQLITE_HOME", str(paths.SHARED_CODEX_HOME)),
            ("Command", shlex.join(argv)),
        ])
        return 0
    if not use_proxy:
        os.execvpe(binary, [binary, *codex_args], env)
    from .proxy import RotationProxy
    proxy = RotationProxy(profile.name, role="session").start()
    return supervise([binary, *proxy_args(proxy.base_url), *codex_args], env, proxy)


def supervise(argv: list[str], env: dict[str, str], proxy: Any = None,
              tick: Callable[[subprocess.Popen[bytes]], int | None] | None = None, interval: float = 2.0,
              on_start: Callable[[subprocess.Popen[bytes]], None] | None = None, interactive: bool = True) -> int:
    """Run codex as a child, keep the proxy alive while it runs, return codex's exit status.

    Interactive: Ctrl-C reaches codex straight from the terminal, so the parent ignores
    SIGINT instead of forwarding a second copy. A service forwards everything. `tick` runs
    every `interval` seconds and may return an exit status to stop codex (failover)."""
    try:
        child = subprocess.Popen(argv, env=env)
    except OSError:
        if proxy:
            proxy.stop()
        raise
    if on_start:
        on_start(child)

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except OSError:
                pass

    # installed after Popen: an ignored SIGINT would otherwise be inherited by codex
    handled = [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGINT]
    previous = {sig: signal.getsignal(sig) for sig in handled}
    for sig in handled:
        signal.signal(sig, signal.SIG_IGN if (sig == signal.SIGINT and interactive) else forward)
    try:
        while True:
            try:
                code = child.wait(timeout=interval if tick else None)
                break
            except subprocess.TimeoutExpired:
                verdict = tick(child) if tick else None
                if verdict is not None:
                    child.terminate()
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    return verdict
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)
        if proxy:
            proxy.stop()
    return code if code >= 0 else 128 - code


def wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False
