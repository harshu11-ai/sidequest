"""Tests for the hand-rolled stdlib WebSocket + CDP client, against a small
fake local WebSocket server -- real sockets, not a mocked transport, same
preference this project already has elsewhere (test_pty_proxy.py,
test_breaks_companion.py) over mocking network/process boundaries.
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from sidequest.cdp import CDPConnection, CDPError, read_devtools_port

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_until_headers_end(sock: socket.socket, buf: list[bytes]) -> bytes:
    while b"\r\n\r\n" not in buf[0]:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("client closed before completing the handshake")
        buf[0] += chunk
    head, _, rest = buf[0].partition(b"\r\n\r\n")
    buf[0] = rest
    return head


def _recv_exact(sock: socket.socket, buf: list[bytes], count: int) -> bytes:
    while len(buf[0]) < count:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("client closed mid-frame")
        buf[0] += chunk
    data, buf[0] = buf[0][:count], buf[0][count:]
    return data


def _decode_client_frame(sock: socket.socket, buf: list[bytes]) -> tuple[int, bytes]:
    header = _recv_exact(sock, buf, 2)
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, buf, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, buf, 8))[0]
    mask_key = _recv_exact(sock, buf, 4) if masked else b"\x00\x00\x00\x00"
    payload = _recv_exact(sock, buf, length)
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_server_frame(sock: socket.socket, payload: bytes) -> None:
    length = len(payload)
    header = bytearray([0x81])  # FIN + text opcode, unmasked (server -> client)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    sock.sendall(bytes(header) + payload)


class _FakeCDPServer:
    """A one-shot WebSocket server good enough to exercise CDPConnection.

    `responder` runs on a background thread -- callers should capture what
    it receives into a plain list/dict and assert on the main thread
    afterward, rather than asserting inside it (an assertion failure on a
    background thread wouldn't fail the test).
    """

    def __init__(self, responder: Callable[[dict], dict]) -> None:
        self._responder = responder
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        buf: list[bytes] = [b""]
        try:
            request = _recv_until_headers_end(conn, buf)
            key = b""
            for line in request.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(key + _GUID.encode()).digest())
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while True:
                opcode, payload = _decode_client_frame(conn, buf)
                if opcode == 0x8:  # close
                    break
                response = self._responder(json.loads(payload.decode("utf-8")))
                _send_server_frame(conn, json.dumps(response).encode("utf-8"))
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()
            self._listener.close()


class CDPConnectionTests(unittest.TestCase):
    def test_round_trip_request_response(self) -> None:
        captured: list[dict] = []

        def responder(request: dict) -> dict:
            captured.append(request)
            return {"id": request["id"], "result": {"windowId": 42}}

        server = _FakeCDPServer(responder)
        connection = CDPConnection("127.0.0.1", server.port, "/devtools/browser/test", timeout=2)
        try:
            result = connection.send("Browser.getWindowForTarget")
        finally:
            connection.close()

        self.assertEqual(result, {"windowId": 42})
        self.assertEqual(captured[0]["method"], "Browser.getWindowForTarget")

    def test_error_response_raises(self) -> None:
        def responder(request: dict) -> dict:
            return {"id": request["id"], "error": {"message": "boom"}}

        server = _FakeCDPServer(responder)
        connection = CDPConnection("127.0.0.1", server.port, "/devtools/browser/test", timeout=2)
        try:
            with self.assertRaises(CDPError):
                connection.send("Browser.setWindowBounds", {"windowId": 1})
        finally:
            connection.close()

    def test_session_id_and_params_are_sent_with_the_request(self) -> None:
        captured: list[dict] = []

        def responder(request: dict) -> dict:
            captured.append(request)
            return {"id": request["id"], "result": {}}

        server = _FakeCDPServer(responder)
        connection = CDPConnection("127.0.0.1", server.port, "/devtools/browser/test", timeout=2)
        try:
            connection.send("Page.navigate", {"url": "http://x"}, session_id="sess-1")
        finally:
            connection.close()

        self.assertEqual(captured[0]["sessionId"], "sess-1")
        self.assertEqual(captured[0]["params"], {"url": "http://x"})

    def test_attach_page_returns_the_session_and_target_id(self) -> None:
        def responder(request: dict) -> dict:
            if request["method"] == "Target.getTargets":
                return {
                    "id": request["id"],
                    "result": {"targetInfos": [{"type": "page", "targetId": "t1"}]},
                }
            return {"id": request["id"], "result": {"sessionId": "sess-9"}}

        server = _FakeCDPServer(responder)
        connection = CDPConnection("127.0.0.1", server.port, "/devtools/browser/test", timeout=2)
        try:
            self.assertEqual(connection.attach_page(), ("sess-9", "t1"))
        finally:
            connection.close()


class ReadDevtoolsPortTests(unittest.TestCase):
    def test_reads_port_and_path_once_written(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n")
            port, path = read_devtools_port(profile, timeout=1)
            self.assertEqual(port, 9333)
            self.assertEqual(path, "/devtools/browser/abc")

    def test_raises_if_the_file_never_appears(self) -> None:
        with TemporaryDirectory() as directory, self.assertRaises(CDPError):
            read_devtools_port(Path(directory), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
