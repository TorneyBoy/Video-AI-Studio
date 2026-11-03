"""Local media metadata analysis.

The first analysis provider deliberately has no GPU or Python media library
dependency.  It shells out to ``ffprobe`` using an argument list, normalises
the JSON output, and exposes extension points for shot analysis and optional
faster-whisper transcription.  Workers can therefore run on a plain Python
machine while richer providers are added later.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .providers import ModelSnapshot, ProviderCapability, ProviderRequest, ProviderResult


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_path(uri: str) -> Path | None:
    """Return a local path for a plain path or file:// URI."""
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        raw = unquote(parsed.path if parsed.scheme == "file" else uri)
        # file://relative/path is unusual; retaining the parsed path is safer
        # than passing the URI to Path unchanged.
        return Path(raw)
    return None


def detect_faster_whisper() -> dict[str, Any]:
    """Report optional faster-whisper availability without importing a model."""
    try:
        importlib.import_module("faster_whisper")
    except Exception:  # optional package may be installed but have missing native libs
        return {"provider": "faster-whisper", "available": False, "status": "unavailable"}
    try:
        version = importlib.metadata.version("faster-whisper")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "provider": "faster-whisper",
        "available": True,
        "status": "available",
        "version": version,
    }


@dataclass(frozen=True)
class AnalysisOptions:
    """Options accepted by the local provider.

    ``transcribe`` only enables the optional capability; actual model loading
    is intentionally opt-in via ``run_transcription`` so normal workers never
    download weights or require a GPU.
    """

    shot_analysis: bool = True
    transcribe: bool = False
    run_transcription: bool = False
    whisper_model: str = "small"
    ffprobe_bin: str = "ffprobe"
    timeout_seconds: int = 30

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "AnalysisOptions":
        value = value or {}
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in allowed if key in value})


class ShotAnalysisProvider:
    """Placeholder shot detector with a stable response shape.

    A CV shot detector can implement this interface later without changing the
    persisted analysis contract.
    """

    name = "placeholder"

    def analyze(self, *, duration_seconds: float | None = None, **_: Any) -> dict[str, Any]:
        return {
            "status": "not_implemented",
            "provider": self.name,
            "shots": [],
            "duration_seconds": duration_seconds,
        }


