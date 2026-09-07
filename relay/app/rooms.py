"""Room lifecycle and chess move validation.

Mirrors the snapshot shape `sidequest.chess_game.ComputerChessGame` already
produces for the local solo game, so the client-side integration can reuse
the same board-rendering code for both modes.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import replace

from Chessnut import Game

from .store import Room

_STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# No 0/O/1/I/L: those are easy to mis-type or mis-read when a code is read
# aloud or typed from a chat message.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_PRESENCE_TIMEOUT_SECONDS = 10
_STATUS_NAMES = {
    Game.NORMAL: "In progress",
    Game.CHECK: "Check",
    Game.CHECKMATE: "Checkmate",
    Game.STALEMATE: "Stalemate",
}


class RoomError(ValueError):
    """A room operation was rejected; `status_code` says how to report it."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def generate_room_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def new_room() -> Room:
    now = time.time()
    return Room(
        code=generate_room_code(),
        white_token=generate_token(),
        black_token=None,
        fen=_STARTING_FEN,
        move_history=(),
        result=None,
        created_at=now,
        updated_at=now,
        white_last_seen=now,
        black_last_seen=None,
    )


def join_room(room: Room) -> Room:
    if room.black_token is not None:
        raise RoomError("room already has two players", 409)
    now = time.time()
    return replace(room, black_token=generate_token(), updated_at=now, black_last_seen=now)


def seat_for_token(room: Room, token: str) -> str:
    if secrets.compare_digest(token, room.white_token):
        return "white"
    if room.black_token and secrets.compare_digest(token, room.black_token):
        return "black"
    raise RoomError("invalid token", 401)


def touch_presence(room: Room, seat: str) -> Room:
    now = time.time()
    if seat == "white":
        return replace(room, white_last_seen=now)
    return replace(room, black_last_seen=now)


def apply_move(room: Room, seat: str, move: str, expected_move_count: int) -> Room:
    if room.result is not None:
        raise RoomError("game is finished", 409)
    if room.black_token is None:
        raise RoomError("waiting for an opponent to join", 409)
    if len(room.move_history) != expected_move_count:
        raise RoomError("stale move: the game has moved on, refresh and retry", 409)

    turn = "white" if len(room.move_history) % 2 == 0 else "black"
    if seat != turn:
        raise RoomError("it is not your turn", 409)

    game = Game(room.fen)
    normalized = move.strip().lower()
    if normalized not in game.get_moves():
        raise RoomError(f"illegal move: {move}", 400)
    game.apply_move(normalized)

    result = None
    if game.status == Game.CHECKMATE:
        result = seat
    elif game.status == Game.STALEMATE:
        result = "draw"

    now = time.time()
    last_seen_field = f"{seat}_last_seen"
    return replace(
        room,
        fen=game.get_fen(),
        move_history=(*room.move_history, normalized),
        result=result,
        updated_at=now,
        **{last_seen_field: now},
    )


def snapshot(room: Room, seat: str) -> dict[str, object]:
    game = Game(room.fen)
    turn = "white" if game.state.player == "w" else "black"
    status = _STATUS_NAMES.get(game.status, "Game over")
    if room.status == "waiting_for_opponent":
        status = "Waiting for opponent to join"

    now = time.time()
    other_seen = room.black_last_seen if seat == "white" else room.white_last_seen
    opponent_connected = bool(other_seen) and (now - other_seen) < _PRESENCE_TIMEOUT_SECONDS

    return {
        "code": room.code,
        "you": seat,
        "fen": room.fen,
        "legal_moves": game.get_moves(),
        "turn": turn,
        "your_turn": turn == seat,
        "status": status,
        "result": room.result,
        "last_move": room.move_history[-1] if room.move_history else None,
        "move_count": len(room.move_history),
        "room_status": room.status,
        "opponent_connected": opponent_connected,
    }
