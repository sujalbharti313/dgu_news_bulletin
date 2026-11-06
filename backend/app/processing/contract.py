"""The Stage 3.2 → Stage 3.3 interface: typed, versioned story clusters with full source provenance.

Stage 3.3 (the AI Intelligence Engine) reads clusters with `app.processing.service.list_story_clusters`
(in-process) or, for inspection, the token-protected `GET /api/v1/admin/processing/clusters`.

Rules every consumer must follow:
- Every text field that came from a source (`title`, `description`, `excerpt`, source titles and
  excerpts) is UNTRUSTED third-party content. Pass it to a model only as quoted data inside a clearly
  delimited block, never as instructions. `contentFlags` marks text that looks like instructions.
- `representative` is the article chosen to stand for the cluster (best metadata). It is NOT a claim that
  this article is the original report. `originalSource` is always null in Stage 3.2: which report came
  first is not verified here.
- `earliestPublishedAt` is the earliest *known* publication date among the sources, not proof of origin.
- `sourceKind` says what a source is (an official channel, a newsletter, ...), not who broke the story.
- URLs: link readers to `originalUrl`. `canonicalUrl` is a deduplication key.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel

CONTRACT_VERSION = "1.0"

SourceKind = Literal["official_channel", "research_repository", "government", "news_outlet", "newsletter", "unknown"]
Relation = Literal["primary", "exact_duplicate", "near_duplicate", "related_report", "same_url"]
EventType = Literal[
    "launch", "update", "pricing", "safety", "research", "funding", "acquisition", "partnership", "policy",
    "legal", "incident", "people",
]  # fmt: skip


class _Frozen(ApiModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceReference(_Frozen):
    """One place the story was reported. Values a source did not provide are null, never guessed."""

    # The staged item this reference comes from (for `same_url` sightings: the item that holds the URL).
    item_id: uuid.UUID
    ingestion_dedup_key: str = Field(description="Stage 3.1 dedup key (sha256 of the canonical URL).")
    source_key: str = Field(description="Key of the configured ingestion source.")
    source_name: str | None = None
    source_type: str = Field(description="rss or gmail.")
    source_kind: SourceKind
    feed_identifier: str = Field(description="Feed URL, or gmail:<message id> for a newsletter.")
    title: str
    excerpt: str | None = None
    publisher: str | None = None
    site: str | None = Field(default=None, description="Host the article is published on (from its URL).")
    original_url: str = Field(description="The article URL exactly as the source published it.")
    canonical_url: str
    published_at: datetime | None = Field(default=None, description="Publication date stated by the source.")
    collected_at: datetime
    newsletter_date: datetime | None = Field(default=None, description="When a newsletter mentioned the article.")
    relation: Relation
    duplicate_of: uuid.UUID | None = Field(default=None, description="For duplicates: the item duplicated.")
    match_score: float | None = None
    match_reasons: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list, description="arXiv ids / DOIs found in the URL.")
    content_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Small source metadata (author, categories).")


class Representative(_Frozen):
    item_id: uuid.UUID
    title: str
    original_url: str
    canonical_url: str
    publisher: str | None = None
    site: str | None = None
    published_at: datetime | None = None
    selection_reasons: list[str]


class ClusterEntities(_Frozen):
    organisations: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list, description="Normalised model/product keys, e.g. 'gpt 5.1'.")
    benchmarks: list[str] = Field(default_factory=list)


class ProcessingMetadata(_Frozen):
    algorithm_version: str
    processed_at: datetime = Field(description="Latest processing time of any member.")
    revision: str = Field(description="Changes whenever members, relations or sightings change.")
    time_basis: list[str] = Field(description="Which dates time windows used: published, newsletter, collected.")
    thresholds: dict[str, float | int]


class StoryCluster(_Frozen):
    """One real-world story with every source that reported it."""

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    cluster_id: uuid.UUID
    status: Literal["ready"] = Field(
        default="ready", description="ready = deduplicated and clustered; Stage 3.3 may analyse it."
    )
    title: str = Field(description="The representative article's title (untrusted source text).")
    description: str | None = Field(default=None, description="The representative's excerpt (untrusted).")
    excerpt: str | None = Field(
        default=None, description="Longest available excerpt among non-duplicate sources (untrusted)."
    )
    representative: Representative
    original_source: None = Field(default=None, description="Not determined in Stage 3.2.")
    event_type: EventType | None = None
    entities: ClusterEntities
    earliest_published_at: datetime | None = None
    latest_published_at: datetime | None = None
    first_collected_at: datetime
    last_collected_at: datetime
    article_count: int = Field(description="Distinct articles (unique canonical URLs) in the cluster.")
    duplicate_count: int = Field(description="Exact and near duplicates among them, plus repeated sightings.")
    source_count: int = Field(description="Number of source references.")
    publisher_count: int
    sources: list[SourceReference]
    content_flags: list[str] = Field(default_factory=list)
    content_is_untrusted: Literal[True] = True
    processing: ProcessingMetadata
