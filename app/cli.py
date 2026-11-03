import argparse
import json
import os
import time

from .database import SessionLocal, engine, init_db
from .migrations import migrate
from .services.provider_preflight import preflight_provider_registry_file
from .services.provider_health import provider_health_report
from .services.provider_registry import ConfiguredProviderRegistry, ProviderRegistryError
from .services.provider_smoke import run_provider_smoke
from .services.worker import process_queued_jobs
from .services.workflows import recover_expired_workflows
from .services.runtime_logging import configure_logging,emit,install_job_observation,failure_code,fingerprint
from .services.operations import operational_snapshot,job_diagnostic,evaluate_alerts
from .services.worker_observation import observed_worker
from .config import get_settings
from fastapi.encoders import jsonable_encoder


def main() -> None:
    parser = argparse.ArgumentParser(description="Creative Backend maintenance commands")
    parser.add_argument(
        "command",
        choices=[
            "init-db",
            "migrate",
            "process-jobs",
            "recover-workflows",
            "provider-preflight",
            "provider-health",
            "provider-smoke",
            "ops-report",
            "ops-check",
        ],
        help="Command to run",
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of queued jobs to process")
    parser.add_argument('--job-id',help='Read one bounded internal job diagnostic (ops-report only)')
    parser.add_argument("--watch", action="store_true", help="Keep polling until interrupted")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between worker polls")
    parser.add_argument(
        "--path",
        "--registry-path",
        dest="registry_path",
        help="Provider registry JSON path (defaults to PROVIDER_REGISTRY_PATH)",
    )
    parser.add_argument("--json", action="store_true", help="Render provider-preflight/provider-health as JSON")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Explicitly probe configured Provider health endpoints (network access; default is network-free)",
    )
    parser.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="Report unset secret references as warnings (template/CI checks only)",
    )
    parser.add_argument(
        "--capabilities",
        help="Comma-separated hosted capabilities required by this rollout (defaults to all)",
    )
    parser.add_argument("--provider", help="Provider name for provider-smoke")
    parser.add_argument("--capability", help="Capability name for provider-smoke")
    parser.add_argument("--request-id", help="Stable request ID for provider-smoke")
    parser.add_argument("--inputs-json", help="JSON object containing remote input references for provider-smoke")
    parser.add_argument("--parameters-json", help="Optional JSON object of provider parameters for provider-smoke")
    parser.add_argument("--model-id", help="Optional configured model ID for provider-smoke")
    parser.add_argument("--model-version", help="Optional configured model version for provider-smoke")
    parser.add_argument("--tenant-id", help="Optional tenant ID for provider-smoke")
    parser.add_argument("--region", help="Optional provider region for provider-smoke")
    parser.add_argument("--provider-job-id", help="Resume an existing asynchronous provider task")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly contact the configured staging Provider Gateway (provider-smoke only)",
    )
    args = parser.parse_args()
    if args.command in {'ops-report','ops-check'}:
        # Diagnostics never silently migrate an operator's target database.
        configure_logging()
        if args.watch and (args.command!='ops-check' or args.interval<5):parser.error('ops-check --watch requires --interval of at least 5 seconds')
        try:
            while True:
                with SessionLocal() as db:
                    if args.command=='ops-report':
                        value=job_diagnostic(db,args.job_id) if args.job_id else operational_snapshot(db)
                        if value is None:raise SystemExit('Diagnostic job not found')
                        print(json.dumps(jsonable_encoder(value),ensure_ascii=False,sort_keys=True),flush=True);break
                    value,transitions=evaluate_alerts(db);db.commit()
                if args.json:print(json.dumps(jsonable_encoder({'snapshot':value,'transitions':transitions}),ensure_ascii=False,sort_keys=True),flush=True)
                else:
                    for item in transitions:emit(item['event'],**{key:field for key,field in item.items() if key!='event'})
                    emit('operator_check',count=len(value['alerts']),outcome='attention' if value['alerts'] else 'clear')
                if not args.watch:
                    if value['alerts']:raise SystemExit(2)
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:pass
        except Exception:
            emit('operator_check',code='observation_unavailable',severity='critical',outcome='error')
            raise SystemExit(1) from None
    elif args.command == "provider-health":
        path = args.registry_path or os.getenv("PROVIDER_REGISTRY_PATH")
        if not path and not os.getenv("PROVIDER_REGISTRY_JSON"):
            parser.error("provider-health requires --path, PROVIDER_REGISTRY_PATH or PROVIDER_REGISTRY_JSON")
        try:
            registry = ConfiguredProviderRegistry.from_file(path) if path else ConfiguredProviderRegistry.from_env()
            report = provider_health_report(registry, source=path or "PROVIDER_REGISTRY_JSON", probe=args.probe)
        except (ProviderRegistryError, OSError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        else:
            print(report.render_text())
        if not report.ok:
            raise SystemExit(2)
    elif args.command == "provider-smoke":
        path = args.registry_path or os.getenv("PROVIDER_REGISTRY_PATH")
        if not path and not os.getenv("PROVIDER_REGISTRY_JSON"):
            parser.error("provider-smoke requires --path, PROVIDER_REGISTRY_PATH or PROVIDER_REGISTRY_JSON")
        required = {
            "provider": args.provider,
            "capability": args.capability,
            "request_id": args.request_id,
            "inputs_json": args.inputs_json,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            parser.error(f"provider-smoke requires: {', '.join('--' + name.replace('_', '-') for name in missing)}")

        def _json_object(raw: str, flag: str) -> dict:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                parser.error(f"{flag} must be valid JSON: {exc.msg}")
            if not isinstance(value, dict):
                parser.error(f"{flag} must decode to a JSON object")
            return value

        inputs = _json_object(args.inputs_json, "--inputs-json")
        parameters = _json_object(args.parameters_json, "--parameters-json") if args.parameters_json else None
        try:
            registry = ConfiguredProviderRegistry.from_file(path) if path else ConfiguredProviderRegistry.from_env()
            result = run_provider_smoke(
                registry,
                provider_name=args.provider,
                capability=args.capability,
                request_id=args.request_id,
                inputs=inputs,
                parameters=parameters,
                model_id=args.model_id,
                model_version=args.model_version,
                tenant_id=args.tenant_id,
                region=args.region,
                execute=args.execute,
                provider_job_id=args.provider_job_id,
            )
        except (ProviderRegistryError, OSError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
        else:
            print(result.render_text())
        if not result.ok:
            raise SystemExit(2)
    elif args.command == "provider-preflight":
        path = args.registry_path or os.getenv("PROVIDER_REGISTRY_PATH")
        if not path:
            parser.error("provider-preflight requires --path or PROVIDER_REGISTRY_PATH")
        try:
            required_capabilities = None
            if args.capabilities is not None:
                required_capabilities = tuple(
                    item.strip() for item in args.capabilities.split(",") if item.strip()
                )
            report = preflight_provider_registry_file(
                path,
                required_capabilities=required_capabilities,
                require_secrets=not args.allow_missing_secrets,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
        else:
            print(report.render_text())
        if not report.ok:
            raise SystemExit(2)
    elif args.command == "migrate":
        migrate(engine)
        print("Database migrations applied")
    elif args.command == "init-db":
        init_db()
        print("Database initialized")
    elif args.command == "process-jobs":
        init_db()
        configure_logging();install_job_observation()
        if args.interval <= 0:
            parser.error("--interval must be greater than zero")
        try:
            with observed_worker(SessionLocal,get_settings().workflow_worker_id) as instance:
                while True:
                    db = SessionLocal()
                    try:
                        results=process_queued_jobs(db, limit=max(1, args.limit))
                        emit('worker_cycle',instance_id=instance,count=len(results),outcome='completed')
                    except Exception:
                        emit('worker_cycle',instance_id=instance,code='worker_cycle_failed',severity='critical',outcome='error')
                        raise SystemExit(1) from None
                    finally:db.close()
                    if not args.watch:break
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Worker stopped", flush=True)
        except Exception:
            emit('worker_cycle',code='worker_start_failed',severity='critical',outcome='error')
            raise SystemExit(1) from None
    elif args.command == "recover-workflows":
        init_db()
        configure_logging();install_job_observation()
        db = SessionLocal()
        try:
            runs = recover_expired_workflows(db, limit=max(1, args.limit))
            db.commit()
            for run in runs:
                emit('worker_cycle',workflow_id=run.id,state=run.status if run.status in {'queued','running','failed','cancelled','succeeded'} else 'other',failure_code=failure_code(run.failure_code),failure_tag=fingerprint(run.failure_code) if run.failure_code else None)
        finally:
            db.close()


if __name__ == "__main__":
    main()
