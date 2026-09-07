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

    def test_seat_is_persisted_and_reused_on_reconnect(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        RemoteChessGame(relay, self.state_path)

        relay_again = Mock()
        relay_again.get_state.return_value = state_body()
        RemoteChessGame(relay_again, self.state_path)

        relay_again.create_room.assert_not_called()
        relay_again.get_state.assert_called_with("ABC123", "tok")

    def test_finished_saved_room_is_replaced_by_a_fresh_one(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        RemoteChessGame(relay, self.state_path)

        relay_again = Mock()
        relay_again.get_state.return_value = state_body(room_status="finished")
        relay_again.create_room.return_value = RoomSeat(code="NEW111", token="tok2", you="white")

        game = RemoteChessGame(relay_again, self.state_path)

        relay_again.create_room.assert_called_once_with()
        self.assertEqual(game.room_code, "NEW111")

    def test_gone_saved_room_falls_back_to_creating_a_fresh_one(self) -> None:
        relay = Mock()
        relay.create_room.return_value = RoomSeat(code="ABC123", token="tok", you="white")
        relay.get_state.return_value = state_body()
        RemoteChessGame(relay, self.state_path)

        relay_again = Mock()
        relay_again.get_state.side_effect = [
            RelayError("room not found"),
            state_body(code="NEW111"),
        ]
        relay_again.create_room.return_value = RoomSeat(code="NEW111", token="tok2", you="white")

        game = RemoteChessGame(relay_again, self.state_path)

        relay_again.create_room.assert_called_once_with()
        self.assertEqual(game.room_code, "NEW111")

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
            time.sleep(0.05)
            self.assertGreaterEqual(relay.get_state.call_count, 2)
        finally:
            game.stop_polling()


if __name__ == "__main__":
    unittest.main()
