"""Loopback web companion shown while an agent turn is running."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from sidequest.chess_game import ComputerChessGame

_MAX_REQUEST_BYTES = 4096
_ASSET_TYPES = {
    "/": ("chess.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/cm-chessboard.js": ("cm-chessboard.js", "text/javascript; charset=utf-8"),
    "/cm-chessboard.css": ("cm-chessboard.css", "text/css; charset=utf-8"),
    "/cm-markers.js": ("cm-markers.js", "text/javascript; charset=utf-8"),
    "/cm-markers.css": ("cm-markers.css", "text/css; charset=utf-8"),
    "/cm-standard.svg": ("cm-standard.svg", "image/svg+xml"),
    "/cm-markers.svg": ("cm-markers.svg", "image/svg+xml"),
}
_MACOS_CHROMIUM_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class CompanionWindow:
    """Launch a disposable app-style browser window that can be closed reliably."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._profile = tempfile.TemporaryDirectory(prefix="sidequest-browser-")

    def open(self, url: str) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        executable = _find_chromium()
        if executable is None:
            webbrowser.open_new(url)
            return
        try:
            self._process = subprocess.Popen(
                [
                    executable,
                    f"--app={url}",
                    f"--user-data-dir={self._profile.name}",
                    "--window-size=780,650",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            webbrowser.open_new(url)

    def hide(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)

    def close(self) -> None:
        self.hide()
        self._profile.cleanup()


def _find_chromium() -> str | None:
    for path in _MACOS_CHROMIUM_PATHS:
        if os.access(path, os.X_OK):
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "msedge"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


class ChessCompanion:
    """Own a private local HTTP server and the current chess-window state."""

    def __init__(
        self,
        game: ComputerChessGame | None = None,
        *,
        browser_open: Any | None = None,
        browser_close: Any | None = None,
    ) -> None:
        self.game = game or ComputerChessGame()
        self._window = CompanionWindow() if browser_open is None else None
        self._browser_open = browser_open or self._window.open
        default_close = self._window.hide if self._window else (lambda: None)
        self._browser_close = browser_close or default_close
        self._token = secrets.token_urlsafe(24)
        self._active = False
        self._active_lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sidequest-chess-server",
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def play_url(self) -> str:
        return f"{self.base_url}/?token={self._token}"

    def lifecycle_url(self, event: str) -> str:
        if event not in {"start", "stop"}:
            raise ValueError(f"unsupported lifecycle event: {event}")
        return f"{self.base_url}/lifecycle/{event}?token={self._token}"

    def show(self) -> None:
        with self._active_lock:
            if self._active:
                return
            self._active = True
        try:
            self._browser_open(self.play_url)
        except OSError:
            with self._active_lock:
                self._active = False

    def hide(self) -> None:
        with self._active_lock:
            was_active = self._active
            self._active = False
        if was_active:
            self._browser_close()

    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    def close(self) -> None:
        self.hide()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        if self._window is not None:
            self._window.close()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                companion._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                companion._handle_post(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path in _ASSET_TYPES:
            asset_name, content_type = _ASSET_TYPES[parsed.path]
            content = files("sidequest").joinpath("web", asset_name).read_bytes()
            self._respond(handler, HTTPStatus.OK, content, content_type)
            return
        if parsed.path == "/api/state":
            if not self._authorized(parsed.query):
                self._json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            state = self.game.snapshot().as_dict()
            state["active"] = self.is_active()
            self._json(handler, HTTPStatus.OK, state)
            return
        self._json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if not self._authorized(parsed.query):
            self._json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if parsed.path == "/lifecycle/start":
            self.show()
            self._json(handler, HTTPStatus.OK, {})
            return
        if parsed.path == "/lifecycle/stop":
            self.hide()
            self._json(handler, HTTPStatus.OK, {})
            return
        if parsed.path not in {"/api/move", "/api/new", "/api/difficulty"}:
            self._json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if handler.headers.get_content_type() != "application/json":
            self._json(handler, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "use JSON"})
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > _MAX_REQUEST_BYTES:
            self._json(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return
        try:
            payload = json.loads(handler.rfile.read(length) or b"{}")
            if parsed.path == "/api/new":
                snapshot = self.game.new_game()
            elif parsed.path == "/api/difficulty":
                snapshot = self.game.set_difficulty(payload.get("difficulty", ""))
            else:
                snapshot = self.game.move(payload.get("move", ""))
        except (json.JSONDecodeError, AttributeError, ValueError) as error:
            self._json(handler, HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        state = snapshot.as_dict()
        state["active"] = self.is_active()
        self._json(handler, HTTPStatus.OK, state)

    def _authorized(self, query: str) -> bool:
        supplied = parse_qs(query).get("token", [""])[0]
        return hmac.compare_digest(supplied, self._token)

    @staticmethod
    def _json(
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: dict[str, object],
    ) -> None:
        content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ChessCompanion._respond(handler, status, content, "application/json")

    @staticmethod
    def _respond(
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        content: bytes,
        content_type: str,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        handler.end_headers()
        handler.wfile.write(content)
