import time
import unittest

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

    def test_save_ignores_unknown_room(self) -> None:
        store = InMemoryRoomStore()
        store.save(make_room("GHOST1", time.time()))
        self.assertIsNone(store.get("GHOST1"))

    def test_sweep_expired_removes_stale_rooms_only(self) -> None:
        store = InMemoryRoomStore()
        fresh = make_room("FRESH1", time.time())
        stale = make_room("STALE1", time.time() - ROOM_TTL_SECONDS - 1)
        store.create(fresh)
        store.create(stale)

        store.sweep_expired()

        self.assertIsNotNone(store.get("FRESH1"))
        self.assertIsNone(store.get("STALE1"))


if __name__ == "__main__":
    unittest.main()
