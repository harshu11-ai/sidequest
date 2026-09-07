"""A tiny in-memory, fixed-window rate limiter.

Only meant to blunt someone hammering room creation and growing the
in-memory room dict unbounded -- not a substitute for a real gateway if this
ever needs to withstand deliberate abuse at scale.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = [hit for hit in self._hits.get(key, []) if hit > cutoff]
            if len(hits) >= self.max_events:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
