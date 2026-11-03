from pathlib import Path

import pytest

from app.services.providers import (
    ModelSnapshot,
    ProviderCapability,
    ProviderRequest,
    ProviderResult,
    ProviderRouter,
)
from app.services.renderer import RenderQualityError, build_ffmpeg_command
from timeline_engine import (
    AssetRef,
    AssetType,
    Clip,
    Placement,
    TextStyle,
    TimeRange,
    Timeline,
    Track,
    TrackType,
    Transition,
    TransitionType,
)


def timeline() -> Timeline:
    return Timeline(
        id="render-demo",
        fps=30,
        duration=2,
        resolution={"width": 1080, "height": 1920},
        assets=[AssetRef(id="a1", type=AssetType.VIDEO, uri="file:///tmp/input.mp4")],
        tracks=[Track(id="v1", type=TrackType.VIDEO, clips=[Clip(id="c1", asset_id="a1", range=TimeRange(start=0, end=2))])],
    )


def test_renderer_command_is_deterministic():
    command = build_ffmpeg_command(timeline(), Path("/tmp/output.mp4"))
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == "/tmp/input.mp4"
    assert Path(command[-1]) == Path("/tmp/output.mp4")


def test_renderer_honors_source_range_and_explicit_mapping():
    item = timeline().model_copy(deep=True)
    item.tracks[0].clips[0].source_start = 3.25
    item.tracks[0].clips[0].source_end = 5.25
    command = build_ffmpeg_command(item, "/tmp/output.mp4")
    assert command[command.index("-ss") + 1] == "3.250000"
    assert command[command.index("-t") + 1] == "2.000000"
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "0:a?"


def test_renderer_loops_still_image_as_base_clip():
    item = Timeline(
        id="still-base",
        fps=30,
        duration=2,
        resolution={"width": 1080, "height": 1920},
        assets=[AssetRef(id="still", type=AssetType.IMAGE, uri="file:///tmp/still.png")],
        tracks=[Track(id="v", type=TrackType.VIDEO, clips=[Clip(id="c", asset_id="still", range=TimeRange(start=0, end=2))])],
    )
    command = build_ffmpeg_command(item, "/tmp/output.mp4")
    assert "-loop" in command and command[command.index("-loop") + 1] == "1"
    assert "-filter_complex" in command


def test_renderer_rejects_unsupported_placement_and_tracks():
    offset = timeline().model_copy(deep=True)
    offset.tracks[0].clips[0].range = {"start": 0.5, "end": 2}
    with pytest.raises(RenderQualityError, match="cover the full timeline"):
        build_ffmpeg_command(offset, "/tmp/output.mp4")

    audio = timeline().model_copy(deep=True)
    audio.tracks.append(
        Track(
            id="a1",
            type=TrackType.AUDIO,
            clips=[Clip(id="ac1", asset_id="music", range=TimeRange(start=0, end=2))],
        )
    )
    audio.assets.append(AssetRef(id="music", type=AssetType.AUDIO, uri="file:///tmp/music.wav"))
    audio_command = build_ffmpeg_command(audio, "/tmp/output.mp4")
    assert "-filter_complex" in audio_command
    assert audio_command[audio_command.index("-filter_complex") + 1].endswith("[aout]")
    assert audio_command.count("-i") == 2

    two_audio = audio.model_copy(deep=True)
    two_audio.tracks.append(
        Track(
            id="a2",
            type=TrackType.AUDIO,
            clips=[Clip(id="ac2", asset_id="music", range=TimeRange(start=0, end=2))],
        )
    )
    with pytest.raises(RenderQualityError, match="at most one audio track"):
        build_ffmpeg_command(two_audio, "/tmp/output.mp4")

    opacity = timeline().model_copy(deep=True)
    opacity.tracks[0].clips[0].opacity = 0.5
    with pytest.raises(RenderQualityError, match="opacity"):
        build_ffmpeg_command(opacity, "/tmp/output.mp4")


class FakeProvider:
    capabilities = (ProviderCapability("transcription", ("video",), ("json",), quality_score=0.8, commercial_allowed=True),)

    def execute(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            artifact_uri="file:///transcript.json",
            artifact_type="transcript",
            model=ModelSnapshot(provider="fake", model_id="asr", version="1"),
        )


def test_provider_router_selects_commercial_provider():
    request = ProviderRequest(request_id="r1", capability="transcription", inputs={"uri": "file:///input.mp4"})
    result = ProviderRouter([FakeProvider()]).execute(request)
    assert result.artifact_type == "transcript"


def test_provider_router_rejects_unavailable_capability():
    with pytest.raises(LookupError):
        ProviderRouter().choose(ProviderRequest(request_id="r1", capability="video_generation", inputs={}))


def test_renderer_builds_multitrack_concat_and_subtitle_filter_graph():
    item = Timeline(
        id="multi",
        fps=30,
        duration=4,
        resolution={"width": 1080, "height": 1920},
        assets=[
            AssetRef(id="a", type=AssetType.VIDEO, uri="file:///tmp/a.mp4"),
            AssetRef(id="b", type=AssetType.VIDEO, uri="file:///tmp/b.mp4"),
        ],
        tracks=[
            Track(
                id="base",
                type=TrackType.VIDEO,
                clips=[
                    Clip(id="a1", asset_id="a", range=TimeRange(start=0, end=2)),
                    Clip(id="b1", asset_id="b", range=TimeRange(start=2, end=4)),
                ],
            ),
            Track(
                id="captions",
                type=TrackType.SUBTITLE,
                clips=[
                    Clip(
                        id="caption1",
                        text="Hello: world",
                        range=TimeRange(start=1, end=3),
                        placement=Placement(x=24, y=40),
                        style=TextStyle(font_size=56),
                    )
                ],
            ),
        ],
    )
    command = build_ffmpeg_command(item, "/tmp/output.mp4")
    graph = command[command.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=0" in graph
    assert "drawtext=" in graph and "Hello\\: world" in graph
    assert command.count("-i") == 2


def test_renderer_builds_xfade_and_audio_mix_filter_graph():
    item = Timeline(
        id="transition",
        fps=30,
        duration=4,
        assets=[
            AssetRef(id="a", type=AssetType.VIDEO, uri="file:///tmp/a.mp4"),
            AssetRef(id="b", type=AssetType.VIDEO, uri="file:///tmp/b.mp4"),
            AssetRef(id="music", type=AssetType.AUDIO, uri="file:///tmp/music.wav"),
        ],
        tracks=[
            Track(
                id="base",
                type=TrackType.VIDEO,
                clips=[
                    Clip(id="a1", asset_id="a", range=TimeRange(start=0, end=2)),
                    Clip(
                        id="b1",
                        asset_id="b",
                        range=TimeRange(start=2, end=4),
                        transition=Transition(type=TransitionType.DISSOLVE, duration=0.5),
                    ),
                ],
            ),
            Track(
                id="music-track",
                type=TrackType.AUDIO,
                clips=[
                    Clip(id="m1", asset_id="music", range=TimeRange(start=0, end=2)),
                    Clip(id="m2", asset_id="music", range=TimeRange(start=2, end=4), volume=0.6),
                ],
            ),
        ],
    )
    command = build_ffmpeg_command(item, "/tmp/output.mp4")
    graph = command[command.index("-filter_complex") + 1]
    assert "xfade=transition=dissolve" in graph
    assert "tpad=stop_mode=clone" in graph
    assert "amix=inputs=2" in graph
    assert graph.count("adelay=") == 2

