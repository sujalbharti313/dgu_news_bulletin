"""Phase 3.2 processing: deduplication and story-cluster columns on the temporary staging table.

Additive only, and no new table. Existing rows keep every value; the new columns start empty.

- ingestion_items gains content_hash, cluster_id, dedup_relation, duplicate_of_id, processed_at and
  processing (derived signals and decision reasons, no article text). They expire and are purged with
  the row, so processing results never outlive the staged item.
- A CHECK ties processing_status = 'processed' to having a cluster and a relation.
- ingestion_jobs.job_type accepts 'story_processing' (one row per processing run; the existing
  one-running-job-per-source index is the cross-process lock).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06 10:30:00+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_JOB_TYPES = "'source_fetch', 'newsletter_import', 'summarisation', 'verification'"
NEW_JOB_TYPES = "'source_fetch', 'newsletter_import', 'story_processing', 'summarisation', 'verification'"


def upgrade() -> None:
    op.add_column("ingestion_items", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.add_column("ingestion_items", sa.Column("cluster_id", sa.UUID(), nullable=True))
    op.add_column("ingestion_items", sa.Column("dedup_relation", sa.String(length=24), nullable=True))
    op.add_column("ingestion_items", sa.Column("duplicate_of_id", sa.UUID(), nullable=True))
    op.add_column("ingestion_items", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ingestion_items", sa.Column("processing", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(op.f("ix_ingestion_items_content_hash"), "ingestion_items", ["content_hash"], unique=False)
    op.create_index(op.f("ix_ingestion_items_cluster_id"), "ingestion_items", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_ingestion_items_duplicate_of_id"), "ingestion_items", ["duplicate_of_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_ingestion_items_duplicate_of_id_ingestion_items"),
        "ingestion_items",
        "ingestion_items",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_ingestion_items_dedup_relation_valid"),
        "ingestion_items",
        "dedup_relation IN ('primary', 'exact_duplicate', 'near_duplicate', 'related_report')",
    )
    op.create_check_constraint(
        op.f("ck_ingestion_items_processed_has_cluster"),
        "ingestion_items",
        "processing_status <> 'processed' OR (cluster_id IS NOT NULL AND dedup_relation IS NOT NULL)",
    )
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({NEW_JOB_TYPES})"
    )


def downgrade() -> None:
    """Drops the processing columns (staged items return to Stage 3.1's shape). Local development only."""
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'story_processing'")
    op.execute("UPDATE ingestion_items SET processing_status = 'pending' WHERE processing_status = 'processed'")
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({OLD_JOB_TYPES})"
    )
    op.drop_constraint(op.f("ck_ingestion_items_processed_has_cluster"), "ingestion_items", type_="check")
    op.drop_constraint(op.f("ck_ingestion_items_dedup_relation_valid"), "ingestion_items", type_="check")
    op.drop_constraint(
        op.f("fk_ingestion_items_duplicate_of_id_ingestion_items"), "ingestion_items", type_="foreignkey"
    )
    op.drop_index(op.f("ix_ingestion_items_duplicate_of_id"), table_name="ingestion_items")
    op.drop_index(op.f("ix_ingestion_items_cluster_id"), table_name="ingestion_items")
    op.drop_index(op.f("ix_ingestion_items_content_hash"), table_name="ingestion_items")
    op.drop_column("ingestion_items", "processing")
    op.drop_column("ingestion_items", "processed_at")
    op.drop_column("ingestion_items", "duplicate_of_id")
    op.drop_column("ingestion_items", "dedup_relation")
    op.drop_column("ingestion_items", "cluster_id")
    op.drop_column("ingestion_items", "content_hash")
