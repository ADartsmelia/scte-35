"""
Overlay controller — restart-based, no ZMQ.

When a SCTE-35 event fires, the encoder is restarted with the overlay
composited (overlay_active=True). When the break ends (either via duration
timeout or an explicit end-type segmentation event), the encoder is restarted
without the overlay (overlay_active=False).

The ~1 s encoder restart causes a brief splice-point interruption, which is
acceptable because SCTE-35 events themselves mark splice points.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .runtime_config import StreamConfig
from .scte_parser import SEGMENTATION_TYPE_IDS

log = logging.getLogger(__name__)


class OverlayController:
    def __init__(self, cfg: StreamConfig, set_overlay_cb=None):
        """
        set_overlay_cb: async callable(active: bool) — provided by the session.
        """
        self.cfg = cfg
        self.set_overlay_cb = set_overlay_cb
        self._overlay_off_task: Optional[asyncio.Task] = None
        self._active = False
        self._last_event_id: Optional[tuple] = None

    async def close(self) -> None:
        if self._overlay_off_task:
            self._overlay_off_task.cancel()

    # -------------------------------------------------------------------------
    # Event handling
    # -------------------------------------------------------------------------

    async def handle_event(self, event: dict) -> dict:
        """Process one SCTE event. Returns a result dict for the marker store."""
        result = {
            "overlay_applied": False,
            "reason": "",
            "applied_at": None,
            "duration_ms": None,
        }

        # Dedupe — both splice_event_id and segmentation_event_id can repeat
        # across PMT updates / start+end pairs.
        identity = (
            event.get("splice_event_id"),
            event.get("segmentation_event_id"),
            event.get("segmentation_type_id"),
        )
        if identity == self._last_event_id:
            result["reason"] = "duplicate"
            return result
        self._last_event_id = identity

        type_id = event.get("segmentation_type_id")
        cmd_type = event.get("splice_command_type")

        # Decide whether this fires
        fire = False
        if type_id is not None and type_id in self.cfg.triggered_segmentation_types:
            fire = True
            result["reason"] = (
                f"segmentation_type_id=0x{type_id:02x} "
                f"({SEGMENTATION_TYPE_IDS.get(type_id, '?')})"
            )
        elif self.cfg.trigger_on_splice_insert_oon and cmd_type == 0x05:
            fire = True
            result["reason"] = "splice_insert"

        if not fire:
            result["reason"] = (
                result["reason"] or
                (f"ignored type=0x{type_id:02x}" if type_id else "no-op")
            )
            return result

        # Decide duration
        duration_ms: Optional[int] = self.cfg.overlay_duration_ms
        if duration_ms is None and event.get("duration_pts"):
            duration_ms = int(event["duration_pts"] * 1000 / 90000)

        # End-type events → turn overlay off
        END_TYPES = {0x11, 0x12, 0x21, 0x23, 0x31, 0x33, 0x35, 0x37, 0x41, 0x51}
        if type_id in END_TYPES:
            await self._overlay_off()
            result["overlay_applied"] = True
            result["applied_at"] = time.time()
            result["reason"] += " (end)"
            return result

        await self._overlay_on(duration_ms)
        result["overlay_applied"] = True
        result["applied_at"] = time.time()
        result["duration_ms"] = duration_ms
        return result

    # -------------------------------------------------------------------------
    # Overlay on / off
    # -------------------------------------------------------------------------

    async def _overlay_on(self, duration_ms: Optional[int]) -> None:
        if self._overlay_off_task and not self._overlay_off_task.done():
            self._overlay_off_task.cancel()

        if self.set_overlay_cb:
            await self.set_overlay_cb(True)
        self._active = True

        if duration_ms:
            self._overlay_off_task = asyncio.create_task(self._auto_off(duration_ms))

    async def _overlay_off(self) -> None:
        if self.set_overlay_cb:
            await self.set_overlay_cb(False)
        self._active = False

    async def _auto_off(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            await self._overlay_off()
            log.info("Overlay auto-off after %d ms", duration_ms)
        except asyncio.CancelledError:
            pass

    @property
    def active(self) -> bool:
        return self._active
