from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import BulletinDep
from app.schemas.common import ErrorResponse, Page, PageParams
from app.schemas.edition import BriefingOut, EditionDetailOut, EditionOut

router = APIRouter(tags=["editions"])
NOT_FOUND = {404: {"model": ErrorResponse}}


@router.get("/editions", response_model=Page[EditionOut])
def list_editions(params: Annotated[PageParams, Query()], service: BulletinDep):
    """Published editions, newest week first."""
    return service.list_editions(params)


@router.get("/editions/current", response_model=EditionDetailOut, responses=NOT_FOUND)
def current_edition(service: BulletinDep):
    """The published edition with the latest week, with its stories (featured first). 404 if none."""
    return service.current_edition()


@router.get("/editions/{slug}", response_model=EditionDetailOut, responses=NOT_FOUND)
def get_edition(slug: Annotated[str, Path(max_length=64)], service: BulletinDep):
    return service.edition(slug)


@router.get("/briefings/latest", response_model=BriefingOut, responses=NOT_FOUND)
def latest_briefing(service: BulletinDep):
    """Briefing for the newest public edition that has one. `audioUrl` stays null until real audio exists."""
    return service.latest_briefing()
