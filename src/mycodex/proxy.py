"""Local rotation proxy between Codex and the ChatGPT Codex backend.

Codex starts with `-c openai_base_url="http://127.0.0.1:<port>/backend-api/codex"`. It
keeps its built-in `openai` provider, so threads stay tagged `openai` and the phone and
every terminal see one thread list, while model requests come here. For each request
the proxy picks an account, swaps in that account's bearer token and ChatGPT-Account-Id,
and forwards the request to https://chatgpt.com unchanged.

Transport: WebSocket upgrades get 426 Upgrade Required. Codex treats that as "this
endpoint has no WebSocket support" and uses HTTPS (SSE) for the rest of the session.
Every request then carries the full input, so a request can move to another account
without any server-side continuation state.

Rotation happens only before anything reaches Codex: an error the backend returns up
front, or a `response.failed` / `error` event that arrives before the first output
event. The rules are adapted from prodex's runtime error policy (Apache-2.0, see NOTICE):

  quota      usage_limit_reached, insufficient_quota, ...   block until reset, next account
  rate       rate_limit_exceeded, other 429                 block briefly, next account
  profile    deactivated_workspace                          block for hours, next account
  401        expired or revoked token                       refresh once, then next account
  other      overload, 5xx, bad request, ...                pass through (Codex retries)

Once output has started streaming, everything passes through untouched. When every
account is exhausted Codex receives the backend's own usage-limit error, with the
earliest reset time among the accounts tried.

Affinity: a turn's sticky-routing token (x-codex-turn-state) stays with the account that
issued it, a session (session-id / thread-id) stays on its account while that account
works, and new sessions start on the current account (initially the launch profile).

Only processes of the same Unix user may connect (checked against /proc/net/tcp).
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import auth, config, paths, profiles, quota, state, validation as check
from .profiles import Profile

UPSTREAM = "https://chatgpt.com"
PATH_PREFIX = "/backend-api/"
HEALTH_PATH = "/__mycodex/health"
REQUEST_SKIP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
                "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
                "authorization", "chatgpt-account-id"}
RESPONSE_SKIP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "content-length", "trailer",
                 "upgrade", "date", "server"}
NEUTRAL_EVENTS = {"response.created", "response.in_progress", "response.queued"}
PRECOMMIT_SECONDS = 4.0
PRECOMMIT_BYTES = 256 * 1024
ERROR_BODY_LIMIT = 1 << 20
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 900          # Codex enforces its own stream idle timeout; this only bounds a dead upstream
POOL_TTL = 20
BINDING_TTL = 6 * 3600
MAX_BINDINGS = 4000

QUOTA_CODES = frozenset({
    "insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "quota_exhausted", "quota_exceeded",
    "resource_exhausted", "usage_limit_reached", "usage_not_included", "workspace_member_credits_depleted",
})
RATE_CODES = frozenset({"rate_limit_exceeded", "rate_limit_exceeded_error", "slow_down"})
PROFILE_CODES = frozenset({"deactivated_workspace"})
OVERLOAD_CODES = frozenset({"server_is_overloaded", "overloaded_error"})
USAGE_PHRASES = ("you've hit your usage limit", "you have hit your usage limit", "you hit your usage limit",
                 "usage limit has been reached")
USAGE_HINTS = ("try again at", "request to your admin", "more access now")
OVERLOAD_PHRASES = ("selected model is at capacity", "backend under high demand", "experiencing high demand",
                    "server is overloaded", "currently overloaded")


# ----------------------------------------------------------------------------- classification
@dataclass
class Verdict:
    kind: str                 # quota | rate | profile | auth | overload | transient | other
    message: str = ""
    resets_at: int | None = None

    @property
    def rotates(self) -> bool:
        return self.kind in ("quota", "rate", "profile", "auth")


def _walk(value: Any, codes: set[str], texts: list[str], resets: list[int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                if key in ("code", "type", "status", "reason"):
                    codes.add(item.lower())
                elif key in ("message", "detail", "error", "error_description"):
                    texts.append(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if key == "resets_at":
                    resets.append(int(item))
                elif key == "resets_in_seconds":
                    resets.append(int(time.time() + item))
            else:
                _walk(item, codes, texts, resets)
    elif isinstance(value, list):
        for item in value:
            _walk(item, codes, texts, resets)


def classify(status: int | None, body: bytes) -> Verdict:
    """Classify a backend error. `status` None means an error event inside an SSE stream."""
    codes: set[str] = set()
    texts: list[str] = []
    resets: list[int] = []
    try:
        _walk(json.loads(body), codes, texts, resets)
        text = " ".join(texts).lower()
    except (ValueError, UnicodeDecodeError):
        text = body[:4096].decode("utf-8", "replace").lower()
    message = " ".join(" ".join(texts).split())[:300] or " ".join(text.split())[:300]
    resets_at = min(resets) if resets else None
    usage = any(p in text for p in USAGE_PHRASES) or ("usage limit" in text and any(h in text for h in USAGE_HINTS))
    workspace = "workspace is out of credits" in text or "workspace_member_credits_depleted" in text \
        or ("out of credits" in text and "workspace owner" in text and "refill" in text)
    quota_hit = bool(codes & QUOTA_CODES) or usage or workspace
    rate = bool(codes & RATE_CODES)
    gone = bool(codes & PROFILE_CODES) or "deactivated_workspace" in text
    overload = bool(codes & OVERLOAD_CODES) or any(p in text for p in OVERLOAD_PHRASES)
    if status is None:
        for kind, hit in (("profile", gone), ("overload", overload), ("quota", quota_hit), ("rate", rate)):
            if hit:
                return Verdict(kind, message, resets_at)
        return Verdict("other", message, resets_at)
    if status in (402, 403):
        if gone:
            return Verdict("profile", message)
        return Verdict("quota", message, resets_at) if quota_hit else Verdict("other", message)
    if status == 429:
        if rate:
            return Verdict("rate", message, resets_at)
        if quota_hit:
            return Verdict("quota", message, resets_at)
        if any("flex" in code for code in codes):
            return Verdict("other", message)      # service-tier capacity, not the account
        return Verdict("rate", message, resets_at)
    if status in (500, 502, 503, 504, 529):
        return Verdict("overload" if overload else "transient", message)
    return Verdict("other", message)


def scan_events(buffer: bytes) -> tuple[str, Verdict | None]:
    """Look at the complete SSE events in `buffer`.

    Returns ("wait", None) while only bookkeeping events arrived, ("fail", verdict) when the
    first real event is an error that should move the request to another account, and
    ("commit", None) otherwise."""
    blocks = buffer.replace(b"\r\n", b"\n").split(b"\n\n")
    for block in blocks[:-1]:
        data = b"\n".join(line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:"))
        if not data:
            continue
        if data.strip() == b"[DONE]":
            return "commit", None
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return "commit", None
        kind = event.get("type") if isinstance(event, dict) else None
        if kind in NEUTRAL_EVENTS:
            continue
        if kind in ("response.failed", "error"):
            verdict = classify(None, data)
            return ("fail", verdict) if verdict.rotates else ("commit", None)
        return "commit", None
    return "wait", None


# ----------------------------------------------------------------------------- logging / registry
class ProxyLog:
    """Append-only event log shared by every proxy (one line per event, no secrets)."""

    MAX_BYTES = 5 * 1024 * 1024
    _lock = threading.Lock()

    def __init__(self, tag: str, echo: bool = False, path: Path | None = None):
        self.tag = tag
        self.echo = echo
        self.path = path or paths.PROXY_LOG

    def write(self, event: str, **fields: Any) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        detail = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
        line = f"{stamp} {self.tag} {event} {detail}".rstrip()
        if self.echo:
            print(f"mycodex-proxy: {event} {detail}".rstrip(), file=sys.stderr, flush=True)
        try:
            with self._lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                    os.replace(self.path, self.path.with_suffix(".log.1"))
                with open(self.path, "a") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass


def live_proxies() -> list[dict[str, Any]]:
    """Registered proxies whose process is still alive (stale entries are removed)."""
    found = []
    directory = paths.PROXIES_DIR
    if not directory.is_dir():
        return found
    for entry in sorted(directory.glob("*.json")):
        try:
            data = json.loads(entry.read_text())
            os.kill(int(data["pid"]), 0)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        found.append(data)
    return found


def health(port: int, timeout: float = 3) -> dict[str, Any] | None:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", HEALTH_PATH)
        response = conn.getresponse()
        data = json.loads(response.read()) if response.status == 200 else None
        conn.close()
        return data
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _peer_uid(peer: tuple[str, int], local_port: int) -> int | None:
    """Owner of the client socket of a loopback TCP connection."""
    wanted_local = f"0100007F:{peer[1]:04X}"
    wanted_remote = f"0100007F:{local_port:04X}"
    try:
        with open("/proc/net/tcp") as handle:
            next(handle)
            for line in handle:
                parts = line.split()
                if len(parts) > 7 and parts[1] == wanted_local and parts[2] == wanted_remote:
                    return int(parts[7])
    except (OSError, StopIteration, ValueError):
        return None
    return None


# ----------------------------------------------------------------------------- upstream
class Upstream:
    """One keep-alive connection to the backend per client connection."""

    def __init__(self, base: str):
        parts = urllib.parse.urlsplit(base)
        self.https = parts.scheme == "https"
        self.host = parts.hostname or "chatgpt.com"
        self.port = parts.port or (443 if self.https else 80)
        self.conn: http.client.HTTPConnection | None = None
        self.sock: socket.socket | None = None

    def discard(self) -> None:
        """Drop the connection. shutdown() also wakes a reader thread blocked in recv()."""
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None
        self.sock = None

    def drain(self, response: http.client.HTTPResponse) -> bytes:
        """Read an error body; a body too large to finish makes the connection unusable."""
        raw = response.read(ERROR_BODY_LIMIT)
        if not response.isclosed():
            self.discard()
        return raw

    def send(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> http.client.HTTPResponse:
        for attempt in (0, 1):
            fresh = self.conn is None
            if fresh:
                if self.https:
                    self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=CONNECT_TIMEOUT,
                                                            context=ssl.create_default_context())
                else:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=CONNECT_TIMEOUT)
            conn = self.conn
            try:
                conn.request(method, path, body=body, headers=headers)
                self.sock = conn.sock
                if self.sock is not None:
                    self.sock.settimeout(READ_TIMEOUT)
                return conn.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                self.discard()
                if fresh or attempt:      # only a reused keep-alive connection earns a retry
                    raise
            except BaseException:
                self.discard()
                raise
        raise ConnectionError("unreachable")


class _Reader(threading.Thread):
    """Moves response bytes into a queue so the handler can wait with a deadline."""

    def __init__(self, response: http.client.HTTPResponse):
        super().__init__(daemon=True)
        self.response = response
        self.items: queue.Queue[bytes | BaseException | None] = queue.Queue()

    def run(self) -> None:
        try:
            while True:
                chunk = self.response.read1(65536)
                if not chunk:
                    break
                self.items.put(chunk)
        except BaseException as exc:  # closed under us, timeout, reset
            self.items.put(exc)
        finally:
            self.items.put(None)


@dataclass
class Failure:
    verdict: Verdict
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes
    profile: str


# ----------------------------------------------------------------------------- handler
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mycodex-proxy"
    timeout = 900               # idle keep-alive connections from Codex
    server: "_Server"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence default stderr logging
        return

    def setup(self) -> None:
        super().setup()
        self.upstream = Upstream(self.server.proxy.upstream)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.upstream.discard()

    def do_GET(self) -> None:
        self._handle()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    # -- request ---------------------------------------------------------------------------
    def _handle(self) -> None:
        proxy = self.server.proxy
        if self.headers.get("Upgrade"):
            self._local(426, {"error": {"type": "upgrade_required",
                                        "message": "mycodex proxy serves HTTPS/SSE only"}}, close=True)
            return
        if self.path == HEALTH_PATH:
            self._local(200, proxy.health())
            return
        if not self.path.startswith(PATH_PREFIX):
            self._local(404, {"error": {"type": "not_found", "message": f"mycodex proxy: no route for {self.path}"}})
            return
        try:
            body = self._read_body()
        except (ValueError, OSError):
            self._local(400, {"error": {"type": "bad_request", "message": "mycodex proxy: unreadable request body"}},
                        close=True)
            return
        self._proxy(body)

    def _read_body(self) -> bytes | None:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(data)
                data += self.rfile.read(size)
                self.rfile.readline(16)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            return self.rfile.read(length)
        return b"" if self.command in ("POST", "PUT", "PATCH") else None

    def _headers_for(self, creds: auth.Credentials, keep_turn_state: bool) -> dict[str, str]:
        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in REQUEST_SKIP or (lower == "x-codex-turn-state" and not keep_turn_state):
                continue
            headers[key] = value
        headers["Authorization"] = f"Bearer {creds.access_token}"
        if creds.account_id:
            headers["ChatGPT-Account-Id"] = creds.account_id
        return headers

    def _proxy(self, body: bytes | None) -> None:
        proxy = self.server.proxy
        started = time.monotonic()
        turn_state = self.headers.get("x-codex-turn-state")
        session = (self.headers.get("session-id") or self.headers.get("thread-id")
                   or self.headers.get("session_id") or self.headers.get("conversation_id"))
        try:
            plan = proxy.plan(turn_state, session,
                              fresh_turn=self.command == "POST" and self.path.split("?")[0].endswith("/responses"))
        except (state.StateError, SystemExit) as exc:
            self._local(503, {"error": {"type": "mycodex_settings", "message": str(exc)}})
            return
        if not plan:
            self._local(503, {"error": {"type": "mycodex_no_account",
                                        "message": "mycodex: no logged-in profile is available "
                                                   "(mycodex profile list / mycodex profile reauth)"}})
            return
        failures: list[Failure] = []
        for profile in plan:
            outcome = self._attempt(profile, body, turn_state, session)
            if isinstance(outcome, Failure) and outcome.verdict.kind == "quota" \
                    and proxy.pool()[1]["rotation"].get("auto_redeem", False):
                recovered = proxy.redeem(profile)
                if recovered:
                    outcome = self._attempt(profile, body, turn_state, session)
            if outcome is None:
                proxy.log.write("request", profile=profile.name, path=self.path.split("?")[0],
                                session=_short(session), ms=int((time.monotonic() - started) * 1000),
                                tries=len(failures) + 1)
                return
            if isinstance(outcome, Failure):
                failures.append(outcome)
                continue
            # transport failure talking to the backend: not the account's fault, let Codex retry
            proxy.log.write("upstream-error", profile=profile.name, error=type(outcome).__name__)
            self._local(502, {"error": {"type": "mycodex_upstream_error",
                                        "message": f"mycodex proxy could not reach the backend: {outcome}"}})
            return
        self._send_failure(failures)

    def _attempt(self, profile: Profile, body: bytes | None, turn_state: str | None,
                 session: str | None) -> Failure | BaseException | None:
        """None = response delivered to Codex; Failure = try the next account; exception = give up."""
        proxy = self.server.proxy
        try:
            creds = auth.credentials(profile.home)
        except auth.AuthError as exc:
            # 503, not 401: a 401 would make Codex refresh its own (healthy) login
            verdict = Verdict("auth", str(exc))
            proxy.penalize(profile, verdict, 0, permanent=exc.permanent)
            message = f"mycodex: {profile.name} cannot authenticate ({exc}); run `mycodex profile reauth {profile.name}`"
            return Failure(verdict, 503, "Service Unavailable", [("Content-Type", "application/json")],
                           json.dumps({"error": {"type": "mycodex_auth", "message": message}}).encode(), profile.name)
        refreshed = False
        while True:
            headers = self._headers_for(creds, proxy.turn_state_belongs(turn_state, profile.name))
            try:
                response = self.upstream.send(self.command, self.path, headers, body)
            except (OSError, http.client.HTTPException) as exc:
                return exc
            if response.status == 401:
                raw = self.upstream.drain(response)
                if not refreshed:
                    try:
                        creds = auth.refresh(profile.home, stale=creds.access_token)
                        refreshed = True
                        proxy.log.write("token-refreshed", profile=profile.name)
                        continue
                    except auth.AuthError as exc:
                        verdict = Verdict("auth", str(exc))
                        proxy.penalize(profile, verdict, 401, permanent=exc.permanent)
                        return Failure(verdict, 401, response.reason, response.getheaders(), raw, profile.name)
                verdict = Verdict("auth", "the backend rejected a freshly refreshed token")
                proxy.penalize(profile, verdict, 401)
                return Failure(verdict, 401, response.reason, response.getheaders(), raw, profile.name)
            if response.status >= 400:
                raw = self.upstream.drain(response)
                verdict = classify(response.status, raw)
                if verdict.rotates:
                    proxy.penalize(profile, verdict, response.status)
                    return Failure(verdict, response.status, response.reason, response.getheaders(), raw, profile.name)
                self._send_buffered(response.status, response.reason, response.getheaders(), raw)
                proxy.served(profile, session, turn_state, response)
                return None
            if "text/event-stream" in (response.getheader("Content-Type") or ""):
                return self._stream(response, profile, session, turn_state)
            proxy.served(profile, session, turn_state, response)
            self._forward_body(response)
            return None

    # -- responses -------------------------------------------------------------------------
    def _stream(self, response: http.client.HTTPResponse, profile: Profile, session: str | None,
                turn_state: str | None) -> Failure | BaseException | None:
        proxy = self.server.proxy
        reader = _Reader(response)
        reader.start()
        buffer = b""
        ended = False
        decision, verdict = "wait", None
        deadline = time.monotonic() + PRECOMMIT_SECONDS
        while decision == "wait":
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(buffer) > PRECOMMIT_BYTES:
                break
            try:
                item = reader.items.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                ended = True
                decision, verdict = scan_events(buffer + b"\n\n")
                break
            if isinstance(item, BaseException):
                self.upstream.discard()
                return item
            buffer += item
            decision, verdict = scan_events(buffer)
        if decision == "fail" and verdict is not None:
            self.upstream.discard()
            response.close()
            proxy.penalize(profile, verdict, response.status)
            return Failure(verdict, response.status, response.reason, response.getheaders(), buffer, profile.name)

        proxy.served(profile, session, turn_state, response)
        self._start_chunked(response.status, response.reason, response.getheaders())
        alive = self._chunk(buffer)
        broken = False
        while alive and not ended:
            item = reader.items.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                broken = True        # upstream died mid-stream: end abruptly so Codex retries the turn
                break
            alive = self._chunk(item)
        if alive and not broken:
            alive = self._chunk(b"", final=True)
        if not alive or broken:
            self.upstream.discard()
            response.close()
            self.close_connection = True
        return None

    def _forward_body(self, response: http.client.HTTPResponse) -> None:
        bodiless = self.command == "HEAD" or response.status in (204, 304) or response.status < 200
        length = response.getheader("Content-Length")
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in RESPONSE_SKIP:
                self.send_header(key, value)
        if bodiless:
            self.end_headers()
            response.read()
            return
        if length is not None:
            self.send_header("Content-Length", length)
            self.end_headers()
            try:
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except OSError:
                self.close_connection = True
                self.upstream.discard()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        alive = True
        try:
            while alive:
                chunk = response.read1(65536)
                if not chunk:
                    break
                alive = self._chunk(chunk)
        except OSError:
            alive = False
        if alive:
            self._chunk(b"", final=True)
        else:
            self.close_connection = True
            self.upstream.discard()

    def _start_chunked(self, status: int, reason: str, headers: list[tuple[str, str]]) -> None:
        self.send_response(status, reason)
        for key, value in headers:
            if key.lower() not in RESPONSE_SKIP:
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes, final: bool = False) -> bool:
        try:
            if data:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            if final:
                self.wfile.write(b"0\r\n\r\n")
            return True
        except OSError:
            return False

    def _send_buffered(self, status: int, reason: str, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status, reason)
        for key, value in headers:
            if key.lower() not in RESPONSE_SKIP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _send_failure(self, failures: list[Failure]) -> None:
        """Every account failed: hand Codex the most useful backend answer (earliest reset first)."""
        proxy = self.server.proxy
        quota_failures = [f for f in failures if f.verdict.kind == "quota"]
        if quota_failures:
            chosen = min(quota_failures, key=lambda f: f.verdict.resets_at or float("inf"))
        else:
            chosen = failures[-1]
        proxy.log.write("exhausted", tried=",".join(f.profile for f in failures), returned=chosen.verdict.kind,
                        status=chosen.status)
        self._send_buffered(chosen.status, chosen.reason, chosen.headers, chosen.body)

    def _local(self, status: int, payload: dict[str, Any], close: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    proxy: "RotationProxy"

    def verify_request(self, request: Any, client_address: Any) -> bool:
        if os.environ.get("MYCODEX_PROXY_ANY_UID") == "1" or not os.path.exists("/proc/net/tcp"):
            return True
        uid = _peer_uid(client_address, self.server_address[1])
        if uid == os.getuid():
            return True
        self.proxy.log.write("refused", peer=f"{client_address[0]}:{client_address[1]}", uid=uid)
        return False


def _short(value: str | None) -> str | None:
    return value[:8] if value else None


# ----------------------------------------------------------------------------- proxy
class RotationProxy:
    def __init__(self, owner: str, role: str = "session", echo: bool = False, upstream: str | None = None):
        self.owner = owner
        self.role = role
        self.upstream = upstream or os.environ.get("MYCODEX_UPSTREAM") or UPSTREAM
        self.current = owner
        self.lock = threading.RLock()
        self.sessions: dict[str, tuple[str, float]] = {}
        self.turns: dict[str, tuple[str, float]] = {}
        self.issued: dict[str, str] = {}
        self._pool: tuple[float, list[Profile], dict[str, Any]] | None = None
        self.started = time.time()
        self.requests = 0
        self.switches = 0
        self.reset_checks: dict[str, float] = {}
        self.log = ProxyLog(f"pid={os.getpid()} {role}", echo=echo)
        self.server = _Server(("127.0.0.1", 0), Handler)
        self.server.proxy = self
        self._thread: threading.Thread | None = None
        self._registry: Path | None = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/backend-api/codex"

    def start(self) -> "RotationProxy":
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="mycodex-proxy", daemon=True)
        self._thread.start()
        try:
            self._registry = paths.state_dir("proxies") / f"{os.getpid()}.json"
            self._registry.write_text(json.dumps({"pid": os.getpid(), "port": self.port, "owner": self.owner,
                                                  "role": self.role, "started_at": int(self.started)}))
        except OSError:
            self._registry = None
        self.log.write("start", owner=self.owner, port=self.port, upstream=self.upstream)
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._registry:
            try:
                self._registry.unlink()
            except OSError:
                pass
        self.log.write("stop", requests=self.requests, switches=self.switches)

    # -- account selection -----------------------------------------------------------------
    def pool(self) -> tuple[list[Profile], dict[str, Any]]:
        now = time.monotonic()
        if self._pool and now - self._pool[0] < POOL_TTL:
            return self._pool[1], self._pool[2]
        cfg = config.load()
        disabled = set(cfg["rotation"]["disabled"])
        members = [p for p in profiles.all(cfg) if p.logged_in and (p.name not in disabled or p.name == self.owner)]
        self._pool = (now, members, cfg)
        return members, cfg

    @staticmethod
    def _lookup(table: dict[str, tuple[str, float]], key: str | None) -> str | None:
        if not key:
            return None
        hit = table.get(key)
        return hit[0] if hit and time.time() - hit[1] < BINDING_TTL else None

    def plan(self, turn_state: str | None, session: str | None, fresh_turn: bool = True) -> list[Profile]:
        members, cfg = self.pool()
        blocked = state.blocks()
        usable = [p for p in members if p.name not in blocked]
        threshold = cfg["rotation"].get("min_quota_headroom", 0)
        if threshold and not turn_state and fresh_turn:
            # Refresh stale snapshots at a fresh-turn boundary, never during a sticky turn.
            stale = [p for p in usable if quota.cached(p.name, max_age=60) is None]
            quota.fetch_all(stale, timeout=3)
        order = [n for n in cfg["rotation"]["order"] if n in {p.name for p in usable}]

        def rank(p: Profile) -> tuple[int, float, str]:
            cached = quota.cached(p.name, max_age=1800)
            if cached and cached.status == "limited":
                headroom = -1.0
            else:
                headroom = cached.remaining if cached and cached.remaining is not None else 50.0
            return (order.index(p.name) if p.name in order else len(order), -headroom, p.name)

        with self.lock:
            preferred = [self._lookup(self.turns, turn_state), self._lookup(self.sessions, session), self.current]
        by_name = {p.name: p for p in usable}
        result: list[Profile] = []
        for name in preferred:
            if name in by_name and by_name[name] not in result:
                result.append(by_name[name])
        result += [p for p in sorted(usable, key=rank) if p not in result]
        if not turn_state and threshold and fresh_turn and result:
            def headroom(p: Profile) -> float | None:
                cached = quota.cached(p.name, max_age=300)
                if not cached or not cached.ok:
                    return None
                short = next((w for w in cached.windows if w.seconds == 18000), None)
                if not short or (short.reset_at and short.reset_at <= time.time()):
                    return None
                return short.remaining
            current_headroom = headroom(result[0])
            if current_headroom == 0 and cfg["rotation"].get("auto_redeem") and self.redeem(result[0]):
                current_headroom = headroom(result[0])
            if current_headroom is not None and current_headroom < threshold:
                healthy = []
                for member in result[1:]:
                    cached = quota.cached(member.name, max_age=300)
                    short_remaining = headroom(member)
                    if cached and cached.eligible and cached.remaining is not None and cached.remaining >= threshold \
                            and (short_remaining is None or short_remaining >= threshold):
                        healthy.append(member)
                if healthy:
                    result = healthy + [p for p in result if p not in healthy]
        if not result and members:
            # everything is blocked: ask the account that frees up first, so Codex gets a real answer
            result = sorted(members, key=lambda p: blocked.get(p.name, (0, ""))[0])[:1]
        return result

    def redeem(self, profile: Profile) -> bool:
        from . import redemption
        now = time.monotonic()
        with self.lock:
            if now - self.reset_checks.get(profile.name, -float("inf")) < 30:
                return False
            self.reset_checks[profile.name] = now
        try:
            recovered = redemption.try_auto(profile)
        except (auth.AuthError, OSError, RuntimeError, check.InvalidData) as exc:
            self.log.write("reset-credit-error", profile=profile.name, error=type(exc).__name__)
            return False
        if recovered:
            self.log.write("reset-credit-redeemed", profile=profile.name)
        return recovered

    def turn_state_belongs(self, turn_state: str | None, name: str) -> bool:
        if not turn_state:
            return True
        with self.lock:
            issuer = self.issued.get(turn_state)
        return issuer in (None, name)

    def served(self, profile: Profile, session: str | None, turn_state: str | None,
               response: http.client.HTTPResponse) -> None:
        now = time.time()
        with self.lock:
            self.requests += 1
            previous = self.current
            self.current = profile.name
            if session:
                self.sessions[session] = (profile.name, now)
            issued = response.getheader("x-codex-turn-state")
            if issued:
                self.issued[issued] = profile.name
                self.turns[issued] = (profile.name, now)
            if turn_state and turn_state not in self.turns:
                self.turns[turn_state] = (profile.name, now)
            if len(self.sessions) > MAX_BINDINGS or len(self.turns) > MAX_BINDINGS:
                self._prune(now)
        if previous != profile.name:
            self.switches += 1
            self.log.write("switch", **{"from": previous, "to": profile.name, "session": _short(session)})

    def _prune(self, now: float) -> None:
        for table in (self.sessions, self.turns):
            for key in [k for k, (_, at) in table.items() if now - at > BINDING_TTL]:
                del table[key]
        if len(self.issued) > MAX_BINDINGS:
            for key in list(self.issued)[: len(self.issued) // 2]:
                del self.issued[key]

    def penalize(self, profile: Profile, verdict: Verdict, status: int, permanent: bool = True) -> None:
        now = time.time()
        if verdict.kind == "quota":
            until = verdict.resets_at or now + 1800
        elif verdict.kind == "rate":
            until = now + 90
        elif verdict.kind == "profile":
            until = now + 6 * 3600
        else:  # auth
            until = now + (3600 if permanent else 120)
        until = min(max(until, now + 60), now + 8 * 86400)
        try:
            state.block(profile.name, until, f"{verdict.kind}: {verdict.message}".strip(": "))
        except OSError:
            pass
        self.log.write("paused", profile=profile.name, kind=verdict.kind, status=status or None,
                       until=datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       reason=json.dumps(verdict.message[:120]))
        if verdict.kind in ("quota", "rate"):
            threading.Thread(target=self._learn_reset, args=(profile, int(until)), daemon=True).start()

    def _learn_reset(self, profile: Profile, expected_until: int) -> None:
        """Ask the usage endpoint when a just-blocked account frees up, and use that time."""
        q = quota.fetch(profile)
        if q.status == "limited" and q.usable_again_at and q.usable_again_at > time.time():
            state.extend_pause(profile.name, expected_until, q.usable_again_at)

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"pid": os.getpid(), "port": self.port, "owner": self.owner, "role": self.role,
                    "current": self.current, "uptime": int(time.time() - self.started),
                    "requests": self.requests, "switches": self.switches,
                    "sessions": len(self.sessions), "upstream": self.upstream}
