"""Stage 3.4 verification and ranking engine: `story_verifications` (one row per Stage 3.2 cluster) and
`ranking_runs` (one row per edition ranking).

Additive only. Nothing existing is altered, and no source record is copied or deleted:
- story_verifications: verification signals (credibility, evidence, claim consistency, corroboration, technical
  substance, hype flags), the six final dimension scores and the final priority score, eligibility, review
  flags, the ranking explanation and rank, configuration versions, the cached Gemini consistency check with its
  retry state, failure information and `expires_at`. Source metadata only, never article text.
- ranking_runs: the edition week, status (completed / incomplete / failed), totals, configuration versions, the
  shortlist ids and the full RankingResult JSON. `run_key` makes repeated runs on unchanged inputs idempotent.
- The run lock reuses ingestion_jobs with job_type 'verification' (allowed since 0001) and source_key
  'stage-3.4-ranking'.
- updated_at triggers and row-level security with no policies, like every other table, so the Supabase Data
  API (anon/authenticated keys) cannot read or write either table. Only the backend's database role can.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19 04:11:07+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ranking_runs",
        sa.Column("run_key", sa.String(length=64), nullable=False),
        sa.Column("edition_start", sa.Date(), nullable=False),
        sa.Column("edition_end", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("ranking_config_version", sa.String(length=64), nullable=False),
        sa.Column("verification_config_version", sa.String(length=128), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_evaluated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_eligible", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_review_required", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_ineligible", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_pending", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("shortlist_cluster_ids", postgresql.ARRAY(sa.UUID()), server_default="{}", nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'incomplete', 'failed')", name=op.f("ck_ranking_runs_status_valid")
        ),
        sa.CheckConstraint("edition_end = edition_start + 6", name=op.f("ck_ranking_runs_edition_is_one_week")),
        sa.CheckConstraint(
            "total_evaluated >= 0 AND total_eligible >= 0 AND total_review_required >= 0 AND total_ineligible >= 0 AND total_pending >= 0 AND total_failed >= 0",
            name=op.f("ck_ranking_runs_totals_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["ingestion_jobs.id"], name=op.f("fk_ranking_runs_job_id_ingestion_jobs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_runs")),
        sa.UniqueConstraint("run_key", name=op.f("uq_ranking_runs_run_key")),
    )
    op.create_index(
        "ix_ranking_runs_edition_processed", "ranking_runs", ["edition_start", "processed_at"], unique=False
    )
    op.create_index(op.f("ix_ranking_runs_job_id"), "ranking_runs", ["job_id"], unique=False)
    op.create_table(
        "story_verifications",
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("cluster_revision", sa.String(length=64), nullable=False),
        sa.Column("analysis_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_key", sa.String(length=64), nullable=False),
        sa.Column("ranking_config_version", sa.String(length=64), nullable=False),
        sa.Column("verification_config_version", sa.String(length=128), nullable=False),
        sa.Column("edition_start", sa.Date(), nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("eligibility", sa.String(length=24), nullable=False),
        sa.Column("source_credibility_score", sa.SmallInteger(), nullable=True),
        sa.Column("source_credibility_level", sa.String(length=16), nullable=True),
        sa.Column("evidence_quality_score", sa.SmallInteger(), nullable=True),
        sa.Column("claim_consistency_score", sa.SmallInteger(), nullable=True),
        sa.Column("technical_substance_score", sa.SmallInteger(), nullable=True),
        sa.Column("corroboration_level", sa.String(length=32), nullable=True),
        sa.Column("source_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("independent_source_count", sa.Integer(), nullable=True),
        sa.Column("final_priority_score", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("priority_classification", sa.String(length=16), nullable=True),
        sa.Column("dimension_scores", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("verification", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("review_flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("ranking_explanation", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("shortlist_rank", sa.Integer(), nullable=True),
        sa.Column("ai_check_status", sa.String(length=24), server_default="disabled", nullable=False),
        sa.Column("ai_check_key", sa.String(length=64), nullable=True),
        sa.Column("ai_check", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("ai_check_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ai_check_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("errors", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("ranking_run_id", sa.UUID(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "eligibility IN ('eligible', 'review_required', 'ineligible')",
            name=op.f("ck_story_verifications_eligibility_valid"),
        ),
        sa.CheckConstraint(
            "priority_classification IS NULL OR priority_classification IN ('Exceptional', 'High', 'Medium', 'Low', 'Very Low')",
            name=op.f("ck_story_verifications_priority_classification_valid"),
        ),
        sa.CheckConstraint(
            "verification_status IN ('completed', 'pending_ai_check', 'ai_check_failed', 'insufficient_input')",
            name=op.f("ck_story_verifications_verification_status_valid"),
        ),
        sa.CheckConstraint(
            "ai_check_attempts >= 0", name=op.f("ck_story_verifications_ai_check_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "claim_consistency_score IS NULL OR (claim_consistency_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_verifications_claim_consistency_score_range"),
        ),
        sa.CheckConstraint(
            "evidence_quality_score IS NULL OR (evidence_quality_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_verifications_evidence_quality_score_range"),
        ),
        sa.CheckConstraint(
            "final_priority_score IS NULL OR (final_priority_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_verifications_final_score_range"),
        ),
        sa.CheckConstraint(
            "source_credibility_score IS NULL OR (source_credibility_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_verifications_source_credibility_score_range"),
        ),
        sa.CheckConstraint(
            "technical_substance_score IS NULL OR (technical_substance_score BETWEEN 0 AND 100)",
            name=op.f("ck_story_verifications_technical_substance_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["ranking_run_id"],
            ["ranking_runs.id"],
            name=op.f("fk_story_verifications_ranking_run_id_ranking_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_verifications")),
        sa.UniqueConstraint("cluster_id", name=op.f("uq_story_verifications_cluster_id")),
    )
    op.create_index("ix_story_verifications_edition", "story_verifications", ["edition_start"], unique=False)
    op.create_index(
        "ix_story_verifications_eligibility_score",
        "story_verifications",
        ["eligibility", "final_priority_score"],
        unique=False,
    )
    op.create_index(op.f("ix_story_verifications_expires_at"), "story_verifications", ["expires_at"], unique=False)
    op.create_index(
        op.f("ix_story_verifications_ranking_run_id"), "story_verifications", ["ranking_run_id"], unique=False
    )
    for table in ("ranking_runs", "story_verifications"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Drops both tables and this stage's run records. Local development only."""
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'verification' AND source_key = 'stage-3.4-ranking'")
    op.drop_index(op.f("ix_story_verifications_ranking_run_id"), table_name="story_verifications")
    op.drop_index(op.f("ix_story_verifications_expires_at"), table_name="story_verifications")
    op.drop_index("ix_story_verifications_eligibility_score", table_name="story_verifications")
    op.drop_index("ix_story_verifications_edition", table_name="story_verifications")
    op.drop_table("story_verifications")
    op.drop_index(op.f("ix_ranking_runs_job_id"), table_name="ranking_runs")
    op.drop_index("ix_ranking_runs_edition_processed", table_name="ranking_runs")
    op.drop_table("ranking_runs")
