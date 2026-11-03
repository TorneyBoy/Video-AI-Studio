"""Vendor contracts with SDK substitutes. No real cloud jobs are created."""

import json
from types import SimpleNamespace

import pytest

from media_gateway.ice_client import ICEAPIError, ICEClient, normalize_transcript
from media_gateway.shot_client import DETECTOR_VERSION, ShotClient, ShotServiceError, normalize_shots


class SDK:
    def __init__(self):
        self.value, self.requests, self.error = {}, [], None

    def call(self, request, runtime):
        assert runtime.autoretry is False
        self.requests.append(request.to_map())
        if self.error:
            raise self.error
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: self.value))

    submit_asrjob_with_options = call
    get_smart_handle_job_with_options = call


def clients():
    sdk, configs = SDK(), []
    provider = SimpleNamespace(get_credentials=lambda: SimpleNamespace(access_key_id="fixture-id",
                               access_key_secret="fixture-secret", security_token="fixture-token"))

    def factory(config):
        configs.append(config)
        return sdk

    return (ShotClient(account_id="1234567890123456", region="cn-beijing", function_name="video-ai-shot-analysis",
                       credentials_provider=provider, client_factory=factory),
            ICEClient(region="cn-beijing", credentials_provider=provider, client_factory=factory), sdk, configs)


def test_shot_submission_uses_stable_task_id_scoped_function_and_temporary_credentials():
    detector, _, sdk, configs = clients()

    def invoke(name, request, headers, runtime):
        assert name == "video-ai-shot-analysis" and runtime.autoretry is False
        assert headers.x_fc_async_task_id == "shots_" + "a" * 64 and headers.x_fc_invocation_type == "Async"
        assert json.loads(request.body.read())["operation_id"] == "a" * 64
        if sdk.error:
            raise sdk.error
        return SimpleNamespace(status_code=202, headers={"x-fc-request-id": "submit-request"})

    sdk.invoke_function_with_options = invoke
    result = detector.submit_shots(operation_id="a" * 64, event={"operation_id": "a" * 64})
    assert result["job_id"] == "shots_" + "a" * 64
    assert configs[0].protocol == "https" and configs[0].region_id == "cn-beijing"
    assert configs[0].security_token == "fixture-token"
    with pytest.raises(ValueError):
        detector.submit_shots(operation_id="wrong", event={"operation_id": "wrong"})
    sdk.error = TimeoutError("fixture-secret signed-url")
    with pytest.raises(ShotServiceError) as caught:
        detector.submit_shots(operation_id="a" * 64, event={"operation_id": "a" * 64})
    assert not caught.value.submission_rejected and "fixture-secret" not in str(caught.value)


def test_shots_are_real_seconds_bound_to_cloud_job_and_source_duration():
    document = {"schema_version": "shot-boundaries/1", "detector_version": DETECTOR_VERSION,
        "operation_id": "a" * 64, "source_sha256": "b" * 64, "clock": "decoded-frame-pts-seconds",
        "shots": [{"start": 0, "end": 1.25}, {"start": 1.25, "end": 3.5}]}
    result = normalize_shots(document, duration=3.5, operation_id="a" * 64, source_sha256="b" * 64)
    assert result == [{"start": 0, "end": 1.25, "duration": 1.25}, {"start": 1.25, "end": 3.5, "duration": 2.25}]
    assert "confidence" not in result[0]
    with pytest.raises(ValueError):
        normalize_shots(document, duration=3.5, operation_id="wrong", source_sha256="b" * 64)
    with pytest.raises(ValueError):
        normalize_shots(document, duration=1, operation_id="a" * 64, source_sha256="b" * 64)


def test_task_status_redacts_signed_payloads_and_checks_identity():
    detector, _, sdk, _ = clients()

    def query(name, task_id, request, headers, runtime):
        assert name == "video-ai-shot-analysis" and not runtime.autoretry
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {"taskId": "task", "status": "Succeeded",
            "taskPayload": "signed-source-and-result-secrets", "requestId": "poll-request", "durationMs": 123}))

    sdk.get_async_task_with_options = query
    assert "signed-source" not in str(detector.shot_status("task"))
    with pytest.raises(ShotServiceError):
        detector.shot_status("other-task")


@pytest.mark.parametrize("elements", [[], [{"start": 0, "end": "NaN"}],
    [{"start": -1, "end": 1}], [{"start": 2, "end": 1}],
    [{"start": 0, "end": 2}, {"start": 1, "end": 3}]])
def test_invalid_shot_boundaries_are_rejected(elements):
    with pytest.raises(ValueError):
        normalize_shots({"schema_version": "shot-boundaries/1", "detector_version": DETECTOR_VERSION,
            "operation_id": "a" * 64, "source_sha256": "b" * 64, "clock": "decoded-frame-pts-seconds", "shots": elements},
            duration=3, operation_id="a" * 64, source_sha256="b" * 64)


def test_asr_retains_seconds_and_verifies_source_and_job_type():
    _, ice, sdk, _ = clients()
    source = "oss://fixture-bucket/tenants/t/source"
    sdk.value = {"JobId": "asr-job", "RequestId": "submit-request"}
    assert ice.submit_asr(object_uri=source, operation_id="operation")["job_id"] == "asr-job"
    assert "ClientToken" not in sdk.requests[-1]
    assert sdk.requests[-1]["InputFile"] == "oss://fixture-bucket.oss-cn-beijing.aliyuncs.com/tenants/t/source"
    sdk.value = {"JobId": "asr-job", "RequestId": "poll-request", "State": "Finished",
        "SmartJobInfo": {"JobType": "ASR", "InputConfig": {"InputFile": "oss://fixture-bucket.oss-cn-beijing.aliyuncs.com/tenants/t/source"}},
        "JobResult": {"AiResult": json.dumps([{"from": 0, "to": 0.846, "content": "测试文本"}])}}
    assert ice.asr_status("asr-job", object_uri=source, duration=3)["segments"] == [{"start": 0, "end": 0.846, "text": "测试文本"}]
    assert ice.asr_status("asr-job", object_uri="https://fixture-bucket.oss-cn-beijing.aliyuncs.com/tenants/t/source", duration=3)["status"] == "succeeded"
    with pytest.raises(ICEAPIError) as caught:
        ice.asr_status("asr-job", object_uri="oss://fixture-bucket/tenants/other/source", duration=3)
    assert caught.value.code == "ice_asr_source_mismatch"
    sdk.value["SmartJobInfo"]["JobType"] = "TextGenerate"
    with pytest.raises(ICEAPIError):
        ice.asr_status("asr-job", object_uri=source, duration=3)


@pytest.mark.parametrize("state", [None, "", "Unrecognized"])
def test_unconfirmed_task_state_keeps_querying_the_same_identity(state):
    detector, _, sdk, _ = clients()

    def query(name, task_id, request, headers, runtime):
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
            "taskId": "task", "status": state, "taskPayload": "signed-secret"}))

    sdk.get_async_task_with_options = query
    with pytest.raises(ShotServiceError) as caught:
        detector.shot_status("task")
    assert caught.value.retryable and caught.value.code == "shot_task_status_unknown"
    assert "signed-secret" not in str(caught.value)


@pytest.mark.parametrize("rows", [None, [{"from": 0, "to": 7, "content": "too long"}],
    [{"from": "NaN", "to": 2, "content": "invalid"}], [{"from": 0, "to": 1, "content": ""}]])
def test_invalid_transcripts_do_not_become_empty_success(rows):
    with pytest.raises(ValueError):
        normalize_transcript(rows, duration=3)
