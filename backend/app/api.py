"""FastAPI control plane."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .marker_detector import MarkerDetector
from .marker_store import MarkerStore
from .overlay_controller import OverlayController
from .runtime_config import RuntimeState, StreamConfig
from .stream_processor import StreamSession

log = logging.getLogger(__name__)


# --- Pydantic API models -----------------------------------------------------

class StartRequest(BaseModel):
    input_url: str
    output_url: str
    output_format: str = "mpegts"
    overlay_path: str = ""
    overlay_x: int = 10
    overlay_y: int = 10
    overlay_w: Optional[int] = None
    overlay_h: Optional[int] = None
    overlay_duration_ms: Optional[int] = None
    overlay_mode: str = "zmq"
    triggered_segmentation_types: Optional[list[int]] = None
    encoder_preset: str = "veryfast"
    encoder_bitrate: str = "4M"


class ZmqRequest(BaseModel):
    command: str  # e.g. "ov enable 1"


class AdBreakRequest(BaseModel):
    duration_ms: int = 15000  # how long to show the overlay, milliseconds


# --- Application singleton ---------------------------------------------------

class App:
    def __init__(self):
        self.state = RuntimeState()
        self.store = MarkerStore()
        self.session: Optional[StreamSession] = None
        self.detector: Optional[MarkerDetector] = None
        self.controller: Optional[OverlayController] = None
        self._detector_task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._ws_clients: set[WebSocket] = set()

    async def start(self, req: StartRequest) -> None:
        if self.session is not None:
            raise HTTPException(409, "pipeline already running")

        cfg = StreamConfig(
            input_url=req.input_url,
            output_url=req.output_url,
            output_format=req.output_format,
            overlay_path=req.overlay_path,
            overlay_x=req.overlay_x,
            overlay_y=req.overlay_y,
            overlay_w=req.overlay_w,
            overlay_h=req.overlay_h,
            overlay_duration_ms=req.overlay_duration_ms,
            overlay_mode=req.overlay_mode,
            encoder_preset=req.encoder_preset,
            encoder_bitrate=req.encoder_bitrate,
        )
        if req.triggered_segmentation_types:
            cfg.triggered_segmentation_types = set(req.triggered_segmentation_types)

        self.state.config = cfg
        self.state.started_at = time.time()

        # 1) Start detector first (binds the UDP listener) so no packets are missed
        self.detector = MarkerDetector(
            listen_host="127.0.0.1",
            listen_port=cfg.detector_feed_port,
        )
        self._detector_task = asyncio.create_task(self.detector.run(), name="detector")

        # 2) Start ingest+encoder
        self.session = StreamSession(cfg)
        await self.session.start()

        # 3) Wire up controller (after encoder is up so zmq has a peer)
        await asyncio.sleep(0.5)
        self.controller = OverlayController(cfg)

        # 4) Consumer loop: detector queue -> controller -> store -> ws broadcast
        self._consumer_task = asyncio.create_task(self._consume_events(), name="consumer")

    async def stop(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
        if self.controller:
            await self.controller.close()
            self.controller = None
        if self.session:
            await self.session.stop()
            self.session = None
        if self.detector:
            self.detector.stop()
        if self._detector_task:
            self._detector_task.cancel()
            try:
                await self._detector_task
            except (asyncio.CancelledError, Exception):
                pass
            self._detector_task = None
        self.detector = None
        self.state = RuntimeState()

    async def _consume_events(self) -> None:
        assert self.detector and self.controller
        while True:
            try:
                event = await self.detector.out_queue.get()
            except asyncio.CancelledError:
                return
            try:
                overlay_result = await self.controller.handle_event(event)
            except Exception as e:
                log.exception("controller failed: %s", e)
                overlay_result = {"overlay_applied": False, "reason": f"error: {e}"}

            row = self.store.add(event, overlay_result)
            self.state.last_event = row
            self.state.last_overlay_result = overlay_result
            self.state.overlay_active = self.controller.active
            await self._broadcast(row)

    async def _broadcast(self, row: dict) -> None:
        dead = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json({"type": "marker", "data": row})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    def status(self) -> dict:
        out = {
            "running": self.session is not None,
            "started_at": self.state.started_at,
            "overlay_active": self.state.overlay_active,
            "last_event": self.state.last_event,
            "last_overlay_result": self.state.last_overlay_result,
        }
        if self.session:
            out["pipeline"] = self.session.status()
        if self.detector:
            out["detector"] = {
                "stats": self.detector.stats,
                "scte_pid": (
                    f"0x{self.detector._scte_pid:04x}"
                    if self.detector._scte_pid is not None else None
                ),
                "programs": self.detector._pmt_pid_by_program,
            }
        return out


# --- Routes ------------------------------------------------------------------

def make_router(app_state: App) -> APIRouter:
    r = APIRouter()

    @r.post("/api/start")
    async def start(req: StartRequest):
        await app_state.start(req)
        return {"ok": True}

    @r.post("/api/stop")
    async def stop():
        await app_state.stop()
        return {"ok": True}

    @r.get("/api/status")
    async def status():
        return app_state.status()

    @r.get("/api/markers")
    async def markers(limit: int = 200, since_id: int = 0):
        return {"events": app_state.store.list(limit=limit, since_id=since_id)}

    @r.delete("/api/markers")
    async def clear_markers():
        app_state.store.clear()
        return {"ok": True}

    @r.post("/api/zmq")
    async def zmq_send(req: ZmqRequest):
        """Manual ZMQ command pass-through for the dashboard 'force overlay' buttons.

        Accepts e.g. 'ov enable 1', 'ov x 100'. Returns the FFmpeg zmq filter reply.
        """
        if app_state.controller is None:
            raise HTTPException(409, "pipeline not running")
        if app_state.controller.cfg.overlay_mode != "zmq":
            raise HTTPException(400, "overlay_mode is not 'zmq'")
        parts = req.command.strip().split(None, 2)
        if len(parts) != 3:
            raise HTTPException(400, "command must be: '<filter> <option> <value>'")
        filt, opt, val = parts
        try:
            await app_state.controller._zmq_send(filt, opt, val)
            # Reflect the active state if it's the enable toggle
            if filt == "ov" and opt == "enable":
                app_state.controller._active = (val.strip() == "1")
                app_state.state.overlay_active = app_state.controller._active
            return {"ok": True, "command": req.command}
        except Exception as e:
            raise HTTPException(500, f"zmq send failed: {e}")

    @r.post("/api/ad/start")
    async def ad_start(req: AdBreakRequest):
        """Manually trigger an overlay ad break for the given duration."""
        if app_state.controller is None:
            raise HTTPException(409, "pipeline not running")
        # Build a synthetic event that looks like a Break Start
        synthetic_event = {
            "splice_event_id": None,
            "segmentation_event_id": None,
            "segmentation_type_id": 0x22,  # Break Start
            "segmentation_type": "Ad Break Started",
            "duration_pts": int(req.duration_ms * 90),  # ms → 90kHz ticks
            "duration_seconds": req.duration_ms / 1000.0,
            "pts_time": None,
            "pts_adjustment": 0,
            "splice_command_type": 0x06,
            "crc32_ok": True,
            "pid": None,
            "received_at": time.time(),
        }
        # Force a new event ID so dedup doesn't block it
        app_state.controller._last_event_id = None
        overlay_result = await app_state.controller.handle_event(synthetic_event)
        row = app_state.store.add(synthetic_event, overlay_result)
        app_state.state.last_event = row
        app_state.state.overlay_active = app_state.controller.active
        await app_state._broadcast(row)
        return {"ok": True, "duration_ms": req.duration_ms, "overlay_applied": overlay_result.get("overlay_applied")}

    @r.post("/api/ad/stop")
    async def ad_stop():
        """Manually end an overlay ad break immediately."""
        if app_state.controller is None:
            raise HTTPException(409, "pipeline not running")
        await app_state.controller._overlay_off()
        app_state.state.overlay_active = False
        return {"ok": True}

    @r.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        await ws.accept()
        app_state._ws_clients.add(ws)
        try:
            while True:
                # We only push; consume any pings from client
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app_state._ws_clients.discard(ws)

    return r


def make_app() -> FastAPI:
    fa = FastAPI(title="SCTE-35 Overlay Engine")
    state = App()
    fa.include_router(make_router(state))
    fa.state.app_state = state

    from fastapi.middleware.cors import CORSMiddleware
    fa.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @fa.on_event("shutdown")
    async def _shutdown():
        await state.stop()

    return fa
