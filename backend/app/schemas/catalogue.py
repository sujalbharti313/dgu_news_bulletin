"""Tools, research papers and the smaller home-page collections."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.models.enums import CareerSignalKind, ToolCategory, Topic
from app.schemas.common import MAX_QUERY_LENGTH, ApiModel, PageParams, Provenance


class ToolOut(ApiModel):
    id: str
    name: str
    maker: str
    category: ToolCategory
    summary: str
    good_for: list[str]
    limitations: str
    access: str
    official_url: str
    pricing_notes: str | None = Field(description="Only set after checking the official pricing page.")
    last_verified_at: datetime | None


class ToolQuery(PageParams):
    category: ToolCategory | None = None
    q: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_LENGTH)


class ResearchPaperOut(ApiModel):
    id: str
    title: str
    authors: str
    organisation: str
    published: date
    plain_language: str = Field(description="Our plain-language interpretation, not the authors' words.")
    summary_origin: Literal["editorial", "ai_assisted"]
    demonstrated: str = Field(description="What the paper itself reports.")
    limitations: str
    url: str
    doi: str | None
    topic: Topic | None


class ResearchQuery(PageParams):
    topic: Topic | None = None
    q: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_LENGTH)
    sort: Literal["newest", "oldest"] = "newest"


IndiaKindLabel = Literal["Government programme", "Research lab", "Startup", "Public platform"]


class IndiaEntryOut(ApiModel):
    id: str
    name: str
    kind: IndiaKindLabel
    focus: str
    description: str
    url: str


class CareerSignalOut(ApiModel):
    id: str
    kind: CareerSignalKind
    title: str
    description: str
    start_with: str
    related_topics: list[Topic]


class CareerSignalQuery(PageParams):
    kind: CareerSignalKind | None = None
    topic: Topic | None = None


CampusKindLabel = Literal["Project", "Workshop", "Showcase", "Hackathon", "Club"]


class CampusItemOut(ApiModel):
    id: str
    provenance: Provenance
    kind: CampusKindLabel
    title: str
    description: str
    when: str | None
