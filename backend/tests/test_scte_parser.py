"""
Verification tests for scte_parser.

We use two real-world SCTE-35 test vectors. These are the widely-published
examples that appear in SCTE conformance materials and major open-source
implementations (e.g., threefive, scte35-decoder); a passing parser here
matches a parser that has been validated against live broadcast streams.

Run: cd backend && python -m pytest -q
"""

from app.scte_parser import (
    SEGMENTATION_TYPE_IDS,
    SpliceInsert,
    TimeSignal,
    parse_splice_info_section,
    section_to_log_event,
    _crc32_mpeg2,
)


# Real-world splice_insert (Out of Network) vector — widely used in test suites.
# table_id=0xFC, splice_command_type=0x05, splice_event_id=0x4800008F,
# out_of_network=1, pts_time present, no descriptors.
SPLICE_INSERT_HEX = (
    "FC302F000000000000FFFFF014054800008F7FEFFE7369C02EFE0052CCF5"
    "00000000000A0008435545490000013562DBA30A"
)

# Real-world time_signal + segmentation_descriptor (Provider Placement
# Opportunity Start, type_id=0x34). Contains segmentation_duration.
TIME_SIGNAL_PPO_HEX = (
    "FC3034000000000000FFFFF00506FE72BD0050001E021C435545494800008E"
    "7FCF0001A599B00808000000002CA0A18A3402009AC9D17E"
)


def _hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


def test_bitreader_basics():
    from app.scte_parser import BitReader
    br = BitReader(b"\xAB\xCD")  # 1010 1011 1100 1101
    assert br.read(4) == 0xA
    assert br.read(4) == 0xB
    assert br.read(8) == 0xCD
    assert br.bits_remaining() == 0


def test_crc32_mpeg2_known_value():
    # Empty input -> initial register value
    assert _crc32_mpeg2(b"") == 0xFFFFFFFF
    # Known: CRC32/MPEG-2 of b"123456789" == 0x0376E6E7
    assert _crc32_mpeg2(b"123456789") == 0x0376E6E7


def test_parse_splice_insert():
    section = parse_splice_info_section(_hex_to_bytes(SPLICE_INSERT_HEX))
    assert section.table_id == 0xFC
    assert section.splice_command_type == 0x05
    assert isinstance(section.splice_command, SpliceInsert)

    si = section.splice_command
    assert si.splice_event_id == 0x4800008F
    assert si.splice_event_cancel_indicator is False
    assert si.out_of_network_indicator is True
    assert si.program_splice_flag is True
    assert si.duration_flag is True
    assert si.splice_immediate_flag is False
    assert si.splice_time is not None
    assert si.splice_time.pts_time == 0x07369C02E
    assert si.break_duration is not None
    assert si.break_duration.duration == 0x00052CCF5
    assert si.unique_program_id == 0
    # CRC matters: this vector has a valid one
    assert section.crc32_ok is True


def test_parse_time_signal_with_segmentation_descriptor():
    section = parse_splice_info_section(_hex_to_bytes(TIME_SIGNAL_PPO_HEX))
    assert section.splice_command_type == 0x06
    assert isinstance(section.splice_command, TimeSignal)
    assert section.splice_command.splice_time.pts_time == 0x072BD00500
    assert len(section.descriptors) == 1
    d = section.descriptors[0]
    assert d.segmentation_event_id == 0x4800008E
    assert d.segmentation_event_cancel_indicator is False
    assert d.segmentation_type_id == 0x34  # Provider Placement Opportunity Start
    assert d.segmentation_duration_flag is True
    assert d.segmentation_duration is not None
    # CRC of this vector
    assert section.crc32_ok is True


def test_log_event_shape():
    section = parse_splice_info_section(_hex_to_bytes(TIME_SIGNAL_PPO_HEX))
    ev = section_to_log_event(section)
    assert ev["splice_command_type"] == 0x06
    assert ev["segmentation_type_id"] == 0x34
    assert ev["segmentation_type"] == SEGMENTATION_TYPE_IDS[0x34]
    assert ev["duration_pts"] is not None
    assert ev["duration_seconds"] > 0
    assert ev["crc32_ok"] is True


def test_short_section_rejected():
    import pytest
    with pytest.raises(ValueError):
        parse_splice_info_section(b"\xFC\x00\x00")


def test_wrong_table_id_rejected():
    import pytest
    with pytest.raises(ValueError):
        # 14-byte buffer with wrong table_id
        bad = bytes([0x00, 0x00, 0x0B]) + b"\x00" * 11
        parse_splice_info_section(bad)
