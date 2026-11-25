"""Stage 3.6 cleanup admin API: view cleanup jobs, manifests, audit and the retention policy; retry, cancel or
reopen a job; plan a published edition's cleanup or run a cleanup pass now.

- Same authentication as the Stage 3.5 admin API (Supabase Auth token of an active `editorial_admins` user), and
  absent (404) when admin sign-in is not configured. Not in the public schema; nothing here is reachable by
  students or the Supabase Data API.
- There is no endpoint that deletes a chosen record or table, and no client-supplied SQL, filter expression or
  status. Every action runs the same publication and protection checks as the scheduler; none bypasses a safety
  delay, the manifest hold or a database guard. Ids are validated as UUIDs.
- Writes are rate limited per admin (the Stage 3.5 limiter), and "run now" additionally per hour (database-backed).
- Responses are `Cache-Control: no-store`; errors use the standard safe JSON shape.
"""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from app.api.deps import SettingsDep, get_database
from app.api.v1.endpoints.admin_editions import Editor, Writer
from app.cleanup import build_cleanup_service
from app.cleanup.config import RetentionConfigError
from app.cleanup.service import CleanupService
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.editions.config import EditionConfigError

logger = logging.getLogger("app.cleanup.admin")
router = APIRouter(prefix="/admin/cleanup", tags=["admin"], include_in_schema=False)


def get_cleanup_service(request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]):
    factory = getattr(request.app.state, "cleanup_service_factory", None)
    if factory is not None:
        return factory(settings, db)
    if not db.configured:
        raise DatabaseNotConfiguredError()
    try:
        return build_cleanup_service(settings, db)
    except (RetentionConfigError, EditionConfigError) as exc:
        logger.error("cleanup configuration invalid: %s", exc.safe_message)
        raise HTTPException(status_code=503, detail="The retention configuration is invalid.") from None


ServiceDep = Annotated[CleanupService, Depends(get_cleanup_service)]
JobId = Annotated[uuid.UUID, Path()]
State = Literal["pending", "deleted", "protected", "skipped", "failed"]


@router.get("/policy")
def policy(editor: Editor, service: ServiceDep):
    return service.policy()


@router.get("/jobs")
def jobs(editor: Editor, service: ServiceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    return {"items": service.list_jobs(limit)}


@router.get("/jobs/{job_id}")
def job(
    job_id: JobId,
    editor: Editor,
    service: ServiceDep,
    state: Annotated[State | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    return service.job_detail(job_id, state=state, limit=limit)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: JobId, editor: Writer, service: ServiceDep):
    return service.retry(job_id, editor.actor)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: JobId, editor: Writer, service: ServiceDep):
    return service.cancel(job_id, editor.actor)


@router.post("/jobs/{job_id}/reopen")
def reopen(job_id: JobId, editor: Writer, service: ServiceDep):
    return service.reopen(job_id, editor.actor)


@router.post("/editions/{edition_id}/plan")
def plan(edition_id: Annotated[uuid.UUID, Path()], editor: Writer, service: ServiceDep):
    return service.plan_edition(edition_id, editor.actor)


@router.post("/run")
def run_now(editor: Writer, service: ServiceDep):
    return service.run_now(editor.actor)
