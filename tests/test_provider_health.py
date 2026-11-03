from __future__ import annotations

import json

from app.services.provider_health import provider_health_report
from app.services.provider_registry import ConfiguredProviderRegistry


def _registry(transport):
    return ConfiguredProviderRegistry.from_dict(
        {
            "providers": [
                {
                    "name": "hosted-health",
                    "kind": "http",
                    "endpoint": "https://provider.example",
                    "health_path": "/healthz",
                    "headers_env": {},
                    "capabilities": [
                        {"name": "video_generation", "commercial_allowed": True},
                    ],
                    "model": {
                        "model_id": "video-v1",
                        "version": "2026-10",
                        "license_snapshot": {"commercial_allowed": True},
                    },
                }
            ]
        }
    ), transport


def test_configuration_health_is_network_free_by_default(monkeypatch):
    calls = []

    def transport(request, timeout):
        calls.append((request.full_url, timeout))
        return 204, b""

    registry, _ = _registry(transport)
    # Replace the transport after loading the registry to prove the report uses
    # the adapter's explicit probe switch rather than performing a hidden call.
    registry.providers[0]._transport = transport
    report = provider_health_report(registry, source="fixture", probe=False)

    assert report.ok is True
    assert report.probed is False
    assert report.checks[0].status == "configured"
    assert report.checks[0].reason == "network_probe_disabled"
    assert report.checks[0].latency_ms is None
    assert calls == []

    rendered = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "Authorization" not in rendered
    assert "headers" not in rendered


def test_explicit_probe_calls_health_path_and_records_bounded_latency():
    calls = []

    def transport(request, timeout):
        calls.append((request.full_url, timeout, request.method))
        return 204, b"secret response must not be persisted"

    registry, _ = _registry(transport)
    registry.providers[0]._transport = transport
    report = provider_health_report(registry, source="fixture", probe=True)

    assert report.ok is True
    assert report.probed is True
    assert report.checks[0].status == "ready"
    assert report.checks[0].probed is True
    assert report.checks[0].latency_ms is not None
    assert 0 <= report.checks[0].latency_ms <= 60_000
    assert calls == [("https://provider.example/healthz", 10.0, "GET")]
    assert "secret response" not in json.dumps(report.as_dict(), ensure_ascii=False)


def test_explicit_probe_failure_is_reported_without_exception():
    def transport(request, timeout):
        return 503, b"private provider body"

    registry, _ = _registry(transport)
    registry.providers[0]._transport = transport
    report = provider_health_report(registry, source="fixture", probe=True)

    assert report.ok is False
    assert report.checks[0].status == "unavailable"
    assert report.checks[0].reason == "http_503"
    assert "private provider body" not in json.dumps(report.as_dict(), ensure_ascii=False)


def test_provider_health_cli_is_network_free_by_default(tmp_path, monkeypatch, capsys):
    config = {
        "providers": [
            {
                "name": "hosted-health",
                "kind": "http",
                "endpoint": "https://provider.example",
                "capabilities": [{"name": "video_generation", "commercial_allowed": True}],
                "model": {
                    "model_id": "video-v1",
                    "version": "2026-10",
                    "license_snapshot": {"commercial_allowed": True},
                },
            }
        ]
    }
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    def fail_network(*args, **kwargs):
        raise AssertionError("default provider-health must not perform network I/O")

    monkeypatch.setattr("app.services.provider_adapters.urlopen", fail_network)
    monkeypatch.setattr(
        "sys.argv",
        ["video-ai", "provider-health", "--path", str(path), "--json"],
    )
    from app.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["probed"] is False
    assert payload["providers"][0]["status"] == "configured"
