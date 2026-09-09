"""Small, dependency-free runtime metrics for the relay process."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime


class ServiceMetrics:
    """Track request health without retaining request or user data."""

    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._lock = threading.Lock()
        self._in_flight = 0
        self._requests = 0
        self._responses = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        self._duration_ms_total = 0.0
        self._duration_ms_max = 0.0
        self._last_error: dict[str, str] | None = None

    def begin_request(self) -> float:
        with self._lock:
            self._in_flight += 1
        return time.monotonic()

    def finish_request(
        self,
        started_at: float,
        status_code: int,
        *,
        error_id: str | None = None,
    ) -> None:
        duration_ms = max(0.0, (time.monotonic() - started_at) * 1000)
        status_group = f"{status_code // 100}xx"
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._requests += 1
            if status_group in self._responses:
                self._responses[status_group] += 1
            self._duration_ms_total += duration_ms
            self._duration_ms_max = max(self._duration_ms_max, duration_ms)
            if error_id is not None:
                self._last_error = {
                    "request_id": error_id,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            average = self._duration_ms_total / self._requests if self._requests else 0.0
            return {
                "uptime_s": round(time.monotonic() - self._started_at, 3),
                "requests": {
                    "total": self._requests,
                    "in_flight": self._in_flight,
                    "responses": dict(self._responses),
                    "duration_ms_avg": round(average, 3),
                    "duration_ms_max": round(self._duration_ms_max, 3),
                },
                "last_error": dict(self._last_error) if self._last_error else None,
            }
