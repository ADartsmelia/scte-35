"""
FFmpeg pipeline manager.

Two FFmpeg processes per session:
  1. INGEST: pulls input (SRT/UDP/RTMP) and tees MPEG-TS to two local UDP feeds:
       - full TS  -> encoder_feed_port
       - SCTE-only TS -> detector_feed_port
     No re-encoding. -c copy. Failure-isolated from encoder.

  2. ENCODER: reads encoder_feed_port, applies overlay filter (with zmq
     control hook), encodes, writes to the configured output.

Both are supervised: on exit, exponential backoff restart up to a cap.

stderr is consumed line-by-line and stored in a small ring so the API can
expose recent diagnostics.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import shlex
import time
from typing import Optional

import sys

# ZMQ helper runs in a child process to avoid fork-safety issues.
# pyzmq contexts do not survive uvicorn's reloader fork cleanly, causing
# spurious timeouts when the context is held across a fork boundary.
_ZMQ_HELPER = """
import sys, zmq
port = int(sys.argv[1])
cmd  = sys.argv[2]
ctx  = zmq.Context()
sock = ctx.socket(zmq.REQ)
sock.setsockopt(zmq.LINGER, 0)
sock.setsockopt(zmq.RCVTIMEO, 2000)
sock.setsockopt(zmq.SNDTIMEO, 2000)
sock.connect(f"tcp://127.0.0.1:{port}")
try:
    sock.send_string(cmd)
    sock.recv_string()
    sys.exit(0)
except zmq.error.Again:
    sys.exit(1)
except Exception:
    sys.exit(2)
