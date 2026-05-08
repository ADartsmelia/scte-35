"""
Overlay controller.

Bridges SCTE-35 events to the FFmpeg encoder. Two modes:

  * "zmq" (production): send runtime commands to FFmpeg's zmq filter to
    toggle overlay enable/x/y/scale on the running encoder, with no restart.
  * "restart" (MVP): tear down the encoder and rebuild it with an overlay
    filter that has `enable='between(t,T0,T1)'` baked in.

ZMQ message format (FFmpeg zmq filter):
    "<filter_name> <option> <value>"
e.g. "ov enable 1"   "ov x 100"   "ov y 50"

This module owns the policy: which segmentation_type_ids fire the overlay,
how long it stays up, and whether to debounce repeated triggers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .runtime_config import StreamConfig
from .scte_parser import SEGMENTATION_TYPE_IDS

log = logging.getLogger(__name__)

try:
    import zmq
    import zmq.asyncio
    HAVE_ZMQ = True
except ImportError:
    HAVE_ZMQ = False


class OverlayController:
    def __init__(self, cfg: StreamConfig, restart_encoder_cb=None):
        self.cfg = cfg
        self.restart_encoder_cb = restart_encoder_cb  # async callable for restart mode
        self._zmq_ctx = None
        self._zmq_sock = None
        self._overlay_off_task: Optional[asyncio.Task] = None
        self._active = False
        self._last_event_id: Optional[int] = None

        if cfg.overlay_mode == "zmq":
            if not HAVE_ZMQ:
                raise RuntimeError(
                    "overlay_mode=zmq requires pyzmq; install pyzmq or set overlay_mode='restart'"
                )
            self._zmq_ctx = zmq.asyncio.Context()
            self._zmq_sock = self._zmq_ctx.socket(zmq.REQ)
            # Note: FFmpeg's zmq filter binds; we connect.
            self._zmq_sock.connect(cfg.zmq_bind)
            # Don't block forever if FFmpeg isn't there
            self._zmq_sock.setsockopt(zmq.RCVTIMEO, 500)
            self._zmq_sock.setsockopt(zmq.SNDTIMEO, 500)
            self._zmq_sock.setsockopt(zmq.LINGER, 0)

    async def close(self) -> None:
        if self._overlay_off_task:
            self._overlay_off_task.cancel()
        if self._zmq_sock is not None:
            self._zmq_sock.close()
        if self._zmq_ctx is not None:
            self._zmq_ctx.term()

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

        # Dedupe — both splice_event_id and segmentation_event_id can repeat across
        # PMT updates / start+end pairs. We dedupe per (id, type).
        identity = (event.get("splice_event_id"), event.get("segmentation_event_id"),
                    event.get("segmentation_type_id"))
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
            # splice_insert; treat OON as the start of a break
            fire = True
            result["reason"] = "splice_insert"

        if not fire:
            result["reason"] = result["reason"] or f"ignored type=0x{type_id:02x}" if type_id else "no-op"
            return result

        # Decide duration: prefer config override, else use SCTE duration in PTS
        duration_ms: Optional[int] = self.cfg.overlay_duration_ms
        if duration_ms is None and event.get("duration_pts"):
            duration_ms = int(event["duration_pts"] * 1000 / 90000)

        # Some segmentation types are "end" events — turn overlay off, don't on
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
    # Overlay on/off
    # -------------------------------------------------------------------------

    async def _overlay_on(self, duration_ms: Optional[int]) -> None:
        if self.cfg.overlay_mode == "zmq":
            await self._zmq_send("ov", "enable", "1")
            await self._zmq_send("ov", "x", str(self.cfg.overlay_x))
            await self._zmq_send("ov", "y", str(self.cfg.overlay_y))
        else:
            # Restart mode — schedule encoder rebuild with enable='between(t,...)'
            if self.restart_encoder_cb:
                t1 = (duration_ms or 15000) / 1000.0
                await self.restart_encoder_cb(enable_expr=f"between(t,0,{t1})")

        self._active = True

        if self._overlay_off_task and not self._overlay_off_task.done():
            self._overlay_off_task.cancel()

        if duration_ms:
            self._overlay_off_task = asyncio.create_task(self._auto_off(duration_ms))

    async def _overlay_off(self) -> None:
        if self.cfg.overlay_mode == "zmq":
            await self._zmq_send("ov", "enable", "0")
        # Restart mode: nothing to do; encoder will return to normal at end of `between`
        self._active = False

    async def _auto_off(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            await self._overlay_off()
            log.info("Overlay auto-off after %d ms", duration_ms)
        except asyncio.CancelledError:
            pass

    # -------------------------------------------------------------------------
    # ZMQ I/O
    # -------------------------------------------------------------------------

    def _zmq_reconnect(self) -> None:
        """Close and reopen the REQ socket. Called after any send/recv failure.

        A ZMQ REQ socket enforces strict send→recv alternation (EFSM state
        machine). If recv times out the socket is left in a 'must-recv' state
        and the next send raises EFSM. The only recovery is close + reconnect.
        """
        if self._zmq_ctx is None:
            return
        try:
            if self._zmq_sock is not None:
                self._zmq_sock.close(linger=0)
        except Exception:
            pass
        self._zmq_sock = self._zmq_ctx.socket(zmq.REQ)
        self._zmq_sock.connect(self.cfg.zmq_bind)
        self._zmq_sock.setsockopt(zmq.RCVTIMEO, 500)
        self._zmq_sock.setsockopt(zmq.SNDTIMEO, 500)
        self._zmq_sock.setsockopt(zmq.LINGER, 0)
        log.debug("ZMQ socket reconnected to %s", self.cfg.zmq_bind)

    async def _zmq_send(self, filter_name: str, option: str, value: str) -> None:
        if self._zmq_sock is None:
            return
        msg = f"{filter_name} {option} {value}"
        try:
            await self._zmq_sock.send_string(msg)
            reply = await self._zmq_sock.recv_string()
            if not reply.startswith("0"):
                log.warning("ZMQ overlay command rejected: %s -> %s", msg, reply)
            else:
                log.debug("ZMQ overlay command ok: %s", msg)
        except Exception as e:
            log.warning("ZMQ send failed: %s (%s)", msg, e)
            # Reconnect so the next command starts with a clean socket state.
            self._zmq_reconnect()

    @property
    def active(self) -> bool:
        return self._active
