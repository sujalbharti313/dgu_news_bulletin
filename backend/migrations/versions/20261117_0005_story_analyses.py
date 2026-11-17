"""Phase 3.3 AI intelligence engine: `story_analyses` (one row per Stage 3.2 cluster) and `ai_usage_daily`.

Additive only. Nothing existing is altered, and no source record is copied or deleted:
- story_analyses: status and attempt bookkeeping, model id, category, preliminary priority score and level,
  token usage, the validated analysis JSON (generated text plus source *metadata*, never article excerpts),
  errors and review flags, `expires_at` (working data for Stage 3.4, not an archive).
- ai_usage_daily: Gemini requests and tokens per model per quota day (Pacific time), and when the daily free
  quota was reported as used up. Counts only.
- The run lock reuses ingestion_jobs with job_type 'summarisation' (already allowed since 0001) and source_key
  'stage-3.3-analysis'.
- updated_at triggers and row-level security with no policies, like every other table, so the Supabase Data
  API (anon/authenticated keys) cannot read or write either table. Only the backend's database role can.

Works whether or not the rolled-back table `story_intelligence` still exists: it is not touched.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 12:30:00+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'completed', 'needs_review', 'insufficient_evidence', 'invalid_output', 'failed', 'quota_limited'"


def upgrade() -> None:
    op.create_table(
        "story_analyses",
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("cluster_revision", sa.String(length=64), nullable=False),
        sa.Column("engine_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_id", sa.String(length=120), nullable=True),
        sa.Column("primary_category", sa.String(length=48), nullable=True),
        sa.Column("priority_score", sa.SmallInteger(), nullable=True),
        sa.Column("priority_level", sa.String(length=16), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("review_flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_story_analyses_status_valid")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_story_analyses_attempt_count_non_negative")),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name=op.f("ck_story_analyses_tokens_non_negative")
        ),
        sa.CheckConstraint(
            "priority_score IS NULL OR (priority_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_analyses_priority_score_range"),
        ),
        sa.CheckConstraint(
            "priority_level IS NULL OR priority_level IN ('Critical', 'High', 'Medium', 'Low')",
            name=op.f("ck_story_analyses_priority_level_valid"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'needs_review') AND analysis IS NOT NULL) "
            "OR status = 'insufficient_evidence' "
            "OR (status IN ('invalid_output', 'failed', 'quota_limited') AND analysis IS NULL)",
            name=op.f("ck_story_analyses_analysis_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingestion_jobs.id"],
            name=op.f("fk_story_analyses_job_id_ingestion_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_analyses")),
        sa.UniqueConstraint("cluster_id", name=op.f("uq_story_analyses_cluster_id")),
    )
    op.create_index(op.f("ix_story_analyses_expires_at"), "story_analyses", ["expires_at"], unique=False)
    op.create_index(op.f("ix_story_analyses_job_id"), "story_analyses", ["job_id"], unique=False)
    op.create_index("ix_story_analyses_status_updated", "story_analyses", ["status", "updated_at"], unique=False)
    op.create_index("ix_story_analyses_priority", "story_analyses", ["priority_score"], unique=False)

    op.create_table(
        "ai_usage_daily",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("rate_limited", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quota_exhausted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "requests >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND rate_limited >= 0 AND failed_requests >= 0",
            name=op.f("ck_ai_usage_daily_counts_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage_daily")),
        sa.UniqueConstraint(
            "usage_date", "provider", "model", name=op.f("uq_ai_usage_daily_usage_date_provider_model")
        ),
    )
    for table in ("story_analyses", "ai_usage_daily"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Drops both tables and this stage's run records. Local development only."""
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'summarisation' AND source_key = 'stage-3.3-analysis'")
    op.drop_table("ai_usage_daily")
    op.drop_index("ix_story_analyses_priority", table_name="story_analyses")
    op.drop_index("ix_story_analyses_status_updated", table_name="story_analyses")
    op.drop_index(op.f("ix_story_analyses_job_id"), table_name="story_analyses")
    op.drop_index(op.f("ix_story_analyses_expires_at"), table_name="story_analyses")
    op.drop_table("story_analyses")
