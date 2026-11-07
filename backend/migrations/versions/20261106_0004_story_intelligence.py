"""Phase 3.3 AI intelligence: one working table, `story_intelligence`, keyed by Stage 3.2 cluster id.

Additive only. Nothing existing is altered: the run lock reuses ingestion_jobs with the job_type
'summarisation', which migration 0001 already allows.

- One row per story cluster: status, attempt bookkeeping, token usage and estimated cost, the validated
  intelligence JSON (generated text and source metadata only, never article excerpts) and review flags.
- `expires_at` on every row: this is working data for review and later stages, not an archive.
- An updated_at trigger and row-level security with no policies, like every other table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06 14:00:00+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = "'analysed', 'needs_review', 'insufficient_evidence', 'invalid_output', 'failed'"


def upgrade() -> None:
    op.create_table(
        "story_intelligence",
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("cluster_revision", sa.String(length=64), nullable=False),
        sa.Column("engine_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("intelligence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("review_flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_story_intelligence_status_valid")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_story_intelligence_attempt_count_non_negative")),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name=op.f("ck_story_intelligence_tokens_non_negative")
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name=op.f("ck_story_intelligence_cost_non_negative"),
        ),
        sa.CheckConstraint(
            "(status IN ('analysed', 'needs_review')) = (intelligence IS NOT NULL)",
            name=op.f("ck_story_intelligence_intelligence_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["ingestion_jobs.id"], name=op.f("fk_story_intelligence_job_id_ingestion_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_story_intelligence")),
        sa.UniqueConstraint("cluster_id", name=op.f("uq_story_intelligence_cluster_id")),
    )
    op.create_index(op.f("ix_story_intelligence_expires_at"), "story_intelligence", ["expires_at"], unique=False)
    op.create_index(op.f("ix_story_intelligence_job_id"), "story_intelligence", ["job_id"], unique=False)
    op.create_index(
        "ix_story_intelligence_status_updated", "story_intelligence", ["status", "updated_at"], unique=False
    )
    op.execute(
        "CREATE TRIGGER trg_story_intelligence_updated_at BEFORE UPDATE ON story_intelligence "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute("ALTER TABLE story_intelligence ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Drops the intelligence table and the run records. Local development only."""
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'summarisation'")
    op.drop_index("ix_story_intelligence_status_updated", table_name="story_intelligence")
    op.drop_index(op.f("ix_story_intelligence_job_id"), table_name="story_intelligence")
    op.drop_index(op.f("ix_story_intelligence_expires_at"), table_name="story_intelligence")
    op.drop_table("story_intelligence")
