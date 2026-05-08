# Real-time SCTE-35 Overlay Engine — Architecture

This document defines the system that ingests a live MPEG-TS stream (over SRT/UDP/RTMP),
detects SCTE-35 splice markers in real time, and reacts by inserting a graphical overlay
into the outbound stream. It is designed to run 24/7.

---

## 1. Process topology

The system is **multi-process by design**. Each FFmpeg instance has one job. Python
supervises and owns the SCTE-35 logic.

```
                    ┌──────────────────────────────────────────────┐
                    │           FastAPI Control Plane              │
                    │  /api/start  /api/stop  /api/status          │
                    │  /api/markers   ws:/ws/events                │
                    └────────────┬─────────────────┬───────────────┘
                                 │ spawn/supervise │ spawn/supervise
                                 ▼                 ▼
        ┌──────────────────────────────┐  ┌────────────────────────────┐
        │  FFmpeg INGEST (process #1)  │  │  Marker Detector (asyncio) │
        │  -i $INPUT_URL               │  │                            │
        │  -map 0 -c copy              │  │  - reads UDP MPEG-TS feed  │
        │  -f tee \                    │  │  - parses PAT → PMT        │
        │   "[f=mpegts]udp://...:5000  │  │  - locates SCTE-35 PID     │
        │    |[select=d:f=mpegts]      │  │    (stream_type 0x86,      │
        │      udp://...:5001"         │  │     CUEI registration)     │
        │                              │  │  - reassembles PSI sections│
        │  Job: copy in → fan out      │  │  - calls scte_parser       │
        │  Never re-encodes.           │  │  - emits typed events      │
        └──────────────┬───────────────┘  └─────────────┬──────────────┘
                       │ 5000 (full TS)                 │ asyncio.Queue
                       │ 5001 (SCTE-only TS) ───────────┘
                       ▼
        ┌──────────────────────────────┐  ┌────────────────────────────┐
        │  FFmpeg ENCODER (process #2) │◄─┤  Overlay Controller        │
        │  -i udp://...:5000           │  │                            │
        │  -i overlay.mov              │ZMQ│  Policy: which segmentation│
        │  -filter_complex             │  │  types trigger overlay.    │
        │   "[0:v][1:v]overlay=        │  │  Schedules enable on/off   │
        │      x=X:y=Y:                │  │  using PTS or wall clock.  │
        │      enable=0,               │  │  Sends ZMQ commands to     │
        │    zmq=bind_address=tcp://...│  │  FFmpeg's zmq filter.      │
        │  -c:v libx264 -preset ...    │  └────────────────────────────┘
        │  -f $OUT_FORMAT $OUTPUT_URL  │
        └──────────────┬───────────────┘
                       ▼
                  Output (SRT/UDP/RTMP)
```

### Why two FFmpeg processes, not one

* **Restart isolation.** When the encoder restarts (configuration change, overlay file
  swap, codec error), the ingest process keeps the SRT/RTMP session alive. The detector
  keeps reading SCTE-35. The control plane never loses its grip on the source.
* **Failure surface.** Ingest is the part most likely to disconnect (network). Encoder
  is the part most likely to die (codec edge cases, GPU OOM, filter graph errors).
  Separating them means a failure of one doesn't take the other with it.
* **Observability.** You can independently measure: input bitrate (ingest stderr),
  output bitrate (encoder stderr), SCTE arrival rate (detector). Each has its own
  health signal.

### Why not one Python process pulling MPEG-TS itself

You could. `aiortsp` / a custom SRT reader would give you direct packet access. But:

* SRT, RTMP, and UDP each have their own quirks and you'd be re-implementing them.
* FFmpeg's I/O is battle-tested for 24/7. Use it.
* The cost is one extra UDP hop on localhost — negligible (< 1ms, no packet loss
  on `lo` if you size buffers right).

---

## 2. Data flow

```
Input           Ingest FFmpeg          Local UDP        Detector / Encoder
─────           ─────────────          ─────────        ──────────────────

SRT             demux MPEG-TS          full TS  ─────►  Encoder FFmpeg
UDP        ───► copy all PIDs    ───►   :5000           (overlay + re-encode)
RTMP            tee muxer:                                       │
                                                                 ▼
                                       SCTE-only ─────►  Output (SRT/UDP/RTMP)
                                       :5001
                                                ─────►  Detector (Python)
                                                        parses splice_info_section
                                                        emits events
                                                                 │
                                                                 ▼
                                                        Overlay Controller
                                                        ZMQ ──► Encoder filter graph
```

Two key properties:

1. **The encoder consumes the same MPEG-TS that contains SCTE-35.** When we toggle
   the overlay via ZMQ, the toggle is applied to whatever frame the encoder is
   currently processing. The detector tells us *that* a marker fired; the encoder
   already has that PTS in its pipeline.

2. **Detector latency is bounded by one TS packet (188 bytes) plus PSI section
   reassembly (typically a single packet for splice_info_section).** From the moment
   the SCTE-35 packet hits the wire to the moment our parser emits the event:
   single-digit milliseconds.

---

## 3. SCTE-35 extraction — the right way

