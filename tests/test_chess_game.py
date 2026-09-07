import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequest.chess_game import (
    ComputerChessGame,
    InvalidChessMove,
    _stockfish_move,
)


class ComputerChessGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "chess.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_applies_user_move_and_computer_reply(self) -> None:
        game = ComputerChessGame(self.state_path, stockfish_path="")

        snapshot = game.move("e2e4")

        self.assertEqual(snapshot.turn, "white")
        self.assertIsNotNone(snapshot.player_fen)
        self.assertIsNotNone(snapshot.computer_move)
        self.assertNotEqual(snapshot.player_fen, snapshot.fen)
        self.assertNotEqual(snapshot.fen.split()[0], ComputerChessGame(
            self.state_path.with_name("unused.json"), stockfish_path=""
        ).snapshot().fen.split()[0])
        self.assertTrue(self.state_path.exists())

    def test_rejects_illegal_move(self) -> None:
        game = ComputerChessGame(self.state_path, stockfish_path="")
        with self.assertRaisesRegex(InvalidChessMove, "illegal move"):
            game.move("e2e5")

    def test_persists_and_restores_position(self) -> None:
        first = ComputerChessGame(self.state_path, stockfish_path="")
        first.set_difficulty("hard")
        expected = first.move("d2d4")

        restored = ComputerChessGame(self.state_path, stockfish_path="").snapshot()

        self.assertEqual(restored.fen, expected.fen)
        self.assertEqual(restored.last_move, expected.last_move)
        self.assertEqual(restored.difficulty, "hard")

    def test_rejects_unknown_difficulty(self) -> None:
        game = ComputerChessGame(self.state_path, stockfish_path="")
        with self.assertRaisesRegex(ValueError, "unsupported difficulty"):
            game.set_difficulty("impossible")

    def test_invalid_saved_state_starts_a_new_game(self) -> None:
        self.state_path.write_text(json.dumps({"fen": "not a fen"}), encoding="utf-8")
        snapshot = ComputerChessGame(self.state_path, stockfish_path="").snapshot()
        self.assertTrue(snapshot.fen.startswith("rnbqkbnr/pppppppp"))

    @patch("sidequest.chess_game.subprocess.run")
    def test_reads_stockfish_best_move(self, run) -> None:
        run.return_value.stdout = "uciok\nreadyok\nbestmove e7e5 ponder g1f3\n"
        self.assertEqual(_stockfish_move("stockfish", "example fen", 100, "hard"), "e7e5")
        sent_commands = run.call_args.kwargs["input"]
        self.assertIn("setoption name Skill Level value 18", sent_commands)
        self.assertIn("go movetime 250", sent_commands)


if __name__ == "__main__":
    unittest.main()
