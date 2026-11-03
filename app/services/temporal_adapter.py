"""Optional Temporal compatibility layer.

The application does not require ``temporalio`` for local development.  When
the package is installed, ``TemporalWorkflowAdapter`` can start the same
workflow contract on a Temporal client; otherwise callers can inspect
``available`` and continue using the durable local lease worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Activity = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class TemporalWorkflowSpec:
    """Stable names shared by local WorkflowRun and Temporal executions."""

    workflow_type: str
    task_queue: str = "video-ai"


class TemporalUnavailable(RuntimeError):
    pass


class TemporalWorkflowAdapter:
    """Thin adapter that avoids coupling control-plane code to Temporal SDK."""

    def __init__(self, client: Any | None = None, *, task_queue: str = "video-ai"):
        self.client = client
        self.task_queue = task_queue

    @property
    def available(self) -> bool:
        return self.client is not None

    async def start(
        self,
        spec: TemporalWorkflowSpec,
        workflow: Any,
        *,
        workflow_id: str,
        args: list[Any] | None = None,
    ) -> Any:
        if self.client is None:
            raise TemporalUnavailable("temporalio is not configured; use the local durable worker")
        return await self.client.start_workflow(
            workflow,
            *(args or []),
            id=workflow_id,
            task_queue=spec.task_queue or self.task_queue,
        )


def temporal_workflow(name: str) -> Callable[[Any], Any]:
    """Mark a workflow function with a stable logical name.

    Temporal's decorators can be applied by the optional runtime integration;
    local imports remain dependency-free and deterministic.
    """

    def decorate(fn: Any) -> Any:
        setattr(fn, "__video_ai_workflow_name__", name)
        return fn

    return decorate


def temporal_activity(name: str) -> Callable[[Activity], Activity]:
    def decorate(fn: Activity) -> Activity:
        setattr(fn, "__video_ai_activity_name__", name)
        return fn

    return decorate

