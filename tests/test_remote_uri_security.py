import pytest

from app.services.remote_uri import RemoteURIError, validate_presigned_upload_url, validate_remote_uri


@pytest.mark.parametrize(
    "uri",
    [
        "http://cdn.example.com/video.mp4",
        "https://localhost/video.mp4",
        "https://127.0.0.1/video.mp4",
        # Legacy IPv4 spellings that some HTTP stacks normalize to loopback.
        "https://2130706433/video.mp4",
        "https://0x7f000001/video.mp4",
        "https://0177.0.0.1/video.mp4",
        "https://127.1/video.mp4",
        "https://10.0.0.8/video.mp4",
        "https://[::1]/video.mp4",
        "https://user:password@cdn.example.com/video.mp4",
    ],
)
def test_remote_uri_rejects_insecure_or_internal_targets(uri):
    with pytest.raises(RemoteURIError):
        validate_remote_uri(uri)


def test_object_storage_uri_remains_supported():
    assert validate_remote_uri("s3://studio-inputs/clip.mp4") == "s3://studio-inputs/clip.mp4"


def test_presigned_upload_url_requires_https_and_public_host():
    assert validate_presigned_upload_url("https://storage.example.com/upload?signature=redacted").startswith("https://")
    with pytest.raises(RemoteURIError):
        validate_presigned_upload_url("http://storage.example.com/upload")


@pytest.mark.parametrize("uri", ["https://cdn.example.com:bad/video.mp4", "https://cdn.example.com/video\n.mp4"])
def test_remote_uri_rejects_malformed_url_components(uri):
    with pytest.raises(RemoteURIError):
        validate_remote_uri(uri)
