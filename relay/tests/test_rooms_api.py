import unittest
from unittest.mock import patch

from app import main
from app.observability import ServiceMetrics
from app.store import InMemoryRoomStore
from fastapi.testclient import TestClient


class RoomsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        main.store = InMemoryRoomStore()
        main.metrics = ServiceMetrics()
        self.client = TestClient(main.app)

    def create_and_join(self):
        created = self.client.post("/rooms").json()
        joined = self.client.post(f"/rooms/{created['code']}/join").json()
        return created, joined

    def test_create_room_returns_white_seat(self) -> None:
        response = self.client.post("/rooms")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["you"], "white")
        self.assertEqual(len(body["code"]), 6)
        self.assertTrue(body["token"])

    def test_second_join_is_rejected(self) -> None:
        created, _ = self.create_and_join()
        response = self.client.post(f"/rooms/{created['code']}/join")
        self.assertEqual(response.status_code, 409)

    def test_join_unknown_room_is_404(self) -> None:
        response = self.client.post("/rooms/ZZZZZZ/join")
        self.assertEqual(response.status_code, 404)

    def test_state_before_opponent_joins(self) -> None:
        created = self.client.post("/rooms").json()
        state = self.client.get(
            f"/rooms/{created['code']}/state", params={"token": created["token"]}
        ).json()
        self.assertEqual(state["room_status"], "waiting_for_opponent")
        self.assertTrue(state["your_turn"])
        self.assertFalse(state["opponent_connected"])

    def test_state_rejects_wrong_token(self) -> None:
        created = self.client.post("/rooms").json()
        response = self.client.get(f"/rooms/{created['code']}/state", params={"token": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_opponent_connected_reflects_recent_polling(self) -> None:
        created, joined = self.create_and_join()
        self.client.get(f"/rooms/{created['code']}/state", params={"token": joined["token"]})
        state = self.client.get(
            f"/rooms/{created['code']}/state", params={"token": created["token"]}
        ).json()
        self.assertTrue(state["opponent_connected"])

    def test_move_before_opponent_joins_is_rejected(self) -> None:
        created = self.client.post("/rooms").json()
        response = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": created["token"], "move": "e2e4", "expected_move_count": 0},
        )
        self.assertEqual(response.status_code, 409)

    def test_full_move_exchange(self) -> None:
        created, joined = self.create_and_join()
        white_move = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": created["token"], "move": "e2e4", "expected_move_count": 0},
        )
        self.assertEqual(white_move.status_code, 200)
        white_body = white_move.json()
        self.assertEqual(white_body["turn"], "black")
        self.assertEqual(white_body["last_move"], "e2e4")

        black_move = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": joined["token"], "move": "e7e5", "expected_move_count": 1},
        )
        self.assertEqual(black_move.status_code, 200)
        self.assertEqual(black_move.json()["turn"], "white")

    def test_moving_out_of_turn_is_rejected(self) -> None:
        created, joined = self.create_and_join()
        response = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": joined["token"], "move": "e7e5", "expected_move_count": 0},
        )
        self.assertEqual(response.status_code, 409)

    def test_stale_move_count_is_rejected(self) -> None:
        created, joined = self.create_and_join()
        self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": created["token"], "move": "e2e4", "expected_move_count": 0},
        )
        response = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": joined["token"], "move": "e7e5", "expected_move_count": 0},
        )
        self.assertEqual(response.status_code, 409)

    def test_illegal_move_is_rejected(self) -> None:
        created, _ = self.create_and_join()
        response = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": created["token"], "move": "e2e5", "expected_move_count": 0},
        )
        self.assertEqual(response.status_code, 400)

    def test_fools_mate_ends_the_game(self) -> None:
        created, joined = self.create_and_join()
        moves = [
            (created["token"], "f2f3", 0),
            (joined["token"], "e7e5", 1),
            (created["token"], "g2g4", 2),
            (joined["token"], "d8h4", 3),
        ]
        response = None
        for token, move, expected_move_count in moves:
            response = self.client.post(
                f"/rooms/{created['code']}/move",
                json={"token": token, "move": move, "expected_move_count": expected_move_count},
            )
            self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], "black")
        self.assertEqual(body["room_status"], "finished")

    def test_move_after_game_over_is_rejected(self) -> None:
        created, joined = self.create_and_join()
        moves = ["f2f3", "e7e5", "g2g4", "d8h4"]
        tokens = [created["token"], joined["token"]]
        for index, move in enumerate(moves):
            self.client.post(
                f"/rooms/{created['code']}/move",
                json={
                    "token": tokens[index % 2],
                    "move": move,
                    "expected_move_count": index,
                },
            )
        response = self.client.post(
            f"/rooms/{created['code']}/move",
            json={"token": created["token"], "move": "a2a3", "expected_move_count": 4},
        )
        self.assertEqual(response.status_code, 409)

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(len(response.headers["X-Request-ID"]), 16)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_metrics_report_requests_and_room_counts(self) -> None:
        self.client.post("/rooms")

        response = self.client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertGreaterEqual(body["uptime_s"], 0)
        self.assertEqual(body["requests"]["total"], 1)
        self.assertEqual(body["requests"]["in_flight"], 1)
        self.assertEqual(body["requests"]["responses"]["2xx"], 1)
        self.assertEqual(body["rooms"], {"total": 1, "waiting": 1, "active": 0, "finished": 0})
        self.assertIsNone(body["last_error"])

    def test_unhandled_errors_are_reported_without_leaking_details(self) -> None:
        with (
            patch.object(main.store, "stats", side_effect=RuntimeError("private detail")),
            self.assertLogs("sidequest.relay", level="ERROR") as captured,
        ):
            response = self.client.get("/metrics?token=do-not-log")

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"], "internal server error")
        self.assertEqual(body["request_id"], response.headers["X-Request-ID"])
        log_output = "\n".join(captured.output)
        self.assertIn('"event":"unhandled_request_error"', log_output)
        self.assertIn(f'"request_id":"{body["request_id"]}"', log_output)
        self.assertIn('"path":"/metrics"', log_output)
        self.assertNotIn("do-not-log", log_output)

        metrics_response = self.client.get("/metrics").json()
        self.assertEqual(metrics_response["requests"]["responses"]["5xx"], 1)
        self.assertEqual(metrics_response["last_error"]["request_id"], body["request_id"])


if __name__ == "__main__":
    unittest.main()
