import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from sidequest.breaks.companion import BreaksCompanion
from sidequest.chess.game import ComputerChessGame
from sidequest.video.queue import VideoQueue


def _entry(letter: str, duration_s: int) -> dict[str, object]:
    return {
        "id": letter,
        "title": f"Video {letter.upper()}",
        "channel": f"Chan {letter.upper()}",
        "youtube_id": letter * 11,
        "duration_s": duration_s,
    }


_CATALOG = [_entry("a", 100), _entry("b", 200)]


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


class BreaksCompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        # A no-op shuffle keeps the video draw order deterministic (catalog
        # order), same as test_video_queue.py/the old test_video_companion.py.
        self.shuffle_patcher = patch("sidequest.video.queue.random.shuffle")
        self.shuffle_patcher.start()
        self.temporary_directory = tempfile.TemporaryDirectory()
        chess_game = ComputerChessGame(
            Path(self.temporary_directory.name) / "chess.json", stockfish_path=""
        )
        video_queue = VideoQueue(
            Path(self.temporary_directory.name) / "video.json", catalog=_CATALOG
        )
        self.opened_urls: list[str] = []
        self.close_count = 0
        self.companion = BreaksCompanion(
            chess_game,
            video_queue,
            browser_open=self.opened_urls.append,
            browser_close=self._record_close,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()
        self.shuffle_patcher.stop()

    def _record_close(self) -> None:
        self.close_count += 1

    def _url(self, path: str) -> str:
        return self.companion.play_url.replace("/?", f"{path}?")

    def _state(self) -> dict:
        with urllib.request.urlopen(self._url("/api/breaks/state"), timeout=2) as response:
            return json.load(response)

    # -- toggles / random pick -------------------------------------------

    def test_neither_toggled_still_opens_a_window(self) -> None:
        self.companion.show()
        self.assertEqual(self.opened_urls, [self.companion.play_url])
        self.assertEqual(self._state()["mode"], "off")

    def test_toggle_endpoint_flips_state(self) -> None:
        payload = _post(self._url("/api/breaks/toggle"), {"mode": "chess", "on": True})
        self.assertEqual(payload["toggles"], {"chess": True, "video": False})

    def test_toggle_rejects_unknown_mode(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/breaks/toggle"), {"mode": "poker", "on": True})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_toggling_chess_only_opens_chess_mode(self) -> None:
        _post(self._url("/api/breaks/toggle"), {"mode": "chess", "on": True})
        self.companion.show()
        self.assertEqual(self._state()["mode"], "chess")

    def test_toggling_video_only_opens_video_mode(self) -> None:
        _post(self._url("/api/breaks/toggle"), {"mode": "video", "on": True})
        self.companion.show()
        self.assertEqual(self._state()["mode"], "video")

    def test_both_toggled_picks_randomly_between_them(self) -> None:
        _post(self._url("/api/breaks/toggle"), {"mode": "chess", "on": True})
        _post(self._url("/api/breaks/toggle"), {"mode": "video", "on": True})
        with patch(
            "sidequest.breaks.companion.random.choice", return_value="video"
        ) as choice:
            self.companion.show()
        choice.assert_called_once_with(["chess", "video"])
        self.assertEqual(self._state()["mode"], "video")

    # -- settings ----------------------------------------------------------

    def test_settings_endpoint_updates_difficulty(self) -> None:
        payload = _post(self._url("/api/breaks/settings"), {"difficulty": "hard"})
        self.assertEqual(payload["settings"]["difficulty"], "hard")

    def test_settings_endpoint_rejects_bad_difficulty(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/breaks/settings"), {"difficulty": "nightmare"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_settings_endpoint_rejects_missing_stockfish_path(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/breaks/settings"), {"stockfish_path": "/does/not/exist"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_stockfish_path_locks_after_the_first_move(self) -> None:
        _post(self._url("/api/chess/move"), {"move": "e2e4"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/breaks/settings"), {"stockfish_path": "/bin/echo"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_clearing_stockfish_path_falls_back_to_auto_detect(self) -> None:
        payload = _post(self._url("/api/breaks/settings"), {"stockfish_path": ""})
        self.assertIsNone(payload["settings"]["stockfish_path"])

    # -- chess passthrough ---------------------------------------------

    def test_move_endpoint_returns_updated_game(self) -> None:
        payload = _post(self._url("/api/chess/move"), {"move": "e2e4"})
        self.assertEqual(payload["turn"], "white")
        self.assertIsNotNone(payload["computer_move"])

    def test_new_game_endpoint_resets_the_board(self) -> None:
        _post(self._url("/api/chess/move"), {"move": "e2e4"})
        payload = _post(self._url("/api/chess/new"), {})
        self.assertIsNone(payload["last_move"])

    def test_difficulty_endpoint_updates_game(self) -> None:
        payload = _post(self._url("/api/chess/difficulty"), {"difficulty": "hard"})
        self.assertEqual(payload["difficulty"], "hard")

    # -- video passthrough ------------------------------------------------

    def test_video_state_returns_current_video_and_trimmed_upcoming(self) -> None:
        with urllib.request.urlopen(self._url("/api/video/state"), timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["video"]["id"], "a")
        self.assertEqual([entry["id"] for entry in payload["upcoming"]], ["b"])
        self.assertEqual(set(payload["upcoming"][0]), {"id", "title", "channel", "duration_s"})

    def test_skip_endpoint_advances_the_queue(self) -> None:
        payload = _post(self._url("/api/video/skip"), {})
        self.assertEqual(payload["video"]["id"], "b")
        self.assertEqual(payload["watched"], ["a"])

    def test_previous_endpoint_returns_to_the_prior_video(self) -> None:
        _post(self._url("/api/video/skip"), {})
        payload = _post(self._url("/api/video/previous"), {})
        self.assertEqual(payload["video"]["id"], "a")

    def test_position_endpoint_rejects_bad_value(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/video/position"), {"position_s": "nope"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    # -- lifecycle / page serving -------------------------------------------

    def test_lifecycle_start_and_stop_toggle_visibility(self) -> None:
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
        self.assertEqual(self.close_count, 1)

    def test_breaks_state_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.companion.base_url}/api/breaks/state", timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_default_page_is_the_off_panel(self) -> None:
        with urllib.request.urlopen(self.companion.base_url + "/", timeout=2) as response:
            body = response.read()
        self.assertIn(b"Sidequest Breaks", body)

    def test_serves_the_page_for_the_active_mode(self) -> None:
        _post(self._url("/api/breaks/toggle"), {"mode": "video", "on": True})
        self.companion.show()
        with urllib.request.urlopen(self.companion.base_url + "/", timeout=2) as response:
            body = response.read()
        self.assertIn(b"Sidequest Videos", body)

    def test_serves_bundled_chess_assets(self) -> None:
        for path, content_type in (
            ("/chess/app.js", "text/javascript"),
            ("/chess/cm-chessboard.js", "text/javascript"),
            ("/chess/cm-standard.svg", "image/svg+xml"),
        ):
            url = f"{self.companion.base_url}{path}"
            with self.subTest(path=path), urllib.request.urlopen(url, timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertTrue(response.read(20))

    def test_serves_bundled_video_assets(self) -> None:
        url = f"{self.companion.base_url}/video/app.js"
        with urllib.request.urlopen(url, timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertTrue(response.read(20))

    def test_serves_panel_assets(self) -> None:
        for path, content_type in (
            ("/breaks/panel.css", "text/css"),
            ("/breaks/panel.js", "text/javascript"),
        ):
            url = f"{self.companion.base_url}{path}"
            with self.subTest(path=path), urllib.request.urlopen(url, timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertTrue(response.read(20))


class BreaksCompanionWindowSizingTests(unittest.TestCase):
    """Real (uninjected) window path -- verifies the per-mode window sizes."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        chess_game = ComputerChessGame(
            Path(self.temporary_directory.name) / "chess.json", stockfish_path=""
        )
        video_queue = VideoQueue(
            Path(self.temporary_directory.name) / "video.json", catalog=_CATALOG
        )
        self.companion = BreaksCompanion(chess_game, video_queue)

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def _toggle(self, mode: str, on: bool) -> None:
        url = self.companion.play_url.replace("/?", "/api/breaks/toggle?")
        _post(url, {"mode": mode, "on": on})

    def test_off_mode_opens_a_small_window(self) -> None:
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()
        window_type.assert_called_once_with(width=340, height=480)
        window_type.return_value.open.assert_called_once_with(self.companion.play_url)

    def test_chess_mode_opens_the_chess_sized_window(self) -> None:
        self._toggle("chess", True)
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()
        window_type.assert_called_once_with(width=768, height=650)

    def test_video_mode_opens_the_video_sized_window(self) -> None:
        self._toggle("video", True)
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()
        window_type.assert_called_once_with(width=1000, height=650)

    def test_window_is_reused_across_turns_for_the_same_mode(self) -> None:
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()
            self.companion.hide()
            self.companion.show()
        window_type.assert_called_once_with(width=340, height=480)
        self.assertEqual(window_type.return_value.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
