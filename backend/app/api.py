"""FastAPI control plane — multi-stream edition.

Each named stream slot stores its configuration in SQLite. At runtime, any
number of slots can be started simultaneously; each runs its own ingest +
encoder + detector + overlay controller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .db import StreamStore
from .marker_detector import MarkerDetector
from .marker_store import MarkerStore
from .overlay_controller import OverlayController
from .runtime_config import RuntimeState, StreamConfig
from .stream_processor import StreamSession

log = logging.getLogger(__name__)


# ── Pydantic API models ───────────────────────────────────────────────────────

class StreamBody(BaseModel):
    """Full config for creating or updating a stream slot."""
    name: str
    input_url: str
    output_url: str
    output_format: str = "mpegts"
    overlay_path: str = ""
    overlay_x: int = 10
    overlay_y: int = 10
    overlay_w: Optional[int] = None
    overlay_h: Optional[int] = None
    overlay_duration_ms: Optional[int] = None
    triggered_segmentation_types: Optional[list[int]] = None
    encoder_preset: str = "veryfast"
    encoder_bitrate: str = "4M"


class StreamUpdateBody(BaseModel):
    """Partial update — all fields optional."""
    name: Optional[str] = None
    input_url: Optional[str] = None
    output_url: Optional[str] = None
    output_format: Optional[str] = None
    overlay_path: Optional[str] = None
    overlay_x: Optional[int] = None
    overlay_y: Optional[int] = None
    overlay_w: Optional[int] = None
    overlay_h: Optional[int] = None
    overlay_duration_ms: Optional[int] = None
    triggered_segmentation_types: Optional[list[int]] = None
    encoder_preset: Optional[str] = None
    encoder_bitrate: Optional[str] = None


class AdBreakRequest(BaseModel):
    duration_ms: int = 15000


# ── Per-stream runtime instance ───────────────────────────────────────────────

class StreamInstance:
    """One running pipeline. Created on /start, torn down on /stop."""

    def __init__(self, stream_id: int, cfg: StreamConfig):
        self.stream_id = stream_id
        self.cfg = cfg
        self.store = MarkerStore()
        self.session: Optional[StreamSession] = None
        self.detector: Optional[MarkerDetector] = None
        self.controller: Optional[OverlayController] = None
        self._detector_task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._ws_clients: set[WebSocket] = set()
        self.overlay_active: bool = False
        self.last_event: Optional[dict] = None
        self.started_at: float = time.time()

    async def start(self) -> None:
        self.detector = MarkerDetector(
            listen_host="127.0.0.1",
            listen_port=self.cfg.detector_feed_port,
        )
        self._detector_task = asyncio.create_task(
            self.detector.run(), name=f"detector-{self.stream_id}"
        )

        self.session = StreamSession(self.cfg)
        await self.session.start()

        self.controller = OverlayController(
            self.cfg,
            set_overlay_cb=self.session.set_overlay,
        )

        self._consumer_task = asyncio.create_task(
            self._consume_events(), name=f"consumer-{self.stream_id}"
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name=f"watchdog-{self.stream_id}"
        )

    async def stop(self) -> None:
        for task in [self._watchdog_task, self._consumer_task]:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._watchdog_task = None
        self._consumer_task = None

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

            row = self.store.add(event, overlay_result, source="auto")
            self.last_event = row
            self.overlay_active = self.controller.active
            await self._broadcast(row)

    async def _watchdog(self) -> None:
        down_since: Optional[float] = None
        while True:
            try:
                await asyncio.sleep(5.0)
                if self.session is None:
                    return
                both_down = (
                    not self.session.ingest.running
                    and not self.session.encoder.running
                )
                if both_down:
                    if down_since is None:
                        down_since = time.time()
                    elif time.time() - down_since > 10.0:
                        log.warning(
                            "Watchdog [stream %d]: both processes down >10 s — restarting",
                            self.stream_id,
                        )
                        down_since = None
                        await self.session.ingest.stop()
                        await self.session.ingest.start()
                        await asyncio.sleep(0.5)
                        await self.session.encoder.stop()
                        await self.session.encoder.start()
                else:
                    down_since = None
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Watchdog [stream %d] error", self.stream_id)

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
        out: dict = {
            "running": self.session is not None,
            "started_at": self.started_at,
            "overlay_active": self.overlay_active,
            "last_event": self.last_event,
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


# ── Global registry ───────────────────────────────────────────────────────────

class StreamRegistry:
    """Maps stream_id → StreamInstance for currently running streams."""

    def __init__(self):
        self._running: dict[int, StreamInstance] = {}
        self._lock = asyncio.Lock()
        self.db = StreamStore()

    def is_running(self, stream_id: int) -> bool:
        return stream_id in self._running

    def get_instance(self, stream_id: int) -> Optional[StreamInstance]:
        return self._running.get(stream_id)

    async def start(self, stream_id: int, cfg_data: dict) -> StreamInstance:
        async with self._lock:
            if stream_id in self._running:
                raise HTTPException(409, "stream already running")
            cfg = _build_config(cfg_data)
            inst = StreamInstance(stream_id, cfg)
            await inst.start()
            self._running[stream_id] = inst
            return inst

    async def stop(self, stream_id: int) -> None:
        async with self._lock:
            inst = self._running.pop(stream_id, None)
            if inst is None:
                raise HTTPException(409, "stream not running")
            await inst.stop()

    async def stop_all(self) -> None:
        ids = list(self._running.keys())
        for sid in ids:
            try:
                await self.stop(sid)
            except Exception:
                pass

    def enriched_list(self, db_rows: list[dict]) -> list[dict]:
        """Merge DB records with live runtime status."""
        result = []
        for row in db_rows:
            sid = row["id"]
            inst = self._running.get(sid)
            entry = dict(row)
            if inst:
                entry.update(inst.status())
            else:
                entry["running"] = False
                entry["overlay_active"] = False
            result.append(entry)
        return result


def _build_config(data: dict) -> StreamConfig:
    cfg = StreamConfig(
        input_url=data["input_url"],
        output_url=data["output_url"],
        output_format=data.get("output_format", "mpegts"),
        overlay_path=data.get("overlay_path", ""),
        overlay_x=data.get("overlay_x", 10),
        overlay_y=data.get("overlay_y", 10),
        overlay_w=data.get("overlay_w"),
        overlay_h=data.get("overlay_h"),
        overlay_duration_ms=data.get("overlay_duration_ms"),
        encoder_preset=data.get("encoder_preset", "veryfast"),
        encoder_bitrate=data.get("encoder_bitrate", "4M"),
    )
    types = data.get("triggered_segmentation_types")
    if types:
        cfg.triggered_segmentation_types = set(types)
    return cfg


def _config_to_dict(cfg: StreamConfig) -> dict:
    return {
        "input_url":  cfg.input_url,
        "output_url": cfg.output_url,
        "output_format": cfg.output_format,
        "overlay_path": cfg.overlay_path,
        "overlay_x": cfg.overlay_x,
        "overlay_y": cfg.overlay_y,
        "overlay_w": cfg.overlay_w,
        "overlay_h": cfg.overlay_h,
        "overlay_duration_ms": cfg.overlay_duration_ms,
        "encoder_preset": cfg.encoder_preset,
        "encoder_bitrate": cfg.encoder_bitrate,
        "triggered_segmentation_types": list(cfg.triggered_segmentation_types),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

def make_router(reg: StreamRegistry) -> APIRouter:
    r = APIRouter()

    # ── Stream CRUD ──────────────────────────────────────────────────────────

    @r.get("/api/streams")
    async def list_streams():
        rows = await reg.db.list()
        return {"streams": reg.enriched_list(rows)}

    @r.post("/api/streams", status_code=201)
    async def create_stream(body: StreamBody):
        data = body.model_dump(exclude={"name"})
        saved = await reg.db.create(body.name, data)
        return {**saved, "running": False, "overlay_active": False}

    @r.get("/api/streams/{stream_id}")
    async def get_stream(stream_id: int):
        row = await reg.db.get(stream_id)
        if row is None:
            raise HTTPException(404, "stream not found")
        inst = reg.get_instance(stream_id)
        if inst:
            return {**row, **inst.status()}
        return {**row, "running": False, "overlay_active": False}

    @r.put("/api/streams/{stream_id}")
    async def update_stream(stream_id: int, body: StreamUpdateBody):
        if reg.is_running(stream_id):
            raise HTTPException(409, "stop the stream before editing its config")
        existing = await reg.db.get(stream_id)
        if existing is None:
            raise HTTPException(404, "stream not found")
        patch = body.model_dump(exclude_none=True)
        new_name = patch.pop("name", None)
        merged = {**existing["config"], **patch}
        updated = await reg.db.update(stream_id, name=new_name, data=merged)
        return {**updated, "running": False, "overlay_active": False}

    @r.delete("/api/streams/{stream_id}")
    async def delete_stream(stream_id: int):
        if reg.is_running(stream_id):
            raise HTTPException(409, "stop the stream before deleting it")
        deleted = await reg.db.delete(stream_id)
        if not deleted:
            raise HTTPException(404, "stream not found")
        return {"ok": True}

    # ── Start / Stop ─────────────────────────────────────────────────────────

    @r.post("/api/streams/{stream_id}/start")
    async def start_stream(stream_id: int):
        row = await reg.db.get(stream_id)
        if row is None:
            raise HTTPException(404, "stream not found")
        inst = await reg.start(stream_id, row["config"])
        return {"ok": True, "stream_id": stream_id}

    @r.post("/api/streams/{stream_id}/stop")
    async def stop_stream(stream_id: int):
        await reg.stop(stream_id)
        return {"ok": True}

    # ── Status ───────────────────────────────────────────────────────────────

    @r.get("/api/streams/{stream_id}/status")
    async def stream_status(stream_id: int):
        inst = reg.get_instance(stream_id)
        if inst is None:
            return {"running": False}
        return inst.status()

    # ── Markers ──────────────────────────────────────────────────────────────

    @r.get("/api/streams/{stream_id}/markers")
    async def get_markers(stream_id: int, limit: int = 200, since_id: int = 0):
        inst = reg.get_instance(stream_id)
        events = inst.store.list(limit=limit, since_id=since_id) if inst else []
        return {"events": events}

    @r.delete("/api/streams/{stream_id}/markers")
    async def clear_markers(stream_id: int):
        inst = reg.get_instance(stream_id)
        if inst:
            inst.store.clear()
        return {"ok": True}

    # ── Manual overlay ───────────────────────────────────────────────────────

    @r.post("/api/streams/{stream_id}/ad/start")
    async def ad_start(stream_id: int, req: AdBreakRequest):
        inst = reg.get_instance(stream_id)
        if inst is None or inst.controller is None:
            raise HTTPException(409, "stream not running")
        synthetic_event = {
            "splice_event_id": None,
            "segmentation_event_id": None,
            "segmentation_type_id": 0x22,
            "segmentation_type": "Ad Break Started",
            "duration_pts": int(req.duration_ms * 90),
            "duration_seconds": req.duration_ms / 1000.0,
            "pts_time": None,
            "pts_adjustment": 0,
            "splice_command_type": 0x06,
            "crc32_ok": True,
            "pid": None,
            "received_at": time.time(),
        }
        inst.controller._last_event_id = None
        overlay_result = await inst.controller.handle_event(synthetic_event)
        row = inst.store.add(synthetic_event, overlay_result, source="manual")
        inst.last_event = row
        inst.overlay_active = inst.controller.active
        await inst._broadcast(row)
        return {"ok": True, "duration_ms": req.duration_ms, "overlay_applied": overlay_result.get("overlay_applied")}

    @r.post("/api/streams/{stream_id}/ad/stop")
    async def ad_stop(stream_id: int):
        inst = reg.get_instance(stream_id)
        if inst is None or inst.controller is None:
            raise HTTPException(409, "stream not running")
        await inst.controller._overlay_off()
        inst.overlay_active = False
        return {"ok": True}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @r.websocket("/ws/streams/{stream_id}/events")
    async def ws_events(stream_id: int, ws: WebSocket):
        await ws.accept()
        inst = reg.get_instance(stream_id)
        if inst:
            inst._ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            inst = reg.get_instance(stream_id)
            if inst:
                inst._ws_clients.discard(ws)

    return r


def make_app() -> FastAPI:
    fa = FastAPI(title="SCTE-35 Overlay Engine")
    reg = StreamRegistry()
    fa.include_router(make_router(reg))
    fa.state.registry = reg

    from fastapi.middleware.cors import CORSMiddleware
    fa.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @fa.on_event("shutdown")
    async def _shutdown():
        await reg.stop_all()

    return fa
