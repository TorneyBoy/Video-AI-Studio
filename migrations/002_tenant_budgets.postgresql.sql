-- Video AI Studio 002: tenant hosted-provider budgets and charge ledger.
-- Run once against an existing PostgreSQL database before enabling budget APIs.

CREATE TABLE IF NOT EXISTS tenant_budgets (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    monthly_limit_micros BIGINT,
    task_limit_micros BIGINT,
    enforcement VARCHAR(20) NOT NULL DEFAULT 'block',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_tenant_budgets_tenant_id ON tenant_budgets(tenant_id);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id VARCHAR(36) NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    period_key VARCHAR(7) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'reserved',
    decision VARCHAR(40) NOT NULL DEFAULT 'unconfigured',
    source VARCHAR(40) NOT NULL,
    provider VARCHAR(120),
    model_id VARCHAR(160),
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    amount_micros BIGINT NOT NULL DEFAULT 0,
    usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_usage_ledger_tenant_id ON usage_ledger(tenant_id);
CREATE INDEX IF NOT EXISTS ix_usage_ledger_job_id ON usage_ledger(job_id);
CREATE INDEX IF NOT EXISTS ix_usage_ledger_period_key ON usage_ledger(period_key);
