"""Administrative Stage 3.4 endpoints. Server-to-server only, same protection as the other admin endpoints.

- They exist only when INGESTION_ADMIN_TOKEN is set (404 otherwise) and need that bearer token. They are not in
  the public OpenAPI schema; the public API never serves verification results, scores or the shortlist.
- Read-only: scores, eligibility and rankings are computed server-side by the ranking engine and cannot be
  changed through the API. `POST /runs` only triggers a computation (prefer `python -m scripts.rank run` for
  scheduled work; with the AI check on it can take a few minutes). Nothing here publishes or approves content.
"""

import threading
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep, get_database
from app.api.v1.endpoints.admin_ingestion import require_admin
from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.ranking.config import RankingConfigError
from app.ranking.contract import RankingResult
from app.ranking.runner import RankingRunner
from app.ranking.service import build_runner as build_sql_runner
from app.ranking.store import SchemaNotReady

_run_lock = threading.Lock()
WEEK_PATTERN = r"^(previous|current|\d{4}-\d{2}-\d{2})$"


def build_runner(settings: Settings, db: Database) -> RankingRunner:
    if not db.configured:
        raise DatabaseNotConfiguredError()
    return build_sql_runner(settings, db)


def get_runner(
    request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]
) -> RankingRunner:
    factory = getattr(request.app.state, "ranking_runner_factory", None) or build_runner
    return factory(settings, db)


router = APIRouter(
    prefix="/admin/ranking", tags=["admin"], dependencies=[Depends(require_admin)], include_in_schema=False
)
RunnerDep = Annotated[RankingRunner, Depends(get_runner)]
Week = Annotated[str, Query(pattern=WEEK_PATTERN)]


@router.post("/runs")
def trigger_run(
    runner: RunnerDep,
    week: Week = "previous",
    retry_failed: Annotated[bool, Query(alias="retryFailed")] = False,
):
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A ranking run is already in progress.")
    try:
        report = runner.run(week=week, retry_failed=retry_failed)
    finally:
        _run_lock.release()
    status_code = {"completed": 200, "incomplete": 202, "skipped": 409}.get(report.status, 500)
    if report.status == "failed" and report.job_id is None:
        status_code = 503  # configuration or schema not ready
    return JSONResponse(report.as_dict(), status_code=status_code)


@router.get("/runs/latest", response_model=RankingResult)
def latest(
    runner: RunnerDep,
    week: Annotated[str | None, Query(pattern=WEEK_PATTERN)] = None,
    include_incomplete: Annotated[bool, Query(alias="includeIncomplete")] = True,
):
    try:
        result = runner.latest(week=week, include_incomplete=include_incomplete)
    except (SchemaNotReady, RankingConfigError) as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="No ranking stored for this selection.")
    return result


@router.get("/runs")
def runs(runner: RunnerDep, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        runner.store.check_schema()
        rows = runner.store.list_runs(limit=limit)
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
    return [
        {
            "runId": str(r.id),
            "editionStart": r.edition_start.isoformat(),
            "editionEnd": r.edition_end.isoformat(),
            "status": r.status,
            "statusReason": r.status_reason,
            "processedAt": r.processed_at.isoformat(),
            "rankingConfigVersion": r.ranking_config_version,
            "verificationConfigVersion": r.verification_config_version,
            "totals": {
                "evaluated": r.total_evaluated,
                "eligible": r.total_eligible,
                "reviewRequired": r.total_review_required,
                "ineligible": r.total_ineligible,
                "pending": r.total_pending,
                "failed": r.total_failed,
            },
            "shortlist": [str(c) for c in r.shortlist_cluster_ids],
        }
        for r in rows
    ]


@router.get("/verifications")
def verifications(
    runner: RunnerDep,
    eligibility: Annotated[list[Literal["eligible", "review_required", "ineligible"]] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    try:
        runner.store.check_schema()
        rows = runner.store.list_verifications(now=runner.clock(), eligibility=eligibility, limit=limit)
    except SchemaNotReady as exc:
        raise HTTPException(status_code=503, detail=exc.safe_message) from exc
    return [
        {
            "clusterId": str(r.cluster_id),
            "editionStart": r.edition_start.isoformat(),
            "verificationStatus": r.verification_status,
            "eligibility": r.eligibility,
            "finalPriorityScore": r.final_priority_score,
            "priorityClassification": r.priority_classification,
            "sourceCredibility": {"score": r.source_credibility_score, "level": r.source_credibility_level},
            "evidenceQualityScore": r.evidence_quality_score,
            "claimConsistencyScore": r.claim_consistency_score,
            "technicalSubstanceScore": r.technical_substance_score,
            "corroborationLevel": r.corroboration_level,
            "sourceCount": r.source_count,
            "independentSourceCount": r.independent_source_count,
            "rank": r.rank,
            "shortlistRank": r.shortlist_rank,
            "reviewFlags": r.review_flags,
            "rankingExplanation": r.ranking_explanation,
            "aiCheckStatus": r.ai_check_status,
            "aiCheckAttempts": r.ai_check_attempts,
            "errors": r.errors,
            "evaluatedAt": r.evaluated_at.isoformat(),
        }
        for r in rows
    ]
