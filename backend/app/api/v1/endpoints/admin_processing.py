"""Administrative Stage 3.2 endpoints. Server-to-server only, same protection as admin ingestion.

- They exist only when INGESTION_ADMIN_TOKEN is set (404 otherwise) and need that bearer token.
- `POST /runs` runs deduplication and clustering over the staged items (one run per process at a time,
  and one across processes through the job lock). `GET /clusters` returns the Stage 3.3 contract for
  inspection. Neither is in the public OpenAPI schema, and CORS stays GET-only for public routes.
- Internal processing records are never served by the public API. Nothing here publishes content.
"""

import logging
import threading
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, get_database
from app.api.v1.endpoints.admin_ingestion import require_admin
from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.processing.contract import StoryCluster
from app.processing.runner import ProcessingRunner
from app.processing.store import SchemaNotReady, SqlProcessingStore

logger = logging.getLogger("app.processing.admin")
_run_lock = threading.Lock()


def build_runner(settings: Settings, db: Database) -> ProcessingRunner:
    if not db.configured:
        raise DatabaseNotConfiguredError()
    return ProcessingRunner(settings, SqlProcessingStore(db))


def get_runner(
    request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]
) -> ProcessingRunner:
    factory = getattr(request.app.state, "processing_runner_factory", None) or build_runner
    return factory(settings, db)


router = APIRouter(
    prefix="/admin/processing", tags=["admin"], dependencies=[Depends(require_admin)], include_in_schema=False
)
RunnerDep = Annotated[ProcessingRunner, Depends(get_runner)]


@router.post("/runs")
def trigger_run(runner: RunnerDep):
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A processing run is already in progress.")
    try:
        report = runner.run()
    finally:
        _run_lock.release()
    status_code = {"succeeded": 200, "skipped": 409}.get(report.status, 500)
    if report.reason and report.reason == SchemaNotReady.safe_message:
        status_code = 503
    return JSONResponse(report.as_dict(), status_code=status_code)


@router.get("/clusters", response_model=list[StoryCluster])
def clusters(
    runner: RunnerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    updated_since: Annotated[datetime | None, Query(alias="updatedSince")] = None,
):
    try:
        return runner.clusters(limit=limit, updated_since=updated_since)
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
