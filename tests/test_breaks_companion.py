import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from sidequest.breaks.companion import BreaksCompanion
from sidequest.chess.game import ComputerChessGame
from sidequest.chess.multiplayer import MultiplayerError, MultiplayerSnapshot
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

    def test_neither_toggled_still_opens_the_idle_panel(self) -> None:
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
        self.assertEqual(self.opened_urls, [self.companion.play_url])

    def test_toggling_video_only_opens_video_mode(self) -> None:
        _post(self._url("/api/breaks/toggle"), {"mode": "video", "on": True})
        self.companion.show()
        self.assertEqual(self._state()["mode"], "video")
        self.assertEqual(self.opened_urls, [self.companion.play_url])

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

    def test_chess_mode_endpoint_requires_multiplayer_to_be_enabled(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/chess/mode"), {"mode": "practice"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_chess_state_has_no_multiplayer_key_when_disabled(self) -> None:
        with urllib.request.urlopen(self._url("/api/chess/state"), timeout=2) as response:
            payload = json.load(response)
        self.assertNotIn("multiplayer", payload)
        self.assertEqual(payload["mode"], "practice")

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

    # -- lifecycle / idle panel ---------------------------------------------

    def test_lifecycle_start_opens_and_stop_does_not_close_the_idle_panel(self) -> None:
        start = urllib.request.Request(
            self.companion.lifecycle_url("start"), data=b"{}", method="POST"
        )
        stop = urllib.request.Request(
            self.companion.lifecycle_url("stop"), data=b"{}", method="POST"
        )
        urllib.request.urlopen(start, timeout=2).close()
        self.assertTrue(self.companion.is_active())
        self.assertEqual(len(self.opened_urls), 1)

        urllib.request.urlopen(stop, timeout=2).close()
        self.assertFalse(self.companion.is_active())
        self.assertEqual(self.close_count, 0)  # the idle panel is left alone

    def test_idle_panel_is_only_opened_once_across_repeated_turns(self) -> None:
        self.companion.show()
        self.companion.hide()
        self.companion.show()
        self.companion.hide()
        self.assertEqual(self.opened_urls, [self.companion.play_url])
        self.assertEqual(self.close_count, 0)

    def test_toggling_something_on_closes_a_lingering_idle_panel_on_the_next_turn(self) -> None:
        self.companion.open_initial_panel()
        self.assertEqual(len(self.opened_urls), 1)
        _post(self._url("/api/breaks/toggle"), {"mode": "chess", "on": True})

        # The user never clicked "All set" -- just went and submitted a
        # prompt. The real turn should still close the lingering panel and
        # open the break window instead, without needing the button.
        self.companion.show()
        self.assertEqual(self.close_count, 1)
        self.assertEqual(len(self.opened_urls), 2)
        self.assertEqual(self._state()["mode"], "chess")

    def test_open_initial_panel_does_not_affect_is_active(self) -> None:
        self.companion.open_initial_panel()
        self.assertFalse(self.companion.is_active())
        self.assertEqual(self.opened_urls, [self.companion.play_url])

    def test_open_initial_panel_then_a_real_turn_still_works(self) -> None:
        # The bug this guards: if the initial panel reused show()'s
        # `_active` flag, the first real turn would see `_active` already
        # True and silently no-op.
        self.companion.open_initial_panel()
        self.companion.show()
        self.assertTrue(self.companion.is_active())
        self.assertEqual(self._state()["mode"], "off")

    def test_confirm_endpoint_closes_the_idle_panel_without_touching_is_active(self) -> None:
        self.companion.open_initial_panel()
        payload = _post(self._url("/api/breaks/confirm"), {})
        self.assertEqual(self.close_count, 1)
        self.assertFalse(self.companion.is_active())
        self.assertIn("toggles", payload)

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


class BreaksCompanionRealWindowTests(unittest.TestCase):
    """Real (uninjected) window path -- verifies per-mode sizing and that a
    turn always closes and reopens a fresh window (the persisted case is
    the idle panel alone, covered in BreaksCompanionIdlePanelDismissalTests).
    """

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

    def test_chess_window_closes_and_reopens_fresh_every_turn(self) -> None:
        self._toggle("chess", True)
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()
            self.companion.hide()
            self.companion.show()
        window_type.assert_called_once_with(width=768, height=650)  # one pool entry, reused
        window = window_type.return_value
        self.assertEqual(window.open.call_count, 2)
        window.hide.assert_called_once_with()

    def test_close_tears_down_every_real_window(self) -> None:
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            self.companion.show()  # opens the idle panel
            self.companion.close()
        window_type.return_value.close.assert_called_once_with()


class BreaksCompanionIdlePanelDismissalTests(unittest.TestCase):
    """Real (uninjected) window path -- the user closing the idle panel
    themselves must be detected and respected for the rest of the process.
    """

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

    def test_a_self_closed_panel_is_never_reopened(self) -> None:
        with patch("sidequest.breaks.companion.CompanionWindow") as window_type:
            window = window_type.return_value
            window.is_running.return_value = True
            self.companion.open_initial_panel()
            window_type.assert_called_once_with(width=340, height=480)

            # The user closes the real window themselves -- nothing in
            # sidequest called hide(), so is_running() would now report False.
            window.is_running.return_value = False
            self.companion.show()  # a real turn, nothing toggled on
            self.companion.hide()
            self.companion.show()

        # Never reconstructed or reopened after the dismissal was detected.
        window_type.assert_called_once_with(width=340, height=480)
        self.assertEqual(window.open.call_count, 1)


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


class BreaksCompanionMultiplayerTests(unittest.TestCase):
    """A multiplayer game injected via the constructor -- same pattern the
    deleted test_chess_companion.py used, ported to the merged endpoints.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        chess_game = ComputerChessGame(
            Path(self.temporary_directory.name) / "chess.json", stockfish_path=""
        )
        video_queue = VideoQueue(
            Path(self.temporary_directory.name) / "video.json", catalog=_CATALOG
        )
        self.multiplayer = Mock()
        self.multiplayer.snapshot.return_value = _multiplayer_snapshot()
        self.companion = BreaksCompanion(
            chess_game,
            video_queue,
            multiplayer=self.multiplayer,
            browser_open=lambda url: None,
            browser_close=lambda: None,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def _url(self, path: str) -> str:
        return self.companion.play_url.replace("/?", f"{path}?")

    def test_polling_follows_the_chess_window_not_process_lifetime(self) -> None:
        # Presence (the opponent's "connected" dot) should track whether this
        # player is actually looking at the board, not whether the whole
        # sidequest process happens to still be alive.
        self.multiplayer.start_polling.assert_not_called()
        _post(self._url("/api/breaks/toggle"), {"mode": "chess", "on": True})

        self.companion.show()
        self.multiplayer.start_polling.assert_called_once_with()
        self.multiplayer.stop_polling.assert_not_called()

        self.companion.hide()
        self.multiplayer.stop_polling.assert_called_once_with()

        self.companion.show()
        self.assertEqual(self.multiplayer.start_polling.call_count, 2)

        self.companion.close()
        self.assertGreaterEqual(self.multiplayer.stop_polling.call_count, 2)

    def test_state_defaults_to_multiplayer_mode_with_room_metadata(self) -> None:
        with urllib.request.urlopen(self._url("/api/chess/state"), timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["mode"], "multiplayer")
        self.assertEqual(payload["fen"], "startpos")
        self.assertEqual(payload["multiplayer"]["room_code"], "ABC123")
        self.assertTrue(payload["multiplayer"]["opponent_connected"])

    def test_move_dispatches_to_multiplayer_game(self) -> None:
        self.multiplayer.move.return_value = _multiplayer_snapshot(
            turn="black", your_turn=False, last_move="e2e4"
        )
        payload = _post(self._url("/api/chess/move"), {"move": "e2e4"})
        self.multiplayer.move.assert_called_once_with("e2e4")
        self.assertEqual(payload["last_move"], "e2e4")

    def test_new_game_is_rejected_in_multiplayer_mode(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/chess/new"), {})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_difficulty_is_rejected_in_multiplayer_mode(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/chess/difficulty"), {"difficulty": "hard"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_mode_can_switch_to_practice_and_back(self) -> None:
        practice_payload = _post(self._url("/api/chess/mode"), {"mode": "practice"})
        self.assertEqual(practice_payload["mode"], "practice")
        self.assertEqual(practice_payload["engine"], "Sidequest practice bot")
        # Room metadata stays visible even while looking at the practice board.
        self.assertEqual(practice_payload["multiplayer"]["room_code"], "ABC123")

        multiplayer_payload = _post(self._url("/api/chess/mode"), {"mode": "multiplayer"})
        self.assertEqual(multiplayer_payload["mode"], "multiplayer")
        self.assertEqual(multiplayer_payload["fen"], "startpos")

    def test_mode_rejects_unknown_value(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/chess/mode"), {"mode": "spectator"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()


class MultiplayerHostJoinTests(unittest.TestCase):
    """/api/multiplayer/host and /join -- RelayClient/RemoteChessGame mocked
    at construction time, no real network.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        chess_game = ComputerChessGame(
            Path(self.temporary_directory.name) / "chess.json", stockfish_path=""
        )
        video_queue = VideoQueue(
            Path(self.temporary_directory.name) / "video.json", catalog=_CATALOG
        )
        self.companion = BreaksCompanion(
            chess_game,
            video_queue,
            browser_open=lambda url: None,
            browser_close=lambda: None,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()

    def _url(self, path: str) -> str:
        return self.companion.play_url.replace("/?", f"{path}?")

    @patch("sidequest.breaks.companion.RemoteChessGame")
    @patch("sidequest.breaks.companion.RelayClient")
    def test_host_creates_a_room_and_auto_enables_chess(self, relay_type, game_type) -> None:
        game_type.return_value.snapshot.return_value = _multiplayer_snapshot()
        payload = _post(
            self._url("/api/multiplayer/host"),
            {"relay_url": "https://relay.example", "profile": "p1"},
        )

        relay_type.assert_called_once_with("https://relay.example")
        state_path = game_type.call_args.args[1]
        self.assertEqual(state_path.name, "multiplayer-p1.json")
        self.assertIsNone(game_type.call_args.kwargs["code"])
        self.assertEqual(payload["mode"], "multiplayer")
        self.assertEqual(payload["multiplayer"]["room_code"], "ABC123")

        with urllib.request.urlopen(self._url("/api/breaks/state"), timeout=2) as response:
            breaks_state = json.load(response)
        self.assertTrue(breaks_state["toggles"]["chess"])

    @patch("sidequest.breaks.companion.RemoteChessGame")
    @patch("sidequest.breaks.companion.RelayClient")
    def test_host_uses_the_default_relay_when_none_given(self, relay_type, game_type) -> None:
        from sidequest.chess.relay_client import DEFAULT_RELAY_URL

        game_type.return_value.snapshot.return_value = _multiplayer_snapshot()
        _post(self._url("/api/multiplayer/host"), {})
        relay_type.assert_called_once_with(DEFAULT_RELAY_URL)

    @patch("sidequest.breaks.companion.RemoteChessGame")
    @patch("sidequest.breaks.companion.RelayClient")
    def test_join_passes_the_stripped_code_through(self, _relay_type, game_type) -> None:
        game_type.return_value.snapshot.return_value = _multiplayer_snapshot()
        _post(self._url("/api/multiplayer/join"), {"code": " abc123 "})
        self.assertEqual(game_type.call_args.kwargs["code"], "abc123")

    def test_join_rejects_a_blank_code(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/multiplayer/join"), {"code": "  "})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    @patch(
        "sidequest.breaks.companion.RemoteChessGame",
        side_effect=MultiplayerError("relay unreachable"),
    )
    @patch("sidequest.breaks.companion.RelayClient")
    def test_host_failure_surfaces_as_a_400(self, _relay_type, _game_type) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/multiplayer/host"), {})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
