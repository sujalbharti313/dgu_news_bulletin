import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.models.enums import Topic
from app.schemas.common import MAX_QUERY_LENGTH, ApiModel, Page, PageParams, Provenance


class StudentImpact(ApiModel):
    why_it_matters: str
    explore: list[str]
    build_idea: str
    disciplines: list[str]


class StoryImage(ApiModel):
    src: str
    alt: str
    credit: str


class StoryOut(ApiModel):
    """Mirrors `Story` in src/lib/types.ts. `editionId` is the edition slug, as in the frontend."""

    id: uuid.UUID
    slug: str
    provenance: Provenance
    edition_id: str
    headline: str
    dek: str
    body: list[str]
    topic: Topic
    source_name: str
    source_url: str | None = Field(description="Null until a verified original URL exists.")
    published_at: date = Field(description="Original publication date when known, otherwise our publication date.")
    original_published_at: datetime | None
    reading_minutes: int
    impact: StudentImpact
    image: StoryImage | None
    featured: bool
    tags: list[str]


StorySort = Literal["newest", "oldest", "relevance"]


class StoryQuery(PageParams):
    topic: Topic | None = None
    edition: str | None = Field(default=None, max_length=64, description="Edition slug, e.g. 2026-w43.")
    featured: bool | None = None
    q: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_LENGTH, description="Keyword search.")
    published_from: date | None = Field(default=None, alias="from")
    published_to: date | None = Field(default=None, alias="to")
    sort: StorySort | None = Field(default=None, description="Defaults to relevance with q, otherwise newest.")


class SearchQuery(PageParams):
    q: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    topic: Topic | None = None
    edition: str | None = Field(default=None, max_length=64)


class StorySearchPage(Page[StoryOut]):
    query: str
    mode: Literal["keyword"] = Field(
        default="keyword",
        description="PostgreSQL full-text keyword search. Semantic search is planned for Phase 3.",
    )
