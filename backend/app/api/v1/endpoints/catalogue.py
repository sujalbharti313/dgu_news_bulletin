from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import CatalogueDep
from app.schemas.catalogue import (
    CampusItemOut,
    CareerSignalOut,
    CareerSignalQuery,
    IndiaEntryOut,
    ResearchPaperOut,
    ResearchQuery,
    ToolOut,
    ToolQuery,
)
from app.schemas.common import ErrorResponse, Page, PageParams

router = APIRouter()
NOT_FOUND = {404: {"model": ErrorResponse}}


@router.get("/tools", response_model=Page[ToolOut], tags=["tools"])
def list_tools(params: Annotated[ToolQuery, Query()], service: CatalogueDep):
    return service.tools(params)


@router.get("/tools/{slug}", response_model=ToolOut, responses=NOT_FOUND, tags=["tools"])
def get_tool(slug: Annotated[str, Path(max_length=120)], service: CatalogueDep):
    return service.tool(slug)


@router.get("/research", response_model=Page[ResearchPaperOut], tags=["research"])
def list_research(params: Annotated[ResearchQuery, Query()], service: CatalogueDep):
    return service.papers(params)


@router.get("/research/{id_or_slug}", response_model=ResearchPaperOut, responses=NOT_FOUND, tags=["research"])
def get_research(id_or_slug: Annotated[str, Path(max_length=120)], service: CatalogueDep):
    return service.paper(id_or_slug)


@router.get("/india", response_model=Page[IndiaEntryOut], tags=["reference"])
def list_india_entries(params: Annotated[PageParams, Query()], service: CatalogueDep):
    """Organisations and programmes in India's AI ecosystem."""
    return service.india_entries(params)


@router.get("/career-signals", response_model=Page[CareerSignalOut], tags=["reference"])
def list_career_signals(params: Annotated[CareerSignalQuery, Query()], service: CatalogueDep):
    """General learning suggestions. Not job listings."""
    return service.career_signals(params)


@router.get("/campus", response_model=Page[CampusItemOut], tags=["reference"])
def list_campus_items(params: Annotated[PageParams, Query()], service: CatalogueDep):
    """Approved campus listings. Demo formats are only visible when demo content is enabled."""
    return service.campus_items(params)
