import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from app.store import ROOM_TTL_SECONDS, InMemoryRoomStore, Room


def make_room(code: str, updated_at: float) -> Room:
    return Room(
        code=code,
        white_token="white-token",
        black_token=None,
        fen="startpos",
        move_history=(),
        result=None,
        created_at=updated_at,
        updated_at=updated_at,
        white_last_seen=updated_at,
        black_last_seen=None,
    )


class InMemoryRoomStoreTests(unittest.TestCase):
    def test_get_returns_none_for_unknown_code(self) -> None:
        store = InMemoryRoomStore()
        self.assertIsNone(store.get("MISSING"))

    def test_create_then_get_round_trips(self) -> None:
        store = InMemoryRoomStore()
        room = make_room("ABC123", time.time())
        store.create(room)
        self.assertEqual(store.get("ABC123"), room)

    def test_update_rejects_unknown_room(self) -> None:
        store = InMemoryRoomStore()
        with self.assertRaises(KeyError):
            store.update("GHOST1", lambda room: room)

    def test_update_is_atomic_across_threads(self) -> None:
        store = InMemoryRoomStore()
        store.create(make_room("ABC123", 0))

        def increment() -> None:
            store.update("ABC123", lambda room: replace(room, updated_at=room.updated_at + 1))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: increment(), range(100)))

        self.assertEqual(store.get("ABC123").updated_at, 100)

    def test_sweep_expired_removes_stale_rooms_only(self) -> None:
        store = InMemoryRoomStore()
        fresh = make_room("FRESH1", time.time())
        stale = make_room("STALE1", time.time() - ROOM_TTL_SECONDS - 1)
        store.create(fresh)
        store.create(stale)

        store.sweep_expired()

        self.assertIsNotNone(store.get("FRESH1"))
        self.assertIsNone(store.get("STALE1"))

    def test_stats_group_rooms_by_lifecycle(self) -> None:
        store = InMemoryRoomStore()
        waiting = make_room("WAIT01", time.time())
        active = replace(waiting, code="PLAY01", black_token="black-token")
        finished = replace(active, code="DONE01", result="white")
        for room in (waiting, active, finished):
            store.create(room)

        self.assertEqual(
            store.stats(),
            {"total": 3, "waiting": 1, "active": 1, "finished": 1},
        )


if __name__ == "__main__":
    unittest.main()
