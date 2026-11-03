# Draft renderer capability contract

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
