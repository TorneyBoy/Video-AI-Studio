# Durable workflow runtime

`Job` remains the public asynchronous API. `WorkflowRun` adds a durable lease
and `WorkflowEvent` provides an append-only execution history. A run can be
claimed by one worker, heartbeated, recovered after a process crash, retried
with a persisted attempt budget, or cancelled without losing its audit trail.
`WorkflowStep` stores activity-level input, output, progress and lease state.

The local runtime only provides the durable control-plane poller. In the default
`PROVIDER_MODE=api_only`, media analysis and rendering are delegated to configured
HTTP Providers; the worker does not run inference or local media computation. Set
`PROVIDER_MODE=development_mock` only for contract tests. The poller itself needs
no external orchestration dependency:

```bash
python -m app.cli recover-workflows --limit 100
```

The HTTP control plane exposes the same operations under
`/api/v1/jobs/{job_id}/workflow` and `/api/v1/workflows/{workflow_id}/...`.
The built-in analysis, Provider and render workers create and claim a run before
execution, then complete or reschedule it in the same database transaction as
the Job update. `WORKFLOW_WORKER_ID` identifies the worker and
`WORKFLOW_LEASE_SECONDS` should cover the longest configured media operation;
longer custom activities should call the heartbeat endpoint periodically.
The built-in worker mirrors its media activity into `WorkflowStep`, keeping
step status, attempts and result metadata aligned with the parent run and the
legacy `JobStep`.

For Temporal deployments, install `temporalio`, create a Temporal client, and
pass it to `TemporalWorkflowAdapter`. The adapter uses the same workflow ID
and task queue contract; local development falls back to the database lease
worker when no client is configured.
