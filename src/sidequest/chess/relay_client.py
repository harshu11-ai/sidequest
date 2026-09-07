"""Thin stdlib HTTP client for the sidequest multiplayer chess relay.

Deliberately dependency-free (no `requests`) since this is the only piece
of the published package that talks to the network unprompted, beyond the
update check -- keeping it to `urllib` makes that easy to audit.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_RELAY_URL = os.environ.get(
    "SIDEQUEST_RELAY_URL", "https://sidequest-chess-relay.onrender.com"
)
_TIMEOUT_SECONDS = 8


class RelayError(RuntimeError):
    """The relay is unreachable, or rejected a request."""


@dataclass(frozen=True, slots=True)
class RoomSeat:
    code: str
    token: str
    you: str  # "white" | "black"


class RelayClient:
    """Talks to the multiplayer relay's REST API (see `relay/app/main.py`)."""

    def __init__(self, base_url: str = DEFAULT_RELAY_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def create_room(self) -> RoomSeat:
        body = self._request("POST", "/rooms")
        return RoomSeat(code=body["code"], token=body["token"], you=body["you"])

    def join_room(self, code: str) -> RoomSeat:
        body = self._request("POST", f"/rooms/{code}/join")
        return RoomSeat(code=body["code"], token=body["token"], you=body["you"])

    def get_state(self, code: str, token: str) -> dict[str, object]:
        return self._request("GET", f"/rooms/{code}/state", query={"token": token})

    def submit_move(
        self, code: str, token: str, move: str, expected_move_count: int
    ) -> dict[str, object]:
        return self._request(
            "POST",
            f"/rooms/{code}/move",
            payload={
                "token": token,
                "move": move,
                "expected_move_count": expected_move_count,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RelayError(_error_detail(error)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RelayError(f"could not reach the relay: {error}") from error
        except json.JSONDecodeError as error:
            raise RelayError("relay returned an invalid response") from error


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read())
        return str(body.get("detail", error.reason))
    except (json.JSONDecodeError, AttributeError, ValueError):
        return str(error.reason)
