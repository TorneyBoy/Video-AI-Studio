"""ICE SDK contract tests use injected clients and never spend cloud credit."""

import json
from types import SimpleNamespace

import pytest

from media_gateway.ice_client import ICEAPIError, ICEClient, normalize_media_info


def _info():
    return {"FileBasicInfo": {"Duration": "5.0", "FileSize": "1234", "FormatName": "mov,mp4,m4a"},
            "VideoStreamInfoList": [{"Width": "1080", "Height": "1920", "Codec_name": "h264", "Avg_fps": "30/1"}],
            "AudioStreamInfoList": [{"CodecName": "aac", "SampleRate": "48000", "Channels": "2"}]}


class FakeSDK:
    def __init__(self):
        self.response = {"JobId": "cloud-job", "RequestId": "cloud-request", "MediaId": "cloud-media"}
        self.requests, self.error = [], None

    def _call(self, request, runtime):
        self.requests.append(request.to_map())
        assert runtime.autoretry is False
        if self.error:
            raise self.error
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: self.response))

    submit_media_producing_job_with_options = _call
    get_media_producing_job_with_options = _call
    submit_sync_media_info_job_with_options = _call


def _client():
    sdk, configs, issued = FakeSDK(), [], []

    def credentials():
        issued.append(len(issued))
        return SimpleNamespace(access_key_id=f"temporary-{len(issued)}", access_key_secret="fixture-secret",
                               security_token=f"temporary-token-{len(issued)}")

    def factory(config):
        configs.append(config)
        return sdk

    return ICEClient(region="cn-beijing", credentials_provider=SimpleNamespace(get_credentials=credentials),
                     client_factory=factory), sdk, configs


def test_submit_uses_same_token_refreshes_credentials_and_keeps_region():
    client, sdk, configs = _client()
    for _ in range(2):
        result = client.submit_render(client_token="stable-operation", timeline={"VideoTracks": []},
                                      output={"MediaURL": "https://bucket.oss-cn-beijing.aliyuncs.com/output.mp4"})
        assert result["job_id"] == "cloud-job" and result["service_api_version"] == "2020-11-09"
    assert [row["ClientToken"] for row in sdk.requests] == ["stable-operation", "stable-operation"]
    assert all(row["OutputMediaTarget"] == "oss-object" for row in sdk.requests)
    assert all(json.loads(row["EditingProduceConfig"]) == {"AutoRegisterOutputImsMedia": False,
        "NeedSnapshot": False, "NeedSprite": False,
        "InputMediaRegisterConfig": {"NeedSnapshot": False, "NeedSprite": False}} for row in sdk.requests)
    assert all(config.region_id == "cn-beijing" and config.protocol == "https" for config in configs)
    assert all(config.endpoint == "ice.cn-beijing.aliyuncs.com" for config in configs)
    assert configs[0].access_key_id != configs[1].access_key_id
    with pytest.raises(ValueError):
        client.submit_render(client_token="非ASCII", timeline={}, output={})
    assert len(sdk.requests) == 2


@pytest.mark.parametrize("cloud, expected", [("Init", "queued"), ("Queuing", "queued"),
    ("Processing", "running"), ("Success", "succeeded"), ("Failed", "failed")])
def test_status_mapping_and_job_identity(cloud, expected):
    client, sdk, _ = _client()
    sdk.response = {"RequestId": "poll-request", "MediaProducingJob": {"JobId": "cloud-job", "Status": cloud}}
    assert client.render_status("cloud-job")["status"] == expected
    with pytest.raises(ICEAPIError) as caught:
        client.render_status("different-job")
    assert caught.value.code == "ice_job_identity_mismatch"


def test_metadata_is_bound_to_source_and_measured_streams():
    client, sdk, _ = _client()
    uri = "oss://bucket/tenants/tenant/source"
    sdk.response = {"RequestId": "probe-request", "MediaInfoJob": {"JobId": "probe-job", "Status": "Success",
        "Input": {"Type": "OSS", "Media": uri}, "MediaInfoProperty": _info()}}
    result = client.media_info(uri)
    assert sdk.requests[0]["Input"] == {"Type": "OSS", "Media": uri}
    assert result["facts"]["video"]["frame_rate"] == 30
    assert result["facts"]["audio"]["channels"] == 2 and result["facts"]["container"] == "mp4"
    with pytest.raises(ICEAPIError) as caught:
        client.media_info("oss://bucket/other-source")
    assert caught.value.code == "ice_probe_identity_mismatch"


def test_incomplete_sync_probe_queries_same_job_for_codec():
    client, sdk, _ = _client()
    uri = "oss://bucket/tenants/tenant/source"
    incomplete = _info()
    incomplete["VideoStreamInfoList"][0].pop("Codec_name")
    sdk.response = {"RequestId": "sync-request", "MediaInfoJob": {"JobId": "probe-job", "Status": "Success",
        "Input": {"Type": "OSS", "Media": uri}, "MediaInfoProperty": incomplete}}

    def followup(request, runtime):
        sdk.requests.append(request.to_map())
        assert request.job_id == "probe-job" and runtime.autoretry is False
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
            "RequestId": "followup-request", "MediaInfoJob": {"JobId": "probe-job", "Status": "Success",
            "Input": {"Type": "OSS", "Media": uri}, "MediaInfoProperty": _info()}}))

    sdk.get_media_info_job_with_options = followup
    result = client.media_info(uri)
    assert len(sdk.requests) == 2 and sdk.requests[1] == {"JobId": "probe-job"}
    assert result["facts"]["video"]["codec"] == "h264"
    assert result["submission_request_id"] == "sync-request" and result["request_id"] == "followup-request"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0/0", "-1", "0", "missing"])
def test_invalid_measurements_are_not_replaced_with_defaults(value):
    info = _info()
    info["VideoStreamInfoList"][0]["Avg_fps"] = value
    with pytest.raises(ICEAPIError):
        normalize_media_info(info)


def test_errors_hide_secrets_and_preserve_unknown_submission_outcome():
    client, sdk, _ = _client()
    sdk.error = TimeoutError("fixture-secret temporary-token signed-url")
    with pytest.raises(ICEAPIError) as caught:
        client.submit_render(client_token="stable", timeline={}, output={})
    assert caught.value.retryable and not caught.value.submission_rejected
    assert "fixture-secret" not in str(caught.value) and "signed-url" not in str(caught.value)
