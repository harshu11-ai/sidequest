import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from sidequest.chess_companion import ChessCompanion, CompanionWindow
from sidequest.chess_game import ComputerChessGame


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
