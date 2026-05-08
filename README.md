# SCTE-35 Real-time Overlay Engine

Backend pipeline + dashboard that ingests a live MPEG-TS stream (SRT/UDP/RTMP),
detects SCTE-35 splice markers in real time by parsing the binary
`splice_info_section`, and toggles a graphical overlay on the outbound stream.

Designed to run 24/7. Read **[ARCHITECTURE.md](./ARCHITECTURE.md)** first — it
explains the design decisions and tradeoffs.

```
backend/
  app/
    main.py                # FastAPI entry
    api.py                 # REST + WebSocket
    runtime_config.py      # StreamConfig dataclass
    stream_processor.py    # FFmpeg ingest + encoder supervisors
    marker_detector.py     # MPEG-TS demuxer, PAT/PMT, SCTE PID
    scte_parser.py         # splice_info_section binary parser
    overlay_controller.py  # SCTE event -> ZMQ overlay toggle
    marker_store.py        # ring buffer of events
  tests/
    test_scte_parser.py    # vectors verified against the spec
  requirements.txt
frontend/
  src/App.jsx              # dashboard
  ...
ARCHITECTURE.md
```

## Prerequisites

* **Python 3.10+**
* **FFmpeg 6+ built with `--enable-libzmq` and `--enable-libsrt`**.
  Verify:

  ```sh
  ffmpeg -hide_banner -filters | grep -E '^[ T.]+(zmq|overlay)\b'
  ffmpeg -hide_banner -protocols | grep -E 'srt'
  ```

  Both `zmq` and `overlay` filters and the `srt` protocol must be present.
  On Debian/Ubuntu, the `ffmpeg` package usually has SRT but **not** ZMQ;
  use [BtbN's static builds](https://github.com/BtbN/FFmpeg-Builds) or build
  from source with `--enable-libzmq`.

* **Node 18+** (for the dashboard)

## Backend

```sh
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Start a pipeline:

```sh
curl -X POST http://localhost:8000/api/start \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url":  "srt://0.0.0.0:9002?mode=listener&latency=200",
    "output_url": "srt://0.0.0.0:9003?mode=listener",
    "output_format": "mpegts",
    "overlay_path": "/abs/path/to/overlay.mov",
    "overlay_x": 50, "overlay_y": 50,
    "overlay_mode": "zmq"
  }'
```

Inspect:

```sh
curl http://localhost:8000/api/status   | jq
curl http://localhost:8000/api/markers  | jq
```

Stop:

```sh
curl -X POST http://localhost:8000/api/stop
```

## Frontend

```sh
cd frontend
npm install
npm run dev      # serves on http://localhost:5173
```

Set `VITE_API` if the backend isn't on `localhost:8000`.

## Verifying SCTE-35 detection on a real source

If your source has SCTE-35, this will confirm the PID and stream type before
you even start the pipeline:

```sh
ffprobe -hide_banner -show_streams -select_streams d \
  "srt://0.0.0.0:9002?mode=listener&latency=200"
```

Look for `codec_name=scte_35` and `codec_type=data`. The detector finds this PID
itself once the pipeline is running — this command is just a pre-flight check.

## Tests

```sh
cd backend
python -m pytest -q     # if pytest is installed
```

The parser is verified against two real-world test vectors:
`splice_insert` (out of network) and `time_signal` with a Provider
Placement Opportunity Start `segmentation_descriptor`. CRC-32/MPEG-2 is
checked end-to-end.

## Debugging tips

* **No SCTE events ever?** Check the detector branch is getting bytes:
  `nc -ul 5001 | xxd | head`. If empty, the source has no SCTE PID.
* **Overlay never appears?** Confirm FFmpeg sees the ZMQ filter:
  `ffmpeg -filters | grep zmq` (must show `T..` for "timeline support" — it does
  even though that flag isn't directly relevant to ZMQ). Then test the socket:
  `python -c "import zmq; s=zmq.Context().socket(zmq.REQ); s.connect('tcp://127.0.0.1:5555'); s.send_string('ov enable 1'); print(s.recv_string())"`
* **Encoder dies on start?** Most common cause is the overlay file path. If the
  alpha MOV is unreadable or its codec isn't installed, FFmpeg exits during
  filter graph init. Check `/api/status` — `pipeline.encoder.stderr_tail`
  shows the last 20 stderr lines.
* **High latency at the splice?** Either you're in `restart` mode (expected
  cost: 1–3s gap), or the encoder GOP/lookahead is too large. For ad-insertion,
  use `-tune zerolatency`, GOP <= 2× framerate, no B-frames.

## License & attribution

The SCTE-35 test vectors used in `tests/test_scte_parser.py` are widely
distributed conformance examples; they appear in many open-source
implementations. The parser implementation is original.
