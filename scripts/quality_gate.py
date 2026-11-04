#!/usr/bin/env python3
"""Run the repository's deterministic fast/integration quality gates.

The gate is intentionally a control-plane check.  It forces the production
boundaries (``PROVIDER_MODE=api_only`` and ``ASSET_STORAGE_MODE=external``),
uses an isolated SQLite/storage directory, and never opts in to a Provider
health probe or a real Provider smoke request.  A staging smoke request is a
separate, explicitly authorised operator action.

Examples::

    python scripts/quality_gate.py fast
    python scripts/quality_gate.py integration
    python scripts/quality_gate.py all

The script resolves all repository paths from its own location rather than
the current working directory.  Its fixture paths also normalize relative
Windows separators, which matters when it is launched from PowerShell/Git
Bash using ``scripts\\quality_gate.py`` or from another CI working directory.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CommandResult:
    name: str
    returncode: int


class GateFailure(RuntimeError):
    """Raised when one deterministic gate step fails."""


def repository_root() -> Path:
    """Return the checkout root, independent of the process CWD."""

    return Path(__file__).resolve().parents[1]


def normalize_repo_path(value: str | Path, *, root: Path | None = None) -> Path:
    """Resolve a path supplied by a shell or CI job in a cross-platform way.

    Relative Windows paths are sometimes passed to Python from a POSIX shell
    with backslashes (for example ``config\\providers.json``).  ``Path`` on
    POSIX treats that as one filename, so convert separators only for a
    relative path.  Absolute paths retain their native representation.
    """

    base = root or repository_root()
    raw = os.fspath(value)
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (base / Path(str(raw).replace("\\", "/"))).resolve()


def _command_label(args: Sequence[str]) -> str:
    return " ".join(str(part) for part in args)


def run_command(
    name: str,
    args: Sequence[str],
    *,
    root: Path,
    env: dict[str, str],
) -> CommandResult:
    """Run one gate command without a shell or network-specific flags."""

    print(f"[gate] {name}: {_command_label(args)}", flush=True)
    completed = subprocess.run(args, cwd=root, env=env, check=False)
    if completed.returncode:
        raise GateFailure(f"{name} failed with exit code {completed.returncode}")
    print(f"[gate] {name}: PASS", flush=True)
    return CommandResult(name, completed.returncode)


def _base_environment(tmp: Path) -> dict[str, str]:
    """Build a hermetic, production-boundary environment for subprocesses."""

    env = dict(os.environ)
    # These are deliberately hard-coded gate invariants.  A developer's
    # shell must not make a PR gate accidentally run a local model or accept
    # local media bytes.
    env.update(
        {
            "APP_ENVIRONMENT": "test",
            "PROVIDER_MODE": "api_only",
            "ASSET_STORAGE_MODE": "external",
            "AUTH_MODE": "development_header",
            "DEBUG": "false",
            "METRICS_API_SECRET": "",
            "OBSERVATION_QUEUE_WARNING_SECONDS": "300",
            "OBSERVATION_REMOTE_WARNING_SECONDS": "900",
            "OBSERVATION_BILLING_WARNING_SECONDS": "3600",
            "OBSERVATION_WORKER_STALE_SECONDS": "120",
            "PYTHONDONTWRITEBYTECODE": "1",
            "DATABASE_URL": f"sqlite:///{(tmp / 'gate.db').as_posix()}",
            "STORAGE_ROOT": str(tmp / "storage"),
        }
    )
    # A stale local registry could contact a vendor if a test accidentally
    # calls the opt-in smoke command.  The static gate supplies its own
    # fixture and no subprocess receives an operator registry by accident.
    env.pop("PROVIDER_REGISTRY_PATH", None)
    env.pop("PROVIDER_REGISTRY_JSON", None)
    return env


def _python(*args: str) -> list[str]:
    return [sys.executable, *args]


FAST_TESTS = (
    "tests/test_provider_policy.py",
    "tests/test_provider_preflight.py",
    "tests/test_provider_registry_templates.py",
    "tests/test_provider_smoke.py",
    "tests/test_provider_acceptance.py",
    "tests/test_provider_health.py",
    "tests/test_provider_gateway_smoke.py",
    "tests/test_quality_gate.py",
    "tests/test_provider_adapters.py",
    "tests/test_remote_uri_security.py",
    "tests/test_timeline_engine.py",
)


def run_fast(*, root: Path, env: dict[str, str], skip_console: bool = False, include_unit_tests: bool = True) -> list[CommandResult]:
    """Run quick static and provider-boundary checks."""

    results: list[CommandResult] = []
    results.append(run_command("compile", _python("-m", "compileall", "-q", "app", "timeline_engine", "media_gateway", "cloud_functions"), root=root, env=env))
    results.append(run_command("lint", _python("-m", "ruff", "check", "app", "timeline_engine", "media_gateway", "cloud_functions", "tests", "scripts"), root=root, env=env))
    if include_unit_tests:
        results.append(run_command("unit-tests", _python("-m", "pytest", "-q", *FAST_TESTS), root=root, env=env))
    if not skip_console:
        node = shutil.which("node")
        if not node:
            raise GateFailure("console check requires Node.js (use --skip-console only for Python-only work)")
        results.append(run_command("console-syntax", [node, "--check", "app.js"], root=root, env=env))
        feature_scripts = json.loads((root / "web" / "manifest.json").read_text(encoding="utf-8"))["scripts"]
        for script in ["auth.js", "share.js", *("web/" + name for name in feature_scripts)]:
            results.append(run_command("console-syntax-" + Path(script).stem, [node, "--check", script], root=root, env=env))
        results.append(run_command("console-recovery", [node, "tests/frontend_retry.cjs"], root=root, env=env))
        results.append(run_command("console-upload-recovery", [node, "tests/frontend_upload.cjs"], root=root, env=env))
        results.append(run_command("console-editor-recovery", [node, "tests/frontend_editor.cjs"], root=root, env=env))
        results.append(run_command("console-review-recovery", [node, "tests/frontend_reviews.cjs"], root=root, env=env))
        results.append(run_command("mini-review-recovery", [node, "tests/miniprogram_review.cjs"], root=root, env=env))
        results.append(run_command("console-delivery-recovery", [node, "tests/frontend_delivery.cjs"], root=root, env=env))
        results.append(run_command("console-billing-recovery", [node, "tests/frontend_billing.cjs"], root=root, env=env))
        results.append(run_command("console-preparation-recovery", [node, "tests/frontend_preparation.cjs"], root=root, env=env))
        results.append(run_command("console-batches", [node, "tests/frontend_batches.cjs"], root=root, env=env))
        results.append(run_command("console-batch-runs", [node, "tests/frontend_batch_runs.cjs"], root=root, env=env))
        results.append(run_command("console-batch-delivery", [node, "tests/frontend_batch_delivery.cjs"], root=root, env=env))
        results.append(run_command("console-storyboards", [node, "tests/frontend_storyboards.cjs"], root=root, env=env))
        results.append(run_command("console-shot-matching", [node, "tests/frontend_shot_matching.cjs"], root=root, env=env))
        results.append(run_command("console-ai-tools", [node, "tests/frontend_ai_tools.cjs"], root=root, env=env))
        results.append(run_command("mini-ai-tools", [node, "tests/miniprogram_ai_tools.cjs"], root=root, env=env))
    return results


def run_static_provider_acceptance(*, root: Path, env: dict[str, str]) -> None:
    """Run the shared network-free Provider admission gate on the fixture."""

    # Import after the script has inserted the repository root.  The module
    # only parses JSON/preflight data and never constructs an HTTP adapter.
    sys.path.insert(0, str(root))
    from app.services.provider_acceptance import provider_acceptance_file

    fixture = normalize_repo_path(r"config\providers.contract.fixture.json", root=root)
    report = provider_acceptance_file(
        fixture,
        require_secrets=False,
        provider_mode=env.get("PROVIDER_MODE", ""),
        asset_storage_mode=env.get("ASSET_STORAGE_MODE", ""),
    )
    if not report.ok:
        raise GateFailure("provider acceptance failed: " + "; ".join(check.code for check in report.errors))
    print(f"[gate] provider-acceptance: PASS ({fixture})", flush=True)


def run_static_provider_smoke(*, root: Path) -> None:
    """Validate the smoke contract in dry-run mode; never contact a gateway."""

    sys.path.insert(0, str(root))
    from app.services.provider_registry import ConfiguredProviderRegistry
    from app.services.provider_smoke import run_provider_smoke

    fixture = normalize_repo_path(r"config\provider-smoke.example.json", root=root)
    registry = ConfiguredProviderRegistry.from_file(fixture)
    result = run_provider_smoke(
        registry,
        provider_name="staging-video-gateway",
        capability="video_generation",
        request_id="quality-gate-dry-run",
        inputs={"asset_uri": "s3://quality-gate/input.mp4"},
        execute=False,
    )
    if not result.ok or result.executed:
        raise GateFailure("provider smoke dry-run did not pass without execution")
    print(f"[gate] provider-smoke dry-run: PASS ({fixture})", flush=True)


def run_integration(*, root: Path, env: dict[str, str]) -> list[CommandResult]:
    """Run the full network-free integration contract and static admissions."""

    results: list[CommandResult] = []
    run_static_provider_acceptance(root=root, env=env)
    run_static_provider_smoke(root=root)
    # The complete test suite is deliberately run in integration mode.  All
    # external Provider calls are in-process transports/fakes; no test opts in
    # to --execute or provider health probing.
    results.append(run_command("integration-tests", _python("-m", "pytest", "-q"), root=root, env=env))
    return results


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Video AI Studio deterministic quality gates")
    parser.add_argument("gate", choices=("fast", "integration", "all"), help="Gate to run")
    parser.add_argument(
        "--skip-console",
        action="store_true",
        help="Skip the Node syntax check (only for Python-only environments)",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    root = repository_root()
    with tempfile.TemporaryDirectory(prefix="video-ai-quality-gate-") as temporary:
        env = _base_environment(Path(temporary))
        try:
            if args.gate in {"fast", "all"}:
                run_fast(root=root, env=env, skip_console=args.skip_console, include_unit_tests=args.gate == "fast")
            if args.gate in {"integration", "all"}:
                run_integration(root=root, env=env)
        except GateFailure as exc:
            print(f"[gate] FAIL: {exc}", file=sys.stderr, flush=True)
            return 1
    print(f"[gate] {args.gate.upper()}: PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
