"""In-memory ring buffer of recent SCTE events. Swap for SQL when needed."""

from __future__ import annotations

import collections
import threading
import time
from typing import Iterable, Optional


class MarkerStore:
    def __init__(self, capacity: int = 10_000):
        self._buf: collections.deque[dict] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._counter = 0

    def add(self, event: dict, overlay_result: Optional[dict] = None) -> dict:
        with self._lock:
            self._counter += 1
            row = {
                "id": self._counter,
                "time": event.get("received_at") or time.time(),
                "splice_event_id": event.get("splice_event_id"),
                "segmentation_event_id": event.get("segmentation_event_id"),
                "segmentation_type_id": event.get("segmentation_type_id"),
                "segmentation_type": event.get("segmentation_type"),
                "duration_pts": event.get("duration_pts"),
                "duration_seconds": event.get("duration_seconds"),
                "pid": event.get("pid"),
                "pts_time": event.get("pts_time"),
                "splice_command_type": event.get("splice_command_type"),
                "crc32_ok": event.get("crc32_ok"),
                "overlay_applied": bool(overlay_result and overlay_result.get("overlay_applied")),
                "overlay_reason": (overlay_result or {}).get("reason"),
                "overlay_duration_ms": (overlay_result or {}).get("duration_ms"),
            }
            self._buf.append(row)
            return row

    def list(self, limit: int = 200, since_id: int = 0) -> list[dict]:
        with self._lock:
            return [r for r in list(self._buf)[-limit:] if r["id"] > since_id]

    def latest(self) -> Optional[dict]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._counter = 0
