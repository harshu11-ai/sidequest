"""Minimal stdlib WebSocket + Chrome DevTools Protocol client.

Deliberately narrow, not a general-purpose WebSocket client: local,
unencrypted, one connection, flat CDP sessions, just enough to resize and
navigate a Chrome window `CompanionWindow` spawned itself. Python's stdlib
has no WebSocket client, so this hand-rolls the pieces actually needed --
matching this project's existing precedent of staying stdlib-only for
network code (`chess/relay_client.py`'s docstring says the same for HTTP).

Every blocking call here is timeout-bounded on purpose: a hung CDP call
must never hang the companion's request-handling thread, the same lesson
an unrelated `osascript` spike turned up this session (a macOS permission
dialog nobody could answer hung indefinitely with no timeout of its own).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from contextlib import suppress
from pathlib import Path

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CDPError(RuntimeError):
    """A CDP handshake, frame, or request/response failed."""


def read_devtools_port(profile_dir: Path, timeout: float = 5.0) -> tuple[int, str]:
    """Poll for the port + browser target path Chrome writes on launch.

    Returns ``(port, browser_ws_path)`` read from
    ``<profile_dir>/DevToolsActivePort`` -- written a moment after Chrome
    starts when launched with ``--remote-debugging-port=0``. Raises
    CDPError if it never appears within `timeout` seconds.
    """
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = port_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip():
                return int(lines[0].strip()), lines[1].strip()
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise CDPError(f"Chrome never wrote a DevTools port file at {port_file}")


class CDPConnection:
    """A single WebSocket connection to a browser-level CDP endpoint."""

    def __init__(self, host: str, port: int, path: str, *, timeout: float = 3.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buffer = b""
        self._next_id = 1
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._sock.sendall(request)
        response = self._read_http_response()
        status_line = response.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise CDPError(f"CDP handshake failed: {status_line!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected.encode("ascii") not in response:
            raise CDPError("CDP handshake failed: Sec-WebSocket-Accept mismatch")

    def _read_http_response(self) -> bytes:
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPError("connection closed during handshake")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        self._buffer = rest
        return head

    # -- framing (client frames must be masked per RFC 6455) --------------

    def _send_text(self, payload: bytes) -> None:
        length = len(payload)
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x81])  # FIN + text opcode
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        self._sock.sendall(bytes(header) + mask_key + masked)

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CDPError("connection closed while reading a frame")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _recv_text(self) -> str:
        message = b""
        while True:
            header = self._recv_exact(2)
            fin = header[0] & 0x80
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x8:  # close
                raise CDPError("CDP connection closed by peer")
            if opcode in (0x0, 0x1):  # continuation or text
                message += payload
            # Ping/pong (0x9/0xA) ignored -- Chrome doesn't send these
            # during the short request/response exchanges this client makes.
            if fin:
                break
        return message.decode("utf-8")

    # -- CDP JSON-RPC --------------------------------------------------------

    def send(
        self, method: str, params: dict | None = None, *, session_id: str | None = None
    ) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, object] = {"id": request_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        self._send_text(json.dumps(message).encode("utf-8"))
        while True:
            reply = json.loads(self._recv_text())
            if reply.get("id") != request_id:
                continue  # an event notification, or a reply to a stale id -- skip it
            if "error" in reply:
                raise CDPError(f"{method} failed: {reply['error']}")
            return reply.get("result", {})

    def attach_page(self) -> tuple[str, str]:
        """Attach to the (single) page target, return (sessionId, targetId).

        Both are needed downstream: sessionId to address Page-domain
        commands at this target over the flat/multiplexed connection,
        targetId because Browser.getWindowForTarget errors with "No web
        contents in the target" if asked to infer it (it defaults to the
        browser-level target, which has no window of its own).
        """
        targets = self.send("Target.getTargets")["targetInfos"]
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise CDPError("no page target found to attach to")
        target_id = pages[0]["targetId"]
        result = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return result["sessionId"], target_id

    def close(self) -> None:
        with suppress(OSError):
            self._sock.close()
