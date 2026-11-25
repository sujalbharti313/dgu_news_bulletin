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
    Phase 3.2, SUMMARISATION by Phase 3.3 (one AI analysis run), VERIFICATION by Stage 3.4 (one verification
    and ranking run), EDITION_GENERATION by Stage 3.5 (one weekly edition generation attempt),
    CLEANUP by Stage 3.6 (one data-retention pass)."""

    SOURCE_FETCH = "source_fetch"
    NEWSLETTER_IMPORT = "newsletter_import"
    # Phase 3.2: one run of normalisation, deduplication and story clustering over staged items.
    STORY_PROCESSING = "story_processing"
    SUMMARISATION = "summarisation"
    VERIFICATION = "verification"
    # Stage 3.5: one scheduled (or admin-triggered) weekly edition generation attempt.
    EDITION_GENERATION = "edition_generation"
    # Stage 3.6: one cleanup pass (the run lock of the retention engine; no news content).
    CLEANUP = "cleanup"


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


class AnalysisStatus(StrEnum):
    """Per-story state of the Phase 3.3 AI intelligence engine (one row per story cluster)."""

    # Valid, evidence-checked analysis with no blocking review flag. Stage 3.4 may use it.
    COMPLETED = "completed"
    # Valid analysis that a human must look at first (flagged source content, instruction-like text).
    NEEDS_REVIEW = "needs_review"
    # The sources are too thin: either not sent to the model at all (no analysis stored), or the model said the
    # evidence is insufficient (analysis stored, with low priority and the missing information listed).
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    # The model's output still failed validation after the repair attempts. Nothing stored; retried later.
    INVALID_OUTPUT = "invalid_output"
    # Gemini error (timeout, outage, persistent rate limit). Nothing stored; retried later.
    FAILED = "failed"
    # Not analysed because the free quota (Google's, or the configured daily limit) ran out. Not counted as an
    # attempt; picked up again automatically after the quota resets.
    QUOTA_LIMITED = "quota_limited"


class VerificationStatus(StrEnum):
    """Per-story state of the Stage 3.4 engine (one `story_verifications` row per cluster)."""

    # Embedded, evaluated by Gemini, scored and gated: has a final score and an editorial eligibility.
    COMPLETED = "completed"
    # Technical hold-ups, never editorial decisions (eligibility `pending`). Retried on a later run.
    PENDING_EMBEDDING = "pending_embedding"
    PENDING_EVALUATION = "pending_evaluation"
    EMBEDDING_FAILED = "embedding_failed"
    EVALUATION_FAILED = "evaluation_failed"
    # Stage 3.3 produced no usable analysis (too little source text); kept as ineligible for inspection.
    INSUFFICIENT_INPUT = "insufficient_input"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    REVIEW_REQUIRED = "review_required"
    INELIGIBLE = "ineligible"
    # Not yet decided because of a technical hold-up (see VerificationStatus); never shortlisted meanwhile.
    PENDING = "pending"


class EmbeddingStatus(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    QUOTA_LIMITED = "quota_limited"
    FAILED = "failed"
    INVALID_OUTPUT = "invalid_output"
    NOT_APPLICABLE = "not_applicable"


class RankingRunStatus(StrEnum):
    # Every story in the window was evaluated; the shortlist is final for this configuration.
    COMPLETED = "completed"
    # Some stories are still pending (not analysed, analysis out of date, embedding or evaluation waiting).
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class EditionStatus(StrEnum):
    """Lifecycle of a weekly edition (Stage 3.5). Only PUBLISHED is ever served publicly.

    Transitions are defined and enforced in app.editions.states; the database adds CHECKs for the invariants
    (an approved edition records its approved revision, a published one its published revision)."""

    GENERATING = "generating"
    DRAFT = "draft"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    PUBLISHED = "published"
    GENERATION_FAILED = "generation_failed"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    # Kept for editions created before Stage 3.5 (the original Phase 2 vocabulary). Not reachable from 3.5.
    WITHDRAWN = "withdrawn"


class EditionAuditAction(StrEnum):
    GENERATION_STARTED = "generation_started"
    GENERATED = "generated"
    GENERATION_FAILED = "generation_failed"
    GENERATION_DEFERRED = "generation_deferred"
    OPENED_FOR_REVIEW = "opened_for_review"
    REVISION_CREATED = "revision_created"
    STORY_ADDED = "story_added"
    STORY_REMOVED = "story_removed"
    STORY_REORDERED = "story_reordered"
    CONTENT_EDITED = "content_edited"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    APPROVAL_INVALIDATED = "approval_invalidated"
    APPROVAL_REJECTED = "approval_rejected"
    REJECTED = "rejected"
    RETURNED_FOR_REVISION = "returned_for_revision"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    REGENERATED = "regenerated"
    RETRY_TRIGGERED = "retry_triggered"
    ARCHIVED = "archived"


class CleanupJobKind(StrEnum):
    """What a Stage 3.6 cleanup job covers."""

    # Unselected working data of one week whose edition is confirmed published.
    PUBLISHED_WEEK = "published_week"
    # Expired working data that no week, edition or pipeline stage can use any more (never ranked, or ranked for a
    # week before the first edition week).
    EXPIRED_ORPHANS = "expired_orphans"


class CleanupJobStatus(StrEnum):
    """Lifecycle of a Stage 3.6 cleanup job (app.cleanup.service).

    scheduled → staged → running → completed
    scheduled/staged/running → blocked (publication no longer verifiable; re-checked every run)
    running → failed (retryable, with backoff) → staged
    scheduled/staged/blocked/failed → cancelled (admin; can be reopened by an admin)"""

    SCHEDULED = "scheduled"
    BLOCKED = "blocked"
    STAGED = "staged"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CleanupTarget(StrEnum):
    # A Stage 3.2 story cluster's working data: its Stage 3.4 verification, its embedding and its Stage 3.3
    # analysis, deleted together.
    STORY_CLUSTER = "story_cluster"
    # A superseded Stage 3.4 ranking run of a published week.
    RANKING_RUN = "ranking_run"
    # A Stage 3.4 verification row whose Stage 3.3 analysis no longer exists.
    ORPHAN_VERIFICATION = "orphan_verification"


class ManifestState(StrEnum):
    PENDING = "pending"
    DELETED = "deleted"
    PROTECTED = "protected"
    SKIPPED = "skipped"
    FAILED = "failed"


class CleanupAuditAction(StrEnum):
    JOB_CREATED = "job_created"
    PUBLICATION_VERIFIED = "publication_verified"
    PUBLICATION_CHECK_FAILED = "publication_check_failed"
    MANIFEST_STAGED = "manifest_staged"
    DEFERRED = "deferred"
    DELETION_STARTED = "deletion_started"
    BATCH_DELETED = "batch_deleted"
    RECORDS_SKIPPED = "records_skipped"
    DELETION_COMPLETED = "deletion_completed"
    DELETION_FAILED = "deletion_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_REQUESTED = "retry_requested"
    JOB_CANCELLED = "job_cancelled"
    JOB_REOPENED = "job_reopened"
    MANUAL_TRIGGER = "manual_trigger"
