"""Persistent local queue over the bundled educational-video catalog."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from threading import Lock


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
    index: int
    count: int
    position_s: float
    watched: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "video": self.video,
            "index": self.index,
            "count": self.count,
            "position_s": self.position_s,
            "watched": list(self.watched),
        }


class VideoQueue:
    """A resumable walk through the catalog: one current video, one saved position."""

    def __init__(
        self,
        state_path: Path | None = None,
        catalog: list[dict[str, object]] | None = None,
    ) -> None:
        self.state_path = state_path or default_video_state_path()
        self.catalog = catalog if catalog is not None else load_catalog()
        self._lock = Lock()
        self._index = 0
        self._position_s = 0.0
        self._watched: set[str] = set()
        self._load()

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
            self._watched.add(str(self._current()["id"]))
            self._index = (self._index + 1) % len(self.catalog)
            self._position_s = 0.0
            self._save()
            return self._snapshot_unlocked()

    def previous(self) -> VideoSnapshot:
        with self._lock:
            self._index = (self._index - 1) % len(self.catalog)
            self._position_s = 0.0
            self._save()
            return self._snapshot_unlocked()

    def _current(self) -> dict[str, object]:
        return self.catalog[self._index]

    def _snapshot_unlocked(self) -> VideoSnapshot:
        return VideoSnapshot(
            video=self._current(),
            index=self._index,
            count=len(self.catalog),
            position_s=self._position_s,
            watched=tuple(sorted(self._watched)),
        )

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            index = data.get("index", 0)
            self._index = index if isinstance(index, int) and 0 <= index < len(self.catalog) else 0
            position = data.get("position_s", 0.0)
            self._position_s = float(position) if isinstance(position, int | float) else 0.0
            watched = data.get("watched", [])
            self._watched = {str(item) for item in watched} if isinstance(watched, list) else set()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._index = 0
            self._position_s = 0.0
            self._watched = set()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "index": self._index,
                "position_s": self._position_s,
                "watched": sorted(self._watched),
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
