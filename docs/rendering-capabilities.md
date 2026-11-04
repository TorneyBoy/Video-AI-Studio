# Draft renderer capability contract

## External ICE compiler and rollout

Production keeps `PROVIDER_MODE=api_only` and `ASSET_STORAGE_MODE=external`.
`media_gateway.ice_timeline` translates the immutable Timeline into cloud
requests; it does not execute FFmpeg or decode media in the control plane.

The `ice-timeline/4` compiler now represents the following additional contracts:

| Feature | Compiler behavior | Cloud rollout |
| --- | --- | --- |
| Independent/multiple audio tracks | Preserve source trims and output offsets, mix tracks, apply gain and explicit mute; linear `AFade` envelopes preserve the declared in/out duration. | Disabled by default; requires actual audio and cost acceptance. |
| Images and media overlays | Explicit image duration; preserve layer order, placement, opacity and Contain/Cover/Fill fit. Overlay audio must use an explicit audio track. | Disabled by default; requires actual visual and combination acceptance. |
| Explicit overlap transitions | Map incoming fade/wipeleft/wiperight to the preceding ICE clip; use ordinary Transition with the reduced canonical duration. Never use frozen-frame DLTransition. | Disabled by default; requires actual transition/audio synchronization acceptance. |
| Additional hidden tracks | Omit hidden overlay/text; retain muted audio as zero gain. | Requires explicit `track_mute` admission. |

`GATEWAY_ICE_RENDER_FEATURES` is a comma-separated deployment allowlist, empty
by default. Unknown entries refuse startup. It preserves the existing operation
quota and the accepted M1 subset; enabling a compiler option is not cloud
acceptance. Health returns compiler and enabled features separately and continues
to report `cloud_verified=false`.

The control Provider capability must independently carry version-specific media
and price evidence. A single accepted feature can use `limits.features`; two or
more required feature names must also be covered by one complete
`limits.feature_combinations` row on the same Provider/model/version. For example,
audio plus fades requires:

```json
{
  "features": ["audio_tracks", "audio_fades"],
  "feature_combinations": [["audio_tracks", "audio_fades"]]
}
```

Independent rows are not merged into a verified combination. Quotes, queue
admission and new remote submissions share this rule; existing remote operations
remain queryable. Accepted results freeze compiler version and actual required
features. Additional effects return unknown cost for reconciliation rather than
reuse the basic M1 tariff without evidence. The dated sample matrix below records
the media evidence actually obtained; issued bill reconciliation and broader
feature/combination acceptance remain outstanding.

Legacy transitions, unmapped dissolve/slide effects, custom fonts, subtitle
backgrounds/outlines, text bounding boxes and media overlays above text still
fail explicitly in this profile. No unsupported edit is silently discarded.

