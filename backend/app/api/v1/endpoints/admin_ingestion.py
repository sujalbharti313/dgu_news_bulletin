"""Administrative ingestion endpoints. Server-to-server only.

- They exist only when INGESTION_ADMIN_TOKEN is set; otherwise every path answers 404.
- Every request needs `Authorization: Bearer <INGESTION_ADMIN_TOKEN>` (compared in constant time).
- Callers can only choose among configured source keys; no URL or query can be supplied.
- One run at a time per process (409 otherwise); across processes the database allows one running job
  per source. CORS stays GET-only, so browsers on other origins cannot call these routes.
- Nothing here publishes content.
"""

import hmac
import logging
import threading
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, get_database
from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.ingestion.errors import SourceConfigError
from app.ingestion.runner import IngestionRunner
from app.ingestion.store import SqlIngestionStore
from app.schemas.ingestion import IngestionRunAccepted, IngestionRunIn, IngestionStatusOut, SourceStatusOut

logger = logging.getLogger("app.ingestion.admin")
_run_lock = threading.Lock()


def require_admin(settings: SettingsDep, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = settings.ingestion_admin_token
    if expected is None:
        raise HTTPException(status_code=404)  # feature off: indistinguishable from a missing route
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode(), expected.get_secret_value().encode()
    ):
        logger.warning("rejected admin ingestion request (missing or invalid token)")
        raise HTTPException(
            status_code=401, detail="Valid admin credentials are required.", headers={"WWW-Authenticate": "Bearer"}
        )


def build_runner(settings: Settings, db: Database) -> IngestionRunner:
    if not db.configured:
        raise DatabaseNotConfiguredError()
    return IngestionRunner(settings, SqlIngestionStore(db))


def get_runner(
    request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]
) -> IngestionRunner:
    factory = getattr(request.app.state, "ingestion_runner_factory", None) or build_runner
    try:
        return factory(settings, db)
    except SourceConfigError as exc:
        logger.error("ingestion source configuration invalid: %s", exc.safe_message)
        raise HTTPException(status_code=503, detail="Ingestion source configuration is invalid.") from exc


router = APIRouter(
    prefix="/admin/ingestion", tags=["admin"], dependencies=[Depends(require_admin)], include_in_schema=False
)
RunnerDep = Annotated[IngestionRunner, Depends(get_runner)]


def _locked_run(runner: IngestionRunner, keys: list[str] | None) -> None:
    try:
        runner.run(keys)
    except Exception:  # noqa: BLE001 - background task: log, never crash the worker
        logger.exception("background ingestion run failed")
    finally:
        _run_lock.release()


@router.post("/runs", status_code=202)
def trigger_run(body: IngestionRunIn, runner: RunnerDep, background: BackgroundTasks):
    try:
        selected = [s.key for s in runner.select(body.sources)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An ingestion run is already in progress.")
    if body.wait:
        try:
            report = runner.run(body.sources)
        finally:
            _run_lock.release()
        return JSONResponse(report.as_dict(), status_code=200)
    background.add_task(_locked_run, runner, body.sources)
    return IngestionRunAccepted(status="accepted", sources=selected)


@router.get("/status", response_model=IngestionStatusOut)
def status(runner: RunnerDep, settings: SettingsDep):
    keys = [s.key for s in runner.sources]
    statuses = runner.store.source_statuses(keys)
    out = []
    for source in runner.sources:
        st = statuses[source.key]
        out.append(
            SourceStatusOut(
                key=source.key,
                type=source.type,
                name=source.name,
                category=source.category,
                enabled=source.enabled,
                configured=settings.gmail_configured if source.type == "gmail" else True,
                last_status=st.last_status,
                last_started_at=st.last_started_at,
                last_completed_at=st.last_completed_at,
                last_success_at=st.last_success_at,
                last_error=st.last_error,
                last_counts=st.last_counts,
                consecutive_failures=st.consecutive_failures,
                pending_items=st.pending_items,
            )
        )
    return IngestionStatusOut(running_in_this_process=_run_lock.locked(), sources=out)
