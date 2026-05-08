"""Runtime configuration for one pipeline session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamConfig:
    # Input
    input_url: str
    # Output
    output_url: str
    output_format: str = "mpegts"  # "mpegts" for SRT/UDP, "flv" for RTMP

    # Internal localhost fan-out (don't expose to UI unless you want to)
    encoder_feed_port: int = 5000   # full-TS feed for encoder
    detector_feed_port: int = 5001  # SCTE-only feed for detector

    # Overlay
    overlay_path: str = ""
    overlay_x: int = 10
    overlay_y: int = 10
    overlay_w: Optional[int] = None
    overlay_h: Optional[int] = None
    overlay_duration_ms: Optional[int] = None  # None ⇒ derive from segmentation_duration

    # SCTE policy: which segmentation_type_ids should fire the overlay
    triggered_segmentation_types: set[int] = field(
        default_factory=lambda: {0x22, 0x30, 0x32, 0x34, 0x36}
    )
    # If true, splice_insert (out_of_network) also triggers regardless of segmentation desc
    trigger_on_splice_insert_oon: bool = True

    # Encoder settings
    video_codec: str = "libx264"
    encoder_preset: str = "veryfast"
    encoder_tune: str = "zerolatency"
    encoder_bitrate: str = "4M"
    encoder_gop: int = 60
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"



@dataclass
class RuntimeState:
    config: Optional[StreamConfig] = None
    ingest_running: bool = False
    encoder_running: bool = False
    detector_running: bool = False
    overlay_active: bool = False
    last_event: Optional[dict] = None
    last_overlay_result: Optional[dict] = None
    started_at: Optional[float] = None