Implementation fields follow the official [Timeline configuration](https://help.aliyun.com/zh/ims/developer-reference/timeline-configuration-description),
[Effect configuration](https://help.aliyun.com/zh/ims/developer-reference/effect-configuration-description)
and [transition examples](https://help.aliyun.com/en/ims/developer-reference/normal-transition-effect-example).

## 2026-11-04 real-output sample matrix

These are local QC observations of three new, real ICE outputs from isolated
acceptance requests. They do not enable a Provider capability or change the
empty-by-default deployment allowlist. All three outputs are 480×832 at 30fps;
none establishes other frame rates or resolutions. Original M1 analysis `3/6`
and render `6/6` stay consumed. New samples are recorded separately, the existing
cumulative USD10 ceiling is unchanged, and unknown bills retain their separate
reservations.

| Feature / sample | Actual result | Acceptance boundary |
| --- | --- | --- |
| Planner-derived single-source trim, `auto-edit-real-v1` | **Pass for this structural trim.** Actual source `0..3s` became 3.000s/90 frames; complete decode passed, all 90 same-index frames were compared with the source, mean SSIM `0.988093`, and paired first/middle/last images match. | The planner used `source_order` with a whole-source fallback. No shots/ASR/semantic scoring was available; the production analysis guard still returned `analysis_not_ready`. This is not semantic selection or multi-material editing acceptance. |
| Chinese/English text, `effects-basic-v1` | **Pass for these cues.** Both texts are complete and legible, present on frames `15..134` and absent at frame `135`, matching `[0.5,4.5)`. | Cloud default font, black 28px Chinese and 20px English only; no custom font/style/general script coverage claim. |
| Independent audio, gain, linear fades, `effects-basic-v1` | **Pass within one AAC frame.** Independent 440Hz WAV is present; measured stereo total-energy gain `0.549783` for requested `0.55`. Linear fade-in/out fits are `0.601012s`/`0.810897s`, both `R²≈0.999998`; the fade-out difference is below one 21.333ms AAC frame. | One mono source converted to equal stereo channels, not multi-audio mixing or sample-exact timing. Source video has no audio, so this does not prove primary-track mute. |
| Image placement, Contain and transparent composition, `effects-basic-v1` | **Spatial behavior passes for this logo.** A 160×96 RGBA logo in the `(24,32,120,100)` container produces 120×72 content at `(24,46)` with all calibration corners and a transparent ring hole/background. | Exact temporal behavior fails separately. Cover/Stretch were only numerical comparison hypotheses, not real rendered samples. |
| Exact Image end, `effects-basic-v1` | **Fail.** Expected 120 visible frames `15..134`; observed 121 frames `15..135`. Logo remains at 4.500s after both text cues disappear and is gone at 4.533333s. | Preserve the original failed/partial report; the full audio+text+image combination is not verified. |
| Image end diagnostic, `image-boundary-v1` | **No tested fix passes.** Across all 60 frames, original `TimelineOut+Duration`, Out-only and Duration-only each display frames `15..45` (31 frames). Decreasing both fields by one frame displays only `15..43` (29 frames). Target was `15..44` (30 frames). | Removing TimelineOut is ineffective in this sample. The minus-one-frame candidate loses the intended last frame and must not become a compiler fix. No safe inference for one-frame clips, other fps rounding or primary-image sequences is established. |
| Overlap fade/wipe transitions, multiple audio tracks, original-video mute, video overlays, other output profiles | **No new real-output acceptance.** Compiler/contracts exist where documented above. | The prepared overlap sample was not submitted in this three-job round. A successful job or unrelated sample does not supply this missing evidence. |

Evidence: [effects sample QC](../data/testing/ai-production-20261104/ice-effects/effects-basic-v1.qc.md),
[image boundary diagnostic](../data/testing/ai-production-20261104/ice-effects/image-boundary-v1.qc.md),
[planner trim QC](../data/testing/ai-production-20261104/auto-edit-real-v1.qc.md).
The diagnostic deliberately varies three rows after baseline compilation; it is
not an exact production compiler-output replay and does not revalidate the
entire original feature combination. Frame-by-frame measurements and paired
contact sheets, rather than cloud Job Success, determine these results.
The diagnostic's actual cloud-returned Timeline was compared field by field
with the submitted JSON: all four variants, their omitted fields and timing
values were preserved. The UI retained the intended experiment; the cause of
the Image boundary behavior remains unresolved.

The official [Timeline fields](https://help.aliyun.com/zh/ims/developer-reference/timeline-configuration-description)
describe Image Duration and TimelineOut but do not define inclusive/exclusive
frame-end semantics. The “Duration and TimelineOut only one takes effect” note
belongs to EffectTrackItem and is not an Image rule. The official
[Duration-only overlay example](https://help.aliyun.com/zh/ims/use-cases/video-picture-mix-cut)
motivated diagnostic C; the real result still failed exact timing. The compiler
therefore remains `ice-timeline/4` with no guessed timestamp compensation.

The prior full offline baseline was **858 passed / 74 skipped** after the person
output-contract fix ([log](../data/testing/ai-production-20261104/quality-gate-person-output.log)).
The new runtime safeguards below are a later implementation increment. The
compiler output remains unchanged; no timestamp compensation or new cloud
acceptance has been asserted. Pending bill and deployment checks remain open.

## Runtime quality safeguards and located findings

The known native ICE Image boundary failure now has a runtime fence in
`app.services.media_quality.assert_renderer_quality`. A new Image request for
`aliyun-ice-cloud-editing` fails with `renderer_quality_unverified`, including
the affected track, clip, output interval and acceptance-evidence reference.
It does not restrict image handling by a different renderer, visible video
clips or fully hidden overlays. This mitigates new unsupported execution;
the underlying Image timing defect remains unresolved.

The gateway checks this rule after finding an existing idempotent operation
and before creating a new row or consuming quota. Existing operation IDs and
historical specification hashes stay readable/replayable. A saved queued
operation without a cloud ID cannot start a new Image submission; an existing
cloud ID is polled to preserve the original charge-bearing task and result.
Such a result retains the known renderer finding instead of asserting that the
Image timing passed. The gateway health response exposes the blocked constraint.

`metrics.render_quality_assessment` is an explicit `structural_probe` assessment.
It reports independently located object-binding, dimensions, duration, fps and
requested-audio-presence failures. Facts, narrative, visual quality and audio
content remain `not_assessed`; a detected audio stream is not a listening or
tail-syllable pass. The existing `media-quality/1` report shape remains available
for the legacy archive/review contract, while `renderer_quality_findings` records
known renderer limitations separately.

The shared `QualityFinding` contract carries severity, quality dimension,
track/clip, a half-open output time range and typed evidence. Canonical-layout
findings are labelled `canonical_timeline`, never rendered pixel measurements.
A subtitle origin completely outside the canvas is an error; a declared region
overflow requires review. An in-bounds origin does not establish actual font
layout, line wrapping, platform safety or legibility.

`visibility_timing_findings` consumes externally measured, unobscured clip
visibility bound to the actual output SHA256, canonical Timeline hash and fps.
Every output frame must be inspected in order; sparse samples or a different
output cannot count as exact timing evidence. It locates both extra visible
frames and missing frames. The prior 150-frame real measurements have been
normalized with this implementation: the logo yields an error for
`[4.500,4.533333...)`, while both subtitle cues have no visibility-timing
mismatch ([normalized findings](../data/testing/ai-production-20261104/ice-effects/effects-basic-v1.normalized-findings.json)).
This reuses existing measurements and bytes, with no new cloud task or image
detector run, and does not broaden the original sample's acceptance.

`audio_tail_finding` compares measured isolated stream bounds under an explicit,
bounded tolerance chosen by the calling quality policy. A shorter stream is
reported for tail/padding investigation; it does not infer a lost word or
complete audio fidelity. The earlier person sample's 64ms audio difference
continues to require investigation rather than being silently waived.

## 图片边界的后续诊断

2026-11-04只读复查：[官方Timeline配置](https://help.aliyun.com/zh/ims/developer-reference/timeline-configuration-description)规定四位小数时间精度，未明确图片结束帧的开闭规则；EffectTrackItem的Duration/TimelineOut规则不能直接套用于Image。[官方FAQ](https://www.alibabacloud.com/help/en/ims/support/intelligent-production-making-faq)建议在部分图片不稳定场景先用视频验证，但没有保证精确帧边界。本轮未据此修改编译器或新增云调用。

已有四组诊断中，双字段各减一帧同时改变了两个变量，仍不足以分离字段交互与小数量化。后续最小实验保留原对照，仅增加以下单因素变体；这只是待验证方案：

| 变体 | TimelineIn | TimelineOut | Duration | 区分目的 |
| --- | --- | --- | --- | --- |
| E | 0.5 | 1.4667 | 不填 | 单独缩短出点 |
| F | 0.5 | 不填 | 0.9667 | 单独缩短时长 |
| G，可选 | 0.5 | 1.4666 | 不填 | 四位小数跨帧舍入 |
| H，可选 | 0.5 | 1.5 | 0.9667 | 与双字段缩短组比较交互 |

目标仍为30fps下frame15–44，共30帧，须检查全部60帧；即使单项通过，也须另验不同fps、位置与组合后才能开放通用支持。原requirements中的submitted:false是提交前快照，实际状态以随后cloud-status和QC为准。后续执行继续遵守原累计预算与任务台账，不重提旧任务。

另一候选方案是在托管渲染服务先将全屏图片转为定帧率视频，再按Video合同合成；尚未实现或验收。透明Logo转为普通H.264会丢失透明区域，须另验alpha/遮罩合同，或由托管渲染器一次合成背景与Logo。生产控制面仍保持api_only/external。

## Local development renderer

生产默认的 `PROVIDER_MODE=api_only` 不在平台进程执行下表的本地 FFmpeg 路径。Worker
只把 Canonical Timeline 和可访问的远端素材 URI 发送给 `video_editing` Provider，并
保存远程 Artifact 指针；下表适用于 `development_mock` 的本地契约测试。

The local FFmpeg worker treats the Canonical Timeline as the source of truth.
It either represents every supported track explicitly or rejects the timeline;
it never drops a track, clip, or effect silently.

| Timeline feature | Draft worker | Contract |
| --- | --- | --- |
| One video track with one clip | Supported | Clip covers `0..timeline.duration`; local video or still-image asset; video source ranges may seek into the asset. Still images are looped for the clip duration. Video-only timelines use the legacy command path for deterministic backwards compatibility. |
| One video track with contiguous clips | Supported | Clips are concatenated in timeline order. Gaps and overlaps are rejected with `RenderQualityError`. |
| Video transitions | Supported | Add `transition` to every clip after the first; the renderer emits FFmpeg `xfade` and pads the final frame when the transition shortens the visual stream. Mixed transition/cut boundaries are rejected. |
| One audio track with one clip | Supported | Clip covers `0..timeline.duration`; local audio asset; source audio is replaced by the external track. Volume is applied with `volume` and timestamps are normalized with `aresample`. |
| One audio track with multiple clips | Supported | Each clip is trimmed, delayed to its timeline position, volume-adjusted, and mixed with `amix`. Multiple audio tracks are rejected to preserve the existing track contract. |
| Embedded source audio | Supported | Preserved when no external audio track exists, through an optional `0:a?` map. |
| Subtitle/text tracks | Supported | Use a `Clip(text=...)` with optional `Placement` and `TextStyle`; FFmpeg `drawtext` is enabled only for the cue range. Text is escaped before entering the filter graph. |
| Image/video overlay tracks | Supported | Local image/video assets are composited by `overlay`; optional placement, dimensions, opacity, and z-order (`track.metadata.z_index`) are honored. |
| Multiple audio tracks | Rejected | Returns `RenderQualityError`; use one audio track with multiple clips or a pre-mixed audio asset. |
| Clip effects, base opacity/video volume, muted tracks | Rejected | Must be lowered into an approved filter graph first. Overlay opacity is supported; unsupported effects return a quality error before FFmpeg starts. |
| Remote object-store URIs | Rejected | Worker input must be prefetched to a local path before FFmpeg. |

The worker renders to a job-specific temporary path, runs `ffprobe` quality
checks for the expected resolution and duration, computes the output SHA-256,
and atomically renames the file before creating an Artifact. A failed quality
check therefore cannot publish a partial render.
