import pytest

from timeline_engine import (
    AssetRef, AssetType, Clip, JobState, RenderJob, SlotConstraints,
    SlotType, SlotValue, TemplateBinding, TemplateDefinition, TemplateSlot,
    TimeRange, Timeline, Track, TrackType, create_manifest, validate_template_binding, validate_timeline,
)


def sample():
    timeline = Timeline(
        id="demo", fps=30, duration=2,
        assets=[AssetRef(id="a", type=AssetType.VIDEO, uri="file:///a.mp4")],
        tracks=[Track(id="v", type=TrackType.VIDEO, clips=[Clip(id="c", asset_id="a", range=TimeRange(start=0, end=2))])],
    )
    template = TemplateDefinition(id="t", version="1", duration=2, slots=[TemplateSlot(name="hero", type=SlotType.VIDEO, constraints=SlotConstraints(allowed_asset_types=[AssetType.VIDEO]))])
    binding = TemplateBinding(template_id="t", values={"hero": SlotValue(asset_ids=["a"], duration=2)})
    return timeline, template, binding


def test_manifest_is_deterministic_and_verifiable():
    timeline, template, binding = sample()
    a = create_manifest(timeline, template=template, binding=binding)
    b = create_manifest(timeline, template=template, binding=binding)
    assert a.manifest_id == b.manifest_id
    assert a.verify_hash()


def test_binding_type_and_required_slot():
    timeline, template, _ = sample()
    report = validate_template_binding(template, TemplateBinding(template_id="t"), timeline=timeline)
    assert not report.valid
    assert any(i.code == "required_slot" for i in report.errors)


def test_timeline_rejects_concrete_clip_without_declared_asset():
    with pytest.raises(ValueError, match="unknown asset"):
        Timeline(
            id="broken",
            fps=30,
            duration=1,
            tracks=[
                Track(
                    id="v",
                    type=TrackType.VIDEO,
                    clips=[
                        Clip(
                            id="c",
                            asset_id="missing",
                            range=TimeRange(start=0, end=1),
                        )
                    ],
                )
            ],
        )


def test_job_transitions_and_monotonic_progress():
    job = RenderJob(id="j", manifest_id="m")
    job.transition(JobState.RUNNING).update_progress(0.5).succeed("file:///out.mp4")
    assert job.terminal and job.progress == 1
    with pytest.raises(ValueError):
        job.transition(JobState.RUNNING)


def test_multitrack_schema_validates_text_and_asset_roles():
    timeline = Timeline(
        id="captions",
        fps=30,
        duration=2,
        assets=[AssetRef(id="still", type=AssetType.IMAGE, uri="file:///still.png")],
        tracks=[
            Track(
                id="caption",
                type=TrackType.SUBTITLE,
                clips=[Clip(id="cue", text="hello", range=TimeRange(start=0, end=1))],
            ),
            Track(
                id="overlay",
                type=TrackType.OVERLAY,
                clips=[Clip(id="still-clip", asset_id="still", range=TimeRange(start=1, end=2))],
            ),
        ],
    )
    assert validate_timeline(timeline).valid


def test_multitrack_schema_reports_subtitle_without_text():
    timeline = Timeline(
        id="bad-caption",
        fps=30,
        duration=1,
        assets=[AssetRef(id="still", type=AssetType.IMAGE, uri="file:///still.png")],
        tracks=[Track(id="caption", type=TrackType.SUBTITLE, clips=[Clip(id="cue", asset_id="still", range=TimeRange(start=0, end=1))])],
    )
    report = validate_timeline(timeline)
    assert not report.valid
    assert any(issue.code == "subtitle_text_required" for issue in report.errors)

