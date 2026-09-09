"""Loopback web companion for `sidequest --breaks`.

One window, one origin, one server -- Chess and Video are live toggles
inside it rather than two separate launch-time companions. When a turn
starts, whichever games are toggled on are candidates: one is picked (at
random if both are on) and the window opens showing it; if neither is
toggled on, the window still opens, showing just the toggle panel, so you
can turn something on for next time.

Multiplayer chess is intentionally not wired here -- Chess is solo-only in
this pass; see the plan for the follow-up that moves host/join into the
panel.
"""

from __future__ import annotations

import hmac
import json
import random
import secrets
import shutil
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from sidequest.browser_window import CompanionWindow
from sidequest.chess.game import ComputerChessGame
from sidequest.video.queue import VideoQueue

_MAX_REQUEST_BYTES = 4096
_MODES = ("chess", "video")

# Sized per mode -- a small window for the toggle panel alone, and the same
# footprints chess/video used to launch at on their own before the merge.
_WINDOW_SIZES = {
    "off": (340, 480),
    "chess": (768, 650),
    "video": (1000, 650),
}

_PANEL_ASSETS = {
    "/breaks/panel.css": ("panel.css", "text/css; charset=utf-8"),
    "/breaks/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
}
_MODE_PAGES = {"off": "off.html", "chess": "chess.html", "video": "video.html"}
_CHESS_ASSETS = {
    "/chess/style.css": ("style.css", "text/css; charset=utf-8"),
    "/chess/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/chess/cm-chessboard.js": ("cm-chessboard.js", "text/javascript; charset=utf-8"),
    "/chess/cm-chessboard.css": ("cm-chessboard.css", "text/css; charset=utf-8"),
    "/chess/cm-markers.js": ("cm-markers.js", "text/javascript; charset=utf-8"),
    "/chess/cm-markers.css": ("cm-markers.css", "text/css; charset=utf-8"),
    "/chess/cm-standard.svg": ("cm-standard.svg", "image/svg+xml"),
    "/chess/cm-markers.svg": ("cm-markers.svg", "image/svg+xml"),
}
_VIDEO_ASSETS = {
    "/video/style.css": ("style.css", "text/css; charset=utf-8"),
    "/video/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_UPCOMING_FIELDS = ("id", "title", "channel", "duration_s")
_DEFAULT_CSP = "default-src 'self'; frame-ancestors 'none'"
# Video's page alone needs the YouTube IFrame API and its player frame.
_VIDEO_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://www.youtube.com https://s.ytimg.com; "
    "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
    "img-src 'self' https://i.ytimg.com https://yt3.ggpht.com; "
    "frame-ancestors 'none'"
)


