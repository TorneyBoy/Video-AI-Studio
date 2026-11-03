"""Real ICE SDK boundary. No media decoding or rendering runs on this host.

The API version is a service protocol version, not a neural model version.
SDK credentials are refreshed for each request and are never serialized.
"""

from __future__ import annotations

from fractions import Fraction
import json
import math
import re


API_VERSION = "2020-11-09"
SDK_VERSION = "6.20.2"
# Current official endpoints. SDK 6.20.2 still maps Beijing to the legacy
# global endpoint, which returned ServiceUnavailable during M1 acceptance.
ENDPOINTS = {region: f"ice.{region}.aliyuncs.com" for region in (
    "cn-beijing", "cn-hangzhou", "cn-shanghai", "cn-shenzhen", "ap-southeast-1", "us-west-1")}


class ICEAPIError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False, submission_rejected: bool = False):
        super().__init__("ICE could not confirm the requested media operation")
        self.code = code
        self.retryable = retryable
        self.submission_rejected = submission_rejected


class ICEClient:
    def __init__(self, *, region: str, credentials_provider, client_factory=None):
        self.region, self.credentials_provider = region, credentials_provider
        if region not in ENDPOINTS:
            raise ValueError("ICE region has no verified service endpoint")
        self.client_factory = client_factory

    def _client(self):
        from alibabacloud_ice20201109.client import Client
        from alibabacloud_tea_openapi.utils_models import Config

        credential = self.credentials_provider.get_credentials()
        config = Config(region_id=self.region, protocol="https", read_timeout=20000, connect_timeout=5000,
                        access_key_id=credential.access_key_id, access_key_secret=credential.access_key_secret,
                        security_token=credential.security_token)
        # https://help.aliyun.com/zh/ims/developer-reference/api-ice-2020-11-09-endpoint
        config.endpoint = ENDPOINTS[self.region]
        return (self.client_factory or Client)(config)

    def _call(self, method: str, request) -> dict:
        from darabonba.runtime import RuntimeOptions

        try:
            response = getattr(self._client(), method)(request, RuntimeOptions(autoretry=False))
            value = response.body.to_map()
        except ICEAPIError:
            raise
        except Exception as exc:
            status = str(getattr(exc, "statusCode", None) or getattr(exc, "status_code", ""))
            rejected = status in {"400", "401", "403", "404", "409", "413", "415", "422"}
            raise ICEAPIError("ice_request_rejected" if rejected else "ice_request_unconfirmed",
                              retryable=not rejected, submission_rejected=rejected) from exc
        if not isinstance(value, dict) or len(json.dumps(value).encode()) > 1024 * 1024:
            raise ICEAPIError("ice_response_invalid")
        return value

    def submit_render(self, *, client_token: str, timeline: dict, output: dict) -> dict:
        from alibabacloud_ice20201109.models import SubmitMediaProducingJobRequest

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_token):
            raise ValueError("A stable ASCII ICE client token is required")
        value = self._call("submit_media_producing_job_with_options", SubmitMediaProducingJobRequest(
            client_token=client_token, timeline=json.dumps(timeline, ensure_ascii=False, separators=(",", ":")),
            output_media_target="oss-object", output_media_config=json.dumps(output, separators=(",", ":")),
            # The project owns its analysis/archive paths. Disable implicit
            # IMS registrations and derivatives outside the declared output.
            editing_produce_config=json.dumps({"AutoRegisterOutputImsMedia": False,
                "NeedSnapshot": False, "NeedSprite": False,
                "InputMediaRegisterConfig": {"NeedSnapshot": False, "NeedSprite": False}}),
            source="OpenAPI"))
        if not value.get("JobId") or not value.get("RequestId"):
            raise ICEAPIError("ice_submission_identity_missing", retryable=True)
        return {"job_id": str(value["JobId"]), "request_id": str(value["RequestId"]),
                "media_id": value.get("MediaId"), "service_api_version": API_VERSION}

    def render_status(self, job_id: str) -> dict:
        from alibabacloud_ice20201109.models import GetMediaProducingJobRequest

        value = self._call("get_media_producing_job_with_options", GetMediaProducingJobRequest(job_id=job_id))
        job = value.get("MediaProducingJob") or {}
        if job.get("JobId") != job_id or not value.get("RequestId"):
            raise ICEAPIError("ice_job_identity_mismatch")
        state = job.get("Status")
        states = {"Init": "queued", "Queuing": "queued", "Processing": "running", "Success": "succeeded", "Failed": "failed"}
        if state not in states:
            raise ICEAPIError("ice_job_status_unknown")
        return {"job_id": job_id, "request_id": value["RequestId"], "status": states[state],
                "media_url": job.get("MediaURL"), "media_id": job.get("MediaId"),
                "duration_seconds": job.get("Duration"), "cloud_error_code": job.get("Code") if state == "Failed" else None}

    def media_info(self, object_uri: str) -> dict:
        from alibabacloud_ice20201109.models import GetMediaInfoJobRequest, SubmitSyncMediaInfoJobRequest, SubmitSyncMediaInfoJobRequestInput

        value = self._call("submit_sync_media_info_job_with_options", SubmitSyncMediaInfoJobRequest(
            input=SubmitSyncMediaInfoJobRequestInput(type="OSS", media=object_uri)))
        job = value.get("MediaInfoJob") or {}
        if (job.get("Input") or {}).get("Media") != object_uri or not job.get("JobId") or not value.get("RequestId"):
            raise ICEAPIError("ice_probe_identity_mismatch")
        if job.get("Status") != "Success":
            raise ICEAPIError("ice_probe_not_ready", retryable=job.get("Status") == "Init")
        submission_request_id = value["RequestId"]
        videos = (job.get("MediaInfoProperty") or {}).get("VideoStreamInfoList") or []
        if videos and not videos[0].get("Codec_name"):
            # The real synchronous response may omit codec details. Query the
            # same known job; do not submit another probe or infer the codec.
            original_job_id = job["JobId"]
            value = self._call("get_media_info_job_with_options", GetMediaInfoJobRequest(job_id=original_job_id))
            job = value.get("MediaInfoJob") or {}
            if (job.get("JobId") != original_job_id or (job.get("Input") or {}).get("Media") != object_uri
                    or not value.get("RequestId") or job.get("Status") != "Success"):
                raise ICEAPIError("ice_probe_followup_unconfirmed")
            videos = (job.get("MediaInfoProperty") or {}).get("VideoStreamInfoList") or []
            if not videos or not videos[0].get("Codec_name"):
                raise ICEAPIError("ice_probe_codec_unavailable")
        return {"job_id": job["JobId"], "request_id": value["RequestId"],
                "submission_request_id": submission_request_id,
                "facts": normalize_media_info(job.get("MediaInfoProperty") or {}),
                "service_api_version": API_VERSION, "sdk_version": SDK_VERSION}

    def submit_asr(self, *, object_uri: str, operation_id: str) -> dict:
        from alibabacloud_ice20201109.models import SubmitASRJobRequest
        from urllib.parse import urlparse
        from .ice_timeline import oss_media_url

        parsed = urlparse(object_uri)
        bucket = parsed.netloc.removesuffix(f".oss-{self.region}.aliyuncs.com")
        # ASR documents OSS URLs with the regional bucket endpoint, unlike
        # the canonical bucket-only references kept in our own database.
        input_file = "oss://" + oss_media_url(object_uri, bucket=bucket, region=self.region)[8:]
        value = self._call("submit_asrjob_with_options", SubmitASRJobRequest(input_file=input_file,
            title="Video AI Studio transcription", user_data=json.dumps({"operation_id": operation_id}),
            editing_config=json.dumps({"EnableSemanticSentenceDetection": True})))
        if not value.get("JobId") or not value.get("RequestId"):
            raise ICEAPIError("ice_asr_submission_identity_missing")
        return {"job_id": str(value["JobId"]), "request_id": str(value["RequestId"])}

    def asr_status(self, job_id: str, *, object_uri: str, duration: float) -> dict:
        from alibabacloud_ice20201109.models import GetSmartHandleJobRequest
        from .ice_timeline import oss_media_url
        from urllib.parse import urlparse

        value = self._call("get_smart_handle_job_with_options", GetSmartHandleJobRequest(job_id=job_id))
        info = value.get("SmartJobInfo") or {}
        if value.get("JobId") != job_id or not value.get("RequestId") or info.get("JobType") != "ASR":
            raise ICEAPIError("ice_asr_job_identity_mismatch")
        parsed = urlparse(object_uri)
        bucket = parsed.netloc
        if parsed.scheme == "https" and bucket.endswith(f".oss-{self.region}.aliyuncs.com"):
            bucket = bucket.removesuffix(f".oss-{self.region}.aliyuncs.com")
        try:
            expected = oss_media_url(object_uri, bucket=bucket, region=self.region)
            actual_uri = (info.get("InputConfig") or {}).get("InputFile") or ""
            # ICE's documented OSS form includes the bucket endpoint in netloc.
            endpoint = f"{bucket}.oss-{self.region}.aliyuncs.com"
            if actual_uri.startswith(f"oss://{endpoint}/"):
                actual_uri = "https://" + actual_uri[6:]
            if oss_media_url(actual_uri, bucket=bucket, region=self.region) != expected:
                raise ValueError("ASR source differs")
        except ValueError as exc:
            raise ICEAPIError("ice_asr_source_mismatch") from exc
        states = {"Created": "queued", "Executing": "running", "Finished": "succeeded", "Failed": "failed"}
        if value.get("State") not in states:
            raise ICEAPIError("ice_asr_status_unknown")
        result = {"job_id": job_id, "request_id": value["RequestId"], "status": states[value["State"]]}
        if result["status"] == "succeeded":
            raw = (value.get("JobResult") or {}).get("AiResult") or value.get("Output")
            try:
                result["segments"] = normalize_transcript(json.loads(raw), duration=duration)
            except (ValueError, TypeError, KeyError) as exc:
                raise ICEAPIError("ice_asr_result_invalid") from exc
        return result


