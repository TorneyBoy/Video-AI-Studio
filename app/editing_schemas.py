"""Personal authoring actions; no provider execution or task-state mutation."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class EditAction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SetLock(EditAction):
    op: Literal["set_lock"]
    selection_id: str = Field(min_length=1, max_length=100)
    locked: bool


class ReplaceShot(EditAction):
    op: Literal["replace"]
    selection_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=150)


class ReplanShots(EditAction):
    op: Literal["replan"]
    selection_ids: list[str] = Field(default_factory=list, max_length=500)


class RestoreRevision(EditAction):
    op: Literal["restore"]
    revision_id: str = Field(min_length=1, max_length=36)


class UndoEdit(EditAction):
    op: Literal["undo"]


class ConfigurePlan(EditAction):
    op: Literal["configure"]
    target_duration: float | None = Field(default=None, ge=0.1, le=600)
    aspect_ratio: Literal["9:16", "16:9", "1:1"]


class RefreshCandidates(EditAction):
    op: Literal["refresh_candidates"]


class ClipAction(EditAction):
    clip_id: str = Field(min_length=1, max_length=100)


class TrimClip(ClipAction):
    op: Literal["trim_clip"]
    source_start: float = Field(ge=0)
    source_end: float = Field(gt=0)


class SplitClip(ClipAction):
    op: Literal["split_clip"]
    offset_seconds: float = Field(gt=0)


class MoveClip(ClipAction):
    op: Literal["move_clip"]
    to_index: int = Field(ge=0, le=9999)


class RemoveClip(ClipAction):
    op: Literal["remove_clip"]


class SetClipAudio(ClipAction):
    op: Literal["set_clip_audio"]
    volume: float = Field(ge=0, le=4)


class SubtitleStyle(EditAction):
    font_size: int = Field(default=48, ge=12, le=160)
    font_color: str = Field(default="white", pattern=r"^(white|black|red|#[A-Fa-f0-9]{6})$")


class SubtitlePosition(EditAction):
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class ImportTranscript(EditAction):
    op: Literal["import_transcript"]
    track_id: str = Field(default="subtitles", min_length=1, max_length=100)
    asset_ids: list[str] = Field(default_factory=list, max_length=200)
    refresh_sources: bool = False


class SaveSubtitle(EditAction):
    op: Literal["save_subtitle"]
    cue_id: str | None = Field(default=None, min_length=1, max_length=100)
    track_id: str = Field(default="subtitles", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20000)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    anchor_clip_id: str | None = Field(default=None, min_length=1, max_length=100)
    anchor_mode: Literal["keep", "absolute", "clip"] = "keep"
    style: SubtitleStyle | None = None
    position: SubtitlePosition | None = None


class DeleteSubtitle(EditAction):
    op: Literal["delete_subtitle"]
    cue_id: str = Field(min_length=1, max_length=100)


class LayerClip(EditAction):
    clip_id: str | None = Field(default=None, min_length=1, max_length=100)
    track_id: str = Field(min_length=1, max_length=100)
    asset_id: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    source_start: float = Field(default=0, ge=0)


class SaveAudioClip(LayerClip):
    op: Literal["save_audio_clip"]
    volume: float = Field(default=1, ge=0, le=4)
    fade_in: float = Field(default=0, ge=0, le=30)
    fade_out: float = Field(default=0, ge=0, le=30)
    role: Literal["voice", "music", "other"] = "music"


class OverlayPlacement(EditAction):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class SaveOverlayClip(LayerClip):
    op: Literal["save_overlay_clip"]
    placement: OverlayPlacement
    opacity: float = Field(default=1, ge=0, le=1)
    fit: Literal["contain", "cover", "stretch"] = "contain"


class DeleteLayerClip(ClipAction):
    op: Literal["delete_layer_clip"]


class SetTrackMute(EditAction):
    op: Literal["set_track_mute"]
    track_id: str = Field(min_length=1, max_length=100)
    muted: bool


class MoveTrack(EditAction):
    op: Literal["move_track"]
    track_id: str = Field(min_length=1, max_length=100)
    to_index: int = Field(ge=1, le=63)


class DeleteLayerTrack(EditAction):
    op: Literal["delete_layer_track"]
    track_id: str = Field(min_length=1, max_length=100)


class SetTransition(ClipAction):
    op: Literal["set_transition"]
    type: Literal["none", "fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright"]
    duration: float = Field(default=0, ge=0, le=30)


class SetVideoFit(ClipAction):
    op: Literal["set_video_fit"]
    mode: Literal["contain", "cover", "stretch"]


AuthoringAction = Annotated[SetLock | ReplaceShot | ReplanShots | RestoreRevision | UndoEdit | ConfigurePlan | RefreshCandidates
                             | TrimClip | SplitClip | MoveClip | RemoveClip | SetClipAudio
                             | ImportTranscript | SaveSubtitle | DeleteSubtitle
                             | SaveAudioClip | SaveOverlayClip | DeleteLayerClip | SetTrackMute | MoveTrack | DeleteLayerTrack
                             | SetTransition | SetVideoFit,
                             Field(discriminator="op")]


class EditApplyRequest(EditAction):
    base_revision_id: str = Field(min_length=1, max_length=36)
    operations: list[AuthoringAction] = Field(min_length=1, max_length=100)
    source_asset_ids: list[str] = Field(default_factory=list, max_length=200)
    note: str = Field(default="", max_length=2000)


class EditRevisionRead(BaseModel):
    document_id: str
    project_id: str
    origin_job_id: str
    name: str
    revision_id: str
    head_revision_id: str
    parent_revision_id: str | None
    undo_revision_id: str | None
    number: int
    plan_hash: str
    timeline_hash: str
    timeline_id: str
    source_asset_ids: list[str]
    template: dict
    plan: dict
    operation: dict
    editor: dict = Field(default_factory=dict)
    created_at: datetime
