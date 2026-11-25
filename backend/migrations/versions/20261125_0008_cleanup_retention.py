"""Stage 3.6 automated cleanup, data retention and published-edition preservation.

Additive except for one tightened foreign key and two widened CHECK constraints. No row is deleted or changed:
- cleanup_jobs: one row per cleanup of a published week (or one sweep of expired orphans), with its safety-delay
  deadline, manifest hold, status, counts, retry state and safe error text. `job_key` is unique, and a partial
  unique index allows only one job per edition, so duplicate scheduler runs never create a second job.
- cleanup_manifest_items: every record a job considered (target + id), with its state (pending, deleted,
  protected, skipped, failed) and a reason code. Ids only, no payloads.
- cleanup_audit_log: append-only audit trail (the existing reject_append_only_change trigger).
- DELETE and TRUNCATE are rejected on all three tables: the cleanup history cannot be removed.
- Database-level protection of published content, independent of the application:
  * story_analyses: a BEFORE DELETE trigger refuses to delete the analysis of any story that appears in any
    edition revision (edition_items already blocks deletion with ON DELETE RESTRICT since 0007).
  * editions: a published Stage 3.5 edition (current_revision > 0, not demo) cannot be deleted.
  * stories: a published story of such an edition cannot be deleted.
  * edition_items: the selection of such an edition cannot be deleted.
  * editions.ranking_run_id: ON DELETE SET NULL becomes ON DELETE RESTRICT, so a ranking run an edition was
    built from can never be removed.
- ingestion_jobs.job_type accepts 'cleanup' (the Stage 3.6 run lock).
- Row-level security with no policies on the new tables, and all privileges revoked from Supabase's
  anon/authenticated roles.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-25 10:00:00+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_JOB_TYPES = (
    "'source_fetch', 'newsletter_import', 'story_processing', 'summarisation', 'verification', 'edition_generation'"
)
NEW_JOB_TYPES = OLD_JOB_TYPES + ", 'cleanup'"
JOB_STATUSES = "'scheduled', 'blocked', 'staged', 'running', 'completed', 'failed', 'cancelled'"
AUDIT_ACTIONS = (
    "'job_created', 'publication_verified', 'publication_check_failed', 'manifest_staged', 'deferred', "
    "'deletion_started', 'batch_deleted', 'records_skipped', 'deletion_completed', 'deletion_failed', "
    "'retry_scheduled', 'retry_requested', 'job_cancelled', 'job_reopened', 'manual_trigger'"
)
NEW_TABLES = ("cleanup_jobs", "cleanup_manifest_items", "cleanup_audit_log")


def _revoke_api_roles(table: str) -> None:
    op.execute(
        f"""
        DO $do$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format('REVOKE ALL ON TABLE public.{table} FROM %I', r);
                END IF;
            END LOOP;
        END
        $do$
        """
    )


def upgrade() -> None:
    op.create_table(
        "cleanup_jobs",
        sa.Column("job_key", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("edition_id", sa.UUID(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eligible_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("protected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deleted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked_reasons", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("protection_summary", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('published_week', 'expired_orphans')", name=op.f("ck_cleanup_jobs_kind_valid")),
        sa.CheckConstraint(f"status IN ({JOB_STATUSES})", name=op.f("ck_cleanup_jobs_status_valid")),
        sa.CheckConstraint("trigger IN ('scheduler', 'admin', 'cli')", name=op.f("ck_cleanup_jobs_trigger_valid")),
        sa.CheckConstraint(
            "kind <> 'published_week' OR (edition_id IS NOT NULL AND window_start IS NOT NULL "
            "AND window_end IS NOT NULL)",
            name=op.f("ck_cleanup_jobs_published_week_has_edition"),
        ),
        sa.CheckConstraint(
            "window_end IS NULL OR window_end = window_start + 6", name=op.f("ck_cleanup_jobs_window_is_one_week")
        ),
        sa.CheckConstraint(
            "eligible_count >= 0 AND protected_count >= 0 AND deleted_count >= 0 AND skipped_count >= 0 "
            "AND failure_count >= 0 AND retry_count >= 0",
            name=op.f("ck_cleanup_jobs_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL", name=op.f("ck_cleanup_jobs_completed_has_timestamp")
        ),
        sa.CheckConstraint(
            "status <> 'staged' OR staged_at IS NOT NULL", name=op.f("ck_cleanup_jobs_staged_has_timestamp")
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 500", name=op.f("ck_cleanup_jobs_last_error_length")
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name=op.f("fk_cleanup_jobs_edition_id_editions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["editorial_admins.user_id"],
            name=op.f("fk_cleanup_jobs_requested_by_editorial_admins"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cleanup_jobs")),
        sa.UniqueConstraint("job_key", name=op.f("uq_cleanup_jobs_job_key")),
    )
    op.create_index(
        "uq_cleanup_jobs_one_per_edition",
        "cleanup_jobs",
        ["edition_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'published_week'"),
    )
    op.create_index("ix_cleanup_jobs_status_not_before", "cleanup_jobs", ["status", "not_before"], unique=False)

    op.create_table(
        "cleanup_manifest_items",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("target", sa.String(length=24), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "target IN ('story_cluster', 'ranking_run', 'orphan_verification')",
            name=op.f("ck_cleanup_manifest_items_target_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'deleted', 'protected', 'skipped', 'failed')",
            name=op.f("ck_cleanup_manifest_items_state_valid"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_cleanup_manifest_items_attempts_non_negative")),
        sa.CheckConstraint(
            "state = 'pending' OR processed_at IS NOT NULL",
            name=op.f("ck_cleanup_manifest_items_processed_has_timestamp"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('protected', 'skipped', 'failed') OR reason IS NOT NULL",
            name=op.f("ck_cleanup_manifest_items_has_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["cleanup_jobs.id"],
            name=op.f("fk_cleanup_manifest_items_job_id_cleanup_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cleanup_manifest_items")),
        sa.UniqueConstraint(
            "job_id", "target", "record_id", name=op.f("uq_cleanup_manifest_items_job_id_target_record_id")
        ),
    )
    op.create_index("ix_cleanup_manifest_items_job_state", "cleanup_manifest_items", ["job_id", "state"], unique=False)

    op.create_table(
        "cleanup_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("edition_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("details", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"action IN ({AUDIT_ACTIONS})", name=op.f("ck_cleanup_audit_log_action_valid")),
        sa.CheckConstraint(
            "actor_kind IN ('admin', 'scheduler', 'system', 'cli')", name=op.f("ck_cleanup_audit_log_actor_kind_valid")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["editorial_admins.user_id"],
            name=op.f("fk_cleanup_audit_log_actor_id_editorial_admins"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name=op.f("fk_cleanup_audit_log_edition_id_editions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["cleanup_jobs.id"], name=op.f("fk_cleanup_audit_log_job_id_cleanup_jobs"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cleanup_audit_log")),
    )
    op.create_index("ix_cleanup_audit_log_job_occurred", "cleanup_audit_log", ["job_id", "occurred_at"], unique=False)

    # --- the history cannot be deleted ---
    op.execute(
        "CREATE TRIGGER trg_cleanup_audit_log_append_only BEFORE UPDATE OR DELETE ON cleanup_audit_log "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
    )
    for table in NEW_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
        )
    for table in ("cleanup_jobs", "cleanup_manifest_items"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _revoke_api_roles(table)

    # --- database-level protection of published content ---
    op.execute(
        """
        CREATE FUNCTION protect_edition_story_analysis() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.edition_revisions r
                WHERE r.content -> 'stories' @> jsonb_build_array(jsonb_build_object('cluster_id', OLD.cluster_id::text))
            ) THEN
                RAISE EXCEPTION 'story_analyses %: the story is part of an edition revision and cannot be deleted',
                    OLD.cluster_id USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END
        $fn$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_story_analyses_protect_edition_story BEFORE DELETE ON story_analyses "
        "FOR EACH ROW EXECUTE FUNCTION protect_edition_story_analysis()"
    )
    op.execute(
        """
        CREATE FUNCTION protect_published_edition() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
            IF OLD.status = 'published' AND OLD.current_revision > 0 AND NOT OLD.is_demo THEN
                RAISE EXCEPTION 'edition %: a published edition cannot be deleted', OLD.slug
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END
        $fn$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_editions_protect_published BEFORE DELETE ON editions "
        "FOR EACH ROW EXECUTE FUNCTION protect_published_edition()"
    )
    op.execute(
        """
        CREATE FUNCTION protect_published_story() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
            IF OLD.status = 'published' AND NOT OLD.is_demo AND EXISTS (
                SELECT 1 FROM public.editions e
                WHERE e.id = OLD.edition_id AND e.status = 'published' AND e.current_revision > 0 AND NOT e.is_demo
            ) THEN
                RAISE EXCEPTION 'story %: a story of a published edition cannot be deleted', OLD.slug
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END
        $fn$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_stories_protect_published BEFORE DELETE ON stories "
        "FOR EACH ROW EXECUTE FUNCTION protect_published_story()"
    )
    op.execute(
        """
        CREATE FUNCTION protect_published_selection() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $fn$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.editions e
                WHERE e.id = OLD.edition_id AND e.status = 'published' AND e.current_revision > 0 AND NOT e.is_demo
            ) THEN
                RAISE EXCEPTION 'edition_items %: the selection of a published edition cannot be deleted', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END
        $fn$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_edition_items_protect_published BEFORE DELETE ON edition_items "
        "FOR EACH ROW EXECUTE FUNCTION protect_published_selection()"
    )
    op.drop_constraint(op.f("fk_editions_ranking_run_id_ranking_runs"), "editions", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_editions_ranking_run_id_ranking_runs"),
        "editions",
        "ranking_runs",
        ["ranking_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- ingestion_jobs: the Stage 3.6 run lock ---
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({NEW_JOB_TYPES})"
    )


def downgrade() -> None:
    """Removes the Stage 3.6 tables, triggers and the cleanup run records. Local development only: the cleanup
    history is lost (records already deleted by cleanup are not restored)."""
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'cleanup'")
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({OLD_JOB_TYPES})"
    )
    op.drop_constraint(op.f("fk_editions_ranking_run_id_ranking_runs"), "editions", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_editions_ranking_run_id_ranking_runs"),
        "editions",
        "ranking_runs",
        ["ranking_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("DROP TRIGGER IF EXISTS trg_edition_items_protect_published ON edition_items")
    op.execute("DROP FUNCTION IF EXISTS protect_published_selection()")
    op.execute("DROP TRIGGER IF EXISTS trg_stories_protect_published ON stories")
    op.execute("DROP FUNCTION IF EXISTS protect_published_story()")
    op.execute("DROP TRIGGER IF EXISTS trg_editions_protect_published ON editions")
    op.execute("DROP FUNCTION IF EXISTS protect_published_edition()")
    op.execute("DROP TRIGGER IF EXISTS trg_story_analyses_protect_edition_story ON story_analyses")
    op.execute("DROP FUNCTION IF EXISTS protect_edition_story_analysis()")
    # The no-delete/append-only triggers would block dropping the rows; dropping the tables removes them.
    op.drop_index("ix_cleanup_audit_log_job_occurred", table_name="cleanup_audit_log")
    op.drop_table("cleanup_audit_log")
    op.drop_index("ix_cleanup_manifest_items_job_state", table_name="cleanup_manifest_items")
    op.drop_table("cleanup_manifest_items")
    op.drop_index("ix_cleanup_jobs_status_not_before", table_name="cleanup_jobs")
    op.drop_index("uq_cleanup_jobs_one_per_edition", table_name="cleanup_jobs")
    op.drop_table("cleanup_jobs")
