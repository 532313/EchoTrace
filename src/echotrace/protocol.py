"""Version 3. All source times are microseconds in the edge boot clock."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

VERSION = 3
HEADER = struct.Struct("!HHIIQI")
MAX_PACKET = 65536
AUDIO_OPUS, MEDIA_CHUNK, SCREEN_FRAME = 1, 2, 3
SCREEN_WIDTH, SCREEN_HEIGHT = 240, 136
SCREEN_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT * 2


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)


class SessionCreate(StrictModel):
    project_id: str
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    boot_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source_time_us: int = Field(ge=0, le=9223372036854775807)
    utc_ms: Optional[int] = Field(default=None, ge=0)
    clock_quality: Literal["unknown", "unsynced", "synced"] = "unknown"
    title: str = Field(default="", max_length=200)


class EventIn(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    session_id: str
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    boot_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    seq: int = Field(ge=0, le=9223372036854775807)
    source_time_us: int = Field(ge=0, le=9223372036854775807)
    # Removed peripheral kinds are accepted only to replay existing journals.
    kind: Literal["mark", "voice", "scene", "gap", "playback", "pedal", "tray", "sensor_fault"]
    note: str = Field(default="", max_length=8000)
    payload: Dict[str, Any] = Field(default_factory=dict)


class MediaIn(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    session_id: str
    device_id: str
    boot_id: str
    start_us: int = Field(ge=0, le=9223372036854775807)
    end_us: int = Field(ge=1, le=9223372036854775807)
    kind: Literal["video", "audio", "image"]
    size_bytes: int = Field(gt=0, le=128 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["video/mp4", "audio/wav", "image/jpeg"]
    discontinuity: bool = False


class QueryIn(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=20)
    semantic: bool = True


class NoteUpdate(StrictModel):
    note: str = Field(max_length=8000)
    revision: int = Field(ge=0)


class StateChange(StrictModel):
    state: Literal["recording", "paused", "stopped"]
    source_time_us: int = Field(ge=0, le=9223372036854775807)


class ExportIn(StrictModel):
    anchor_ids: list[str] = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class Packet:
    kind: int
    seq: int
    source_time_us: int
    samples: int
    payload: bytes

    def pack(self) -> bytes:
        if self.kind not in (AUDIO_OPUS, MEDIA_CHUNK, SCREEN_FRAME):
            raise ValueError("unknown binary type")
        if not self.payload or len(self.payload) > MAX_PACKET:
            raise ValueError("invalid payload length")
        if self.kind == AUDIO_OPUS and self.samples != 640:
            raise ValueError("one 40ms/16kHz Opus frame is required")
        if self.kind == SCREEN_FRAME and (self.samples or len(self.payload) != 16 + SCREEN_BYTES):
            raise ValueError("one complete screen frame and stream UUID are required")
        return (
            HEADER.pack(
                VERSION,
                self.kind,
                len(self.payload),
                self.seq,
                self.source_time_us,
                self.samples,
            )
            + self.payload
        )

    @classmethod
    def unpack(cls, data: bytes) -> "Packet":
        if len(data) < HEADER.size:
            raise ValueError("truncated header")
        version, kind, size, seq, stamp, samples = HEADER.unpack_from(data)
        if version != VERSION or kind not in (AUDIO_OPUS, MEDIA_CHUNK, SCREEN_FRAME):
            raise ValueError("unsupported version or binary type")
        if not 0 < size <= MAX_PACKET or len(data) != HEADER.size + size:
            raise ValueError("payload length must match exactly")
        if kind == AUDIO_OPUS and samples != 640:
            raise ValueError("one 40ms/16kHz Opus frame is required")
        if kind == SCREEN_FRAME and (samples or size != 16 + SCREEN_BYTES):
            raise ValueError("one complete screen frame and stream UUID are required")
        return cls(kind, seq, stamp, samples, data[HEADER.size :])
