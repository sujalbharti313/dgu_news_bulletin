from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import BulletinDep
from app.schemas.common import ErrorResponse, Page
from app.schemas.story import SearchQuery, StoryOut, StoryQuery, StorySearchPage

router = APIRouter(tags=["stories"])
NOT_FOUND = {404: {"model": ErrorResponse}}


@router.get("/stories", response_model=Page[StoryOut])
def list_stories(params: Annotated[StoryQuery, Query()], service: BulletinDep):
    """Published stories. Filter by topic, edition slug, featured flag, date range, or keywords (`q`)."""
    return service.list_stories(params)


@router.get("/stories/{story_id_or_slug}", response_model=StoryOut, responses=NOT_FOUND)
def get_story(story_id_or_slug: Annotated[str, Path(max_length=160)], service: BulletinDep):
    return service.get_story(story_id_or_slug)


@router.get("/search", response_model=StorySearchPage)
def search(params: Annotated[SearchQuery, Query()], service: BulletinDep):
    """Keyword search (PostgreSQL full text) over story titles, summaries and bodies. Not semantic search."""
    return service.search(params)
