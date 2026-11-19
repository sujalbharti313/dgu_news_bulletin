import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check
from app.models.enums import EligibilityStatus, EmbeddingStatus, RankingRunStatus, VerificationStatus

# The pgvector column size fixed by migration 0006. EMBEDDING_DIMENSIONS must equal it; `check_schema` compares
# the setting with the live column before any run, so a mismatch stops the run instead of failing on insert.
EMBEDDING_COLUMN_DIMENSIONS = 768

SCORE_COLUMNS = (
    "source_credibility_score",
    "evidence_quality_score",
    "claim_consistency_score",
    "technical_significance_score",
    "global_impact_score",
    "student_relevance_score",
    "timeliness_score",
)


class Vector(UserDefinedType):
    """pgvector `vector(n)`. Values go in and out as pgvector's text form ('[0.1,0.2,...]'); similarity search
    runs in the database (`match_story_embeddings`), never by loading vectors into the application."""

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None or isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None or not isinstance(value, str):
                return value
            return [float(x) for x in value.strip("[]").split(",") if x]

        return process


class StoryEmbedding(UUIDPrimaryKey, Timestamps, Base):
    """The Gemini embedding of one analysed story (Stage 3.2 cluster), for semantic retrieval in Stage 3.4.

    Linked to the story's Stage 3.3 analysis row (`story_analyses.cluster_id`, ON DELETE CASCADE): an embedding
    never outlives its story. One row per cluster; `content_hash` (model, dimension, input format version and
    text) decides whether it must be regenerated. The embedded text is not stored, only its length. Nothing here
    is public: RLS is on with no policies and the search function is not executable by the API roles."""

    __tablename__ = "story_embeddings"
    __table_args__ = (
        CheckConstraint(f"dimensions = {EMBEDDING_COLUMN_DIMENSIONS}", name="dimensions_match_column"),
        CheckConstraint("input_chars > 0", name="input_chars_positive"),
        CheckConstraint("first_collected_at <= last_collected_at", name="collected_range_ordered"),
        Index("ix_story_embeddings_collected", "last_collected_at", "first_collected_at"),
    )

    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("story_analyses.cluster_id", ondelete="CASCADE"), unique=True
    )
    cluster_revision: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_COLUMN_DIMENSIONS))
    model: Mapped[str] = mapped_column(String(120))
    dimensions: Mapped[int] = mapped_column(SmallInteger)
    embedding_version: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    input_chars: Mapped[int] = mapped_column(Integer)
    # Collection range of the cluster, so retrieval can be limited to the edition week plus a lookback.
    first_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RankingRun(UUIDPrimaryKey, Timestamps, Base):
    """One Stage 3.4 ranking of one edition week (Monday-Sunday, Asia/Kolkata).

    `run_key` is a hash of the edition, both configuration versions and every story's input key, so running
    again on unchanged inputs updates the same row instead of creating a duplicate. `result` holds the full
    `RankingResult` contract (scores, verification, explanations, shortlist); no article text. Nothing here is
    public, and a run is never a publication decision."""

    __tablename__ = "ranking_runs"
    __table_args__ = (
        enum_check("status", RankingRunStatus),
        CheckConstraint("edition_end = edition_start + 6", name="edition_is_one_week"),
        CheckConstraint(
            "total_evaluated >= 0 AND total_eligible >= 0 AND total_review_required >= 0 AND total_ineligible >= 0 "
            "AND total_pending >= 0 AND total_failed >= 0",
            name="totals_non_negative",
        ),
        Index("ix_ranking_runs_edition_processed", "edition_start", "processed_at"),
    )

    run_key: Mapped[str] = mapped_column(String(64), unique=True)
    edition_start: Mapped[date] = mapped_column(Date)
    edition_end: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    status_reason: Mapped[str | None] = mapped_column(Text)
    ranking_config_version: Mapped[str] = mapped_column(String(64))
    verification_config_version: Mapped[str] = mapped_column(String(128))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_evaluated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_eligible: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_review_required: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_ineligible: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_pending: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    shortlist_cluster_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    result: Mapped[dict] = mapped_column(JSONB)
    errors: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )


