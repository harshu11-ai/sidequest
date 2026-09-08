"""Loopback web companion shown while an agent turn is running (video sidequest)."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from sidequest.browser_window import CompanionWindow
from sidequest.video.queue import VideoQueue

_MAX_REQUEST_BYTES = 4096
_ASSET_TYPES = {
    "/": ("video.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
# The YouTube IFrame Player API needs its loader script from youtube.com and
# plays back (with YouTube's own full control bar -- seek, captions,
# fullscreen, ...) through a youtube-nocookie.com frame; everything else on
# this page -- markup, our own API calls -- stays same-origin like chess's
# board.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://www.youtube.com https://s.ytimg.com; "
    "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
    "img-src 'self' https://i.ytimg.com https://yt3.ggpht.com; "
    "frame-ancestors 'none'"
)
# Fields shipped for each "up next" entry -- deliberately not the whole
# catalog record (no need to ship views/category over the wire).
_UPCOMING_FIELDS = ("id", "title", "channel", "duration_s")
# Wider than chess's default window -- there's a 16:9 video plus a queue
# sidebar to fit, not a square board.
_WINDOW_WIDTH = 1000
_WINDOW_HEIGHT = 650


class VideoCompanion:
    """Own a private local HTTP server and the current video-queue state."""

    def __init__(
        self,
        queue: VideoQueue | None = None,
        *,
        browser_open: Any | None = None,
        browser_close: Any | None = None,
    ) -> None:
        self.queue = queue or VideoQueue()
        self._catalog_by_id = {str(entry["id"]): entry for entry in self.queue.catalog}
        self._window = (
            CompanionWindow(width=_WINDOW_WIDTH, height=_WINDOW_HEIGHT)
            if browser_open is None
            else None
        )
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
            name="sidequest-video-server",
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
            content = files("sidequest.video").joinpath("web", asset_name).read_bytes()
            self._respond(handler, HTTPStatus.OK, content, content_type)
            return
        if parsed.path == "/api/state":
            if not self._authorized(parsed.query):
                self._json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            state = self._augment(self.queue.snapshot().as_dict())
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
        if parsed.path not in {"/api/position", "/api/skip", "/api/previous"}:
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
            if parsed.path == "/api/position":
                snapshot_dict = self.queue.record_position(payload.get("position_s", 0)).as_dict()
            elif parsed.path == "/api/skip":
                snapshot_dict = self.queue.skip().as_dict()
            else:
                snapshot_dict = self.queue.previous().as_dict()
        except (json.JSONDecodeError, AttributeError, ValueError, TypeError) as error:
            self._json(handler, HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(handler, HTTPStatus.OK, self._augment(snapshot_dict))

    def _augment(self, state: dict[str, object]) -> dict[str, object]:
        state["active"] = self.is_active()
        state["upcoming"] = [self._trim(video_id) for video_id in state["upcoming"]]
        return state

    def _trim(self, video_id: str) -> dict[str, object]:
        entry = self._catalog_by_id.get(video_id, {})
        return {field: entry.get(field) for field in _UPCOMING_FIELDS}

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
        VideoCompanion._respond(handler, status, content, "application/json")

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
        handler.send_header("Content-Security-Policy", _CSP)
        handler.end_headers()
        handler.wfile.write(content)
