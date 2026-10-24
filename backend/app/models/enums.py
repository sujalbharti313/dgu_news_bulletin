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
    """Reserved for Phase 3. No job runner exists yet."""

    SOURCE_FETCH = "source_fetch"
    NEWSLETTER_IMPORT = "newsletter_import"
    SUMMARISATION = "summarisation"
    VERIFICATION = "verification"


class IngestionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