class StoryVerificationRecord(UUIDPrimaryKey, Timestamps, Base):
    """Stage 3.4 verification and final scores of one Stage 3.2 cluster (latest evaluation), plus the cached
    Gemini editorial evaluation and the embedding/evaluation retry state.

    Signals Stage 3.5 filters on are columns; the full `Verification`, `FinalScores` and evaluation are JSON.
    Source *metadata* only (publishers, URLs, flags); article text stays in ingestion_items. `eligibility` is
    `pending` for a technical hold-up (embedding or evaluation not done yet), never for an editorial decision.
    Working data with `expires_at`, like story_analyses. Nothing here is public."""

    __tablename__ = "story_verifications"
    __table_args__ = (
        enum_check("verification_status", VerificationStatus),
        enum_check("eligibility", EligibilityStatus),
        enum_check("embedding_status", EmbeddingStatus),
        *(CheckConstraint(f"{c} IS NULL OR ({c} BETWEEN 0 AND 100)", name=f"{c}_range") for c in SCORE_COLUMNS),
        CheckConstraint(
            "final_priority_score IS NULL OR (final_priority_score BETWEEN 0 AND 100)", name="final_score_range"
        ),
        CheckConstraint(
            "priority_classification IS NULL OR priority_classification IN "
            "('Exceptional', 'High', 'Medium', 'Low', 'Very Low')",
            name="priority_classification_valid",
        ),
        CheckConstraint("evaluation_attempts >= 0 AND embedding_attempts >= 0", name="attempts_non_negative"),
        # A final score exists exactly when the story was fully evaluated.
        CheckConstraint(
            "(verification_status = 'completed') = (final_priority_score IS NOT NULL)",
            name="final_score_iff_completed",
        ),
        CheckConstraint(
            "(eligibility = 'pending') = (verification_status IN "
            "('pending_embedding', 'pending_evaluation', 'embedding_failed', 'evaluation_failed'))",
            name="pending_iff_technical_hold",
        ),
        Index("ix_story_verifications_eligibility_score", "eligibility", "final_priority_score"),
        Index("ix_story_verifications_edition", "edition_start"),
    )

    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    cluster_revision: Mapped[str] = mapped_column(String(64))
    analysis_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Hash of everything the result depended on (analysis, related stories, evaluation, configuration, week).
    input_key: Mapped[str] = mapped_column(String(64))
    ranking_config_version: Mapped[str] = mapped_column(String(64))
    verification_config_version: Mapped[str] = mapped_column(String(128))
    edition_start: Mapped[date] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(24))
    eligibility: Mapped[str] = mapped_column(String(24))
    source_credibility_score: Mapped[int | None] = mapped_column(SmallInteger)
    source_credibility_level: Mapped[str | None] = mapped_column(String(16))
    evidence_quality_score: Mapped[int | None] = mapped_column(SmallInteger)
    claim_consistency_score: Mapped[int | None] = mapped_column(SmallInteger)
    technical_significance_score: Mapped[int | None] = mapped_column(SmallInteger)
    global_impact_score: Mapped[int | None] = mapped_column(SmallInteger)
    student_relevance_score: Mapped[int | None] = mapped_column(SmallInteger)
    timeliness_score: Mapped[int | None] = mapped_column(SmallInteger)
    corroboration_level: Mapped[str | None] = mapped_column(String(32))
    source_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    independent_source_count: Mapped[int | None] = mapped_column(Integer)
    final_priority_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    priority_classification: Mapped[str | None] = mapped_column(String(16))
    dimension_scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    verification: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Retrieved related stories: [{clusterId, similarity, relation, inWindow}] (ids and numbers only).
    related: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    review_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    ranking_explanation: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(Integer)
    shortlist_rank: Mapped[int | None] = mapped_column(Integer)
    # Embedding state (the vector itself is in story_embeddings).
    embedding_status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    embedding_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedding_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Gemini editorial evaluation cache: the input key it was made for, its validated result, and retry state.
    evaluation_status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    evaluation_key: Mapped[str | None] = mapped_column(String(64))
    evaluation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    evaluation_model: Mapped[str | None] = mapped_column(String(120))
    evaluation_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    evaluation_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    errors: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    ranking_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ranking_runs.id", ondelete="SET NULL"), index=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
