"""Stage 3.5 editorial admin API: review, edit, approve, reject, publish and retry weekly editions.

- Exists only when SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are set (404 otherwise). Not in the public schema.
- Every request needs a Supabase Auth access token of an active `editorial_admins` user (app.editions.auth).
  The ingestion admin token does not work here.
- The client never sends a status. Each action is a separate endpoint that the backend checks against the state
  machine, the caller's `expectedRevision` (and, for approval, the content hash the admin reviewed), and the
  database records. Approval and publication are recorded with the admin's user id.
- Responses are `Cache-Control: no-store`. Errors are the standard safe JSON shape.
- Admin-triggered generation runs in the request with tighter timeouts and does not start a Stage 3.4 run
  (the scheduler does that); a quota or Gemini failure leaves the edition in a retryable state.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from pydantic import Field
from sqlalchemy import select

from app.api.deps import SettingsDep, get_database
from app.core.config import Settings
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.editions import build_service
from app.editions.auth import AuthUnavailable, SupabaseTokenVerifier, WriteLimiter
from app.editions.config import EditionConfigError
from app.editions.content import StoryText
from app.editions.errors import EditionError
from app.editions.schedule import week_for
from app.editions.service import Actor, EditionService, RevisionInput, StoryInput
from app.models import EditorialAdmin
from app.schemas.common import ApiModel

logger = logging.getLogger("app.editions.admin")


class AdminContext:
    def __init__(self, actor: Actor, email: str, name: str | None):
        self.actor = actor
        self.email = email
        self.name = name


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401, detail="Sign in as an editorial admin.", headers={"WWW-Authenticate": "Bearer"}
        )
    return token.strip()


def _verifier(request: Request, settings: Settings):
    verifier = getattr(request.app.state, "admin_token_verifier", None)
    if verifier is None:
        verifier = SupabaseTokenVerifier(settings)
        request.app.state.admin_token_verifier = verifier
    return verifier


def require_editor(
    request: Request,
    response: Response,
    settings: SettingsDep,
    db: Annotated[Database, Depends(get_database)],
    authorization: Annotated[str | None, Header()] = None,
) -> AdminContext:
    response.headers["Cache-Control"] = "no-store"
    if not settings.admin_auth_configured:
        raise HTTPException(status_code=404)
    token = _bearer(authorization)
    try:
        user = _verifier(request, settings).verify(token)
    except AuthUnavailable:
        raise HTTPException(status_code=503, detail="Sign-in could not be checked right now; try again.") from None
    if user is None:
        raise HTTPException(
            status_code=401, detail="Your sign-in is invalid or has expired.", headers={"WWW-Authenticate": "Bearer"}
        )
    if not db.configured:
        raise DatabaseNotConfiguredError()
    with db.write_session() as s:
        admin = s.scalar(
            select(EditorialAdmin).where(EditorialAdmin.user_id == user.user_id, EditorialAdmin.is_active.is_(True))
        )
        if admin is None:
            logger.warning("signed-in user %s is not an active editorial admin", user.user_id)
            raise HTTPException(status_code=403, detail="This account is not an editorial admin.")
        return AdminContext(Actor("admin", admin.user_id), admin.email, admin.display_name)


Editor = Annotated[AdminContext, Depends(require_editor)]


def _limit_writes(request: Request, settings: SettingsDep, editor: Editor) -> AdminContext:
    limiter = getattr(request.app.state, "admin_write_limiter", None)
    if limiter is None:
        limiter = WriteLimiter(settings.admin_write_rate_per_minute)
        request.app.state.admin_write_limiter = limiter
    if not limiter.allow(editor.actor.user_id):
        raise HTTPException(status_code=429, detail="Too many changes in a short time; wait a minute.")
    return editor


Writer = Annotated[AdminContext, Depends(_limit_writes)]


def get_service(request: Request, settings: SettingsDep, db: Annotated[Database, Depends(get_database)]):
    factory = getattr(request.app.state, "edition_service_factory", None)
    if factory is not None:
        return factory(settings, db)
    if not db.configured:
        raise DatabaseNotConfiguredError()
    # Requests must finish quickly: one Gemini retry, shorter timeout, no Stage 3.4 run from the API.
    quick = settings.model_copy(update={"ai_max_retries": 1, "ai_request_timeout_seconds": 40.0})
    try:
        return build_service(quick, db, rank_week=None)
    except EditionConfigError as exc:
        logger.error("edition configuration invalid: %s", exc.safe_message)
        raise HTTPException(status_code=503, detail="The edition configuration is invalid.") from None


ServiceDep = Annotated[EditionService, Depends(get_service)]
EditionId = Annotated[uuid.UUID, Path()]
router = APIRouter(prefix="/admin/editions", tags=["admin"], include_in_schema=False)


class RevisionRef(ApiModel):
    expected_revision: int = Field(ge=0, le=100_000)


class ApproveIn(RevisionRef):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RejectIn(RevisionRef):
    reason: str = Field(min_length=3, max_length=2000)
    return_for_revision: bool = False


class GenerateIn(ApiModel):
    week: str = Field(default="previous", pattern=r"^(previous|\d{4}-\d{2}-\d{2})$")


class StoryEditIn(ApiModel):
    cluster_id: uuid.UUID
    headline: str = Field(max_length=600)
    summary: str = Field(max_length=4000)
    technical_explanation: str = Field(default="", max_length=12000)
    why_it_matters: str = Field(max_length=6000)
    student_relevance: str = Field(max_length=6000)
    project_idea: str = Field(default="", max_length=4000)


class RevisionIn(RevisionRef):
    title: str = Field(max_length=400)
    introduction: str = Field(max_length=8000)
    editorial_note: str = Field(default="", max_length=4000)
    stories: list[StoryEditIn] = Field(max_length=20)
    acknowledge_review: list[uuid.UUID] = Field(default_factory=list, max_length=20)


@router.get("/session")
def session(editor: Editor):
    return {"userId": str(editor.actor.user_id), "email": editor.email, "name": editor.name}


@router.get("")
def list_editions(editor: Editor, service: ServiceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    return {"items": service.list_editions(limit)}


@router.post("/generate")
def generate(body: GenerateIn, editor: Writer, service: ServiceDep):
    try:
        window = week_for(body.week, service.clock(), service.config.schedule)
    except ValueError as exc:
        raise EditionError(str(exc), status_code=422, code="invalid_week") from None
    report = service.generate_week(window.start_date, actor=editor.actor)
    return report.as_dict()


@router.get("/{edition_id}")
def detail(edition_id: EditionId, editor: Editor, service: ServiceDep):
    return service.detail(edition_id, actor=editor.actor)


@router.get("/{edition_id}/revisions/{revision}")
def revision(edition_id: EditionId, revision: Annotated[int, Path(ge=1)], editor: Editor, service: ServiceDep):
    return service.revision_content(edition_id, revision)


@router.get("/{edition_id}/candidates")
def candidates(edition_id: EditionId, editor: Editor, service: ServiceDep):
    return {"items": service.candidates(edition_id)}


@router.get("/{edition_id}/preview")
def preview(edition_id: EditionId, editor: Editor, service: ServiceDep):
    return service.preview(edition_id)


@router.put("/{edition_id}/content")
def save(edition_id: EditionId, body: RevisionIn, editor: Writer, service: ServiceDep):
    data = RevisionInput(
        expected_revision=body.expected_revision,
        title=body.title,
        introduction=body.introduction,
        editorial_note=body.editorial_note,
        stories=[
            StoryInput(
                st.cluster_id,
                StoryText(
                    headline=st.headline,
                    summary=st.summary,
                    technical_explanation=st.technical_explanation,
                    why_it_matters=st.why_it_matters,
                    student_relevance=st.student_relevance,
                    project_idea=st.project_idea,
                ),
            )
            for st in body.stories
        ],
        acknowledge_review=set(body.acknowledge_review),
    )
    return service.save_revision(edition_id, editor.actor, data)


@router.post("/{edition_id}/submit")
def submit(edition_id: EditionId, body: RevisionRef, editor: Writer, service: ServiceDep):
    return service.submit(edition_id, editor.actor, body.expected_revision)


@router.post("/{edition_id}/approve")
def approve(edition_id: EditionId, body: ApproveIn, editor: Writer, service: ServiceDep):
    return service.approve(edition_id, editor.actor, body.expected_revision, body.content_hash)


@router.post("/{edition_id}/reject")
def reject(edition_id: EditionId, body: RejectIn, editor: Writer, service: ServiceDep):
    return service.reject(
        edition_id, editor.actor, body.expected_revision, body.reason, to_draft=body.return_for_revision
    )


@router.post("/{edition_id}/publish")
def publish(edition_id: EditionId, body: RevisionRef, editor: Writer, service: ServiceDep):
    return service.publish(edition_id, editor.actor, body.expected_revision)


@router.post("/{edition_id}/regenerate")
def regenerate(edition_id: EditionId, body: RevisionRef, editor: Writer, service: ServiceDep):
    return service.regenerate(edition_id, editor.actor, body.expected_revision).as_dict()


@router.post("/{edition_id}/archive")
def archive(edition_id: EditionId, body: RevisionRef, editor: Writer, service: ServiceDep):
    return service.archive(edition_id, editor.actor, body.expected_revision)
