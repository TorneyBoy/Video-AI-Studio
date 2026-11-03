"""Provision the one SSH acceptance tenant, without public tenant-admin APIs."""

import argparse

from app.database import SessionLocal
from app.models import Tenant
from app.services.budgets import upsert_budget


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        tenant = db.get(Tenant, args.tenant_id)
        if tenant is None:
            db.add(Tenant(id=args.tenant_id, slug="m1-private-acceptance", name="媒体闭环验收工作区"))
            db.flush()
            # This additional application reservation envelope is not a claim
            # about remaining USD budget or final supplier billing. Prepaid ICE,
            # FC and OSS costs still need cumulative external reconciliation.
            upsert_budget(db, args.tenant_id, currency="CNY", monthly_limit_micros=10_000_000,
                          task_limit_micros=1_000_000, enforcement="block", enabled=True)
    print("Acceptance tenant provisioned; existing policy and spend were preserved.")


if __name__ == "__main__":
    main()
