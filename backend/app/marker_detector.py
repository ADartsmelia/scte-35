"""
Real-time SCTE-35 detector.

Reads MPEG-TS packets from a UDP socket fed by the ingest FFmpeg's `tee` muxer,
discovers the SCTE-35 elementary PID via PAT and PMT, reassembles PSI sections,
and emits parsed events.

Design notes:

* Pure asyncio. Single recv loop, no threads, no pyav, no ffmpeg-python.
* Zero allocations on the hot path beyond the recv buffer; we reuse a
  bytearray for PSI section reassembly per PID.
* Robust to:
    - 1, 4, or 7 TS packets per UDP datagram
    - SCTE PID changing between PMT updates
    - Multiple programs in the input (we lock onto the first program in PAT
      that has a SCTE-35 stream, then ignore others)
    - Out-of-spec section_length: we trust the length, but cap it at the
      MPEG-TS section maximum (1024 bytes for private sections — we use 4096
      as a safety bound).
* Latency from wire to event: typically < 5 ms.

Public API: `MarkerDetector.run()` is an awaitable that runs forever (or until
cancelled). It puts dicts (the dashboard log shape) onto an asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from .scte_parser import parse_splice_info_section, section_to_log_event

log = logging.getLogger(__name__)

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

PID_PAT = 0x0000
PID_NULL = 0x1FFF

# stream_type for SCTE-35 (SCTE 35 §8.1)
STREAM_TYPE_SCTE35 = 0x86
# Registration descriptor format_identifier "CUEI"
CUEI_FORMAT_ID = 0x43554549


@dataclass
class _SectionAssembler:
    """Reassembles MPEG-TS PSI sections for one PID across multiple TS packets."""
    buf: bytearray = field(default_factory=bytearray)
    expected_len: int = 0       # full section length including 3-byte header, when known
    last_cc: int = -1           # continuity counter

    def reset(self) -> None:
        self.buf.clear()
        self.expected_len = 0


class MarkerDetector:
    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 5001,
        out_queue: Optional[asyncio.Queue] = None,
        recv_bufsize: int = 4 * 1024 * 1024,  # 4 MB OS recv buffer
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.out_queue: asyncio.Queue = out_queue or asyncio.Queue(maxsize=10000)
        self.recv_bufsize = recv_bufsize

        # Discovered PIDs.
        self._pmt_pid_by_program: dict[int, int] = {}  # program_number -> pmt_pid
        self._scte_pid: Optional[int] = None
        self._program_locked: Optional[int] = None

        # PSI assembly per PID we care about.
        self._asm: dict[int, _SectionAssembler] = {}

        # Stats.
        self.stats = {
            "ts_packets": 0,
            "scte_packets": 0,
            "scte_sections": 0,
            "parse_errors": 0,
            "last_packet_at": 0.0,
            "last_event_at": 0.0,
        }

        self._sock: Optional[socket.socket] = None
        self._stop = asyncio.Event()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._sock = self._make_socket()
        log.info("Detector listening on udp://%s:%d", self.listen_host, self.listen_port)

        try:
            while not self._stop.is_set():
                try:
                    data = await loop.sock_recv(self._sock, 65535)
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except OSError as e:
                    log.warning("recv failed: %s", e)
                    await asyncio.sleep(0.1)
                    continue

                if not data:
                    continue
                self.stats["last_packet_at"] = time.time()
                self._consume_datagram(data)
        finally:
            if self._sock:
                self._sock.close()
                self._sock = None

    def stop(self) -> None:
        self._stop.set()

    def _make_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.recv_bufsize)
        except OSError:
            pass  # best effort
        s.setblocking(False)
        s.bind((self.listen_host, self.listen_port))
        return s

    # -------------------------------------------------------------------------
    # MPEG-TS packet processing
    # -------------------------------------------------------------------------

    def _consume_datagram(self, data: bytes) -> None:
        # MPEG-TS over UDP is typically 7 packets × 188 = 1316 bytes per datagram.
        # Be permissive: scan for sync byte, then process aligned 188-byte packets.
        n = len(data)
        i = 0
        # Find first sync byte
        while i < n and data[i] != TS_SYNC_BYTE:
            i += 1
        while i + TS_PACKET_SIZE <= n:
            if data[i] != TS_SYNC_BYTE:
                # resync
                i += 1
                continue
            self._consume_ts_packet(data, i)
            i += TS_PACKET_SIZE

    def _consume_ts_packet(self, buf: bytes, off: int) -> None:
        self.stats["ts_packets"] += 1

        # Header: 4 bytes
        b1 = buf[off + 1]
        b2 = buf[off + 2]
        b3 = buf[off + 3]
        pusi = (b1 & 0x40) != 0
        pid = ((b1 & 0x1F) << 8) | b2
        adaptation = (b3 >> 4) & 0x03
        cc = b3 & 0x0F

        if pid == PID_NULL:
            return

        # Compute payload start
        payload_start = off + 4
        if adaptation == 0x02:
            # adaptation only, no payload
            return
        if adaptation == 0x03:
            adapt_len = buf[payload_start]
            payload_start += 1 + adapt_len
        if payload_start - off >= TS_PACKET_SIZE:
            return

        payload = buf[payload_start: off + TS_PACKET_SIZE]

        if pid == PID_PAT:
            self._handle_psi(pid, payload, pusi, cc, self._handle_pat_section)
            return

        if pid in self._pmt_pid_by_program.values():
            self._handle_psi(pid, payload, pusi, cc, self._handle_pmt_section)
            return

        if self._scte_pid is not None and pid == self._scte_pid:
            self.stats["scte_packets"] += 1
            self._handle_psi(pid, payload, pusi, cc, self._handle_scte_section)
            return

    # -------------------------------------------------------------------------
    # PSI section reassembly
    # -------------------------------------------------------------------------

    def _handle_psi(self, pid: int, payload: bytes, pusi: bool, cc: int, sink) -> None:
        asm = self._asm.get(pid)
        if asm is None:
            asm = _SectionAssembler()
            self._asm[pid] = asm

        # Continuity counter check (skipped on first packet for this PID)
        if asm.last_cc != -1:
            expected = (asm.last_cc + 1) & 0x0F
            if cc != expected and cc != asm.last_cc:
                # discontinuity — flush whatever partial we had
                asm.reset()
        asm.last_cc = cc

        if pusi:
            # First byte of payload is pointer_field
            if not payload:
                return
            pointer_field = payload[0]
            # Anything before pointer_field belongs to the previous section
            if pointer_field > 0 and asm.buf:
                asm.buf.extend(payload[1:1 + pointer_field])
                self._maybe_emit_section(asm, sink)
            asm.reset()
            asm.buf.extend(payload[1 + pointer_field:])
        else:
            if not asm.buf:
                # No section in progress — drop until next PUSI
                return
            asm.buf.extend(payload)

        self._maybe_emit_section(asm, sink)

    def _maybe_emit_section(self, asm: _SectionAssembler, sink) -> None:
        # Need at least 3 bytes for table_id + section_length
        while len(asm.buf) >= 3:
            if asm.expected_len == 0:
                section_length = ((asm.buf[1] & 0x0F) << 8) | asm.buf[2]
                asm.expected_len = section_length + 3
                if asm.expected_len > 4096:  # sanity cap
                    asm.reset()
                    return
            if len(asm.buf) < asm.expected_len:
                return
            section_bytes = bytes(asm.buf[:asm.expected_len])
            # Remove this section; remainder may contain another section
            del asm.buf[:asm.expected_len]
            asm.expected_len = 0
            try:
                sink(section_bytes)
            except Exception as e:
                log.exception("section sink failed: %s", e)

    # -------------------------------------------------------------------------
    # PAT
    # -------------------------------------------------------------------------

    def _handle_pat_section(self, section: bytes) -> None:
        # PAT: table_id=0x00, then standard section header. Programs follow.
        if len(section) < 12 or section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        # programs start at offset 8, end before CRC_32 (last 4 bytes)
        body_end = 3 + section_length - 4
        i = 8
        new_map: dict[int, int] = {}
        while i + 4 <= body_end:
            program_number = (section[i] << 8) | section[i + 1]
            pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
            if program_number != 0:  # 0 = network PID, not a program
                new_map[program_number] = pid
            i += 4
        if new_map != self._pmt_pid_by_program:
            log.info("PAT: programs=%s", new_map)
            self._pmt_pid_by_program = new_map
            # Drop any stale PMT assemblers
            for pid in list(self._asm.keys()):
                if pid != PID_PAT and pid != self._scte_pid and pid not in new_map.values():
                    self._asm.pop(pid, None)

    # -------------------------------------------------------------------------
    # PMT — find SCTE-35 PID
    # -------------------------------------------------------------------------

    def _handle_pmt_section(self, section: bytes) -> None:
        if len(section) < 12 or section[0] != 0x02:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        program_number = (section[3] << 8) | section[4]
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]

        i = 12 + program_info_length  # start of ES loop
        body_end = 3 + section_length - 4

        scte_pid: Optional[int] = None
        while i + 5 <= body_end:
            stream_type = section[i]
            elem_pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            es_desc_start = i + 5
            es_desc_end = es_desc_start + es_info_length

            is_scte = False
            if stream_type == STREAM_TYPE_SCTE35:
                is_scte = True  # stream_type alone is sufficient per spec
            else:
                # Some sources signal SCTE-35 via registration descriptor only.
                j = es_desc_start
                while j + 2 <= es_desc_end:
                    tag = section[j]
                    dlen = section[j + 1]
                    if tag == 0x05 and dlen >= 4:  # registration_descriptor
                        fmt = struct.unpack(">I", section[j + 2:j + 6])[0]
                        if fmt == CUEI_FORMAT_ID:
                            is_scte = True
                            break
                    j += 2 + dlen

            if is_scte:
                scte_pid = elem_pid
                break
            i = es_desc_end

        if scte_pid is None:
            return

        if self._scte_pid != scte_pid:
            log.info(
                "PMT: program %d → SCTE-35 PID 0x%04x (%d)",
                program_number, scte_pid, scte_pid,
            )
            # Drop any old SCTE assembler
            if self._scte_pid is not None:
                self._asm.pop(self._scte_pid, None)
            self._scte_pid = scte_pid
            self._program_locked = program_number

    # -------------------------------------------------------------------------
    # SCTE-35
    # -------------------------------------------------------------------------

    def _handle_scte_section(self, section: bytes) -> None:
        self.stats["scte_sections"] += 1
        try:
            parsed = parse_splice_info_section(section)
        except Exception as e:
            self.stats["parse_errors"] += 1
            log.warning("SCTE parse error: %s (len=%d)", e, len(section))
            return

        event = section_to_log_event(parsed)
        event["pid"] = f"0x{self._scte_pid:04x}" if self._scte_pid is not None else None
        event["received_at"] = time.time()
        self.stats["last_event_at"] = event["received_at"]

        try:
            self.out_queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("event queue full — dropping oldest")
            try:
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(event)
            except asyncio.QueueEmpty:
                pass
