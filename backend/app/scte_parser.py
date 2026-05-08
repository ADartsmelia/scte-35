"""
SCTE-35 splice_info_section binary parser.

Reference: ANSI/SCTE 35 2019b (later editions are backward compatible for the
parts we use). Implements splice_null, splice_insert, time_signal, and the
segmentation_descriptor — which together cover virtually all real-world
ad-insertion and program-boundary signaling.

This module has zero I/O. It takes bytes and returns a dataclass. Test it
with hand-crafted vectors from the spec; do not rely on tools that have
already pre-decoded the section.

Bit layout below follows the section() definition in SCTE 35 §9.6.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Optional


class BitReader:
    """MSB-first bit reader over a bytes object. Sufficient for short SCTE sections."""

    __slots__ = ("data", "_bitpos")

    def __init__(self, data: bytes):
        self.data = data
        self._bitpos = 0

    @property
    def bitpos(self) -> int:
        return self._bitpos

    def bits_remaining(self) -> int:
        return len(self.data) * 8 - self._bitpos

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        if self._bitpos + n > len(self.data) * 8:
            raise ValueError(
                f"BitReader: tried to read {n} bits at pos {self._bitpos}, "
                f"only {self.bits_remaining()} remain"
            )
        value = 0
        bp = self._bitpos
        for _ in range(n):
            byte = self.data[bp >> 3]
            bit = (byte >> (7 - (bp & 7))) & 1
            value = (value << 1) | bit
            bp += 1
        self._bitpos = bp
        return value

    def skip(self, n: int) -> None:
        self._bitpos += n

    def byte_align(self) -> None:
        self._bitpos = (self._bitpos + 7) & ~7

    def read_bytes(self, n: int) -> bytes:
        if self._bitpos & 7:
            raise ValueError("read_bytes requires byte alignment")
        start = self._bitpos >> 3
        end = start + n
        self._bitpos = end << 3
        return bytes(self.data[start:end])


# -----------------------------------------------------------------------------
# Public dataclasses
# -----------------------------------------------------------------------------

@dataclass
class SpliceTime:
    pts_time: Optional[int]  # 33-bit, in 90 kHz ticks; None if not specified


@dataclass
class BreakDuration:
    auto_return: bool
    duration: int  # 33-bit, 90 kHz ticks


@dataclass
class SpliceInsert:
    splice_event_id: int
    splice_event_cancel_indicator: bool
    out_of_network_indicator: Optional[bool] = None
    program_splice_flag: Optional[bool] = None
    duration_flag: Optional[bool] = None
    splice_immediate_flag: Optional[bool] = None
    splice_time: Optional[SpliceTime] = None
    break_duration: Optional[BreakDuration] = None
    unique_program_id: Optional[int] = None
    avail_num: Optional[int] = None
    avails_expected: Optional[int] = None


@dataclass
class TimeSignal:
    splice_time: SpliceTime


@dataclass
class SegmentationDescriptor:
    segmentation_event_id: int
    segmentation_event_cancel_indicator: bool
    program_segmentation_flag: Optional[bool] = None
    segmentation_duration_flag: Optional[bool] = None
    delivery_not_restricted_flag: Optional[bool] = None
    segmentation_duration: Optional[int] = None  # 40-bit, 90 kHz ticks
    segmentation_upid_type: Optional[int] = None
    segmentation_upid: Optional[bytes] = None
    segmentation_type_id: Optional[int] = None
    segment_num: Optional[int] = None
    segments_expected: Optional[int] = None


@dataclass
class SpliceInfoSection:
    table_id: int
    section_length: int
    protocol_version: int
    encrypted_packet: bool
    pts_adjustment: int
    tier: int
    splice_command_type: int
    splice_command: object  # SpliceInsert | TimeSignal | None (for splice_null)
    descriptors: list[SegmentationDescriptor] = field(default_factory=list)
    crc32_ok: bool = False


# -----------------------------------------------------------------------------
# Splice command types (SCTE 35 §9.7.1)
# -----------------------------------------------------------------------------

CMD_SPLICE_NULL = 0x00
CMD_SPLICE_SCHEDULE = 0x04
CMD_SPLICE_INSERT = 0x05
CMD_TIME_SIGNAL = 0x06
CMD_BANDWIDTH_RESERVATION = 0x07
CMD_PRIVATE_COMMAND = 0xFF

# -----------------------------------------------------------------------------
# Segmentation type_ids (SCTE 35 §10.3.3.1) — partial, the operationally relevant ones
# -----------------------------------------------------------------------------

SEGMENTATION_TYPE_IDS = {
    0x00: "Not Indicated",
    0x01: "Content Identification",
    0x10: "Program Start",
    0x11: "Program End",
    0x12: "Program Early Termination",
    0x13: "Program Breakaway",
    0x14: "Program Resumption",
    0x15: "Program Runover Planned",
    0x16: "Program Runover Unplanned",
    0x17: "Program Overlap Start",
    0x18: "Program Blackout Override",
    0x19: "Program Start - In Progress",
    0x20: "Chapter Start",
    0x21: "Chapter End",
    0x22: "Break Start",
    0x23: "Break End",
    0x30: "Provider Advertisement Start",
    0x31: "Provider Advertisement End",
    0x32: "Distributor Advertisement Start",
    0x33: "Distributor Advertisement End",
    0x34: "Provider Placement Opportunity Start",
    0x35: "Provider Placement Opportunity End",
    0x36: "Distributor Placement Opportunity Start",
    0x37: "Distributor Placement Opportunity End",
    0x40: "Unscheduled Event Start",
    0x41: "Unscheduled Event End",
    0x50: "Network Start",
    0x51: "Network End",
}


# -----------------------------------------------------------------------------
# Section parser
# -----------------------------------------------------------------------------

def parse_splice_info_section(data: bytes) -> SpliceInfoSection:
    """Parse a complete splice_info_section.

    `data` must be exactly the section bytes (table_id through CRC_32).
    Returns a SpliceInfoSection. Raises ValueError on malformed input.
    """
    if len(data) < 14:
        raise ValueError(f"section too short: {len(data)} bytes")

    br = BitReader(data)

    table_id = br.read(8)
    if table_id != 0xFC:
        raise ValueError(f"not a splice_info_section: table_id=0x{table_id:02x}")

    section_syntax_indicator = br.read(1)
    private_indicator = br.read(1)
    if section_syntax_indicator != 0 or private_indicator != 0:
        # Not strictly fatal — some encoders set bits incorrectly. Warn via flag if needed.
        pass
    br.skip(2)  # sap_type (was reserved in earlier editions)
    section_length = br.read(12)

    if len(data) != section_length + 3:
        raise ValueError(
            f"section_length mismatch: header says {section_length}, "
            f"buffer has {len(data) - 3} bytes after length field"
        )

    protocol_version = br.read(8)
    encrypted_packet = bool(br.read(1))
    br.skip(6)  # encryption_algorithm
    pts_adjustment = br.read(33)
    br.skip(8)  # cw_index
    tier = br.read(12)
    splice_command_length = br.read(12)
    splice_command_type = br.read(8)

    cmd_start_bit = br.bitpos
    cmd: object = None

    if splice_command_type == CMD_SPLICE_NULL:
        cmd = None
    elif splice_command_type == CMD_SPLICE_INSERT:
        cmd = _parse_splice_insert(br)
    elif splice_command_type == CMD_TIME_SIGNAL:
        cmd = TimeSignal(splice_time=_parse_splice_time(br))
    elif splice_command_type == CMD_BANDWIDTH_RESERVATION:
        cmd = None
    elif splice_command_type == CMD_PRIVATE_COMMAND:
        # identifier(32) + private_byte(N)
        cmd = None
    else:
        # Unknown command — skip via splice_command_length if it was non-0xFFF
        pass

    # Snap to the position the header advertised, regardless of how far the
    # command parser actually went. This is defensive against vendor extensions.
    if splice_command_length != 0xFFF:
        br._bitpos = cmd_start_bit + splice_command_length * 8

    descriptor_loop_length = br.read(16)
    descriptors: list[SegmentationDescriptor] = []
    loop_end_bit = br.bitpos + descriptor_loop_length * 8

    while br.bitpos < loop_end_bit:
        d = _parse_splice_descriptor(br)
        if isinstance(d, SegmentationDescriptor):
            descriptors.append(d)

    # CRC_32: last 4 bytes
    crc32_ok = _crc32_mpeg2(data[:-4]) == int.from_bytes(data[-4:], "big")

    return SpliceInfoSection(
        table_id=table_id,
        section_length=section_length,
        protocol_version=protocol_version,
        encrypted_packet=encrypted_packet,
        pts_adjustment=pts_adjustment,
        tier=tier,
        splice_command_type=splice_command_type,
        splice_command=cmd,
        descriptors=descriptors,
        crc32_ok=crc32_ok,
    )


def _parse_splice_time(br: BitReader) -> SpliceTime:
    time_specified_flag = br.read(1)
    if time_specified_flag:
        br.skip(6)  # reserved
        pts_time = br.read(33)
        return SpliceTime(pts_time=pts_time)
    else:
        br.skip(7)  # reserved
        return SpliceTime(pts_time=None)


def _parse_break_duration(br: BitReader) -> BreakDuration:
    auto_return = bool(br.read(1))
    br.skip(6)  # reserved
    duration = br.read(33)
    return BreakDuration(auto_return=auto_return, duration=duration)


def _parse_splice_insert(br: BitReader) -> SpliceInsert:
    splice_event_id = br.read(32)
    cancel = bool(br.read(1))
    br.skip(7)  # reserved

    si = SpliceInsert(
        splice_event_id=splice_event_id,
        splice_event_cancel_indicator=cancel,
    )
    if cancel:
        return si

    si.out_of_network_indicator = bool(br.read(1))
    si.program_splice_flag = bool(br.read(1))
    si.duration_flag = bool(br.read(1))
    si.splice_immediate_flag = bool(br.read(1))
    br.skip(4)  # reserved

    if si.program_splice_flag and not si.splice_immediate_flag:
        si.splice_time = _parse_splice_time(br)

    if not si.program_splice_flag:
        component_count = br.read(8)
        for _ in range(component_count):
            br.skip(8)  # component_tag
            if not si.splice_immediate_flag:
                _parse_splice_time(br)  # discarded; we don't currently expose per-component timing

    if si.duration_flag:
        si.break_duration = _parse_break_duration(br)

    si.unique_program_id = br.read(16)
    si.avail_num = br.read(8)
    si.avails_expected = br.read(8)
    return si


def _parse_splice_descriptor(br: BitReader) -> object:
    tag = br.read(8)
    length = br.read(8)
    end_bit = br.bitpos + length * 8

    if tag == 0x02:  # segmentation_descriptor
        identifier = br.read(32)  # "CUEI"
        d = _parse_segmentation_descriptor_body(br, end_bit)
        # snap to declared end (defensive)
        br._bitpos = end_bit
        return d
    else:
        # Unknown / uninteresting descriptor — skip
        br._bitpos = end_bit
        return None


def _parse_segmentation_descriptor_body(br: BitReader, end_bit: int) -> SegmentationDescriptor:
    seg_event_id = br.read(32)
    cancel = bool(br.read(1))
    br.skip(7)  # reserved

    d = SegmentationDescriptor(
        segmentation_event_id=seg_event_id,
        segmentation_event_cancel_indicator=cancel,
    )
    if cancel:
        return d

    d.program_segmentation_flag = bool(br.read(1))
    d.segmentation_duration_flag = bool(br.read(1))
    d.delivery_not_restricted_flag = bool(br.read(1))
    if d.delivery_not_restricted_flag:
        br.skip(5)  # reserved
    else:
        br.skip(1)  # web_delivery_allowed_flag
        br.skip(1)  # no_regional_blackout_flag
        br.skip(1)  # archive_allowed_flag
        br.skip(2)  # device_restrictions

    if not d.program_segmentation_flag:
        component_count = br.read(8)
        for _ in range(component_count):
            br.skip(8)   # component_tag
            br.skip(7)   # reserved
            br.skip(33)  # pts_offset

    if d.segmentation_duration_flag:
        d.segmentation_duration = br.read(40)

    d.segmentation_upid_type = br.read(8)
    upid_length = br.read(8)
    d.segmentation_upid = br.read_bytes(upid_length)

    # Some vendors emit malformed descriptors where bits to here exceed end_bit.
    if br.bitpos >= end_bit:
        return d

    d.segmentation_type_id = br.read(8)
    d.segment_num = br.read(8)
    d.segments_expected = br.read(8)
    # sub_segment_num / sub_segments_expected for type_ids 0x34/0x36 — skipped for MVP

    return d


# -----------------------------------------------------------------------------
# CRC-32/MPEG-2 (poly 0x04C11DB7, init 0xFFFFFFFF, no xorout, no reflect)
# -----------------------------------------------------------------------------

def _crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


# -----------------------------------------------------------------------------
# Convenience: extract the dashboard view in one call
# -----------------------------------------------------------------------------

def section_to_log_event(section: SpliceInfoSection) -> dict:
    """Reduce a parsed section to the flat dict the dashboard logs table expects."""
    splice_event_id: Optional[int] = None
    duration_pts: Optional[int] = None
    pts_time: Optional[int] = None

    cmd = section.splice_command
    if isinstance(cmd, SpliceInsert):
        splice_event_id = cmd.splice_event_id
        if cmd.break_duration:
            duration_pts = cmd.break_duration.duration
        if cmd.splice_time and cmd.splice_time.pts_time is not None:
            pts_time = cmd.splice_time.pts_time
    elif isinstance(cmd, TimeSignal):
        if cmd.splice_time.pts_time is not None:
            pts_time = cmd.splice_time.pts_time

    seg_event_id: Optional[int] = None
    seg_type_id: Optional[int] = None
    seg_type_name: Optional[str] = None
    seg_duration: Optional[int] = None
    if section.descriptors:
        d = section.descriptors[0]
        seg_event_id = d.segmentation_event_id
        seg_type_id = d.segmentation_type_id
        seg_type_name = SEGMENTATION_TYPE_IDS.get(seg_type_id) if seg_type_id is not None else None
        seg_duration = d.segmentation_duration

    # Effective duration: prefer break_duration on splice_insert, else segmentation_duration
    duration = duration_pts if duration_pts is not None else seg_duration

    return {
        "splice_event_id": splice_event_id,
        "segmentation_event_id": seg_event_id,
        "segmentation_type_id": seg_type_id,
        "segmentation_type": seg_type_name,
        "pts_time": pts_time,
        "pts_adjustment": section.pts_adjustment,
        "duration_pts": duration,
        "duration_seconds": (duration / 90000.0) if duration is not None else None,
        "splice_command_type": section.splice_command_type,
        "crc32_ok": section.crc32_ok,
    }
