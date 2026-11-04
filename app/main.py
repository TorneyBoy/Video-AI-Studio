from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal, get_db, init_db
from .services.observability import prometheus_job_metrics
from .api.ai_production import router as ai_production_router
from .api.project_quality import router as project_quality_router
from .api.routers import router
from .api.billing import router as billing_router
from .api.preparation import router as preparation_router
from .api.batches import router as batches_router
from .api.batch_runs import router as batch_runs_router
from .api.batch_delivery import router as batch_delivery_router
from .api.operations import router as operations_router
from .api.deps import require_metrics_access,require_available_database
from .services.recovery_guard import RecoveryQuarantined,database_active
from .services.runtime_logging import RequestObservation,configure_logging,install_job_observation,http_measurements
from .services.operations import operational_snapshot,prometheus_operations
from .api.storage import router as storage_router
from .api.identity import router as identity_router
from .api.editing import router as editing_router
from .api.reviews import router as reviews_router
from .api.delivery import router as delivery_router
from .api.storyboards import router as storyboard_router
from .api.shot_matching import router as matching_router
from .services.editing import EditingError
from .services.identity import IdentityError
from .services.storage_gateway import StorageGatewayError

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    configure_logging();install_job_observation()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Multi-tenant creative production API for projects, assets, templates, timelines, jobs, artifacts and consents.",
    lifespan=lifespan,
    dependencies=[Depends(require_available_database)],
)
app.include_router(storage_router, prefix=settings.api_prefix)
app.include_router(editing_router, prefix=settings.api_prefix)
app.include_router(reviews_router, prefix=settings.api_prefix)
app.include_router(delivery_router, prefix=settings.api_prefix)
app.include_router(storyboard_router, prefix=settings.api_prefix)
app.include_router(matching_router, prefix=settings.api_prefix)
app.include_router(router, prefix=settings.api_prefix)
app.include_router(billing_router, prefix=settings.api_prefix)
app.include_router(preparation_router, prefix=settings.api_prefix)
app.include_router(batches_router, prefix=settings.api_prefix)
app.include_router(batch_runs_router, prefix=settings.api_prefix)
app.include_router(batch_delivery_router, prefix=settings.api_prefix)
app.include_router(identity_router, prefix=settings.api_prefix)
app.include_router(operations_router)
app.include_router(ai_production_router, prefix=settings.api_prefix)
app.include_router(project_quality_router, prefix=settings.api_prefix)
app.add_middleware(RequestObservation)


@app.middleware("http")
async def private_session_cache(request, call_next):
    response = await call_next(request)
    if get_settings().auth_mode == "session" and request.url.path.startswith(settings.api_prefix + "/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie, Authorization"
    if request.url.path.startswith(settings.api_prefix + "/shared/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path=='/metrics' or request.url.path.startswith('/internal/operations'):
        response.headers['Cache-Control']='no-store'
    return response


@app.exception_handler(IdentityError)
async def identity_error_handler(request, exc: IdentityError):
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse({"detail": {"code": exc.code, "message": str(exc)}},
                        status_code=exc.status, headers=headers)


@app.exception_handler(RecoveryQuarantined)
async def recovery_quarantine_handler(request,exc):
    return JSONResponse({'detail':{'code':'database_recovery_quarantine','message':str(exc)}},status_code=503,
        headers={'Cache-Control':'no-store','Retry-After':'60'})


@app.exception_handler(StorageGatewayError)
async def storage_gateway_error_handler(request, exc: StorageGatewayError):
    return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code, "message": str(exc)}})


@app.exception_handler(EditingError)
async def editing_error_handler(request, exc: EditingError):
    return JSONResponse(status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": str(exc), "details": exc.details}})


@app.get("/healthz", tags=["system"])
@app.get("/health", include_in_schema=False)
def healthz():
    return {"status": "ok", "service": settings.app_name}


@app.get("/readyz", tags=["system"])
def readyz():
    """Readiness probe for load balancers and Compose.

    ``/healthz`` only reports that the process is alive.  This endpoint also
    verifies the configured database and storage path, so traffic is not sent
    to a process that cannot persist jobs or artifacts.
    """
    checks: dict[str, str] = {}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            checks['recovery']='ok' if database_active(db) else 'quarantined'
        checks["database"] = "ok"
    except SQLAlchemyError as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
    try:
        storage = Path(settings.storage_root)
        storage.mkdir(parents=True, exist_ok=True)
        checks["storage"] = "ok" if storage.is_dir() else "error: not a directory"
    except OSError as exc:
        checks["storage"] = f"error: {exc.__class__.__name__}"

    ready = all(value == "ok" for value in checks.values())
    body = {"status": "ok" if ready else "unavailable", "service": settings.app_name, "checks": checks}
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False, dependencies=[Depends(require_metrics_access)])
def metrics(db: Session = Depends(get_db)):
    """Expose low-cardinality Job counters for a Prometheus-compatible scraper."""
    try:
        body = prometheus_job_metrics(db)+prometheus_operations(operational_snapshot(db))+http_measurements.prometheus()
    except SQLAlchemyError:
        # Keep scrape failures explicit while avoiding database exception details.
        return PlainTextResponse("# metrics unavailable\n", status_code=503)
    return PlainTextResponse(body,headers={'Content-Type':'text/plain; version=0.0.4; charset=utf-8'})


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(Path(__file__).resolve().parent.parent / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/app.js", include_in_schema=False)
def console_script():
    return FileResponse(Path(__file__).resolve().parent.parent / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


CONSOLE_SCRIPTS = frozenset({"ai-tools-form.js", "ai-tools.js", "state.js", "network.js", "projects.js", "tasks.js", "assets.js", "workspace.js", "editor.js", "clip-editor.js", "subtitle-editor.js", "layer-editor.js", "reviews.js", "delivery.js", "billing.js", "preparation.js", "batch-form.js", "batches.js", "batch-runs.js", "batch-delivery.js", "account.js", "uploads.js", "vendor-sha256.js", "storyboard-form.js", "storyboards.js", "shot-matching-form.js", "shot-matching.js"})


@app.get("/web/{file_name}", include_in_schema=False)
def console_feature_script(file_name: str):
    if file_name not in CONSOLE_SCRIPTS:
        raise HTTPException(404)
    return FileResponse(Path(__file__).resolve().parent.parent / "web" / file_name,
                        media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/styles.css", include_in_schema=False)
def console_styles():
    return FileResponse(Path(__file__).resolve().parent.parent / "styles.css", media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(Path(__file__).resolve().parent.parent / "login.html", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})


@app.get("/auth.js", include_in_schema=False)
def login_script():
    return FileResponse(Path(__file__).resolve().parent.parent / "auth.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/auth.css", include_in_schema=False)
def login_styles():
    return FileResponse(Path(__file__).resolve().parent.parent / "auth.css", media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/share", include_in_schema=False)
def shared_page():
    return FileResponse(Path(__file__).resolve().parent.parent / "share.html",headers={
        "Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
        "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; media-src https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})


@app.get("/share.js", include_in_schema=False)
def shared_script():
    return FileResponse(Path(__file__).resolve().parent.parent / "share.js",media_type="text/javascript",headers={"Cache-Control":"no-cache"})


@app.get("/share.css", include_in_schema=False)
def shared_styles():
    return FileResponse(Path(__file__).resolve().parent.parent / "share.css",media_type="text/css",headers={"Cache-Control":"no-cache"})
