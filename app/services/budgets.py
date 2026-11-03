"""Tenant budget enforcement and provider usage reconciliation.

This module only accounts for hosted provider charges. It never downloads
media or performs inference. Provider responses are treated as untrusted
billing envelopes and are converted to integer micro-units before any limit
comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .providers import ProviderExecutionError


MICROS_PER_UNIT = 1_000_000
_RESERVATION_STATUSES = ("reserved", "pending_reconciliation")
_ACTIVE_LEDGER_STATUSES = (*_RESERVATION_STATUSES, "settled")


class BudgetDataError(ProviderExecutionError):
    """Provider returned a cost envelope that cannot be safely accounted for."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="budget_cost_invalid",
            retryable=False,
            details=details or {},
        )


class BudgetExceededError(ProviderExecutionError):
    """Provider work completed after a configured budget was exceeded."""

    def __init__(self, decision: "BudgetDecision") -> None:
        unknown = decision.decision == "amount_unknown"
        super().__init__(
            "Provider cost requires reconciliation" if unknown else "Provider result exceeds the tenant budget",
            code="budget_cost_unknown" if unknown else "budget_exceeded",
            retryable=False,
            details=decision.audit,
        )
        self.decision = decision


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    decision: str
    amount_micros: int
    currency: str
    audit: dict[str, Any]