class MediaAnalysisProvider:
    """ffprobe based provider implementing the common Provider contract."""

    capabilities = (
        ProviderCapability(
            "media_analysis",
            ("video", "audio", "image", "media"),
            ("analysis/json",),
            quality_score=0.5,
            commercial_allowed=True,
        ),
    )

    def __init__(self, *, shot_provider: ShotAnalysisProvider | None = None, ffprobe_bin: str | None = None):
        self.shot_provider = shot_provider or ShotAnalysisProvider()
        self.ffprobe_bin = ffprobe_bin

    def execute(self, request: ProviderRequest) -> ProviderResult:
        uri = str(request.inputs.get("uri") or request.inputs.get("asset_uri") or "")
        if not uri:
            raise ValueError("media analysis requires inputs.uri")
        analysis = self.analyze(uri, options=request.parameters)
        return ProviderResult(
            artifact_uri=uri,
            artifact_type="media_analysis",
            model=ModelSnapshot(provider="local", model_id="ffprobe", version="1"),
            metrics={
                "duration_seconds": analysis.get("media", {}).get("duration_seconds"),
                "stream_count": len(analysis.get("streams", [])),
            },
            provenance={"analysis": analysis},
        )

    def analyze_shots(self, uri: str, *, duration_seconds: float | None = None, **kwargs: Any) -> dict[str, Any]:
        """Stable shot-analysis hook for a future CV detector."""
        return self.shot_provider.analyze(uri=str(uri), duration_seconds=duration_seconds, **kwargs)

    def analyze(self, uri: str, *, options: dict[str, Any] | AnalysisOptions | None = None) -> dict[str, Any]:
        uri = str(uri)
        opts = options if isinstance(options, AnalysisOptions) else AnalysisOptions.from_mapping(options)
        result: dict[str, Any] = {
            "schema_version": "1",
            "provider": "local.ffprobe",
            "analyzed_at": _utc_iso(),
            "source": {"uri": uri},
            "media": {},
            "streams": [],
            "video": None,
            "audio": None,
            # ``shots`` remains a list for the auto-edit planner contract;
            # detector status and provenance live alongside it.
            "shots": [],
            "shot_analysis": {"status": "not_requested", "provider": self.shot_provider.name, "shots": []},
            "transcription": {"provider": "faster-whisper", "status": "not_requested", "available": detect_faster_whisper()["available"]},
            "errors": [],
        }

        path = _local_path(uri)
        if path is not None:
            result["source"].update({"path": str(path), "exists": path.exists()})

        probe = self._run_ffprobe(uri, opts)
        if probe.get("error"):
            result["errors"].append(probe["error"])
            result["media"] = {"status": "unavailable"}
        else:
            self._merge_probe(result, probe["data"])

        duration = result.get("media", {}).get("duration_seconds")
        if opts.shot_analysis:
            shot_analysis = self.shot_provider.analyze(duration_seconds=duration)
            result["shot_analysis"] = shot_analysis
            result["shots"] = shot_analysis.get("shots", []) if isinstance(shot_analysis, dict) else []
        if opts.transcribe:
            result["transcription"] = self._transcription(uri, opts)
        return result

    def analyze_asset(self, asset_uri: str, *, options: dict[str, Any] | AnalysisOptions | None = None) -> dict[str, Any]:
        """Alias used by worker adapters that call the input an asset URI."""
        return self.analyze(asset_uri, options=options)

    def _run_ffprobe(self, uri: str, options: AnalysisOptions) -> dict[str, Any]:
        binary = self.ffprobe_bin or options.ffprobe_bin or shutil.which("ffprobe")
        if not binary:
            return {"error": "ffprobe is not installed"}
        command = [binary, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", uri]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1, int(options.timeout_seconds)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"error": f"ffprobe failed: {exc}"}
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()[:500]
            return {"error": f"ffprobe exited with {completed.returncode}" + (f": {detail}" if detail else "")}
        try:
            return {"data": json.loads(completed.stdout or "{}")}
        except json.JSONDecodeError as exc:
            return {"error": f"ffprobe returned invalid JSON: {exc}"}

    def _merge_probe(self, result: dict[str, Any], probe: dict[str, Any]) -> None:
        fmt = probe.get("format") or {}
        streams = probe.get("streams") or []
        duration = _number(fmt.get("duration"))
        media = {
            "status": "ok",
            "format_name": fmt.get("format_name"),
            "format_long_name": fmt.get("format_long_name"),
            "duration_seconds": duration,
            "size_bytes": int(_number(fmt.get("size"), 0) or 0),
            "bitrate": _number(fmt.get("bit_rate")),
            "start_time": _number(fmt.get("start_time")),
        }
        result["media"] = media
        normalized: list[dict[str, Any]] = []
        for stream in streams:
            kind = stream.get("codec_type")
            item: dict[str, Any] = {
                "index": stream.get("index"),
                "type": kind,
                "codec": stream.get("codec_name"),
                "codec_long_name": stream.get("codec_long_name"),
                "profile": stream.get("profile"),
                "duration_seconds": _number(stream.get("duration")),
                "bitrate": _number(stream.get("bit_rate")),
            }
            if kind == "video":
                item.update({
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "pixel_format": stream.get("pix_fmt"),
                    "frame_rate": stream.get("r_frame_rate") or stream.get("avg_frame_rate"),
                    "rotation": (stream.get("tags") or {}).get("rotate"),
                })
                if result["video"] is None:
                    result["video"] = item
            elif kind == "audio":
                item.update({"sample_rate": _number(stream.get("sample_rate")), "channels": stream.get("channels"), "channel_layout": stream.get("channel_layout")})
                if result["audio"] is None:
                    result["audio"] = item
            normalized.append(item)
        result["streams"] = normalized

    def _transcription(self, uri: str, options: AnalysisOptions) -> dict[str, Any]:
        detected = detect_faster_whisper()
        if not detected["available"]:
            return {**detected, "status": "unavailable", "requested": True, "segments": []}
        if not options.run_transcription:
            return {**detected, "status": "available", "requested": True, "executed": False, "segments": []}
        try:
            module = importlib.import_module("faster_whisper")
            model = module.WhisperModel(options.whisper_model, device="cpu", compute_type="int8")
            segments, info = model.transcribe(uri)
            rows = [{"start": float(segment.start), "end": float(segment.end), "text": segment.text} for segment in segments]
            return {**detected, "status": "ok", "requested": True, "executed": True, "language": getattr(info, "language", None), "segments": rows}
        except Exception as exc:  # optional dependency/model failures are metadata, not worker crashes
            return {**detected, "status": "error", "requested": True, "executed": True, "error": str(exc)[:500], "segments": []}


# Names kept explicit for adapters and callers that prefer service/provider
# terminology.  They intentionally refer to the same implementation.
MediaAnalysisService = MediaAnalysisProvider
FFprobeProvider = MediaAnalysisProvider
FFProbeProvider = MediaAnalysisProvider
FFProbeAnalyzer = MediaAnalysisProvider
MediaMetadataAnalyzer = MediaAnalysisProvider
FFProbeMediaProvider = MediaAnalysisProvider


def analyze_media(uri: str, *, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience function for scripts and tests."""
    return MediaAnalysisProvider().analyze(uri, options=options)

