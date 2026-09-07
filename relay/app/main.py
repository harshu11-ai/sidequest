"""FastAPI routes for the sidequest multiplayer chess relay."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import rooms
from .ratelimit import RateLimiter
from .store import InMemoryRoomStore, Room

_SWEEP_INTERVAL_SECONDS = 15 * 60
_ROOM_CREATE_LIMIT = 20
_ROOM_CREATE_WINDOW_SECONDS = 10 * 60

store = InMemoryRoomStore()
create_limiter = RateLimiter(_ROOM_CREATE_LIMIT, _ROOM_CREATE_WINDOW_SECONDS)


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        store.sweep_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="sidequest multiplayer relay", lifespan=lifespan)


class MoveRequest(BaseModel):
    token: str
    move: str
    expected_move_count: int = Field(ge=0)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _room_or_404(code: str) -> Room:
    room = store.get(code.strip().upper())
    if room is None:
        raise HTTPException(404, "room not found")
    return room


def _room_error(error: rooms.RoomError) -> HTTPException:
    return HTTPException(error.status_code, str(error))


@app.post("/rooms")
def create_room(request: Request):
    if not create_limiter.allow(_client_ip(request)):
        raise HTTPException(429, "too many rooms created, try again later")
    room = rooms.new_room()
    store.create(room)
    return {"code": room.code, "token": room.white_token, "you": "white"}


@app.post("/rooms/{code}/join")
def join_room(code: str):
    room = _room_or_404(code)
    try:
        updated = rooms.join_room(room)
    except rooms.RoomError as error:
        raise _room_error(error) from error
    store.save(updated)
    return {"code": updated.code, "token": updated.black_token, "you": "black"}


@app.get("/rooms/{code}/state")
def get_state(code: str, token: str):
    room = _room_or_404(code)
    try:
        seat = rooms.seat_for_token(room, token)
    except rooms.RoomError as error:
        raise _room_error(error) from error
    updated = rooms.touch_presence(room, seat)
    store.save(updated)
    return rooms.snapshot(updated, seat)


@app.post("/rooms/{code}/move")
def submit_move(code: str, payload: MoveRequest):
    room = _room_or_404(code)
    try:
        seat = rooms.seat_for_token(room, payload.token)
        updated = rooms.apply_move(room, seat, payload.move, payload.expected_move_count)
    except rooms.RoomError as error:
        raise _room_error(error) from error
    store.save(updated)
    return rooms.snapshot(updated, seat)


@app.get("/healthz")
def healthz():
    return {"ok": True}
