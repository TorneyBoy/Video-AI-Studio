"""Safe Provider health reports for operator checks and telemetry.

The report is intentionally separate from job execution.  Configuration-only
reports are network-free by default; endpoint probes require an explicit
operator flag and never include request bodies or secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .provider_registry import ConfiguredProviderRegistry
from .providers import ProviderHealth


@dataclass(frozen=True)
class ProviderHealthReport:
    """Machine-readable low-cardinality Provider availability report."""

    source: str
    checks: tuple[ProviderHealth, ...]
    probed: bool
    generated_at: datetime

    @property
    def ok(self) -> bool:
        # Disabled providers are an intentional operator choice. A configured
        # provider that was probed and failed, however, must fail the command.
        return all(
            check.available or check.status == "disabled"
            for check in self.checks
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "probed": self.probed,
            "ok": self.ok,
            "generated_at": self.generated_at.isoformat(),
            "providers": [check.as_dict() for check in self.checks],
        }

    def render_text(self) -> str:
        lines = [
            f"Provider health: {self.source}",
            f"Probe mode: {'network/executable' if self.probed else 'configuration-only (network-free)'}",
        ]
        for check in self.checks:
            detail = f" reason={check.reason}" if check.reason else ""
            latency = f" latency_ms={check.latency_ms:g}" if check.latency_ms is not None else ""
            lines.append(
                f"[{('READY' if check.available else check.status.upper())}] "
                f"provider={check.provider} status={check.status}{detail}{latency}"
            )
        lines.append(f"Result: {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


def provider_health_report(
    registry: ConfiguredProviderRegistry,
    *,
    source: str = "configured",
    probe: bool = False,
) -> ProviderHealthReport:
    """Build a report without dispatching any Provider job.

    ``probe=False`` is deliberately the default. It only validates that the
    mounted registry loaded and that the adapter is enabled; it does not call
    a third-party endpoint or execute a local command.
    """

    return ProviderHealthReport(
        source=source,
        checks=tuple(registry.health(probe=probe)),
        probed=probe,
        generated_at=datetime.now(timezone.utc),
    )
