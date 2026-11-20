"""Stage 3.5 weekly edition generation, admin review, approval and publishing.

Additive except for three widened CHECK constraints. No news, analysis, verification or ranking data is touched:
- editorial_admins: the people allowed to review/approve/publish (Supabase Auth user id + email), added from the
  command line only.
- editions: the existing weekly edition row now carries the workflow. `status` is widened to the Stage 3.5
  lifecycle (generating, draft, review_required, approved, published, generation_failed, rejected, archived; the
  old 'withdrawn' stays valid). New columns: introduction, editorial note, timezone, current revision, generation
  bookkeeping (attempts, next attempt, safe error, generation JSON, ranking run, scheduler job), approval (revision,
  content hash, approver, time) and publication (revision, publisher). CHECKs: an approved edition's approval is
  for its current revision; a Stage 3.5 published edition was published from its approved revision. Existing rows
  (status draft/published, no revisions) satisfy every new CHECK unchanged.
- edition_revisions: append-only snapshots of the publication content (a trigger rejects UPDATE, DELETE and
  TRUNCATE), so the generated original and every revision stay traceable and a published revision never changes.
- edition_items: the relational selection (story cluster, position, Stage 3.4 signals, the public story created at
  publication). cluster_id references story_analyses ON DELETE RESTRICT so later cleanup (Stage 3.6) cannot
  silently remove a story an edition refers to.
- edition_audit_log: append-only audit trail (same trigger).
- stories.published_complete no longer requires impact_build_idea (project ideas are only published when Stage
  3.3 produced one).
- ingestion_jobs.job_type accepts 'edition_generation' (the Stage 3.5 run lock and schedule execution record).
- updated_at triggers, row-level security with no policies on every new table, and all privileges revoked from
  Supabase's anon/authenticated roles: the Data API cannot read drafts or change workflow state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20 10:00:00+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_EDITION_STATUSES = "'draft', 'published', 'withdrawn'"
NEW_EDITION_STATUSES = (
    "'generating', 'draft', 'review_required', 'approved', 'published', 'generation_failed', 'rejected', "
    "'archived', 'withdrawn'"
)
OLD_JOB_TYPES = "'source_fetch', 'newsletter_import', 'story_processing', 'summarisation', 'verification'"
NEW_JOB_TYPES = OLD_JOB_TYPES + ", 'edition_generation'"
AUDIT_ACTIONS = (
    "'generation_started', 'generated', 'generation_failed', 'generation_deferred', 'opened_for_review', "
    "'revision_created', 'story_added', 'story_removed', 'story_reordered', 'content_edited', "
    "'approval_requested', 'approved', 'approval_invalidated', 'approval_rejected', 'rejected', "
    "'returned_for_revision', 'published', 'publish_failed', 'regenerated', 'retry_triggered', 'archived'"
)
NEW_TABLES = ("editorial_admins", "edition_revisions", "edition_items", "edition_audit_log")
APPEND_ONLY = ("edition_revisions", "edition_audit_log")


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
        "editorial_admins",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(email) BETWEEN 3 AND 320", name=op.f("ck_editorial_admins_email_length")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_editorial_admins")),
        sa.UniqueConstraint("email", name=op.f("uq_editorial_admins_email")),
    )

    # --- editions: widen the lifecycle, add workflow columns ---
    op.drop_constraint(op.f("ck_editions_status_valid"), "editions", type_="check")
    op.alter_column("editions", "status", type_=sa.String(length=24), existing_nullable=False)
    op.create_check_constraint(op.f("ck_editions_status_valid"), "editions", f"status IN ({NEW_EDITION_STATUSES})")
    op.add_column("editions", sa.Column("introduction", sa.Text(), nullable=True))
    op.add_column("editions", sa.Column("editorial_note", sa.Text(), nullable=True))
    op.add_column(
        "editions", sa.Column("timezone", sa.String(length=40), server_default="Asia/Kolkata", nullable=False)
    )
    op.add_column("editions", sa.Column("current_revision", sa.Integer(), server_default="0", nullable=False))
    op.add_column("editions", sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("editions", sa.Column("generation_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("editions", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "editions",
        sa.Column("generation", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    )
    op.add_column("editions", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("editions", sa.Column("ranking_run_id", sa.UUID(), nullable=True))
    op.add_column("editions", sa.Column("generation_job_id", sa.UUID(), nullable=True))
    op.add_column("editions", sa.Column("approved_revision", sa.Integer(), nullable=True))
    op.add_column("editions", sa.Column("approved_content_hash", sa.String(length=64), nullable=True))
    op.add_column("editions", sa.Column("approved_by", sa.UUID(), nullable=True))
    op.add_column("editions", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("editions", sa.Column("published_revision", sa.Integer(), nullable=True))
    op.add_column("editions", sa.Column("published_by", sa.UUID(), nullable=True))
    op.add_column("editions", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_editions_ranking_run_id_ranking_runs"),
        "editions",
        "ranking_runs",
        ["ranking_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_editions_generation_job_id_ingestion_jobs"),
        "editions",
        "ingestion_jobs",
        ["generation_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_editions_approved_by_editorial_admins"),
        "editions",
        "editorial_admins",
        ["approved_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_editions_published_by_editorial_admins"),
        "editions",
        "editorial_admins",
        ["published_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_editions_ranking_run_id"), "editions", ["ranking_run_id"], unique=False)
    op.create_index(op.f("ix_editions_generation_job_id"), "editions", ["generation_job_id"], unique=False)
    op.create_check_constraint(
        op.f("ck_editions_current_revision_non_negative"), "editions", "current_revision >= 0"
    )
    op.create_check_constraint(
        op.f("ck_editions_generation_attempts_non_negative"), "editions", "generation_attempts >= 0"
    )
    op.create_check_constraint(
        op.f("ck_editions_approved_matches_revision"),
        "editions",
        "status <> 'approved' OR (approved_revision = current_revision AND approved_at IS NOT NULL "
        "AND approved_by IS NOT NULL AND approved_content_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_editions_published_from_approved_revision"),
        "editions",
        "status <> 'published' OR current_revision = 0 OR "
        "(published_revision IS NOT NULL AND published_revision = approved_revision)",
    )

    # --- stories: a build idea is optional ---
    op.drop_constraint(op.f("ck_stories_published_complete"), "stories", type_="check")
    op.create_check_constraint(
        op.f("ck_stories_published_complete"),
        "stories",
        "status <> 'published' OR (published_at IS NOT NULL AND source_id IS NOT NULL "
        "AND impact_why_it_matters IS NOT NULL)",
    )

    # --- ingestion_jobs: the Stage 3.5 run lock / schedule execution record ---
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({NEW_JOB_TYPES})"
    )

    op.create_table(
        "edition_revisions",
        sa.Column("edition_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("changes", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("issues", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "origin IN ('generated', 'admin_edit', 'regenerated')", name=op.f("ck_edition_revisions_origin_valid")
        ),
        sa.CheckConstraint("revision > 0", name=op.f("ck_edition_revisions_revision_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["editorial_admins.user_id"],
            name=op.f("fk_edition_revisions_created_by_editorial_admins"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name=op.f("fk_edition_revisions_edition_id_editions"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition_revisions")),
        sa.UniqueConstraint("edition_id", "revision", name=op.f("uq_edition_revisions_edition_id_revision")),
    )
    op.create_index(op.f("ix_edition_revisions_edition_id"), "edition_revisions", ["edition_id"], unique=False)

    op.create_table(
        "edition_items",
        sa.Column("edition_id", sa.UUID(), nullable=False),
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("selection_source", sa.String(length=16), nullable=False),
        sa.Column("eligibility", sa.String(length=24), nullable=False),
        sa.Column("ranking_rank", sa.Integer(), nullable=True),
        sa.Column("shortlist_rank", sa.Integer(), nullable=True),
        sa.Column("final_priority_score", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("review_acknowledged_by", sa.UUID(), nullable=True),
        sa.Column("story_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "eligibility <> 'review_required' OR review_acknowledged_by IS NOT NULL",
            name=op.f("ck_edition_items_review_ack"),
        ),
        sa.CheckConstraint(
            "eligibility IN ('eligible', 'review_required')", name=op.f("ck_edition_items_eligibility_valid")
        ),
        sa.CheckConstraint("position > 0", name=op.f("ck_edition_items_position_positive")),
        sa.CheckConstraint(
            "selection_source IN ('ranking', 'admin_added')", name=op.f("ck_edition_items_selection_source_valid")
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["story_analyses.cluster_id"],
            name=op.f("fk_edition_items_cluster_id_story_analyses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name=op.f("fk_edition_items_edition_id_editions"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["review_acknowledged_by"],
            ["editorial_admins.user_id"],
            name=op.f("fk_edition_items_review_acknowledged_by_editorial_admins"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name=op.f("fk_edition_items_story_id_stories"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition_items")),
        sa.UniqueConstraint("edition_id", "cluster_id", name=op.f("uq_edition_items_edition_id_cluster_id")),
        sa.UniqueConstraint("edition_id", "position", name=op.f("uq_edition_items_edition_id_position")),
    )
    op.create_index(op.f("ix_edition_items_cluster_id"), "edition_items", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_edition_items_edition_id"), "edition_items", ["edition_id"], unique=False)
    op.create_index(op.f("ix_edition_items_story_id"), "edition_items", ["story_id"], unique=False)

    op.create_table(
        "edition_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("edition_id", sa.UUID(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("details", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"action IN ({AUDIT_ACTIONS})", name=op.f("ck_edition_audit_log_action_valid")),
        sa.CheckConstraint(
            "actor_kind IN ('admin', 'scheduler', 'system', 'cli')", name=op.f("ck_edition_audit_log_actor_kind_valid")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["editorial_admins.user_id"],
            name=op.f("fk_edition_audit_log_actor_id_editorial_admins"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name=op.f("fk_edition_audit_log_edition_id_editions"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition_audit_log")),
    )
    op.create_index(
        "ix_edition_audit_log_edition_occurred", "edition_audit_log", ["edition_id", "occurred_at"], unique=False
    )

    op.execute(
        """
        CREATE FUNCTION reject_append_only_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $fn$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $fn$
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_only_change()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_change()"
        )
    for table in ("editorial_admins", "edition_items"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _revoke_api_roles(table)
    # Drafts live in `editions` too: make sure the Data API roles cannot read or write it either way.
    _revoke_api_roles("editions")


def downgrade() -> None:
    """Drops the Stage 3.5 tables and columns. Stage 3.5 statuses fall back to 'draft' (or stay 'published').
    Local development only: revisions and the audit trail are lost."""
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_append_only_change()")
    op.drop_index("ix_edition_audit_log_edition_occurred", table_name="edition_audit_log")
    op.drop_table("edition_audit_log")
    op.drop_index(op.f("ix_edition_items_story_id"), table_name="edition_items")
    op.drop_index(op.f("ix_edition_items_edition_id"), table_name="edition_items")
    op.drop_index(op.f("ix_edition_items_cluster_id"), table_name="edition_items")
    op.drop_table("edition_items")
    op.drop_index(op.f("ix_edition_revisions_edition_id"), table_name="edition_revisions")
    op.drop_table("edition_revisions")

    op.execute("UPDATE editions SET generation_job_id = NULL")
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'edition_generation'")
    op.drop_constraint(op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_ingestion_jobs_job_type_valid"), "ingestion_jobs", f"job_type IN ({OLD_JOB_TYPES})"
    )
    op.drop_constraint(op.f("ck_stories_published_complete"), "stories", type_="check")
    op.execute("UPDATE stories SET impact_build_idea = '' WHERE status = 'published' AND impact_build_idea IS NULL")
    op.create_check_constraint(
        op.f("ck_stories_published_complete"),
        "stories",
        "status <> 'published' OR (published_at IS NOT NULL AND source_id IS NOT NULL "
        "AND impact_why_it_matters IS NOT NULL AND impact_build_idea IS NOT NULL)",
    )

    op.drop_constraint(op.f("ck_editions_published_from_approved_revision"), "editions", type_="check")
    op.drop_constraint(op.f("ck_editions_approved_matches_revision"), "editions", type_="check")
    op.drop_constraint(op.f("ck_editions_generation_attempts_non_negative"), "editions", type_="check")
    op.drop_constraint(op.f("ck_editions_current_revision_non_negative"), "editions", type_="check")
    op.drop_index(op.f("ix_editions_generation_job_id"), table_name="editions")
    op.drop_index(op.f("ix_editions_ranking_run_id"), table_name="editions")
    op.drop_constraint(op.f("fk_editions_published_by_editorial_admins"), "editions", type_="foreignkey")
    op.drop_constraint(op.f("fk_editions_approved_by_editorial_admins"), "editions", type_="foreignkey")
    op.drop_constraint(op.f("fk_editions_generation_job_id_ingestion_jobs"), "editions", type_="foreignkey")
    op.drop_constraint(op.f("fk_editions_ranking_run_id_ranking_runs"), "editions", type_="foreignkey")
    for column in (
        "review_note",
        "published_by",
        "published_revision",
        "approved_at",
        "approved_by",
        "approved_content_hash",
        "approved_revision",
        "generation_job_id",
        "ranking_run_id",
        "last_error",
        "generation",
        "next_attempt_at",
        "generation_attempts",
        "generated_at",
        "current_revision",
        "timezone",
        "editorial_note",
        "introduction",
    ):
        op.drop_column("editions", column)
    op.drop_constraint(op.f("ck_editions_status_valid"), "editions", type_="check")
    op.execute(f"UPDATE editions SET status = 'draft' WHERE status NOT IN ({OLD_EDITION_STATUSES})")
    op.alter_column("editions", "status", type_=sa.String(length=16), existing_nullable=False)
    op.create_check_constraint(op.f("ck_editions_status_valid"), "editions", f"status IN ({OLD_EDITION_STATUSES})")
    op.drop_table("editorial_admins")
