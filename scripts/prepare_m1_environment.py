"""Generate private acceptance secrets once, on the target host, without logs.

Never run this to replace an existing environment. Cloud invocation remains
disabled until the stage gate and resource configuration are ready.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "infra/m1.compose.yml").is_file():
        parser.error("Target is not a prepared project checkout")
    names = (".env.m1", ".env.m1.postgres", ".env.m1.edge", ".env.gateway")
    if any((root / name).exists() for name in names):
        parser.error("Environment already exists; preserve secrets, database and trial counters")
    deployment = root / "data/deployment"
    deployment.mkdir(parents=True, exist_ok=True)
    if (deployment / "identity.json").exists():
        parser.error("Acceptance identity already exists; refusing to replace it")
    password, proxy, gateway, attestation = (secrets.token_hex(32) for _ in range(4))
    tenant = str(uuid4())
    principal = "m1-ssh-operator"
    host = "video-ai-huayuan-cn-beijing.oss-cn-beijing.aliyuncs.com"
    write_private(root / ".env.m1", f"""APP_ENVIRONMENT=production
AUTH_MODE=trusted_proxy
PROVIDER_MODE=api_only
ASSET_STORAGE_MODE=external
DEBUG=false
DATABASE_URL=postgresql+psycopg://video_ai:{password}@127.0.0.1:5434/video_ai
STORAGE_ROOT=/app/data/storage
TRUSTED_PROXY_SHARED_SECRET={proxy}
STORAGE_ATTESTATION_SECRET={attestation}
STORAGE_GATEWAY_API_KEY={gateway}
MEDIA_GATEWAY_AUTHORIZATION=Bearer {gateway}
MEDIA_GATEWAY_CALLBACK_SECRET={secrets.token_hex(32)}
STORAGE_GATEWAY_URL=https://video-ai-gateway.internal:8100
STORAGE_GATEWAY_TIMEOUT_SECONDS=120
STORAGE_UPLOAD_HOSTS={host}
STORAGE_PUBLIC_HOSTS={host}
MAX_UPLOAD_BYTES=104857600
PROVIDER_REGISTRY_PATH=/run/video-ai/providers.json
PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID=true
WORKFLOW_WORKER_ID=aliyun-m1-worker
WORKFLOW_LEASE_SECONDS=900
RENDER_TIMEOUT_SECONDS=900
""")
    write_private(root / ".env.m1.postgres", f"POSTGRES_USER=video_ai\nPOSTGRES_DB=video_ai\nPOSTGRES_PASSWORD={password}\n")
    write_private(root / ".env.m1.edge", f"ACCEPTANCE_TENANT_ID={tenant}\nACCEPTANCE_PRINCIPAL_ID={principal}\nTRUSTED_PROXY_SHARED_SECRET={proxy}\n")
    write_private(root / ".env.gateway", f"""STORAGE_GATEWAY_API_KEY={gateway}
STORAGE_ATTESTATION_SECRET={attestation}
GATEWAY_DATABASE_URL=sqlite:///./data/gateway.db
GATEWAY_MAX_UPLOAD_BYTES=104857600
OSS_BUCKET=video-ai-huayuan-cn-beijing
OSS_REGION=cn-beijing
OSS_USE_INTERNAL_ENDPOINT=true
OSS_CREDENTIAL_MODE=ecs_ram_role
ECS_RAM_ROLE_NAME=VideoAIStudioStorageRole
GATEWAY_ICE_RENDER_ENABLED=false
GATEWAY_ICE_RENDER_MAX_OPERATIONS=6
GATEWAY_ICE_RENDER_MAX_DURATION_SECONDS=30
GATEWAY_ANALYSIS_ENABLED=false
GATEWAY_ANALYSIS_FC_ACCOUNT_ID=1253063092481585
GATEWAY_ANALYSIS_FC_FUNCTION_NAME=video-ai-shot-analysis
GATEWAY_ANALYSIS_MAX_OPERATIONS=6
GATEWAY_ANALYSIS_MAX_DURATION_SECONDS=30
GATEWAY_ANALYSIS_MAX_THUMBNAILS=30
""")
    write_private(deployment / "identity.json", json.dumps({"tenant_id": tenant, "principal_id": principal,
                  "created_at": datetime.now(timezone.utc).isoformat(), "scope": "private_m1_acceptance"}, indent=2) + "\n")
    print("Private M1 configuration created; secrets were not printed. Cloud execution is disabled.")


if __name__ == "__main__":
    main()
