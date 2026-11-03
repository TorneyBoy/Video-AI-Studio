"""Project shot detector invoked on Function Compute, outside the control ECS."""

from __future__ import annotations

import io
import json
import math
import re


API_VERSION = "2023-03-30"
SDK_VERSION = "4.8.3"
DETECTOR_VERSION = "color-change-pts/1"


class ShotServiceError(ValueError):
    def __init__(self, code, *, retryable=False, submission_rejected=False):
        super().__init__("Cloud shot detection could not confirm the operation")
        self.code, self.retryable, self.submission_rejected = code, retryable, submission_rejected


class ShotClient:
    def __init__(self, *, account_id, region, function_name, credentials_provider, client_factory=None):
        if not re.fullmatch(r"[0-9]{8,32}", account_id) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", function_name):
            raise ValueError("Invalid function identity")
        self.account_id, self.region, self.function_name = account_id, region, function_name
        self.credentials_provider, self.client_factory = credentials_provider, client_factory

    def _client(self):
        from alibabacloud_fc20230330.client import Client
        from alibabacloud_tea_openapi.utils_models import Config

        credential = self.credentials_provider.get_credentials()
        # FC 3.0 uses the SDK's fcv3.<region>.aliyuncs.com service map. The
        # account-prefixed domain belongs to function/custom-domain traffic.
        return (self.client_factory or Client)(Config(region_id=self.region, protocol="https",
            read_timeout=20000, connect_timeout=5000,
            access_key_id=credential.access_key_id, access_key_secret=credential.access_key_secret,
            security_token=credential.security_token))

    @staticmethod
    def _error(exc):
        status = str(getattr(exc, "statusCode", None) or getattr(exc, "status_code", ""))
        rejected = status in {"400", "401", "403", "404", "413", "415", "422"}
        return ShotServiceError("shot_request_rejected" if rejected else "shot_request_unconfirmed",
                                retryable=not rejected, submission_rejected=rejected)

    def submit_shots(self, *, operation_id: str, event: dict):
        from alibabacloud_fc20230330.models import InvokeFunctionRequest, InvokeFunctionHeaders
        from darabonba.runtime import RuntimeOptions

        task_id = "shots_" + operation_id
        if not re.fullmatch(r"[0-9a-f]{64}", operation_id) or event.get("operation_id") != operation_id:
            raise ValueError("A stable shot operation identity is required")
        try:
            response = self._client().invoke_function_with_options(self.function_name,
                InvokeFunctionRequest(body=io.BytesIO(json.dumps(event, separators=(",", ":")).encode())),
                InvokeFunctionHeaders(x_fc_invocation_type="Async", x_fc_async_task_id=task_id, x_fc_log_type="None"),
                RuntimeOptions(autoretry=False))
        except Exception as exc:
            # Task mode rejects the same stable task ID with 409. Query it;
            # never replace it with a fresh ID or rely on an SDK auto retry.
            status = str(getattr(exc, "statusCode", None) or getattr(exc, "status_code", ""))
            if status == "409":
                return {"job_id": task_id, "submission": "deduplicated"}
            raise self._error(exc) from exc
        if response.status_code != 202:
            raise ShotServiceError("shot_submission_unconfirmed", retryable=True)
        headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
        return {"job_id": task_id, "request_id": headers.get("x-fc-request-id"), "submission": "accepted"}

    def shot_status(self, job_id: str):
        from alibabacloud_fc20230330.models import GetAsyncTaskRequest
        from darabonba.runtime import RuntimeOptions

        try:
            response = self._client().get_async_task_with_options(self.function_name, job_id,
                GetAsyncTaskRequest(), {}, RuntimeOptions(autoretry=False))
            value = response.body.to_map()
        except Exception as exc:
            raise self._error(exc) from exc
        if value.get("taskId") != job_id:
            raise ShotServiceError("shot_task_identity_mismatch")
        states = {"Enqueued": "queued", "Processed": "queued", "Running": "running", "Retrying": "running",
                  "Succeeded": "succeeded", "Failed": "failed", "Stopped": "failed", "Stopping": "running",
                  "Expired": "failed", "Invalid": "failed"}
        if value.get("status") not in states:
            # Task indexing may briefly return no recognizable state after
            # acceptance. Keep polling this known task within the operation
            # limit; an unfamiliar state is never evidence of completion.
            raise ShotServiceError("shot_task_status_unknown", retryable=True)
        # Do not expose taskPayload: it contains short lived object signatures.
        return {"job_id": job_id, "status": states[value["status"]], "request_id": value.get("requestId"),
                "duration_ms": value.get("durationMs"), "retries": value.get("alreadyRetriedTimes")}


def normalize_shots(value: dict, *, duration: float, operation_id: str, source_sha256: str) -> list[dict]:
    if (not isinstance(value, dict) or value.get("schema_version") != "shot-boundaries/1"
            or value.get("detector_version") != DETECTOR_VERSION or value.get("operation_id") != operation_id
            or value.get("source_sha256") != source_sha256 or value.get("clock") != "decoded-frame-pts-seconds"):
        raise ValueError("Shot result identity differs")
    elements = value.get("shots")
    if not isinstance(elements, list) or not elements or len(elements) > 20000:
        raise ValueError("Shot boundaries missing")
    shots, previous = [], 0.0
    for row in elements:
        if not isinstance(row, dict) or isinstance(row.get("start"), bool) or isinstance(row.get("end"), bool):
            raise ValueError("Invalid shot measurement")
        start, end = float(row["start"]), float(row["end"])
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or
                start < previous - 0.02 or end > duration + 0.02):
            raise ValueError("Shot boundaries are inconsistent")
        shots.append({"start": start, "end": end, "duration": end - start})
        previous = end
    if shots[0]["start"] > 0.02 or abs(shots[-1]["end"] - duration) > 0.1:
        raise ValueError("Shot result does not cover the source")
    return shots
