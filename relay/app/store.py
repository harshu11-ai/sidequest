"""Thread-safe in-memory storage for short-lived multiplayer rooms."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

ROOM_TTL_SECONDS = 6 * 60 * 60  # abandoned rooms are swept after 6 hours


@dataclass(frozen=True, slots=True)
class Room:
    code: str
    white_token: str
    black_token: str | None
    fen: str
    move_history: tuple[str, ...]
    result: str | None  # None while in progress, else "white" | "black" | "draw"
    created_at: float
    updated_at: float
    white_last_seen: float | None
    black_last_seen: float | None

    @property
    def status(self) -> str:
        if self.result is not None:
            return "finished"
        if self.black_token is None:
            return "waiting_for_opponent"
        return "in_progress"


class InMemoryRoomStore:
    """Holds every room in a process-local dict.

    Only correct as long as the service runs as a single instance -- two
    instances behind a load balancer would each have their own dict and
    diverge. Keep `numInstances`/autoscaling off while this is the store.
    """

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self._lock = threading.Lock()

    def create(self, room: Room) -> None:
        with self._lock:
            self._rooms[room.code] = room

    def get(self, code: str) -> Room | None:
        with self._lock:
            return self._rooms.get(code)

    def update(self, code: str, transform: Callable[[Room], Room]) -> Room:
        """Atomically transform and return a room.

        Holding the lock while ``transform`` runs prevents simultaneous joins,
        moves, or presence updates from overwriting one another.
        """
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                raise KeyError(code)
            updated = transform(room)
            self._rooms[code] = updated
            return updated

    def sweep_expired(self) -> None:
        cutoff = time.time() - ROOM_TTL_SECONDS
        with self._lock:
            expired = [code for code, room in self._rooms.items() if room.updated_at < cutoff]
            for code in expired:
                del self._rooms[code]
