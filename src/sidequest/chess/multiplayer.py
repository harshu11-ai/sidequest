"""Client-side orchestration for multiplayer chess.

Unlike `ComputerChessGame`, the relay is authoritative for board state: this
class mirrors whatever it returns, caches it for the local companion server
to hand to the browser, and keeps a background thread polling so an
opponent's move shows up without the browser ever talking to the relay
directly.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sidequest.chess.relay_client import RelayClient, RelayError, RoomSeat

_POLL_INTERVAL_SECONDS = 1.5


class MultiplayerError(ValueError):
    """Raised when a move or setup step is rejected locally or by the relay."""


def default_multiplayer_state_path() -> Path:
    """Return the per-user path used to resume a room between sessions."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "sidequest" / "multiplayer.json"


@dataclass(frozen=True, slots=True)
class MultiplayerSnapshot:
    fen: str
    legal_moves: tuple[str, ...]
    status: str
    turn: str
    last_move: str | None
    you: str
    your_turn: bool
    room_code: str
    room_status: str
    result: str | None
    opponent_connected: bool
    engine: str = "Human opponent"

    def as_dict(self) -> dict[str, object]:
        return {
            "fen": self.fen,
            "legal_moves": list(self.legal_moves),
            "status": self.status,
            "turn": self.turn,
            "last_move": self.last_move,
            "you": self.you,
            "your_turn": self.your_turn,
            "room_code": self.room_code,
            "room_status": self.room_status,
            "result": self.result,
            "opponent_connected": self.opponent_connected,
            "engine": self.engine,
            "difficulty": None,
        }


def _snapshot_from_relay(body: dict[str, object]) -> MultiplayerSnapshot:
    return MultiplayerSnapshot(
        fen=body["fen"],
        legal_moves=tuple(body["legal_moves"]),
        status=body["status"],
        turn=body["turn"],
        last_move=body.get("last_move"),
        you=body["you"],
        your_turn=bool(body["your_turn"]),
        room_code=body["code"],
        room_status=body["room_status"],
        result=body.get("result"),
        opponent_connected=bool(body["opponent_connected"]),
    )


class RemoteChessGame:
    """Owns one multiplayer room and a locally cached, background-polled mirror of it."""

    def __init__(
        self,
        relay: RelayClient,
        state_path: Path | None = None,
        *,
        code: str | None = None,
    ) -> None:
        self.relay = relay
        self.state_path = state_path or default_multiplayer_state_path()
        self._lock = threading.Lock()
        self._move_count = 0
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

        try:
            self._seat = self._resolve_seat(code)
            self._save_seat(self._seat)
            initial = self.relay.get_state(self._seat.code, self._seat.token)
        except RelayError as error:
            raise MultiplayerError(str(error)) from error
        self._snapshot = _snapshot_from_relay(initial)
        self._move_count = int(initial.get("move_count", 0))

    @property
    def room_code(self) -> str:
        return self._seat.code

    def snapshot(self) -> MultiplayerSnapshot:
        with self._lock:
            return self._snapshot

    def move(self, move: str) -> MultiplayerSnapshot:
        with self._lock:
            expected = self._move_count
            seat = self._seat
        try:
            body = self.relay.submit_move(seat.code, seat.token, move.strip().lower(), expected)
        except RelayError as error:
            # Sync up in case the rejection was caused by us being out of
            # date (the opponent moved since our last poll) -- the browser's
            # next state read should show the real position, not a stale one.
            self._fetch_and_cache()
            raise MultiplayerError(str(error)) from error
        return self._cache(body)

    def poll(self) -> MultiplayerSnapshot:
        return self._fetch_and_cache()

    def start_polling(self, interval: float = _POLL_INTERVAL_SECONDS) -> None:
        if self._poll_thread is not None:
            return
        # A prior stop_polling() leaves this set; clear it or the loop below
        # would see it as already-signalled and exit before its first tick,
        # silently turning every restart after the first stop into a no-op.
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval):
                with suppress(RelayError):
                    self._fetch_and_cache()

        self._poll_thread = threading.Thread(
            target=_loop, name="sidequest-multiplayer-poll", daemon=True
        )
        self._poll_thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2)
            self._poll_thread = None

    def _resolve_seat(self, code: str | None) -> RoomSeat:
        # Hosting (no code) always mints a fresh room -- no silent resume.
        # To get back into a game, host or guest, use --join with the code
        # you were given/shared; that's the one path that resumes a seat.
        if not code:
            return self.relay.create_room()

        normalized_code = code.strip().upper()
        saved = self._load_seat()
        if saved is not None and saved.code == normalized_code:
            with suppress(RelayError):
                body = self.relay.get_state(saved.code, saved.token)
                if body.get("room_status") != "finished":
                    return saved
        return self.relay.join_room(normalized_code)

    def _load_seat(self) -> RoomSeat | None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return RoomSeat(code=data["code"], token=data["token"], you=data["you"])
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def _save_seat(self, seat: RoomSeat) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"code": seat.code, "token": seat.token, "you": seat.you},
            separators=(",", ":"),
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=".multiplayer-",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)

    def _fetch_and_cache(self) -> MultiplayerSnapshot:
        with self._lock:
            seat = self._seat
        try:
            body = self.relay.get_state(seat.code, seat.token)
        except RelayError:
            with self._lock:
                return self._snapshot
        return self._cache(body)

    def _cache(self, body: dict[str, object]) -> MultiplayerSnapshot:
        snapshot = _snapshot_from_relay(body)
        with self._lock:
            self._snapshot = snapshot
            self._move_count = int(body.get("move_count", self._move_count))
        return snapshot
