"""Regression tests for the deterministic repository quality gate."""

from pathlib import Path

from scripts.quality_gate import _base_environment, normalize_repo_path, parse_args, repository_root


def test_gate_resolves_repository_root_without_process_cwd():
    root = repository_root()
    assert (root / "config" / "providers.contract.fixture.json").is_file()


def test_gate_normalizes_windows_relative_path_separators(tmp_path: Path):
    expected = (tmp_path / "config" / "providers.json").resolve()
    assert normalize_repo_path(r"config\providers.json", root=tmp_path) == expected


def test_gate_environment_forces_production_boundaries(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PROVIDER_MODE", "development_mock")
    monkeypatch.setenv("ASSET_STORAGE_MODE", "local")
    monkeypatch.setenv("PROVIDER_REGISTRY_PATH", r"C:\\secrets\\providers.json")

    env = _base_environment(tmp_path)

    assert env["PROVIDER_MODE"] == "api_only"
    assert env["ASSET_STORAGE_MODE"] == "external"
    assert env["DEBUG"] == "false"
    assert "PROVIDER_REGISTRY_PATH" not in env
    assert env["DATABASE_URL"].startswith("sqlite:///")
    assert env["STORAGE_ROOT"] == str(tmp_path / "storage")


def test_gate_parser_exposes_three_modes():
    assert parse_args(["fast"]).gate == "fast"
    assert parse_args(["integration", "--skip-console"]).skip_console is True
    assert parse_args(["all"]).gate == "all"
