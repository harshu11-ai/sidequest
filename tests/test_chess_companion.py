import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from sidequest.chess_companion import ChessCompanion, CompanionWindow
from sidequest.chess_game import ComputerChessGame
from sidequest.multiplayer_chess import MultiplayerSnapshot


class ChessCompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        game = ComputerChessGame(
            Path(self.temporary_directory.name) / "game.json",
            stockfish_path="",
        )
        self.opened_urls: list[str] = []
        self.close_count = 0
        self.companion = ChessCompanion(
            game,
            browser_open=self.opened_urls.append,
            browser_close=self._record_close,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def _record_close(self) -> None:
        self.close_count += 1

    def test_show_is_idempotent_until_hidden(self) -> None:
        self.companion.show()
        self.companion.show()
        self.assertEqual(self.opened_urls, [self.companion.play_url])
        self.companion.hide()
        self.assertEqual(self.close_count, 1)
        self.companion.show()
        self.assertEqual(len(self.opened_urls), 2)

    def test_state_endpoint_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.companion.base_url}/api/state", timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_move_endpoint_returns_updated_game(self) -> None:
        self.companion.show()
        request = urllib.request.Request(
            self.companion.play_url.replace("/?", "/api/move?"),
            data=json.dumps({"move": "e2e4"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["turn"], "white")
        self.assertTrue(payload["active"])
        self.assertIsNotNone(payload["player_fen"])
        self.assertIsNotNone(payload["computer_move"])

    def test_serves_bundled_chessboard_assets(self) -> None:
        for path, content_type in (
            ("/cm-chessboard.js", "text/javascript"),
            ("/cm-chessboard.css", "text/css"),
            ("/cm-standard.svg", "image/svg+xml"),
        ):
            url = f"{self.companion.base_url}{path}"
            with self.subTest(path=path), urllib.request.urlopen(url, timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertTrue(response.read(20))

    def test_lifecycle_endpoint_changes_visibility(self) -> None:
        start = urllib.request.Request(
            self.companion.lifecycle_url("start"), data=b"{}", method="POST"
        )
        stop = urllib.request.Request(
            self.companion.lifecycle_url("stop"), data=b"{}", method="POST"
        )
        urllib.request.urlopen(start, timeout=2).close()
        self.assertTrue(self.companion.is_active())
        urllib.request.urlopen(stop, timeout=2).close()
        self.assertFalse(self.companion.is_active())

    def test_difficulty_endpoint_updates_game(self) -> None:
        request = urllib.request.Request(
            self.companion.play_url.replace("/?", "/api/difficulty?"),
            data=json.dumps({"difficulty": "hard"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["difficulty"], "hard")


def _multiplayer_snapshot(**overrides) -> MultiplayerSnapshot:
    fields = {
        "fen": "startpos",
        "legal_moves": ("e2e4",),
        "status": "In progress",
        "turn": "white",
        "last_move": None,
        "you": "white",
        "your_turn": True,
        "room_code": "ABC123",
        "room_status": "in_progress",
        "result": None,
        "opponent_connected": True,
    }
    fields.update(overrides)
    return MultiplayerSnapshot(**fields)


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


class ChessCompanionMultiplayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        game = ComputerChessGame(
            Path(self.temporary_directory.name) / "game.json",
            stockfish_path="",
        )
        self.multiplayer = Mock()
        self.multiplayer.snapshot.return_value = _multiplayer_snapshot()
        self.companion = ChessCompanion(
            game,
            multiplayer=self.multiplayer,
            browser_open=lambda url: None,
            browser_close=lambda: None,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def _url(self, path: str) -> str:
        return self.companion.play_url.replace("/?", f"{path}?")

    def test_starts_and_stops_background_polling(self) -> None:
        self.multiplayer.start_polling.assert_called_once_with()
        self.companion.close()
        self.multiplayer.stop_polling.assert_called_once_with()

    def test_state_defaults_to_multiplayer_mode_with_room_metadata(self) -> None:
        with urllib.request.urlopen(self._url("/api/state"), timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["mode"], "multiplayer")
        self.assertEqual(payload["fen"], "startpos")
        self.assertEqual(payload["multiplayer"]["room_code"], "ABC123")
        self.assertTrue(payload["multiplayer"]["opponent_connected"])

    def test_move_dispatches_to_multiplayer_game(self) -> None:
        self.multiplayer.move.return_value = _multiplayer_snapshot(
            turn="black", your_turn=False, last_move="e2e4"
        )
        payload = _post(self._url("/api/move"), {"move": "e2e4"})
        self.multiplayer.move.assert_called_once_with("e2e4")
        self.assertEqual(payload["last_move"], "e2e4")

    def test_new_game_is_rejected_in_multiplayer_mode(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/new"), {})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_difficulty_is_rejected_in_multiplayer_mode(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/difficulty"), {"difficulty": "hard"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_mode_can_switch_to_practice_and_back(self) -> None:
        practice_payload = _post(self._url("/api/mode"), {"mode": "practice"})
        self.assertEqual(practice_payload["mode"], "practice")
        self.assertEqual(practice_payload["engine"], "Sidequest practice bot")
        # Room metadata stays visible even while looking at the practice board.
        self.assertEqual(practice_payload["multiplayer"]["room_code"], "ABC123")

        multiplayer_payload = _post(self._url("/api/mode"), {"mode": "multiplayer"})
        self.assertEqual(multiplayer_payload["mode"], "multiplayer")
        self.assertEqual(multiplayer_payload["fen"], "startpos")

    def test_mode_rejects_unknown_value(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/mode"), {"mode": "spectator"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()


class ChessCompanionModeDisabledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        game = ComputerChessGame(
            Path(self.temporary_directory.name) / "game.json",
            stockfish_path="",
        )
        self.companion = ChessCompanion(
            game, browser_open=lambda url: None, browser_close=lambda: None
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def test_mode_endpoint_requires_multiplayer_to_be_enabled(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self.companion.play_url.replace("/?", "/api/mode?"), {"mode": "practice"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_state_has_no_multiplayer_key_when_disabled(self) -> None:
        with urllib.request.urlopen(
            self.companion.play_url.replace("/?", "/api/state?"), timeout=2
        ) as response:
            payload = json.load(response)
        self.assertNotIn("multiplayer", payload)
        self.assertEqual(payload["mode"], "practice")


class CompanionWindowTests(unittest.TestCase):
    @patch("sidequest.chess_companion.subprocess.Popen")
    @patch("sidequest.chess_companion._find_chromium", return_value="/browser")
    def test_owned_browser_process_is_terminated(self, _find, popen) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        window = CompanionWindow()
        try:
            window.open("http://127.0.0.1:1234/")
            window.hide()
        finally:
            window.close()

        popen.assert_called_once()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
