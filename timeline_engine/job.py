"""Small, persistence-friendly render job state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import Field

from .schema import StrictModel


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class JobEvent(StrictModel):
    state: JobState
    at: datetime
    message: Optional[str] = None


_TRANSITIONS = {
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELED},
    JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELED: set(),
}


class RenderJob(StrictModel):
    id: str = Field(min_length=1)
    manifest_id: str = Field(min_length=1)
    state: JobState = JobState.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    progress: float = Field(default=0, ge=0, le=1)
    error: Optional[str] = None
    output_uri: Optional[str] = None
    history: List[JobEvent] = Field(default_factory=list)

    def transition(self, state: JobState, *, message: Optional[str] = None, at: Optional[datetime] = None) -> "RenderJob":
        state = JobState(state)
        if state not in _TRANSITIONS[self.state]:
            raise ValueError(f"invalid job transition: {self.state.value} -> {state.value}")
        now = at or datetime.now(timezone.utc)
        self.state = state
        self.updated_at = now
        self.history.append(JobEvent(state=state, at=now, message=message))
        if state == JobState.SUCCEEDED:
            self.progress = 1.0
            self.error = None
        elif state == JobState.FAILED:
            self.error = message or self.error or "render failed"
        elif state == JobState.RUNNING:
            self.progress = max(0.0, self.progress)
        return self

    def update_progress(self, progress: float) -> "RenderJob":
        if self.state != JobState.RUNNING:
            raise ValueError("progress can only be updated for running jobs")
        if progress < self.progress or progress > 1:
            raise ValueError("progress must be monotonic and within [0, 1]")
        self.progress = progress
        self.updated_at = datetime.now(timezone.utc)
        return self

    def succeed(self, output_uri: Optional[str] = None) -> "RenderJob":
        self.output_uri = output_uri
        return self.transition(JobState.SUCCEEDED)

    def fail(self, message: str) -> "RenderJob":
        return self.transition(JobState.FAILED, message=message)

    @property
    def terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED)

