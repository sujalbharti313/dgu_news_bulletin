"""Administrative Stage 3.3 endpoints. Server-to-server only, same protection as admin ingestion.

- They exist only when INGESTION_ADMIN_TOKEN is set (404 otherwise) and need that bearer token. They are not
  in the public OpenAPI schema; the public API never serves analyses, prompts or usage.
- `POST /runs` analyses the clusters that need it (bounded by AI_MAX_STORIES_PER_RUN; it can take minutes,
  prefer `python -m scripts.intelligence run` for scheduled work). `GET /analyses` returns the Stage 3.4
  contract; `GET /records` shows per-story status, attempts and errors; `GET /usage` shows today's Gemini usage
  against the configured free-tier limits. Nothing here publishes content.
"""

import threading
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, get_database
from app.api.v1.endpoints.admin_ingestion import require_admin
from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.intelligence.contract import StoryAnalysis
from app.intelligence.runner import AnalysisRunner
from app.intelligence.service import build_runner as build_sql_runner
from app.intelligence.store import SchemaNotReady

_run_lock = threading.Lock()
StatusFilter = Literal[
    "completed", "needs_review", "insufficient_evidence", "invalid_output", "failed", "quota_limited"
]


def build_runner(settings: Settings, db: Database) -> AnalysisRunner:
    if not db.configured:
        raise DatabaseNotConfiguredError()
    return build_sql_runner(settings, db)


def get_runner(
    request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]
) -> AnalysisRunner:
    factory = getattr(request.app.state, "intelligence_runner_factory", None) or build_runner
    return factory(settings, db)


router = APIRouter(
    prefix="/admin/intelligence", tags=["admin"], dependencies=[Depends(require_admin)], include_in_schema=False
)
RunnerDep = Annotated[AnalysisRunner, Depends(get_runner)]


@router.post("/runs")
def trigger_run(runner: RunnerDep, retry_failed: Annotated[bool, Query(alias="retryFailed")] = False):
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An analysis run is already in progress.")
    try:
        report = runner.run(retry_failed=retry_failed)
    finally:
        _run_lock.release()
    status_code = {"succeeded": 200, "partial": 200, "quota_limited": 429, "skipped": 409}.get(report.status, 500)
    if report.reason == SchemaNotReady.safe_message:
        status_code = 503
    elif report.status == "failed" and report.job_id is None:
        status_code = 503  # not configured (GEMINI_API_KEY)
    return JSONResponse(report.as_dict(), status_code=status_code)


@router.get("/analyses", response_model=list[StoryAnalysis])
def analyses(
    runner: RunnerDep,
    status: Annotated[list[Literal["completed", "needs_review", "insufficient_evidence"]] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    updated_since: Annotated[datetime | None, Query(alias="updatedSince")] = None,
):
    try:
        return runner.list(statuses=status, limit=limit, updated_since=updated_since)
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc


@router.get("/records")
def records(
    runner: RunnerDep,
    status: Annotated[list[StatusFilter] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    try:
        runner.store.check_schema()
        rows = runner.store.list(now=runner.clock(), statuses=status, limit=limit)
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
    return [
        {
            "clusterId": str(r.cluster_id),
            "clusterRevision": r.cluster_revision,
            "status": r.status,
            "attemptCount": r.attempt_count,
            "lastAttemptAt": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
            "nextAttemptAt": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
            "modelId": r.model_id,
            "primaryCategory": r.primary_category,
            "priorityScore": r.priority_score,
            "priorityLevel": r.priority_level,
            "inputTokens": r.input_tokens,
            "outputTokens": r.output_tokens,
            "reviewFlags": r.review_flags,
            "errors": r.errors,
        }
        for r in rows
    ]


@router.get("/usage")
def usage(runner: RunnerDep):
    s = runner.settings
    try:
        runner.store.check_schema()
        snap = runner.ledger.snapshot(s.ai_model, runner.clock())
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
    return {
        **snap.as_dict(requests_per_day=s.ai_requests_per_day, tokens_per_day=s.ai_tokens_per_day),
        "requestsPerMinuteLimit": s.ai_requests_per_minute or None,
        "apiKeyConfigured": s.gemini_api_key is not None,
    }
