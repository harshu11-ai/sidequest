import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from sidequest.chess.multiplayer import MultiplayerError, RemoteChessGame
from sidequest.chess.relay_client import RelayError, RoomSeat


def state_body(**overrides) -> dict:
    body = {
        "code": "ABC123",
        "you": "white",
        "fen": "startpos",
        "legal_moves": ["e2e4"],
        "turn": "white",
        "your_turn": True,
        "status": "In progress",
        "result": None,
        "last_move": None,
        "move_count": 0,
        "room_status": "in_progress",
        "opponent_connected": True,
    }
    body.update(overrides)
    return body


class RemoteChessGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "multiplayer.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_hosting_creates_a_room_and_caches_initial_state(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()

        game = RemoteChessGame(relay, self.state_path)

        relay.create_room.assert_called_once_with()
        self.assertEqual(game.room_code, "ABC123")
        snapshot = game.snapshot()
        self.assertEqual(snapshot.you, "white")
        self.assertTrue(snapshot.your_turn)

    def test_joining_uses_the_supplied_code(self) -> None:
        relay = Mock()
        relay.join_room.return_value = RoomSeat(code="XYZ999", token="tok", you="black")
        relay.get_state.return_value = state_body(code="XYZ999", you="black", your_turn=False)

        game = RemoteChessGame(relay, self.state_path, code="xyz999")

        relay.join_room.assert_called_once_with("XYZ999")
        self.assertEqual(game.room_code, "XYZ999")

    def test_hosting_again_always_creates_a_fresh_room(self) -> None:
        """No silent resume for --multiplayer: every host call is a new room,
        even though a previous session's seat is still saved locally."""
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        RemoteChessGame(relay, self.state_path)

        relay_again = Mock()
        relay_again.create_room.return_value = RoomSeat(code="NEW111", token="tok2", you="white")
        relay_again.get_state.return_value = state_body(code="NEW111")

        game = RemoteChessGame(relay_again, self.state_path)

        relay_again.create_room.assert_called_once_with()
        relay_again.join_room.assert_not_called()
        self.assertEqual(game.room_code, "NEW111")

    def test_joining_with_a_previously_used_code_resumes_your_seat(self) -> None:
        relay = Mock()
        relay.join_room.return_value = RoomSeat(code="ABC123", token="tok", you="black")
        relay.get_state.return_value = state_body(code="ABC123", you="black", your_turn=False)
        RemoteChessGame(relay, self.state_path, code="abc123")

        relay_again = Mock()
        relay_again.get_state.return_value = state_body(code="ABC123", you="black", your_turn=False)
        game = RemoteChessGame(relay_again, self.state_path, code="abc123")

        relay_again.join_room.assert_not_called()
        relay_again.get_state.assert_called_with("ABC123", "tok")
        self.assertEqual(game.snapshot().you, "black")

    def test_rejoining_a_finished_room_with_your_old_code_tries_a_fresh_join(self) -> None:
        relay = Mock()
        relay.join_room.return_value = RoomSeat(code="ABC123", token="tok", you="black")
        relay.get_state.return_value = state_body(code="ABC123")
        RemoteChessGame(relay, self.state_path, code="ABC123")

        relay_again = Mock()
        relay_again.get_state.return_value = state_body(code="ABC123", room_status="finished")
        relay_again.join_room.side_effect = RelayError("room already has two players")

        with self.assertRaises(MultiplayerError):
            RemoteChessGame(relay_again, self.state_path, code="ABC123")

        relay_again.join_room.assert_called_once_with("ABC123")

    def test_rejoining_after_your_saved_seat_is_gone_tries_a_fresh_join(self) -> None:
        relay = Mock()
        relay.join_room.return_value = RoomSeat(code="ABC123", token="tok", you="black")
        relay.get_state.return_value = state_body(code="ABC123")
        RemoteChessGame(relay, self.state_path, code="ABC123")

        relay_again = Mock()
        relay_again.get_state.side_effect = [
            RelayError("room not found"),
            state_body(code="ABC123"),
        ]
        relay_again.join_room.return_value = RoomSeat(code="ABC123", token="tok2", you="black")

        game = RemoteChessGame(relay_again, self.state_path, code="ABC123")

        relay_again.join_room.assert_called_once_with("ABC123")
        self.assertEqual(game.room_code, "ABC123")

    def test_move_updates_cached_snapshot(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        relay.submit_move.return_value = state_body(
            turn="black", your_turn=False, last_move="e2e4", move_count=1
        )
        snapshot = game.move("e2e4")

        relay.submit_move.assert_called_once_with("ABC123", "tok", "e2e4", 0)
        self.assertEqual(snapshot.last_move, "e2e4")
        self.assertEqual(game.snapshot().last_move, "e2e4")

    def test_move_error_refreshes_cache_before_raising(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        relay.submit_move.side_effect = RelayError("it is not your turn")
        relay.get_state.return_value = state_body(
            turn="black", your_turn=False, last_move="e7e5", move_count=1
        )

        with self.assertRaisesRegex(MultiplayerError, "it is not your turn"):
            game.move("e2e4")

        self.assertEqual(game.snapshot().last_move, "e7e5")

    def test_construction_fails_loudly_if_relay_is_unreachable(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.side_effect = RelayError("could not reach the relay")

        with self.assertRaises(MultiplayerError):
            RemoteChessGame(relay, self.state_path)

    def test_poll_updates_snapshot(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        relay.get_state.return_value = state_body(
            turn="black", your_turn=False, last_move="e7e5", move_count=1
        )
        snapshot = game.poll()

        self.assertEqual(snapshot.last_move, "e7e5")

    def test_poll_keeps_last_snapshot_on_transient_relay_error(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        relay.get_state.side_effect = RelayError("timeout")
        snapshot = game.poll()

        self.assertEqual(snapshot.fen, "startpos")

    def test_start_and_stop_polling(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        game.start_polling(interval=0.01)
        try:
            deadline = time.monotonic() + 2.0
            while relay.get_state.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(relay.get_state.call_count, 2)
        finally:
            game.stop_polling()

    def test_polling_resumes_after_being_stopped(self) -> None:
        # Regression: stop_polling() used to leave its threading.Event set,
        # so a later start_polling() would spawn a thread that saw the event
        # already signalled and exited before its first tick -- polling
        # (and so the opponent's presence heartbeat) could never restart
        # after the first stop for the rest of the game.
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        game = RemoteChessGame(relay, self.state_path)

        game.start_polling(interval=0.01)
        try:
            deadline = time.monotonic() + 2.0
            while relay.get_state.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(relay.get_state.call_count, 2)
        finally:
            game.stop_polling()

        calls_before_restart = relay.get_state.call_count
        game.start_polling(interval=0.01)
        try:
            target = calls_before_restart + 2
            deadline = time.monotonic() + 2.0
            while relay.get_state.call_count < target and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(relay.get_state.call_count, target)
        finally:
            game.stop_polling()


if __name__ == "__main__":
    unittest.main()
