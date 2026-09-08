"""Persistent local queue over the bundled educational-video catalog."""

from __future__ import annotations

import json
import os
import random
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from threading import Lock

# How many shuffled picks the queue keeps ready (and exposes as "up next").
_UPCOMING_LENGTH = 5
# How many prior videos "go back" can step through.
_HISTORY_LIMIT = 50


def default_video_state_path() -> Path:
    """Return the per-user path used to resume the queue between sessions."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "sidequest" / "video.json"


def load_catalog() -> list[dict[str, object]]:
    """Load the bundled catalog of educational videos."""
    raw = files("sidequest.video").joinpath("catalog.json").read_text(encoding="utf-8")
    catalog = json.loads(raw)
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("video catalog is empty or malformed")
    return catalog


@dataclass(frozen=True, slots=True)
class VideoSnapshot:
    video: dict[str, object]
    position_s: float
    watched: tuple[str, ...]
    upcoming: tuple[str, ...]
    can_go_back: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "video": self.video,
            "position_s": self.position_s,
            "watched": list(self.watched),
            "upcoming": list(self.upcoming),
            "can_go_back": self.can_go_back,
        }


class VideoQueue:
    """A shuffled, resumable walk through the catalog: one current video, one saved position.

    The play order is a shuffled draw, not catalog order, but *which* video is
    current and how far into it the viewer got are pinned in the saved state --
    reopening the window (even much later, even under a different agent CLI on
    the same machine) resumes that exact video and position rather than
    re-shuffling out from under it.
    """

    def __init__(
        self,
        state_path: Path | None = None,
        catalog: list[dict[str, object]] | None = None,
    ) -> None:
        self.state_path = state_path or default_video_state_path()
        self.catalog = catalog if catalog is not None else load_catalog()
        self._by_id = {str(entry["id"]): entry for entry in self.catalog}
        self._lock = Lock()
        self._current_id = ""
        self._position_s = 0.0
        self._watched: set[str] = set()
        self._upcoming: list[str] = []
        self._history: list[str] = []
        self._load()
        if self._current_id not in self._by_id:
            self._current_id = self._draw_next(exclude=None)
            self._save()

    def snapshot(self) -> VideoSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def record_position(self, position_s: float) -> VideoSnapshot:
        with self._lock:
            try:
                self._position_s = max(0.0, float(position_s))
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid position: {position_s!r}") from error
            self._save()
            return self._snapshot_unlocked()

    def skip(self) -> VideoSnapshot:
        with self._lock:
            self._watched.add(self._current_id)
            self._push_history(self._current_id)
            self._current_id = self._draw_next(exclude=self._current_id)
            self._position_s = 0.0
            self._save()
            return self._snapshot_unlocked()

    def previous(self) -> VideoSnapshot:
        with self._lock:
            if self._history:
                # Going back is an undo, not a "finish" -- the video we're
                # leaving isn't marked watched, and it isn't re-queued either:
                # it's still in the catalog, so the shuffle surfaces it again
                # on its own in due course.
                self._current_id = self._history.pop()
                self._position_s = 0.0
                self._save()
            return self._snapshot_unlocked()

    def _push_history(self, video_id: str) -> None:
        self._history.append(video_id)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[: len(self._history) - _HISTORY_LIMIT]

    def _draw_next(self, *, exclude: str | None) -> str:
        if not self._upcoming:
            self._refill_upcoming()
        next_id = self._upcoming.pop(0)
        if next_id == exclude and self._upcoming:
            # A fresh shuffle can put the video we just left right back up --
            # bump it one spot rather than replay it immediately.
            self._upcoming.append(next_id)
            next_id = self._upcoming.pop(0)
        self._ensure_upcoming_length()
        return next_id

    def _refill_upcoming(self) -> None:
        ids = list(self._by_id)
        random.shuffle(ids)
        self._upcoming.extend(ids)

    def _ensure_upcoming_length(self) -> None:
        target = min(_UPCOMING_LENGTH, len(self._by_id) - 1)
        while len(self._upcoming) < target:
            self._refill_upcoming()

    def _current(self) -> dict[str, object]:
        return self._by_id[self._current_id]

    def _snapshot_unlocked(self) -> VideoSnapshot:
        return VideoSnapshot(
            video=self._current(),
            position_s=self._position_s,
            watched=tuple(sorted(self._watched)),
            upcoming=tuple(self._upcoming[:_UPCOMING_LENGTH]),
            can_go_back=bool(self._history),
        )

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            current_id = data.get("current_id", "")
            self._current_id = current_id if isinstance(current_id, str) else ""
            position = data.get("position_s", 0.0)
            self._position_s = float(position) if isinstance(position, int | float) else 0.0
            watched = data.get("watched", [])
            self._watched = {str(item) for item in watched} if isinstance(watched, list) else set()
            upcoming = data.get("upcoming", [])
            self._upcoming = (
                [str(item) for item in upcoming if str(item) in self._by_id]
                if isinstance(upcoming, list)
                else []
            )
            history = data.get("history", [])
            self._history = (
                [str(item) for item in history if str(item) in self._by_id]
                if isinstance(history, list)
                else []
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._current_id = ""
            self._position_s = 0.0
            self._watched = set()
            self._upcoming = []
            self._history = []

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "current_id": self._current_id,
                "position_s": self._position_s,
                "watched": sorted(self._watched),
                "upcoming": self._upcoming,
                "history": self._history,
            },
            separators=(",", ":"),
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=".video-",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
