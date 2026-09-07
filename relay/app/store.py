"""Room storage.

`RoomStore` is kept deliberately narrow so the only implementation today,
`InMemoryRoomStore`, can later be swapped for something durable (SQLite,
Postgres) without touching the route logic in `main.py`. Games are
short-lived, so losing one on a process restart is an acceptable v1
tradeoff -- the alternative of provisioning a database isn't worth the
complexity until this actually needs to survive redeploys mid-game.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

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


class RoomStore(Protocol):
    def create(self, room: Room) -> None: ...
    def get(self, code: str) -> Room | None: ...
    def save(self, room: Room) -> None: ...
    def sweep_expired(self) -> None: ...


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

    def save(self, room: Room) -> None:
        with self._lock:
            if room.code in self._rooms:
                self._rooms[room.code] = room

    def sweep_expired(self) -> None:
        cutoff = time.time() - ROOM_TTL_SECONDS
        with self._lock:
            expired = [code for code, room in self._rooms.items() if room.updated_at < cutoff]
            for code in expired:
                del self._rooms[code]
