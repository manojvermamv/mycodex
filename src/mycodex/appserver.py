"""JSON-RPC client for a running Codex app-server, via the official relay.

`codex app-server proxy --sock PATH` relays raw bytes to the server's control socket,
which speaks WebSocket. This client performs the WebSocket handshake and framing over
the relay's stdio, then the normal `initialize` / `initialized` exchange (with
`experimentalApi`, required for project/* and remoteControl/* methods).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, paths


class AppServerError(RuntimeError):
    pass


class AppServer:
    def __init__(self, socket_path: str | Path, home: Path | None = None, timeout: float = 30):
        self.socket_path = str(socket_path)
        self.home = home
        self.timeout = timeout
        self.proc: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._buf = b""
        self._next_id = 0
        self.notifications: list[dict[str, Any]] = []

    # -- lifecycle -------------------------------------------------------------------------
    def __enter__(self) -> "AppServer":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        extra = {"NO_COLOR": "1"}
        if self.home:
            extra.update({"CODEX_HOME": str(self.home), "CODEX_SQLITE_HOME": str(paths.SHARED_CODEX_HOME)})
        self.proc = subprocess.Popen(
            [paths.codex_bin(), "app-server", "proxy", "--sock", self.socket_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=paths.tool_env(extra))
        threading.Thread(target=self._pump, daemon=True).start()
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        deadline = time.time() + self.timeout
        while b"\r\n\r\n" not in self._buf:
            self._fill(deadline)
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in status:
            raise AppServerError(f"control socket refused the connection: {status}")
        init = self.call("initialize", {
            "clientInfo": {"name": "mycodex", "title": "mycodex", "version": __version__},
            "capabilities": {"experimentalApi": True},
        })
        if "error" in init:
            raise AppServerError(f"initialize failed: {init['error']}")
        self._send({"method": "initialized"})

    def close(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.proc = None

    # -- io --------------------------------------------------------------------------------
    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                self._queue.put(None)
                return
            self._queue.put(chunk)

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise AppServerError("timed out waiting for the app-server")
        try:
            chunk = self._queue.get(timeout=remaining)
        except queue.Empty:
            raise AppServerError("timed out waiting for the app-server") from None
        if chunk is None:
            err = b""
            if self.proc and self.proc.stderr:
                try:
                    err = self.proc.stderr.read(2000) or b""
                except OSError:
                    pass
            raise AppServerError("control socket closed" + (f": {err.decode(errors='replace').strip()}" if err else ""))
        self._buf += chunk

    def _need(self, size: int, deadline: float) -> bytes:
        while len(self._buf) < size:
            self._fill(deadline)
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def _write(self, data: bytes) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message).encode()
        mask = os.urandom(4)
        size = len(data)
        header = bytes([0x81])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", size)
        self._write(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _receive(self, deadline: float) -> dict[str, Any]:
        payload = b""
        while True:
            b0, b1 = self._need(2, deadline)
            fin, opcode, size = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._need(2, deadline))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._need(8, deadline))[0]
            data = self._need(size, deadline)
            if opcode == 0x8:
                raise AppServerError("app-server closed the connection")
            if opcode in (0x9, 0xA):
                continue
            payload += data
            if fin:
                return json.loads(payload.decode())

    # -- rpc -------------------------------------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.time() + (timeout or self.timeout)
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            self._handle_other(message)

    def result(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        response = self.call(method, params, timeout)
        if "error" in response:
            error = response["error"]
            raise AppServerError(f"{method}: {error.get('message', error)}")
        return response.get("result")

    def wait_for(self, method: str, timeout: float = 120) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        for note in self.notifications:
            if note.get("method") == method:
                return note
        while time.time() < deadline:
            try:
                message = self._receive(deadline)
            except AppServerError:
                return None
            self._handle_other(message)
            if message.get("method") == method:
                return message
        return None

    def _handle_other(self, message: dict[str, Any]) -> None:
        if "method" in message:
            self.notifications.append(message)
            if "id" in message:  # server -> client request (approvals etc.): decline politely
                self._send({"id": message["id"], "error": {"code": -32601, "message": "not supported by mycodex"}})