def period_key(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m")


def _amount_micros(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BudgetDataError("Provider cost amount is not numeric", details={"value_type": type(value).__name__}) from exc
    if not amount.is_finite() or amount < 0:
        raise BudgetDataError("Provider cost amount must be finite and non-negative")
    return int((amount * MICROS_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalize_cost(cost: dict[str, Any] | None, *, default_currency: str = "USD") -> tuple[int, str]:
    """Extract ``amount`` and ``currency`` from a provider cost envelope."""

    value = cost if isinstance(cost, dict) else {}
    amount_value: Any = None
    for key in ("amount", "total", "total_amount", "price", "value"):
        if key in value and value[key] is not None:
            amount_value = value[key]
            break
    if amount_value is None:
        return 0, str(value.get("currency") or default_currency).upper()
    amount = _amount_micros(amount_value)
    currency = str(value.get("currency") or default_currency).strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise BudgetDataError("Provider cost currency must be a three-letter code")
    return amount, currency


def extract_estimated_cost(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Find an optional client/provider estimate without changing API payloads."""

    data = payload if isinstance(payload, dict) else {}
    candidates: list[Any] = [data.get("estimated_cost"), data.get("cost")]
    request = data.get("provider_request")
    if isinstance(request, dict):
        candidates.extend([request.get("estimated_cost"), request.get("cost")])
        options = request.get("options")
        if isinstance(options, dict):
            candidates.extend([options.get("estimated_cost"), options.get("cost")])
    options = data.get("options")
    if isinstance(options, dict):
        candidates.extend([options.get("estimated_cost"), options.get("cost")])
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, (int, float, str)):
            return {"amount": candidate}
    return None


def get_budget(db: Session, tenant_id: str, *, for_update: bool = False) -> models.TenantBudget | None:
    stmt = select(models.TenantBudget).where(models.TenantBudget.tenant_id == tenant_id)
    # PostgreSQL serializes reservations per tenant. SQLite ignores
    # ``FOR UPDATE`` and serializes the eventual write transaction instead.
    if for_update:
        # Serialize even when the tenant has no budget row yet. Lock order is
        # Job -> Workflow -> Tenant -> TenantBudget, shared by all completions.
        # New Job inserts hold KEY SHARE for their tenant foreign key. A
        # FOR UPDATE upgrade deadlocks two concurrent submitters. NO KEY
        # UPDATE still serializes budget writers and permits those FK locks.
        db.execute(select(models.Tenant.id).where(models.Tenant.id == tenant_id).with_for_update(key_share=True))
        stmt = stmt.with_for_update()
    return db.scalar(stmt.execution_options(populate_existing=True))


def upsert_budget(
    db: Session,
    tenant_id: str,
    *,
    currency: str,
    monthly_limit_micros: int | None,
    task_limit_micros: int | None,
    enforcement: str = "block",
    enabled: bool = True,
) -> models.TenantBudget:
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a three-letter code")
    if monthly_limit_micros is not None and monthly_limit_micros < 0:
        raise ValueError("monthly_limit_micros must be non-negative")
    if task_limit_micros is not None and task_limit_micros < 0:
        raise ValueError("task_limit_micros must be non-negative")
    if enforcement != "block":
        raise ValueError("unsupported budget enforcement mode")
    row = get_budget(db, tenant_id, for_update=True)
    if row is None:
        row = models.TenantBudget(tenant_id=tenant_id)
        db.add(row)
    row.currency = currency
    row.monthly_limit_micros = monthly_limit_micros
    row.task_limit_micros = task_limit_micros
    row.enforcement = enforcement
    row.enabled = enabled
    db.flush()
    return row


def _sum_ledger(
    db: Session,
    *,
    tenant_id: str,
    period: str,
    statuses: tuple[str, ...] = _ACTIVE_LEDGER_STATUSES,
    exclude_job_id: str | None = None,
) -> int:
    currency = db.scalar(select(models.TenantBudget.currency).where(models.TenantBudget.tenant_id == tenant_id)) or "USD"
    stmt = select(func.coalesce(func.sum(models.UsageLedger.amount_micros), 0)).where(
        models.UsageLedger.tenant_id == tenant_id,
        models.UsageLedger.period_key == period,
        models.UsageLedger.status.in_(statuses),
        models.UsageLedger.currency == currency,
    )
    if exclude_job_id:
        stmt = stmt.where(models.UsageLedger.job_id != exclude_job_id)
    total = int(db.scalar(stmt) or 0)
    if "settled" in statuses:
        adjustments = select(func.coalesce(func.sum(models.UsageAdjustment.delta_micros), 0)).where(
            models.UsageAdjustment.tenant_id == tenant_id,
            models.UsageAdjustment.period_key == period,
            models.UsageAdjustment.currency == currency,
        )
        if exclude_job_id:
            adjustments = adjustments.where(models.UsageAdjustment.job_id != exclude_job_id)
        total += int(db.scalar(adjustments) or 0)
    return total


def _decision(
    *,
    budget: models.TenantBudget | None,
    amount_micros: int,
    currency: str,
    monthly_used_micros: int,
    reserved_micros: int,
    period: str,
    job_id: str,
    reasons: list[str] | None = None,
    decision: str | None = None,
    db: Session | None = None,
    job: models.Job | None = None,
) -> BudgetDecision:
    reasons = list(reasons or [])
    monthly_limit = budget.monthly_limit_micros if budget and budget.enabled else None
    task_limit = budget.task_limit_micros if budget and budget.enabled else None
    if budget and budget.enabled and currency != budget.currency:
        reasons.append("currency_mismatch")
    if monthly_limit is not None and monthly_used_micros + reserved_micros + amount_micros > monthly_limit:
        reasons.append("monthly_limit")
    if task_limit is not None and amount_micros > task_limit:
        reasons.append("task_limit")
    project = None
    if db is not None and job is not None:
        from .project_spending import project_check
        project_reasons, project = project_check(db, job, amount_micros, currency)
        reasons.extend(project_reasons)
    allowed = not reasons
    if decision is None:
        decision = "allowed" if allowed and budget and budget.enabled else "unconfigured" if allowed else "blocked"
    remaining = None if monthly_limit is None else max(0, monthly_limit - monthly_used_micros - reserved_micros - amount_micros)
    audit = {
        "decision": decision,
        "allowed": allowed,
        "job_id": job_id,
        "period_key": period,
        "amount_micros": amount_micros,
        "currency": currency,
        "monthly_used_micros": monthly_used_micros,
        "reserved_micros": reserved_micros,
        "monthly_limit_micros": monthly_limit,
        "task_limit_micros": task_limit,
        "remaining_micros": remaining,
        "reasons": reasons,
        **({"project_budget": project} if project is not None else {}),
    }
    return BudgetDecision(allowed=allowed, decision=decision, amount_micros=amount_micros, currency=currency, audit=audit)


def capability_reservation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Calculate a conservative envelope from an operator-owned catalogue.

    Callers must supply a server-selected capability, never a client payload.
    Time-priced capabilities reserve their enforced maximum duration, rather
    than trusting a client's shorter duration or a cost claim.
    """
    price = snapshot.get("price") or {}
    limits = snapshot.get("limits") or {}
    if not snapshot.get("available") or not price.get("version"):
        raise BudgetDataError("A verified, versioned capability price is required")
    minute_rate, request_rate = price.get("per_minute"), price.get("per_request")
    bound = limits.get("max_output_duration_seconds") or limits.get("max_duration_seconds")
    timed = minute_rate is not None or price.get("billing_unit") == "started_minute"
    if timed and (isinstance(bound, bool) or not isinstance(bound, (int, float))
                  or not math.isfinite(bound) or bound <= 0):
        raise BudgetDataError("Time-priced capability requires an enforced duration limit")
    # Validate rates before Decimal arithmetic; this rejects NaN and negatives.
    for rate in (minute_rate, request_rate):
        if rate is not None:
            _amount_micros(rate)
    if minute_rate is None and request_rate is None:
        raise BudgetDataError("Capability has no reservation price")
    if price.get("billing_unit") == "started_minute":
        rate = minute_rate if minute_rate is not None else request_rate
        amount = Decimal(str(rate)) * math.ceil(bound / 60)
    else:
        amount = Decimal(str(request_rate or 0))
        if minute_rate is not None:
            amount += Decimal(str(minute_rate)) * Decimal(str(bound)) / 60
    amount_micros, currency = normalize_cost({"amount": str(amount), "currency": price.get("currency")})
    return {"amount": str(Decimal(amount_micros) / MICROS_PER_UNIT), "currency": currency,
            "price_version": price["version"], "provider": snapshot["provider"],
            "model_id": snapshot["model_id"], "model_version": snapshot["model_version"],
            "capability_id": snapshot["id"], "duration_bound_seconds": bound if timed else None,
            "basis": "server_capability_catalog", "bill_confirmed": False}


def preflight_job_budget(db: Session, job: models.Job, *,
                         selected_capability: dict[str, Any] | None = None,
                         recovery_reason: str | None = None) -> BudgetDecision | None:
    """Reserve a declared estimate and mark a queued Job before dispatch.

    No reservation is created when the caller/provider has no estimate. The
    actual provider response remains authoritative and is checked by
    :func:`settle_provider_cost`.
    """

    estimate = (capability_reservation(selected_capability) if selected_capability is not None
                else extract_estimated_cost(job.payload))
    if estimate is None:
        return None
    budget = get_budget(db, job.tenant_id, for_update=True)
    default_currency = budget.currency if budget else "USD"
    amount_micros, currency = normalize_cost(estimate, default_currency=default_currency)
    existing = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
    recovery = bool(recovery_reason and recovery_reason.strip() and selected_capability is not None
                    and existing is not None and existing.status == "pending_reconciliation"
                    and existing.amount_micros == 0 and not existing.metadata_json.get("reservation"))
    if existing is not None and not recovery:
        return BudgetDecision(allowed=bool(existing.metadata_json.get("allowed", False)),
                              decision=existing.decision, amount_micros=existing.amount_micros,
                              currency=existing.currency, audit=dict(existing.metadata_json))
    if amount_micros == 0 and not budget and (not job.project_id or db.get(models.ProjectBudget, job.project_id) is None):
        return None
    current_period = period_key()
    settled = _sum_ledger(db, tenant_id=job.tenant_id, period=current_period, statuses=("settled",), exclude_job_id=job.id)
    reserved = _sum_ledger(db, tenant_id=job.tenant_id, period=current_period, statuses=_RESERVATION_STATUSES, exclude_job_id=job.id)
    decision = _decision(
        budget=budget,
        amount_micros=amount_micros,
        currency=currency,
        monthly_used_micros=settled,
        reserved_micros=reserved,
        period=current_period,
        job_id=job.id, db=db, job=job,
    )
    audit = dict(decision.audit)
    if selected_capability is not None:
        audit["reservation"] = {**estimate, "amount_micros": amount_micros}
    if recovery:
        audit["reservation_recovery"] = {"reason": recovery_reason.strip(),
                                         "previous_audit": existing.metadata_json}
    ledger = existing if recovery else models.UsageLedger(
        tenant_id=job.tenant_id,
        job_id=job.id,
    )
    for name, value in dict(
        period_key=current_period,
        status="reserved" if decision.allowed else "blocked",
        decision="reserved" if decision.allowed else "blocked",
        source="capability_catalog_estimate" if selected_capability is not None else "submission_estimate",
        provider=estimate.get("provider"), model_id=estimate.get("model_id"),
        currency=currency,
        amount_micros=amount_micros,
        metadata_json=audit,
    ).items():
        setattr(ledger, name, value)
    if not recovery:
        ledger.usage_json, ledger.cost_json = {}, dict(estimate)
    db.add(ledger)
    job.payload = {**(job.payload or {}), "budget": audit,
                   **({"estimated_cost": estimate} if selected_capability is not None else {})}
    if not decision.allowed:
        job.status = "failed"
        job.policy_blocked = True
        job.failure_code = "budget_exceeded"
        job.error = "tenant budget exceeded at submission"
    return BudgetDecision(allowed=decision.allowed, decision=decision.decision,
                          amount_micros=amount_micros, currency=currency, audit=audit)


def record_usage_evidence(
    db: Session, *, job: models.Job, cost: dict | None, usage: dict | None,
    provider: str | None, source: str, disposition: str, default_currency: str = "USD",
) -> models.UsageEvidence:
    """Append an observation, deduplicating repeated channel deliveries."""
    value = dict(cost or {})
    known = any(value.get(key) is not None for key in ("amount", "total", "total_amount", "price", "value"))
    try:
        amount, currency = normalize_cost(value, default_currency=default_currency) if known else (None, None)
    except BudgetDataError:
        amount, currency = None, None
        disposition = "invalid_amount"
    digest = hashlib.sha256(json.dumps(
        {"provider": provider, "cost": value, "usage": usage or {}},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    previous = db.scalar(select(models.UsageEvidence).where(
        models.UsageEvidence.job_id == job.id, models.UsageEvidence.evidence_hash == digest,
    ))
    if previous is not None:
        return previous
    row = models.UsageEvidence(
        tenant_id=job.tenant_id, job_id=job.id, evidence_hash=digest,
        provider=provider, source=source, amount_micros=amount, currency=currency,
        disposition=disposition, cost_json=value, usage_json=dict(usage or {}),
    )
    db.add(row)
    db.flush()
    return row


def validate_dispatch_budget(db: Session, job: models.Job) -> None:
    """Recheck an existing hold before the first billable submission.

    A previous item's actual charge or an operator policy change may have
    invalidated a previously affordable batch. Known remote polls bypass this
    check: stopping reconciliation cannot undo work already submitted.
    """
    budget=get_budget(db,job.tenant_id,for_update=True)
    ledger=db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id==job.id))
    if ledger is None:return  # Legacy, unpriced internal jobs retain their contract.
    if ledger.status!='reserved':
        raise ProviderExecutionError('original reservation is not available for new work',code='budget_reservation_unavailable',retryable=False)
    current=period_key()
    decision=_decision(budget=budget,amount_micros=ledger.amount_micros,currency=ledger.currency,
        monthly_used_micros=_sum_ledger(db,tenant_id=job.tenant_id,period=current,statuses=('settled',),exclude_job_id=job.id),
        reserved_micros=_sum_ledger(db,tenant_id=job.tenant_id,period=current,statuses=_RESERVATION_STATUSES,exclude_job_id=job.id),
        period=current,job_id=job.id,db=db,job=job)
    if not decision.allowed:
        raise ProviderExecutionError('current budget no longer covers this queued submission',code='budget_dispatch_exceeded',retryable=False,details=decision.audit)
    if ledger.period_key!=current:
        record_usage_evidence(db,job=job,cost={},usage={'from_period':ledger.period_key,'to_period':current,
            'reserved_micros':ledger.amount_micros,'currency':ledger.currency},provider=ledger.provider,
            source='dispatch_period_rollover',disposition='reservation_rollover')
        ledger.metadata_json={**ledger.metadata_json,'dispatch_period_rollover':{'from':ledger.period_key,'to':current}}
        ledger.period_key=current
    db.flush()


def release_job_reservation(db: Session, job: models.Job) -> None:
    """Release only unsubmitted work; an uncertain remote charge stays held."""
    get_budget(db, job.tenant_id, for_update=True)
    ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
    if ledger is None or ledger.status not in _RESERVATION_STATUSES:
        return
    operation = db.scalar(select(models.ProviderOperation).where(models.ProviderOperation.job_id == job.id))
    remote_may_charge = (ledger.status == "pending_reconciliation"
                         or operation is not None and operation.state not in {"created", "rejected"})
    ledger.status = "pending_reconciliation" if remote_may_charge else "released"
    ledger.decision = ledger.status
    ledger.metadata_json = {**ledger.metadata_json, "reservation_state": ledger.status}
    db.flush()


def settle_provider_cost(
    db: Session,
    *,
    job: models.Job,
    cost: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    provider: str | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    source: str = "provider_result",
) -> BudgetDecision:
    """Reconcile the final Provider charge once, returning an enforcement decision."""

    db.execute(select(models.Job.id).where(models.Job.id == job.id).with_for_update())
    budget = get_budget(db, job.tenant_id, for_update=True)
    default_currency = budget.currency if budget else "USD"
    amount_micros, currency = normalize_cost(cost, default_currency=default_currency)
    existing = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
    known = any((cost or {}).get(key) is not None for key in ("amount", "total", "total_amount", "price", "value"))
    if existing is not None and existing.status == "settled":
        different = known and (existing.amount_micros != amount_micros or existing.currency != currency)
        record_usage_evidence(db, job=job, cost=cost, usage=usage, provider=provider,
                              source=source, disposition="adjustment_pending" if different else "duplicate", default_currency=default_currency)
        audit = dict(existing.metadata_json)
        return BudgetDecision(
            allowed=bool(audit.get("allowed", existing.decision != "over_budget")),
            decision=existing.decision, amount_micros=existing.amount_micros,
            currency=existing.currency, audit=audit,
        )
    evidence = record_usage_evidence(db, job=job, cost=cost, usage=usage, provider=provider,
                                    source=source, disposition="observed" if known else "amount_unknown", default_currency=default_currency)
    if not known:
        if existing is None:
            existing = models.UsageLedger(tenant_id=job.tenant_id, job_id=job.id,
                                          period_key=period_key(), currency=currency,
                                          amount_micros=0, source=source)
            db.add(existing)
        reservation = (existing.metadata_json or {}).get("reservation") or {}
        pinned = (job.payload or {}).get("capability_snapshot") or {}
        pinned_matches = (reservation.get("provider") == pinned.get("provider")
                          and reservation.get("model_id") == pinned.get("model_id")
                          and reservation.get("model_version") == pinned.get("model_version")
                          and reservation.get("price_version") == (pinned.get("price") or {}).get("version"))
        # A specifically audited legacy recovery may add its missing budget
        # envelope without changing the original provider request/hash. The
        # observed model still has to match the server-owned quote exactly.
        legacy_recovery = not pinned and bool((existing.metadata_json or {}).get("reservation_recovery"))
        covered = (existing.status in _RESERVATION_STATUSES
                   and existing.source == "capability_catalog_estimate"
                   and existing.amount_micros > 0
                   and reservation.get("basis") == "server_capability_catalog"
                   and reservation.get("amount_micros") == existing.amount_micros
                   and reservation.get("currency") == existing.currency == currency
                   and reservation.get("provider") == provider
                   and reservation.get("model_id") == model_id
                   and reservation.get("model_version") == model_version
                   and (pinned_matches or legacy_recovery))
        check = _decision(budget=budget, amount_micros=existing.amount_micros or 0,
                          currency=existing.currency or default_currency,
                          monthly_used_micros=_sum_ledger(db, tenant_id=job.tenant_id, period=period_key(),
                                                         statuses=("settled",), exclude_job_id=job.id),
                          reserved_micros=_sum_ledger(db, tenant_id=job.tenant_id, period=period_key(),
                                                     statuses=_RESERVATION_STATUSES, exclude_job_id=job.id),
                          period=period_key(), job_id=job.id, db=db, job=job)
        # Unknown costs are never settled or released. A trusted reservation
        # can cover delivery while the cloud bill is delayed; client estimates
        # cannot grant this permission. Current limits are checked again.
        existing.status = "pending_reconciliation"
        existing.decision = "amount_unknown"
        audit = {"decision": "amount_unknown", "allowed": (not bool(budget and budget.enabled)
                                                           or bool(covered and check.allowed)),
                 "job_id": job.id, "amount_micros": None, "currency": currency,
                 "evidence_id": evidence.id, "reconciliation_required": True,
                 "reserved_micros": existing.amount_micros,
                 "delivery_basis": "trusted_reservation_pending_bill" if covered and check.allowed else "reconciliation_required",
                 "reservation": reservation, "budget_check": check.audit,
                 **({"reservation_recovery": existing.metadata_json["reservation_recovery"]}
                    if "reservation_recovery" in (existing.metadata_json or {}) else {})}
        existing.provider, existing.model_id = provider, model_id
        existing.cost_json, existing.usage_json = dict(cost or {}), dict(usage or {})
        existing.metadata_json = audit
        job.payload = {**(job.payload or {}), "budget": audit}
        db.flush()
        return BudgetDecision(allowed=audit["allowed"], decision="amount_unknown",
                              amount_micros=existing.amount_micros or 0, currency=currency, audit=audit)
    current_period = period_key()
    settled = _sum_ledger(
        db,
        tenant_id=job.tenant_id,
        period=current_period,
        statuses=("settled",),
        exclude_job_id=job.id,
    )
    reserved = _sum_ledger(
        db,
        tenant_id=job.tenant_id,
        period=current_period,
        statuses=_RESERVATION_STATUSES,
        exclude_job_id=job.id,
    )
    decision = _decision(
        budget=budget,
        amount_micros=amount_micros,
        currency=currency,
        monthly_used_micros=settled,
        reserved_micros=reserved,
        period=current_period,
        job_id=job.id,
        decision=None, db=db, job=job,
    )
    if existing is None:
        existing = models.UsageLedger(tenant_id=job.tenant_id, job_id=job.id)
        db.add(existing)
    if not decision.allowed:
        decision = BudgetDecision(
            allowed=False,
            decision="over_budget",
            amount_micros=decision.amount_micros,
            currency=decision.currency,
            audit={**decision.audit, "decision": "over_budget"},
        )
    existing.period_key = current_period
    existing.status = "settled"
    existing.decision = decision.decision if decision.allowed else "over_budget"
    existing.source = source
    existing.provider = provider
    existing.model_id = model_id
    existing.currency = currency
    existing.amount_micros = amount_micros
    existing.usage_json = dict(usage or {})
    existing.cost_json = dict(cost or {})
    existing.metadata_json = decision.audit
    audit = {**decision.audit, "source": source, "provider": provider, "model_id": model_id}
    job.payload = {**(job.payload or {}), "budget": audit}
    if not decision.allowed:
        decision = BudgetDecision(
            allowed=False,
            decision="over_budget",
            amount_micros=decision.amount_micros,
            currency=decision.currency,
            audit=audit,
        )
    return decision


def reconcile_usage_evidence(db: Session, *, job: models.Job, evidence: models.UsageEvidence,
                             approved_by: str, reason: str) -> models.UsageAdjustment:
    """Approve an absolute observed charge as an append-only correction."""
    get_budget(db, job.tenant_id, for_update=True)
    if evidence.job_id != job.id or evidence.tenant_id != job.tenant_id:
        raise BudgetDataError("evidence does not belong to this job")
    previous = db.scalar(select(models.UsageAdjustment).where(models.UsageAdjustment.evidence_id == evidence.id))
    if previous is not None:
        return previous
    ledger = db.scalar(select(models.UsageLedger).where(models.UsageLedger.job_id == job.id))
    if ledger is None or ledger.status != "settled" or evidence.amount_micros is None:
        raise BudgetDataError("reconciliation requires a settled charge and a known observation")
    if evidence.currency != ledger.currency:
        raise BudgetDataError("cross-currency adjustment requires explicit external reconciliation")
    if not approved_by.strip() or not reason.strip():
        raise BudgetDataError("reconciliation requires an actor and reason")
    adjusted = int(db.scalar(select(func.coalesce(func.sum(models.UsageAdjustment.delta_micros), 0))
                            .where(models.UsageAdjustment.job_id == job.id)) or 0)
    row = models.UsageAdjustment(tenant_id=job.tenant_id, job_id=job.id, evidence_id=evidence.id,
                                 period_key=period_key(), currency=ledger.currency,
                                 delta_micros=evidence.amount_micros - ledger.amount_micros - adjusted,
                                 approved_by=approved_by, reason=reason)
    db.add(row)
    db.flush()
    return row


def budget_snapshot(db: Session, budget: models.TenantBudget | None, tenant_id: str) -> dict[str, Any]:
    current_period = period_key()
    settled = _sum_ledger(db, tenant_id=tenant_id, period=current_period, statuses=("settled",))
    reserved = _sum_ledger(db, tenant_id=tenant_id, period=current_period, statuses=_RESERVATION_STATUSES)
    limit = budget.monthly_limit_micros if budget and budget.enabled else None
    return {
        "period_key": current_period,
        "settled_micros": settled,
        "reserved_micros": reserved,
        "remaining_micros": None if limit is None else max(0, limit - settled - reserved),
    }


def list_usage(db: Session, tenant_id: str, *, limit: int = 100) -> list[models.UsageLedger]:
    return list(
        db.scalars(
            select(models.UsageLedger)
            .where(models.UsageLedger.tenant_id == tenant_id)
            .order_by(models.UsageLedger.created_at.desc())
            .limit(limit)
        )
    )