"""


async def _zmq_overlay_cmd(port: int, active: bool) -> bool:
    """Send 'overlay enable 1/0' to FFmpeg via a short-lived child process.

    Spawning a subprocess avoids pyzmq fork-safety issues that arise when
    uvicorn's reloader forks the server process and ZMQ internal threads
    are left in an undefined state inside the forked worker.
    """
    cmd = f"overlay enable {'1' if active else '0'}"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _ZMQ_HELPER, str(port), cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("ZMQ overlay cmd timed out (port=%d)", port)
            return False
        rc = proc.returncode
        if rc != 0:
            log.warning("ZMQ overlay cmd rc=%d port=%d out=%r err=%r", rc, port, stdout, stderr)
        return rc == 0
    except Exception as e:
        log.warning("ZMQ overlay cmd exception: %s: %s", type(e).__name__, e)
        return False

from .runtime_config import StreamConfig

log = logging.getLogger(__name__)


class FFmpegSupervisor:
    def __init__(self, name: str, build_cmd):
        self.name = name
        self.build_cmd = build_cmd  # callable -> list[str]
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stderr_ring: collections.deque[str] = collections.deque(maxlen=400)
        self.last_progress_at: float = 0.0
        # Latest out_time_us value parsed from -progress output (microseconds of
        # stream time that this process has consumed/produced). Used to compute
        # the ingest→encoder pipeline latency.
        self.last_out_time_us: int = 0
        # Quality metrics parsed from -progress output
        self.stats: dict = {
            "fps": 0.0,
            "bitrate_kbps": 0.0,
            "speed": 0.0,
            "frames": 0,
            "drop_frames": 0,
        }
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._restart_count = 0
        self._last_start: float = 0.0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"ffmpeg-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            cmd = self.build_cmd()
            log.info("[%s] starting: %s", self.name, " ".join(shlex.quote(a) for a in cmd))
            self._last_start = time.time()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:
                log.error("[%s] failed to spawn: %s", self.name, e)
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            stderr_task = asyncio.create_task(self._drain_stderr())
            rc = await self.process.wait()
            await stderr_task

            if self._stop.is_set():
                break

            elapsed = time.time() - self._last_start
            log.warning("[%s] exited rc=%s after %.1fs", self.name, rc, elapsed)
            if elapsed > 60:
                backoff = 1.0  # stable run, reset
                self._restart_count = 0
            else:
                self._restart_count += 1
                backoff = min(backoff * 2, 30.0)

            await self._sleep_backoff(backoff)

    async def _sleep_backoff(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        async for line in self.process.stderr:
            try:
                s = line.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            self.stderr_ring.append(s)
            # FFmpeg's -progress pipe:2 writes key=value lines to stderr.
            if "=" not in s:
                continue
            key, _, val = s.partition("=")
            try:
                if key == "out_time_us":
                    v = int(val)
                    if v > 0:
                        self.last_out_time_us = v
                        self.last_progress_at = time.time()
                elif key == "fps":
                    self.stats["fps"] = float(val)
                    self.last_progress_at = time.time()
                elif key == "bitrate":
                    # value looks like "4096.0kbits/s" or "N/A"
                    if val and val[0].isdigit():
                        self.stats["bitrate_kbps"] = float(val.split("k")[0])
                elif key == "speed":
                    # value looks like "1.05x"
                    self.stats["speed"] = float(val.rstrip("x")) if val != "N/A" else 0.0
                elif key == "frame":
                    self.stats["frames"] = int(val)
                elif key == "drop_frames":
                    self.stats["drop_frames"] = int(val)
            except (ValueError, IndexError):
                pass


# -----------------------------------------------------------------------------
# Command builders
# -----------------------------------------------------------------------------

def build_ingest_cmd(cfg: StreamConfig) -> list[str]:
    """
    Ingest pulls the input, copies all streams, and tees to two local UDP feeds.

    The tee muxer with `select=` lets us narrow one branch to just data streams
    (the SCTE-35 PID), keeping the detector cheap and the encoder feed full.
    """
    encoder_feed = f"udp://127.0.0.1:{cfg.encoder_feed_port}?pkt_size=1316"
    # `select` accepts a stream specifier. `d` matches all data streams, which
    # is where the SCTE-35 elementary stream lives. No quoting needed: argv-mode
    # subprocess calls don't pass through a shell.
    detector_feed = f"udp://127.0.0.1:{cfg.detector_feed_port}?pkt_size=1316"

    tee_target = (
        f"[f=mpegts]{encoder_feed}|"
        f"[f=mpegts:select=d]{detector_feed}"
    )

    return [
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",
        # Structured progress to stderr every 1 s — parsed for latency tracking
        "-progress", "pipe:2", "-stats_period", "1",
        "-fflags", "+nobuffer+genpts",
        "-rw_timeout", "5000000",        # 5s socket timeout, applies to UDP/RTMP/SRT readers
        "-i", cfg.input_url,
        "-map", "0",
        "-c", "copy",
        "-copyts",
        "-f", "tee",
        tee_target,
    ]


def build_encoder_cmd(cfg: StreamConfig, overlay_active: bool = False) -> list[str]:
    """
    Encoder reads the local TS feed, optionally composites the overlay, and
    writes to the configured output.

    When overlay_path is set, the overlay and ZMQ filter are always included in
    the graph. The overlay's enable expression is baked to the current
    overlay_active value so crash-restarts self-heal without an extra ZMQ call.
    Runtime toggling is done by sending 'overlay enable 1/0' via ZMQ — no
    process restart needed.
    """
    encoder_feed = f"udp://127.0.0.1:{cfg.encoder_feed_port}?fifo_size=5000000&overrun_nonfatal=1&timeout=60000000"

    use_overlay = bool(cfg.overlay_path)

    # ── No-overlay path: simple re-encode, no filter graph ───────────────────
    if not use_overlay:
        return [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-progress", "pipe:2", "-stats_period", "1",
            "-fflags", "+nobuffer+genpts",
            "-thread_queue_size", "2048",
            "-i", encoder_feed,
            "-map", "0:v",
            "-map", "0:a?",
            "-c:v", cfg.video_codec,
            "-preset", cfg.encoder_preset,
            "-tune", cfg.encoder_tune,
            "-b:v", cfg.encoder_bitrate,
            "-g", str(cfg.encoder_gop),
            "-bf", "0",
            "-threads", "0",
            "-max_muxing_queue_size", "4096",
            "-c:a", cfg.audio_codec,
            "-b:a", cfg.audio_bitrate,
            "-mpegts_flags", "initial_discontinuity",
            "-f", cfg.output_format,
            cfg.output_url,
        ]

    # ── Overlay path: overlay + ZMQ control filter always in the graph ───────
    # The enable expression is baked from overlay_active so that if the encoder
    # crashes and auto-restarts, it comes back in the correct state without
    # needing an extra ZMQ round-trip. Runtime toggling uses ZMQ (no restart).
    scale = ""
    if cfg.overlay_w or cfg.overlay_h:
        w = cfg.overlay_w or -1
        h = cfg.overlay_h or -1
        scale = f"scale={w}:{h},"

    enable = "1" if overlay_active else "0"

    # Use the zmq filter with no explicit bind_address so we avoid FFmpeg's
    # colon-escaping rules entirely. The default is tcp://*:<port> where port
    # is 5557. The Python client connects to tcp://127.0.0.1:5557.
    # If cfg.zmq_port differs from 5557, pass it via the short "b" alias which
    # only needs the port number — no colon in the scheme prefix to escape.
    if cfg.zmq_port == 5557:
        zmq_filter = "zmq"
    else:
        zmq_filter = f"zmq=bind_address=tcp\\\\://*\\\\:{cfg.zmq_port}"

    filter_complex = (
        f"[1:v]{scale}format=rgba,setpts=PTS-STARTPTS[ovin];"
        f"[0:v][ovin]overlay=x={cfg.overlay_x}:y={cfg.overlay_y}:"
        f"format=auto:enable='{enable}'[pre];"
        f"[pre]{zmq_filter}[vout]"
    )

    return [
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:2", "-stats_period", "1",
        "-fflags", "+nobuffer+genpts",
        "-thread_queue_size", "2048",
        "-i", encoder_feed,
        "-stream_loop", "-1",
        "-thread_queue_size", "1024",
        "-i", cfg.overlay_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", cfg.video_codec,
        "-preset", cfg.encoder_preset,
        "-tune", cfg.encoder_tune,
        "-b:v", cfg.encoder_bitrate,
        "-g", str(cfg.encoder_gop),
        "-bf", "0",
        "-threads", "0",
        "-max_muxing_queue_size", "4096",
        "-c:a", cfg.audio_codec,
        "-b:a", cfg.audio_bitrate,
        "-mpegts_flags", "initial_discontinuity",
        "-f", cfg.output_format,
        cfg.output_url,
    ]


# -----------------------------------------------------------------------------
# Combined session
# -----------------------------------------------------------------------------

class StreamSession:
    """One pipeline = ingest + encoder. The detector is owned outside this class."""

    def __init__(self, cfg: StreamConfig):
        self.cfg = cfg
        self.ingest = FFmpegSupervisor("ingest", lambda: build_ingest_cmd(cfg))
        self._overlay_active = False
        self._overlay_lock = asyncio.Lock()
        self.encoder = FFmpegSupervisor(
            "encoder", lambda: build_encoder_cmd(cfg, overlay_active=self._overlay_active)
        )

    async def start(self) -> None:
        await self.ingest.start()
        # Small head start so the encoder's UDP listener has a sender to read from
        await asyncio.sleep(0.4)
        await self.encoder.start()

    async def stop(self) -> None:
        await asyncio.gather(self.encoder.stop(), self.ingest.stop(), return_exceptions=True)

    async def set_overlay(self, active: bool) -> None:
        """Toggle the overlay at runtime.

        When overlay_path is configured, sends a ZMQ command to FFmpeg's zmq
        filter — no process restart, no black frame, sub-millisecond toggle.
        The lock serialises concurrent callers (auto-off + new trigger race).
        Falls back to an encoder restart only if ZMQ fails after retries (e.g.
        encoder is in the middle of restarting after a crash).
        """
        async with self._overlay_lock:
            if self._overlay_active == active:
                return
            self._overlay_active = active

            if self.cfg.overlay_path:
                for attempt in range(3):
                    if attempt:
                        await asyncio.sleep(0.5)
                    if await _zmq_overlay_cmd(self.cfg.zmq_port, active):
                        log.info("Overlay toggled via ZMQ: active=%s", active)
                        return
                log.warning("ZMQ toggle failed after 3 attempts — falling back to encoder restart")

            # Fallback (no overlay_path, or ZMQ unavailable): restart encoder.
            # The new command has overlay_active baked in so it starts correctly.
            log.info("Restarting encoder with overlay_active=%s", active)
            await self.encoder.stop()
            await asyncio.sleep(1.0)
            await self.encoder.start()

    def status(self) -> dict:
        # Compute pipeline latency: how far behind the encoder is versus the
        # ingest process, in milliseconds. Both values come from FFmpeg's
        # -progress output (out_time_us), so they're in the same PTS domain.
        latency_ms: Optional[int] = None
        ing_us = self.ingest.last_out_time_us
        enc_us = self.encoder.last_out_time_us
        if ing_us > 0 and enc_us > 0:
            # Suppress the metric for ~15 s after each encoder restart.
            # Right after a restart, enc_us resets to 0 and climbs slowly while
            # ing_us is already far ahead, producing a spuriously large latency.
            enc_age = time.time() - self.encoder._last_start
            if enc_age > 15.0:
                diff = ing_us - enc_us
                # Guard against transient negative values (encoder briefly ahead due
                # to B-frame reordering or clock skew between progress reports).
                latency_ms = max(0, diff // 1000)

        return {
            "ingest": {
                "running": self.ingest.running,
                "restarts": self.ingest._restart_count,
                "last_progress_at": self.ingest.last_progress_at,
                "out_time_us": ing_us,
                "stats": dict(self.ingest.stats),
                "stderr_tail": list(self.ingest.stderr_ring)[-20:],
            },
            "encoder": {
                "running": self.encoder.running,
                "restarts": self.encoder._restart_count,
                "last_progress_at": self.encoder.last_progress_at,
                "out_time_us": enc_us,
                "stats": dict(self.encoder.stats),
                "stderr_tail": list(self.encoder.stderr_ring)[-20:],
            },
            "stream_latency_ms": latency_ms,
        }
