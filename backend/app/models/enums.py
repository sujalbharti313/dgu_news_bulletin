"""Controlled vocabularies. Values match the frontend's TypeScript unions in src/lib/types.ts."""

from enum import StrEnum


class Topic(StrEnum):
    MODELS = "models"
    RESEARCH = "research"
    TOOLS = "tools"
    POLICY = "policy"
    INDIA = "india"
    CAREERS = "careers"
    CAMPUS = "campus"


class ContentStatus(StrEnum):
    """Editorial lifecycle for stories. Only PUBLISHED is ever served publicly."""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class PublicationStatus(StrEnum):
    """Lifecycle for editions, briefings and reference records."""

    DRAFT = "draft"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


class SourceKind(StrEnum):
    RSS = "rss"
    OFFICIAL_BLOG = "official_blog"
    NEWSLETTER = "newsletter"
    RESEARCH_REPOSITORY = "research_repository"
    NEWS_SITE = "news_site"
    GOVERNMENT = "government"
    DEMO = "demo"


class ToolCategory(StrEnum):
    CODING = "coding"
    AGENTS = "agents"
    MEDIA = "media"
    RESEARCH = "research"
    OPEN_MODELS = "open-models"
    INFRASTRUCTURE = "infrastructure"


class SummaryOrigin(StrEnum):
    """Who wrote a research summary. Kept separate from the paper's own findings."""

    EDITORIAL = "editorial"
    AI_ASSISTED = "ai_assisted"


class IndiaEntryKind(StrEnum):
    GOVERNMENT_PROGRAMME = "government_programme"
    RESEARCH_LAB = "research_lab"
    STARTUP = "startup"
    PUBLIC_PLATFORM = "public_platform"


INDIA_KIND_LABELS = {
    IndiaEntryKind.GOVERNMENT_PROGRAMME: "Government programme",
    IndiaEntryKind.RESEARCH_LAB: "Research lab",
    IndiaEntryKind.STARTUP: "Startup",
    IndiaEntryKind.PUBLIC_PLATFORM: "Public platform",
}


class CareerSignalKind(StrEnum):
    SKILL = "skill"
    PROJECT = "project"
    OPEN_SOURCE = "open-source"


class CampusItemKind(StrEnum):
    PROJECT = "project"
    WORKSHOP = "workshop"
    SHOWCASE = "showcase"
    HACKATHON = "hackathon"
    CLUB = "club"


class IngestionJobType(StrEnum):
    """SOURCE_FETCH (RSS/Atom) and NEWSLETTER_IMPORT (Gmail) are used by Phase 3.1, STORY_PROCESSING by
    Phase 3.2, SUMMARISATION by Phase 3.3 (one AI intelligence run); VERIFICATION is reserved."""

    SOURCE_FETCH = "source_fetch"
    NEWSLETTER_IMPORT = "newsletter_import"
    # Phase 3.2: one run of normalisation, deduplication and story clustering over staged items.
    STORY_PROCESSING = "story_processing"
    SUMMARISATION = "summarisation"
    VERIFICATION = "verification"


class IngestionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # The source was deliberately not collected (disabled, not configured, or already running elsewhere).
    SKIPPED = "skipped"


class IngestionSourceType(StrEnum):
    RSS = "rss"
    GMAIL = "gmail"


class IngestionItemStatus(StrEnum):
    """Hand-off state of a staged item. Stage 3.1 only ever writes PENDING; later stages move it on."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    DISCARDED = "discarded"
    FAILED = "failed"


class DedupRelation(StrEnum):
    """How a processed staged item relates to its story cluster (Phase 3.2)."""

    # The first article of a story cluster.
    PRIMARY = "primary"
    # The same article: same arXiv id / DOI, or identical normalised title and excerpt under another URL.
    EXACT_DUPLICATE = "exact_duplicate"
    # A lightly modified copy of an article already in the cluster (syndication, edited headline).
    NEAR_DUPLICATE = "near_duplicate"
    # A different article reporting the same event.
    RELATED_REPORT = "related_report"


class IntelligenceStatus(StrEnum):
    """Per-story state of the Phase 3.3 AI intelligence engine (one row per story cluster)."""

    # Valid, schema-checked analysis with no blocking review flag. Stage 3.4 may use it.
    ANALYSED = "analysed"
    # Valid analysis that a human must look at first (flagged source content, thin evidence, warnings).
    NEEDS_REVIEW = "needs_review"
    # Not sent to the model: the cluster has too little source text to analyse responsibly.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    # The model's output still failed validation after the repair attempts. Nothing is stored; retried later.
    INVALID_OUTPUT = "invalid_output"
    # Provider error (timeout, rate limit, outage). Nothing is stored; retried later.
    FAILED = "failed"