def _positive(value, *, integer=False):
    try:
        number = float(Fraction(str(value)))
        if not math.isfinite(number) or number <= 0 or (integer and not number.is_integer()):
            raise ValueError("invalid measurement")
        return int(number) if integer else number
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise ICEAPIError("ice_probe_measurement_invalid") from exc


def normalize_media_info(value: dict) -> dict:
    """Only normalize measurements present in the documented ICE response."""
    basic = value.get("FileBasicInfo") or {}
    video = value.get("VideoStreamInfoList") or []
    audio = value.get("AudioStreamInfoList") or []
    formats = str(basic.get("FormatName") or "").split(",")
    if not formats[0] or (not video and not audio):
        raise ICEAPIError("ice_probe_streams_missing")
    facts = {"size_bytes": _positive(basic.get("FileSize"), integer=True),
             "duration_seconds": _positive(basic.get("Duration")), "format": ",".join(formats),
             "container": "mp4" if "mp4" in formats else formats[0], "has_audio": bool(audio)}
    if video:
        stream = video[0]
        facts["video"] = {"width": _positive(stream.get("Width"), integer=True),
                          "height": _positive(stream.get("Height"), integer=True),
                          "codec": str(stream.get("Codec_name") or ""),
                          "frame_rate": _positive(stream.get("Avg_fps") or stream.get("Fps"))}
    if audio:
        stream = audio[0]
        facts["audio"] = {"codec": str(stream.get("CodecName") or ""),
                          "sample_rate": _positive(stream.get("SampleRate")),
                          "channels": _positive(stream.get("Channels"), integer=True)}
    return facts


def normalize_transcript(rows, *, duration: float) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("ASR segments missing")
    result = []
    for row in rows:
        if not isinstance(row, dict) or isinstance(row.get("from"), bool) or isinstance(row.get("to"), bool):
            raise ValueError("Invalid ASR segment")
        start, end, text = float(row["from"]), float(row["to"]), row["content"]
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration + 0.1
                or not isinstance(text, str) or not text.strip() or len(text) > 20000):
            raise ValueError("Invalid ASR measurement")
        result.append({"start": start, "end": end, "text": text})
    return result
