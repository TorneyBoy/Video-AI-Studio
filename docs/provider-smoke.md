# Provider Gateway staging smoke check

`app.services.provider_smoke` provides a small acceptance check for a hosted
Provider Gateway. It checks the mounted provider registry, capability, model
identity, commercial model policy, stable request ID, and remote object
references. It does not run a local model or FFmpeg process.

The default is a **dry run**. A dry run makes no network request and returns
`status=pass` only when the configured contract is valid. A caller must pass
`execute=True` (and, in the CLI, an explicit `--execute`) to contact the
configured HTTP gateway. Use a staging endpoint and a provider operation that
the operator has confirmed is non-billable. The generic gateway contract does
not know how to map `video_generation` to a particular vendor API, and this
repository does not provide vendor credentials, endpoint mappings, or a
quality oracle.

```python
import json
from app.services.provider_registry import ConfiguredProviderRegistry
from app.services.provider_smoke import run_provider_smoke

registry = ConfiguredProviderRegistry.from_file("config/provider-smoke.example.json")
result = run_provider_smoke(
    registry,
    provider_name="staging-video-gateway",
    capability="video_generation",
    request_id="staging-smoke-20261014-001",
    inputs={"asset_uri": "s3://staging-inputs/fixture.mp4"},
    execute=False,  # safe default; no provider request is made
)
print(json.dumps(result.as_dict(), indent=2))
```

The same check is available from the maintenance CLI. The command is dry-run
unless `--execute` is supplied:

```bash
python -m app.cli provider-smoke \
  --path config/provider-smoke.example.json \
  --provider staging-video-gateway \
  --capability video_generation \
  --request-id staging-smoke-20261014-001 \
  --inputs-json '{"asset_uri":"s3://staging-inputs/fixture.mp4"}' \
  --json
```

Add `--execute` only for an operator-approved staging request. A pending
result can be resumed with `--provider-job-id` and the same `--request-id`.

For an explicitly approved staging execution, set `execute=True`. Preserve the
same `request_id` when retrying. If the gateway returns an asynchronous task,
pass the returned `provider_job_id` to resume bounded polling; the adapter
polls the existing task instead of submitting another POST.

Results use three states:

* `pass`: registry/contract validation passed, or an executed provider response
  contained a valid remote artifact URI.
* `pending`: the provider accepted the request but the bounded poll window
  ended; the result includes `provider_job_id` when available.
* `fail`: validation, policy, transport, or provider response failed.

`pass` is a contract result only. Every result sets
`quality_evidence=not_evaluated` and `quality_claim=false`; it never asserts
visual quality, face identity, voice similarity, or generated-media fidelity.

Inputs must be remote references (`https://`, `s3://`, `gs://`, `oss://`, or
Azure object URI). Local paths and `file://` URLs are rejected before an
HTTP request. The example endpoint is a placeholder and intentionally cannot
be used as a production provider until an operator supplies the real staging
mapping and secret environment variables.