SCTE-35 is a binary `splice_info_section` carried in its own elementary stream
inside the MPEG-TS multiplex. Identifying details:

* **Stream type in PMT:** `0x86`
* **Registration descriptor:** `format_identifier == "CUEI"` (0x43554549)
* **Section table_id:** `0xFC`
* **MIME / FFmpeg codec name:** `scte_35` (codec_type=`data`)

There is **no log-based path that is correct**. FFmpeg does not decode the section.
ffprobe will print PTS metadata but not the splice command payload. You must read
the binary section yourself.

### Why we don't use `ffprobe -show_packets`

* It can stream JSON for data packets, but it gives you the TS payload after FFmpeg's
  own buffering — typically several hundred ms behind real time.
* The payload it shows is base64 of the section, which means you'd parse it anyway —
  you've added latency for no reason.
* Worst: ffprobe under load will silently drop or coalesce packets.

### Why we don't use FFmpeg pipes for SCTE

You can do `-map 0:d -c copy -f data pipe:1` and read the elementary stream payload.
This works for getting the bytes out, but:

* You lose the TS framing (PUSI, continuity counter), so you can't detect drops.
* You still have to parse `splice_info_section` yourself.
* Buffering at the FFmpeg → pipe boundary adds ~50ms latency variance.

The cleaner approach is the one shown above: tee the **whole MPEG-TS** into a
local UDP socket, then run a thin Python TS demuxer that only cares about three
PIDs (PAT=0x0000, the program's PMT PID, and the SCTE PID once discovered).

### Why not GStreamer

GStreamer has `tsdemux` and SCTE-35 awareness via `GstMpegtsSCTESIT`. It is a
defensible choice. Tradeoffs vs FFmpeg:

| Concern              | FFmpeg                                  | GStreamer                              |
|----------------------|------------------------------------------|----------------------------------------|
| Input protocol cov.  | excellent (SRT, RTMP, UDP all native)    | excellent                              |
| SCTE-35 native parse | no (we parse manually)                   | yes (GstMpegtsSCTESIT element)         |
| Overlay runtime ctrl | zmq filter (well documented)             | dynamic pad swaps (more flexible, harder) |
| Operational maturity | many engineers know it cold              | smaller talent pool                    |
| Crash recovery       | restart entire process                   | can rebuild parts of pipeline          |
| Python integration   | subprocess + pipes                       | python-gst (heavyweight)               |

For this project FFmpeg + a Python SCTE parser wins on operational simplicity.
The pieces we sacrifice (GStreamer's native SCTE awareness) are exactly the
pieces we want to own anyway, because the SCTE policy is product logic.

---

## 4. Overlay injection — the production path

### MVP path: restart FFmpeg with new filter graph

```
SCTE event arrives
   ↓
overlay_controller.restart_encoder(overlay_args)
   ↓
new FFmpeg encoder with -filter_complex "...overlay=enable='between(t,T0,T1)'..."
   ↓
~1–3 seconds of black/freeze on output while encoder warms up
```

This is acceptable for MVP because:

* The code path is simple and easy to verify.
* Most ad-insertion environments tolerate a brief discontinuity at the splice point.
* You don't have to deal with ZMQ wiring on day one.

### Production path: persistent encoder, ZMQ-driven overlay

```
Encoder is started once with this filter graph:
   [0:v][1:v]overlay=x=10:y=10:enable=0,zmq=bind_address=tcp://127.0.0.1:5555

When SCTE event arrives:
   overlay_controller sends ZMQ:  "Parsed_overlay_0 enable 1"
   schedules a future:           "Parsed_overlay_0 enable 0"  (after segmentation_duration)
```

Caveats:

* You must build FFmpeg with `--enable-libzmq` (most distros do).
* Filter names depend on filter graph order. Use `Parsed_<name>_<index>` — or
  put the filter name explicitly: `[0:v][1:v]overlay@ov=...,zmq...` then send
  `ov enable 1`.
* The overlay video file is read once and looped (`-stream_loop -1 -i overlay.mov`).
  When `enable=0`, the overlay decoder still runs but its output is masked.
* Parameter changes (x, y, w, h) are also valid ZMQ targets:
  `Parsed_overlay_0 x 100`, `Parsed_overlay_0 y 50`.

### Latency from SCTE arrival to overlay visible on output

| Stage                                       | Time      |
|---------------------------------------------|-----------|
| SCTE-35 TS packet → detector parses event   | < 5 ms    |
| Detector → controller decides → ZMQ send    | < 2 ms    |
| ZMQ socket → FFmpeg overlay filter applies  | < 5 ms    |
| Encoder buffer (B-frames, lookahead) → wire | 100–500 ms (codec-dependent) |
| Output protocol stack (SRT latency window)  | 120–500 ms (configured) |

End-to-end you can hit sub-second comfortably. The dominant term is encoder
lookahead + SRT latency, not anything we wrote.

---

## 5. Reliability for 24/7 operation

Three layers, each independently capable of recovering the system.

### Layer 1: per-process supervision

Each FFmpeg subprocess is wrapped by an `asyncio` supervisor that:

* Monitors `process.returncode`.
* On exit, logs the last 4 KB of stderr.
* Restarts with exponential backoff (1s, 2s, 4s, … capped at 30s).
* Resets backoff after 60s of stable runtime.
* Emits an alarm if restart rate exceeds 1/minute over a 10-minute window.

### Layer 2: health probes

Every 2 seconds, the supervisor checks:

* **Ingest health.** Did we see UDP packets on `:5000` and `:5001` in the last
  2s? If `:5000` is alive but `:5001` is not for 30s, either the source has no
  SCTE PID (normal — log once and stop alarming) or FFmpeg's tee dropped the
  data stream (alarm).
* **Encoder health.** Did encoder stderr emit a `frame=` progress line in the
  last 5s? If not, the filter graph may be deadlocked — restart.
* **Detector health.** Is the asyncio task still scheduled? Has it processed
  any TS packets in the last 5s? If `:5001` has bytes but the detector hasn't
  advanced, it's stuck — restart the task (not the process).

### Layer 3: end-to-end watchdog

Independent of the rest, a separate task probes the **output**:

* For UDP/SRT outputs, opens a passive listener (or a non-consuming `srt-live-transmit`
  side-channel) and counts bytes per second.
* If output bytes/sec drops to zero for > 5s while ingest is alive, full pipeline
  restart (kill both FFmpeg processes, re-spawn).

### Memory and file descriptor hygiene

* Marker store is a fixed-size ring buffer (default 10 000 events). No leaks.
* Logs are written to stdout for the orchestrator (systemd / Docker / k8s) to
  rotate. Never write to a file from inside the app.
* Detector reuses a single bytearray buffer for PSI reassembly. No per-packet
  allocations on the hot path.

---

## 6. Configuration model

```python
# runtime_config.py — single source of truth, mutated only by API handlers
@dataclass
class StreamConfig:
    input_url: str            # srt://...?mode=caller, udp://..., rtmp://...
    output_url: str           # srt://...?mode=listener, udp://..., rtmp://...
    output_format: str        # "mpegts" (for SRT/UDP), "flv" (for RTMP)
    overlay_path: str         # absolute path to alpha MOV
    overlay_x: int
    overlay_y: int
    overlay_w: int | None     # None = native size
    overlay_h: int | None
    overlay_duration_ms: int | None  # None = use SCTE segmentation_duration
    triggered_segmentation_types: set[int]  # which type_ids trigger overlay
    encoder_preset: str = "veryfast"
    encoder_bitrate: str = "4M"
```

The config is immutable per pipeline run. To change config, you stop and start
the pipeline. This avoids a class of races where the encoder is mid-frame with
old settings and the controller thinks new settings are live.

---

## 7. Module map

| Module                  | Responsibility                                       |
|-------------------------|------------------------------------------------------|
| `app/main.py`           | FastAPI app factory, lifespan management             |
| `app/api.py`            | REST + WebSocket endpoints                           |
| `app/runtime_config.py` | `StreamConfig` dataclass, validation                 |
| `app/stream_processor.py` | FFmpeg ingest + encoder process supervisors        |
| `app/marker_detector.py`| MPEG-TS demuxer, PAT/PMT discovery, SCTE PID reader  |
| `app/scte_parser.py`    | `splice_info_section` binary parser                  |
| `app/overlay_controller.py` | SCTE event → ZMQ command (or restart)            |
| `app/marker_store.py`   | Ring buffer of parsed events for the dashboard       |
| `app/event_bus.py`      | Internal pub/sub (asyncio.Queue fan-out)             |

---

## 8. Tradeoff summary

| Decision                                | We chose                  | We rejected                          | Why                          |
|-----------------------------------------|---------------------------|--------------------------------------|------------------------------|
| Single vs multi FFmpeg                  | Multi (ingest + encoder)  | Single all-in-one                    | Restart isolation            |
| SCTE extraction                         | Custom Python TS demuxer  | ffprobe / FFmpeg pipe                | Latency, correctness         |
| Overlay control (production)            | ZMQ filter                | FFmpeg restart per event             | No black frame at splice     |
| Overlay control (MVP)                   | Restart accepted          | —                                    | Faster to ship, easier to debug |
| Pipeline framework                      | FFmpeg                    | GStreamer                            | Operational maturity         |
| Marker storage                          | In-memory ring buffer     | Database                             | MVP scope; swap later        |
| Inter-process transport                 | Localhost UDP + ZMQ       | Unix pipes                           | Survives restarts, no SIGPIPE |

---

## 9. What's deliberately out of scope (for now)

* **Frame-accurate splicing.** SCTE 35 specifies splice_time PTS and pre-roll. A
  true ad-server would line the overlay up to the exact PTS. The MVP triggers
  on receipt; the production path can add PTS scheduling once the rest is solid.
* **Multi-program transport streams.** We assume one program per input. Extending
  to multi-program means the detector tracks multiple PMTs.
* **HLS/DASH outputs.** Possible (FFmpeg muxers exist) but not in the requirements.
* **Persistent event storage.** Add Postgres or sqlite when the ring buffer is
  no longer enough.
