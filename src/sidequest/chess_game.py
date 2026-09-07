"""Persistent local chess game and computer opponents."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from Chessnut import Game

_STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
_PIECE_VALUES = {"p": 100, "n": 320, "b": 330, "r": 500, "q": 900, "k": 20_000}
_DIFFICULTIES = {"easy", "medium", "hard"}
_STOCKFISH_SKILL = {"easy": 3, "medium": 10, "hard": 18}
_THINK_TIME_FACTOR = {"easy": 0.5, "medium": 1.0, "hard": 2.5}
_STATUS_NAMES = {
    Game.NORMAL: "Your move",
    Game.CHECK: "Check",
    Game.CHECKMATE: "Checkmate",
    Game.STALEMATE: "Stalemate",
}


class InvalidChessMove(ValueError):
    """Raised when the browser submits an illegal move."""


def default_chess_state_path() -> Path:
    """Return the per-user path used to resume a game between sessions."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "sidequest" / "chess.json"


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    fen: str
    legal_moves: tuple[str, ...]
    status: str
    turn: str
    last_move: str | None
    engine: str
    difficulty: str
    player_fen: str | None = None
    computer_move: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "fen": self.fen,
            "legal_moves": list(self.legal_moves),
            "status": self.status,
            "turn": self.turn,
            "last_move": self.last_move,
            "engine": self.engine,
            "difficulty": self.difficulty,
            "player_fen": self.player_fen,
            "computer_move": self.computer_move,
        }


class ComputerChessGame:
    """A resumable game where the user plays white against a local engine."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        stockfish_path: str | None = None,
        move_time_ms: int = 250,
    ) -> None:
        self.state_path = state_path or default_chess_state_path()
        self.stockfish_path = (
            shutil.which("stockfish") if stockfish_path is None else stockfish_path
        )
        self.move_time_ms = move_time_ms
        self._difficulty = "medium"
        self._lock = Lock()
        self._last_move: str | None = None
        self._game = self._load()

    @property
    def engine_name(self) -> str:
        return "Stockfish" if self.stockfish_path else "Sidequest practice bot"

    def snapshot(self) -> GameSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def move(self, move: str) -> GameSnapshot:
        with self._lock:
            legal_moves = self._game.get_moves()
            normalized = move.strip().lower()
            if self._game.state.player != "w":
                raise InvalidChessMove("wait for the computer to move")
            if normalized not in legal_moves:
                raise InvalidChessMove(f"illegal move: {move}")

            self._game.apply_move(normalized)
            self._last_move = normalized
            player_fen = self._game.get_fen()
            reply = None
            replies = self._game.get_moves()
            if replies:
                reply = self._choose_computer_move(replies)
                self._game.apply_move(reply)
                self._last_move = reply
            self._save()
            return self._snapshot_unlocked(
                player_fen=player_fen,
                computer_move=reply,
            )

    def new_game(self) -> GameSnapshot:
        with self._lock:
            self._game = Game(_STARTING_FEN)
            self._last_move = None
            self._save()
            return self._snapshot_unlocked()

    def set_difficulty(self, difficulty: str) -> GameSnapshot:
        normalized = difficulty.strip().lower()
        if normalized not in _DIFFICULTIES:
            raise ValueError(f"unsupported difficulty: {difficulty}")
        with self._lock:
            self._difficulty = normalized
            self._save()
            return self._snapshot_unlocked()

    def _snapshot_unlocked(
        self,
        *,
        player_fen: str | None = None,
        computer_move: str | None = None,
    ) -> GameSnapshot:
        legal_moves = tuple(self._game.get_moves())
        status = _STATUS_NAMES.get(self._game.status, "Game over")
        if self._game.status == Game.NORMAL and self._game.state.player == "b":
            status = "Computer thinking"
        return GameSnapshot(
            fen=self._game.get_fen(),
            legal_moves=legal_moves,
            status=status,
            turn="white" if self._game.state.player == "w" else "black",
            last_move=self._last_move,
            engine=self.engine_name,
            difficulty=self._difficulty,
            player_fen=player_fen,
            computer_move=computer_move,
        )

    def _choose_computer_move(self, legal_moves: list[str]) -> str:
        if self.stockfish_path:
            move = _stockfish_move(
                self.stockfish_path,
                self._game.get_fen(),
                self.move_time_ms,
                self._difficulty,
            )
            if move in legal_moves:
                return move
        return _practice_bot_move(self._game.get_fen(), legal_moves, self._difficulty)

    def _load(self) -> Game:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            game = Game(data["fen"])
            last_move = data.get("last_move")
            self._last_move = last_move if isinstance(last_move, str) else None
            difficulty = data.get("difficulty", "medium")
            self._difficulty = difficulty if difficulty in _DIFFICULTIES else "medium"
            return game
        except (OSError, IndexError, KeyError, TypeError, ValueError):
            return Game(_STARTING_FEN)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "fen": self._game.get_fen(),
                "last_move": self._last_move,
                "difficulty": self._difficulty,
            },
            separators=(",", ":"),
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=".chess-",
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


def _stockfish_move(
    executable: str,
    fen: str,
    move_time_ms: int,
    difficulty: str = "medium",
) -> str | None:
    skill = _STOCKFISH_SKILL[difficulty]
    think_time = max(50, round(move_time_ms * _THINK_TIME_FACTOR[difficulty]))
    commands = (
        f"uci\nsetoption name Skill Level value {skill}\nisready\n"
        f"position fen {fen}\ngo movetime {think_time}\nquit\n"
    )
    try:
        completed = subprocess.run(
            [executable],
            input=commands,
            text=True,
            capture_output=True,
            timeout=max(3.0, think_time / 1000 + 2.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("bestmove "):
            move = line.split(maxsplit=2)[1]
            return None if move == "(none)" else move
    return None


def _practice_bot_move(fen: str, legal_moves: list[str], difficulty: str) -> str:
    """Choose a deterministic move at one of three lightweight strength levels."""
    if difficulty == "easy":
        return min(legal_moves)
    best_move = legal_moves[0]
    best_score: int | None = None
    for move in legal_moves:
        candidate = Game(fen)
        candidate.apply_move(move)
        score = _material_score(candidate)
        if difficulty == "hard":
            replies = candidate.get_moves()
            if replies:
                score = max(_score_after_move(candidate, reply) for reply in replies)
        if best_score is None or score < best_score or (score == best_score and move < best_move):
            best_move = move
            best_score = score
    return best_move


def _score_after_move(game: Game, move: str) -> int:
    candidate = Game(game.get_fen())
    candidate.apply_move(move)
    return _material_score(candidate)


def _material_score(game: Game) -> int:
    score = 0
    for piece in str(game.board):
        if piece.lower() not in _PIECE_VALUES:
            continue
        value = _PIECE_VALUES[piece.lower()]
        score += value if piece.isupper() else -value
    return score
