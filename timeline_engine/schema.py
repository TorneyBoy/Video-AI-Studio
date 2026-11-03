"""Canonical timeline and template schemas.

The models in this module are intentionally renderer agnostic.  They describe
what should be rendered, while a renderer can consume the resulting manifest.
All durations are seconds and all identifiers are stable strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AssetType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    FONT = "font"
    DATA = "data"
    OTHER = "other"


class TrackType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    OVERLAY = "overlay"
    SUBTITLE = "subtitle"


class TransitionType(str, Enum):
    """FFmpeg xfade transitions supported by the local multi-track renderer."""

    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPELEFT = "wipeleft"
    WIPERIGHT = "wiperight"
    SLIDELEFT = "slideleft"
    SLIDERIGHT = "slideright"


class Transition(StrictModel):
    type: TransitionType = TransitionType.FADE
    duration: float = Field(gt=0, le=30)


class Placement(StrictModel):
    """Optional pixel placement for an overlay or text cue."""

    x: float = 0
    y: float = 0
    width: Optional[int] = Field(default=None, gt=0)
    height: Optional[int] = Field(default=None, gt=0)


class TextStyle(StrictModel):
    font_file: Optional[str] = None
    font_size: int = Field(default=48, gt=0, le=512)
    font_color: str = Field(default="white", min_length=1, pattern=r"^[A-Za-z0-9#@.,()%]+$")
    border_width: int = Field(default=0, ge=0, le=32)
    border_color: str = Field(default="black", min_length=1, pattern=r"^[A-Za-z0-9#@.,()%]+$")
    box: bool = False
    box_color: str = Field(default="black@0.45", min_length=1, pattern=r"^[A-Za-z0-9#@.,()%]+$")
    box_border: int = Field(default=8, ge=0, le=64)


class SlotType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    TEXT = "text"
    COLOR = "color"
    ANY = "any"


class TimeRange(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class AssetRef(StrictModel):
    id: str = Field(min_length=1)
    type: AssetType
    uri: str = Field(min_length=1)
    checksum: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Clip(StrictModel):
    id: str = Field(min_length=1)
    asset_id: Optional[str] = None
    slot: Optional[str] = None
    range: TimeRange
    source_start: float = Field(default=0, ge=0)
    source_end: Optional[float] = Field(default=None, gt=0)
    opacity: float = Field(default=1.0, ge=0, le=1)
    volume: float = Field(default=1.0, ge=0)
    text: Optional[str] = None
    placement: Optional[Placement] = None
    style: Optional[TextStyle] = None
    transition: Optional[Transition] = None
    effects: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def source_range_valid(self) -> "Clip":
        if self.source_end is not None and self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        if not self.asset_id and not self.slot and self.text is None:
            raise ValueError("clip requires asset_id, slot, or text")
        if self.asset_id and self.slot:
            raise ValueError("clip cannot specify both asset_id and slot")
        if self.text is not None and (self.asset_id or self.slot):
            raise ValueError("text clip cannot specify asset_id or slot")
        if self.text is not None and not self.text.strip():
            raise ValueError("text clip cannot be empty")
        return self


class Track(StrictModel):
    id: str = Field(min_length=1)
    type: TrackType
    clips: List[Clip] = Field(default_factory=list)
    muted: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Resolution(StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Timeline(StrictModel):
    """Canonical timeline exchanged between authoring and rendering systems."""

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    fps: float = Field(gt=0, le=240)
    duration: float = Field(gt=0)
    resolution: Resolution = Field(default_factory=lambda: Resolution(width=1920, height=1080))
    tracks: List[Track] = Field(default_factory=list)
    assets: List[AssetRef] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_and_bounds(self) -> "Timeline":
        asset_ids = {a.id for a in self.assets}
        if len(asset_ids) != len(self.assets):
            raise ValueError("asset ids must be unique")
        track_ids = [t.id for t in self.tracks]
        if len(set(track_ids)) != len(track_ids):
            raise ValueError("track ids must be unique")
        clip_ids: set[str] = set()
        for track in self.tracks:
            for clip in track.clips:
                if clip.id in clip_ids:
                    raise ValueError(f"duplicate clip id: {clip.id}")
                clip_ids.add(clip.id)
                if clip.range.end > self.duration + 1e-9:
                    raise ValueError(f"clip {clip.id} exceeds timeline duration")
                # A concrete asset reference must always resolve.  The old
                # conditional skipped this check when ``assets`` was empty,
                # allowing malformed timelines to survive until render time.
                if clip.asset_id and clip.asset_id not in asset_ids:
                    raise ValueError(f"clip {clip.id} references unknown asset {clip.asset_id}")
        return self

    @property
    def frame_count(self) -> int:
        # Half-up style rounding avoids accidental truncation at fractional FPS.
        return int(self.duration * self.fps + 0.5)


class SlotConstraints(StrictModel):
    allowed_asset_types: List[AssetType] = Field(default_factory=list)
    min_duration: Optional[float] = Field(default=None, ge=0)
    max_duration: Optional[float] = Field(default=None, gt=0)
    min_items: int = Field(default=1, ge=0)
    max_items: Optional[int] = Field(default=1, gt=0)
    required_metadata: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounds(self) -> "SlotConstraints":
        if self.max_duration is not None and self.min_duration is not None and self.max_duration < self.min_duration:
            raise ValueError("max_duration must be >= min_duration")
        if self.max_items is not None and self.max_items < self.min_items:
            raise ValueError("max_items must be >= min_items")
        return self


class TemplateSlot(StrictModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    type: SlotType
    required: bool = True
    constraints: SlotConstraints = Field(default_factory=SlotConstraints)
    description: Optional[str] = None


class TemplateDefinition(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    slots: List[TemplateSlot] = Field(default_factory=list)
    duration: Optional[float] = Field(default=None, gt=0)
    fps: Optional[float] = Field(default=None, gt=0, le=240)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_slots(self) -> "TemplateDefinition":
        names = [s.name for s in self.slots]
        if len(set(names)) != len(names):
            raise ValueError("template slot names must be unique")
        return self


class SlotValue(StrictModel):
    """A concrete value supplied for a template slot."""

    asset_ids: List[str] = Field(default_factory=list)
    text: Optional[str] = None
    duration: Optional[float] = Field(default=None, gt=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_value(self) -> "SlotValue":
        if not self.asset_ids and self.text is None:
            raise ValueError("slot value requires asset_ids or text")
        return self


class TemplateBinding(StrictModel):
    template_id: str = Field(min_length=1)
    values: Dict[str, SlotValue] = Field(default_factory=dict)