class BreaksCompanion:
    """Own a private local HTTP server and the shared chess/video toggle state."""

    def __init__(
        self,
        chess_game: ComputerChessGame | None = None,
        video_queue: VideoQueue | None = None,
        *,
        browser_open: Any | None = None,
        browser_close: Any | None = None,
    ) -> None:
        self.chess_game = chess_game or ComputerChessGame()
        self.video_queue = video_queue or VideoQueue()
        self._catalog_by_id = {str(entry["id"]): entry for entry in self.video_queue.catalog}
        self._stockfish_path: str | None = None

        self._chess_on = False
        self._video_on = False
        self._active_mode = "off"
        self._settings_lock = threading.Lock()

        self._injected_open = browser_open
        self._injected_close = browser_close
        self._windows: dict[str, CompanionWindow] = {}

        self._token = secrets.token_urlsafe(24)
        self._active = False
        self._active_lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sidequest-breaks-server",
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

    # -- LifecycleCompanion protocol -----------------------------------

    def show(self) -> None:
        with self._active_lock:
            if self._active:
                return
            self._active = True
        with self._settings_lock:
            if self._chess_on and self._video_on:
                self._active_mode = random.choice(list(_MODES))
            elif self._chess_on:
                self._active_mode = "chess"
            elif self._video_on:
                self._active_mode = "video"
            else:
                self._active_mode = "off"
            mode = self._active_mode
        try:
            self._open_window(mode)
        except OSError:
            with self._active_lock:
                self._active = False

    def hide(self) -> None:
        with self._active_lock:
            was_active = self._active
            self._active = False
        if was_active:
            self._close_window()

    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    def close(self) -> None:
        self.hide()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        if self._injected_close is None:
            for window in self._windows.values():
                window.close()

    # -- window management ----------------------------------------------

    def _open_window(self, mode: str) -> None:
        if self._injected_open is not None:
            self._injected_open(self.play_url)
            return
        width, height = _WINDOW_SIZES[mode]
        window = self._windows.get(mode)
        if window is None:
            window = CompanionWindow(width=width, height=height)
            self._windows[mode] = window
        window.open(self.play_url)

    def _close_window(self) -> None:
        if self._injected_close is not None:
            self._injected_close()
            return
        window = self._windows.get(self._active_mode)
        if window is not None:
            window.hide()

    # -- state -----------------------------------------------------------

    def _breaks_state(self) -> dict[str, object]:
        with self._settings_lock:
            chess_on, video_on, mode = self._chess_on, self._video_on, self._active_mode
        return {
            "toggles": {"chess": chess_on, "video": video_on},
            "mode": mode,
            "settings": {
                "difficulty": self.chess_game.snapshot().difficulty,
                "stockfish_path": self._stockfish_path,
            },
            "active": self.is_active(),
        }

    def _set_toggle(self, payload: dict) -> None:
        mode = payload.get("mode")
        on = payload.get("on")
        if mode not in _MODES:
            raise ValueError(f"unsupported mode: {mode!r}")
        if not isinstance(on, bool):
            raise ValueError("'on' must be a boolean")
        with self._settings_lock:
            if mode == "chess":
                self._chess_on = on
            else:
                self._video_on = on

    def _update_settings(self, payload: dict) -> None:
        if "difficulty" in payload:
            self.chess_game.set_difficulty(payload["difficulty"])
        if "stockfish_path" in payload:
            raw = payload["stockfish_path"]
            if raw is not None and not isinstance(raw, str):
                raise ValueError("stockfish_path must be a string or null")
            path = raw.strip() or None if isinstance(raw, str) else None
            if path is not None and shutil.which(path) is None:
                raise ValueError(f"could not find Stockfish executable: {path}")
            if self.chess_game.snapshot().last_move is not None:
                raise ValueError(
                    "Stockfish path can only be changed before the first move"
                )
            self.chess_game = ComputerChessGame(stockfish_path=path)
            self._stockfish_path = path

    # -- HTTP plumbing -----------------------------------------------------

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

        if parsed.path == "/":
            page = _MODE_PAGES[self._active_mode]
            content = files("sidequest.breaks").joinpath("web", page).read_bytes()
            csp = _VIDEO_CSP if self._active_mode == "video" else _DEFAULT_CSP
            self._respond(handler, HTTPStatus.OK, content, "text/html; charset=utf-8", csp)
            return
        if parsed.path in _PANEL_ASSETS:
            asset_name, content_type = _PANEL_ASSETS[parsed.path]
            content = files("sidequest.breaks").joinpath("web", asset_name).read_bytes()
            self._respond(handler, HTTPStatus.OK, content, content_type)
            return
        if parsed.path in _CHESS_ASSETS:
            asset_name, content_type = _CHESS_ASSETS[parsed.path]
            content = files("sidequest.chess").joinpath("web", asset_name).read_bytes()
            self._respond(handler, HTTPStatus.OK, content, content_type)
            return
        if parsed.path in _VIDEO_ASSETS:
            asset_name, content_type = _VIDEO_ASSETS[parsed.path]
            content = files("sidequest.video").joinpath("web", asset_name).read_bytes()
            self._respond(handler, HTTPStatus.OK, content, content_type)
            return

        if not self._authorized(parsed.query):
            if parsed.path.startswith("/api/"):
                self._json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            self._json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if parsed.path == "/api/breaks/state":
            self._json(handler, HTTPStatus.OK, self._breaks_state())
            return
        if parsed.path == "/api/chess/state":
            state = self.chess_game.snapshot().as_dict()
            state["active"] = self.is_active()
            self._json(handler, HTTPStatus.OK, state)
            return
        if parsed.path == "/api/video/state":
            state = self._augment_video(self.video_queue.snapshot().as_dict())
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

        known_paths = {
            "/api/breaks/toggle",
            "/api/breaks/settings",
            "/api/chess/move",
            "/api/chess/new",
            "/api/chess/difficulty",
            "/api/video/position",
            "/api/video/skip",
            "/api/video/previous",
        }
        if parsed.path not in known_paths:
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
            self._json(
                handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"}
            )
            return
        try:
            payload = json.loads(handler.rfile.read(length) or b"{}")
            response = self._dispatch_post(parsed.path, payload)
        except (json.JSONDecodeError, AttributeError, ValueError, TypeError) as error:
            self._json(handler, HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(handler, HTTPStatus.OK, response)

    def _dispatch_post(self, path: str, payload: dict) -> dict[str, object]:
        if path == "/api/breaks/toggle":
            self._set_toggle(payload)
            return self._breaks_state()
        if path == "/api/breaks/settings":
            self._update_settings(payload)
            return self._breaks_state()
        if path == "/api/chess/move":
            state = self.chess_game.move(payload.get("move", "")).as_dict()
            state["active"] = self.is_active()
            return state
        if path == "/api/chess/new":
            state = self.chess_game.new_game().as_dict()
            state["active"] = self.is_active()
            return state
        if path == "/api/chess/difficulty":
            state = self.chess_game.set_difficulty(payload.get("difficulty", "")).as_dict()
            state["active"] = self.is_active()
            return state
        if path == "/api/video/position":
            state = self.video_queue.record_position(payload.get("position_s", 0)).as_dict()
            return self._augment_video(state)
        if path == "/api/video/skip":
            return self._augment_video(self.video_queue.skip().as_dict())
        if path == "/api/video/previous":
            return self._augment_video(self.video_queue.previous().as_dict())
        raise AssertionError(f"unreachable path: {path}")

    def _augment_video(self, state: dict[str, object]) -> dict[str, object]:
        state["active"] = self.is_active()
        state["upcoming"] = [self._trim_video(video_id) for video_id in state["upcoming"]]
        return state

    def _trim_video(self, video_id: str) -> dict[str, object]:
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
        BreaksCompanion._respond(handler, status, content, "application/json")

    @staticmethod
    def _respond(
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        content: bytes,
        content_type: str,
        csp: str = _DEFAULT_CSP,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy", csp)
        handler.end_headers()
        handler.wfile.write(content)
