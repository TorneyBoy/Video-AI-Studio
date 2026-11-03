from functools import lru_cache
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator


ProviderMode = Literal["api_only", "development_mock"]
AssetStorageMode = Literal["external", "hybrid", "local"]
AuthMode = Literal["development_header", "trusted_proxy", "session"]


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    app_environment: Literal["development", "test", "production"] = "development"
    internal_api_secret: str | None = None
    metrics_api_secret: SecretStr | None = None
    app_name: str = "Video AI Studio"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/dev.db"
    storage_root: str = "./data/storage"
    max_upload_bytes: int = 5 * 1024 * 1024 * 1024
    # ``external`` is the production contract: the API only creates upload
    # intents and records object URIs. ``local`` remains available for
    # development fixtures; ``hybrid`` lets an operator expose both routes
    # while migrating existing clients.
    asset_storage_mode: AssetStorageMode = "external"
    asset_upload_session_ttl_seconds: int = 3600
    # Optional HMAC shared with the external storage gateway. When configured,
    # remote-upload-complete requires a short-lived, asset-bound attestation.
    storage_attestation_secret: str | None = None
    storage_gateway_url: str | None = None
    storage_gateway_api_key: str | None = None
    storage_gateway_timeout_seconds: int = 30
    storage_upload_hosts: tuple[str, ...] = ()
    storage_public_hosts: tuple[str, ...] = ()
    render_timeout_seconds: int = 2 * 60 * 60
    workflow_worker_id: str = "local-worker"
    workflow_lease_seconds: int = 2 * 60 * 60
    dispatch_global_limit: int = 8
    dispatch_tenant_limit: int = 4
    dispatch_project_limit: int = 3
    dispatch_batch_limit: int = 2
    delivery_package_enabled: bool = False
    delivery_package_max_bytes: int = 100 * 1024 * 1024
    observation_queue_warning_seconds: int = 300
    observation_remote_warning_seconds: int = 900
    observation_billing_warning_seconds: int = 3600
    observation_worker_stale_seconds: int = 120
    provider_registry_path: str | None = None
    provider_registry_json: str | None = None
    # AI capabilities are remote API calls by default.  The mock mode is an
    # explicit development escape hatch for contract tests and local demos;
    # it emits manifests and never claims to have generated media.
    provider_mode: ProviderMode = "api_only"
    # ``development_header`` preserves the local X-Tenant-ID contract. In
    # production, ``trusted_proxy`` accepts only signed identity claims from
    # an authenticated edge proxy and never trusts a client tenant header.
    auth_mode: AuthMode = "development_header"
    trusted_proxy_shared_secret: str | None = None
    trusted_proxy_max_age_seconds: int = 300
    auth_public_origin: str | None = None
    artifact_share_secret: SecretStr | None = None
    auth_registration_enabled: bool = False
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_seconds: int = 7 * 24 * 60 * 60
    # Async commercial providers call the control plane after they finish.
    # Empty means callback ingestion is disabled until configured.
    provider_webhook_secret: str | None = None
    provider_webhook_max_age_seconds: int = 300
    # Bind every signed completion event to the Job path. Keep the legacy
    # optional mode available for migrations, but production should enable it
    # so a valid webhook cannot be replayed onto another queued Job.
    provider_webhook_require_request_id: bool = False
    debug: bool = False

    @field_validator("artifact_share_secret")
    @classmethod
    def validate_share_secret(cls, value):
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("ARTIFACT_SHARE_SECRET must contain at least 32 characters")
        return value

    @field_validator("dispatch_global_limit", "dispatch_tenant_limit", "dispatch_project_limit", "dispatch_batch_limit")
    @classmethod
    def validate_dispatch_limit(cls, value: int) -> int:
        if not 1 <= value <= 1024:
            raise ValueError("Dispatch limits must be between 1 and 1024")
        return value

    @field_validator('delivery_package_max_bytes')
    @classmethod
    def validate_package_bytes(cls,value):
        if not 1<=value<=1024**3:raise ValueError('DELIVERY_PACKAGE_MAX_BYTES must be between 1 and 1GiB')
        return value

    @field_validator('observation_queue_warning_seconds','observation_remote_warning_seconds','observation_billing_warning_seconds','observation_worker_stale_seconds')
    @classmethod
    def validate_observation_threshold(cls,value):
        if not 90<=value<=7*24*3600:raise ValueError('Observation thresholds must be between 90 seconds and 7 days')
        return value

    @field_validator("provider_mode", mode="before")
    @classmethod
    def validate_provider_mode(cls, value: object) -> str:
        normalized = str(value or "api_only").strip().lower()
        if normalized not in {"api_only", "development_mock"}:
            raise ValueError("PROVIDER_MODE must be api_only or development_mock")
        return normalized

    @field_validator("auth_mode", mode="before")
    @classmethod
    def validate_auth_mode(cls, value: object) -> str:
        normalized = str(value or "development_header").strip().lower()
        if normalized not in {"development_header", "trusted_proxy", "session"}:
            raise ValueError("AUTH_MODE must be development_header, trusted_proxy or session")
        return normalized

    @model_validator(mode="after")
    def validate_trusted_proxy(self) -> "Settings":
        if self.trusted_proxy_max_age_seconds <= 0:
            raise ValueError("TRUSTED_PROXY_MAX_AGE_SECONDS must be greater than zero")
        if self.auth_mode == "trusted_proxy" and not (self.trusted_proxy_shared_secret or "").strip():
            raise ValueError("TRUSTED_PROXY_SHARED_SECRET is required when AUTH_MODE=trusted_proxy")
        if not 60 <= self.auth_access_ttl_seconds <= 3600 or not self.auth_access_ttl_seconds < self.auth_refresh_ttl_seconds <= 30 * 86400:
            raise ValueError("Session access/refresh lifetimes exceed the supported bounds")
        if self.auth_mode == "session":
            from urllib.parse import urlsplit
            origin = urlsplit(self.auth_public_origin or "")
            if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password
                    or origin.path or origin.query or origin.fragment or origin.netloc.endswith(":")):
                raise ValueError("AUTH_PUBLIC_ORIGIN must be one explicit origin without a path")
            if self.app_environment == "production" and origin.scheme != "https":
                raise ValueError("Production session cookies require an HTTPS public origin")
        if self.app_environment == "production":
            if self.auth_mode not in {"trusted_proxy", "session"} or self.provider_mode != "api_only" or self.asset_storage_mode != "external":
                raise ValueError("production requires trusted_proxy or session authentication, api_only providers and external storage")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("production requires PostgreSQL")
            if not (self.storage_attestation_secret or "").strip():
                raise ValueError("STORAGE_ATTESTATION_SECRET is required in production")
            if not self.provider_webhook_require_request_id:
                raise ValueError("PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID must be true in production")
            if self.provider_webhook_secret and self.provider_webhook_max_age_seconds <= 0:
                raise ValueError("production callback timestamps must have a positive expiry")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            metrics_api_secret=os.getenv('METRICS_API_SECRET') or None,
            observation_queue_warning_seconds=int(os.getenv('OBSERVATION_QUEUE_WARNING_SECONDS','300')),
            observation_remote_warning_seconds=int(os.getenv('OBSERVATION_REMOTE_WARNING_SECONDS','900')),
            observation_billing_warning_seconds=int(os.getenv('OBSERVATION_BILLING_WARNING_SECONDS','3600')),
            observation_worker_stale_seconds=int(os.getenv('OBSERVATION_WORKER_STALE_SECONDS','120')),
            app_environment=os.getenv("APP_ENVIRONMENT", "development"),
            internal_api_secret=os.getenv("INTERNAL_API_SECRET") or None,
            app_name=os.getenv("APP_NAME", cls.model_fields["app_name"].default),
            api_prefix=os.getenv("API_PREFIX", cls.model_fields["api_prefix"].default),
            database_url=os.getenv("DATABASE_URL", cls.model_fields["database_url"].default),
            storage_root=os.getenv("STORAGE_ROOT", cls.model_fields["storage_root"].default),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(cls.model_fields["max_upload_bytes"].default))),
            asset_storage_mode=os.getenv("ASSET_STORAGE_MODE", "external"),
            asset_upload_session_ttl_seconds=int(
                os.getenv("ASSET_UPLOAD_SESSION_TTL_SECONDS", str(cls.model_fields["asset_upload_session_ttl_seconds"].default))
            ),
            storage_attestation_secret=os.getenv("STORAGE_ATTESTATION_SECRET") or None,
            storage_gateway_url=os.getenv("STORAGE_GATEWAY_URL") or None,
            storage_gateway_api_key=os.getenv("STORAGE_GATEWAY_API_KEY") or None,
            storage_gateway_timeout_seconds=int(os.getenv("STORAGE_GATEWAY_TIMEOUT_SECONDS", "30")),
            storage_upload_hosts=tuple(item.strip().lower() for item in os.getenv("STORAGE_UPLOAD_HOSTS", "").split(",") if item.strip()),
            storage_public_hosts=tuple(item.strip().lower() for item in os.getenv("STORAGE_PUBLIC_HOSTS", "").split(",") if item.strip()),
            render_timeout_seconds=int(os.getenv("RENDER_TIMEOUT_SECONDS", str(cls.model_fields["render_timeout_seconds"].default))),
            workflow_worker_id=os.getenv("WORKFLOW_WORKER_ID", cls.model_fields["workflow_worker_id"].default),
            workflow_lease_seconds=int(os.getenv("WORKFLOW_LEASE_SECONDS", str(cls.model_fields["workflow_lease_seconds"].default))),
            dispatch_global_limit=int(os.getenv("DISPATCH_GLOBAL_LIMIT", "8")),
            dispatch_tenant_limit=int(os.getenv("DISPATCH_TENANT_LIMIT", "4")),
            dispatch_project_limit=int(os.getenv("DISPATCH_PROJECT_LIMIT", "3")),
            dispatch_batch_limit=int(os.getenv("DISPATCH_BATCH_LIMIT", "2")),
            delivery_package_enabled=os.getenv("DELIVERY_PACKAGE_ENABLED", "false").lower() == "true",
            delivery_package_max_bytes=int(os.getenv("DELIVERY_PACKAGE_MAX_BYTES", "104857600")),
            provider_registry_path=os.getenv("PROVIDER_REGISTRY_PATH") or None,
            provider_registry_json=os.getenv("PROVIDER_REGISTRY_JSON") or None,
            provider_mode=os.getenv("PROVIDER_MODE", "api_only"),
            auth_mode=os.getenv("AUTH_MODE", "development_header"),
            trusted_proxy_shared_secret=os.getenv("TRUSTED_PROXY_SHARED_SECRET") or None,
            auth_public_origin=os.getenv("AUTH_PUBLIC_ORIGIN") or None,
            artifact_share_secret=os.getenv("ARTIFACT_SHARE_SECRET") or None,
            auth_registration_enabled=os.getenv("AUTH_REGISTRATION_ENABLED", "false").lower() == "true",
            auth_access_ttl_seconds=int(os.getenv("AUTH_ACCESS_TTL_SECONDS", "900")),
            auth_refresh_ttl_seconds=int(os.getenv("AUTH_REFRESH_TTL_SECONDS", "604800")),
            trusted_proxy_max_age_seconds=int(
                os.getenv("TRUSTED_PROXY_MAX_AGE_SECONDS", str(cls.model_fields["trusted_proxy_max_age_seconds"].default))
            ),
            provider_webhook_secret=os.getenv("PROVIDER_WEBHOOK_SECRET") or None,
            provider_webhook_max_age_seconds=int(os.getenv("PROVIDER_WEBHOOK_MAX_AGE_SECONDS", str(cls.model_fields["provider_webhook_max_age_seconds"].default))),
            provider_webhook_require_request_id=os.getenv("PROVIDER_WEBHOOK_REQUIRE_REQUEST_ID", "false").lower() in {"1", "true", "yes", "on"},
            debug=os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
