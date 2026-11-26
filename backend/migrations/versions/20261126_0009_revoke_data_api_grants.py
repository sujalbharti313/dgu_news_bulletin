"""Phase 4.1 audit: remove the Supabase Data API roles' table privileges on the Phase 2-3.4 tables.

Migrations 0007 and 0008 already revoke every privilege from Supabase's `anon` and `authenticated` roles on their
new tables. The tables created by 0001-0006 (stories, sources, editions' predecessors' reference tables,
ingestion_items, story_analyses, story_embeddings, ranking_runs, ai_usage_daily, ...) still carry Supabase's
default grants (SELECT/INSERT/UPDATE/DELETE/TRUNCATE ...) to those roles. Row-level security with no policies
blocks every row today, so nothing is exposed, but RLS is then the only barrier: one permissive policy or one
"disable RLS" click in the dashboard would let anyone holding the public (publishable) key read or change news,
analyses, embeddings or job records through the Data API.

This migration only changes privileges; it creates, changes or deletes no table, column or row:
- REVOKE ALL on every table and sequence in `public`, and EXECUTE on every function in `public`, from `anon` and
  `authenticated` (only when those roles exist, so plain local PostgreSQL is unaffected).
- The same for objects created later by the migration owner (ALTER DEFAULT PRIVILEGES), so a future table does not
  silently get the grants back.

The backend connects as the database owner (`postgres`), which keeps all its rights, and the frontend never uses the
Data API (only Supabase Auth, which has its own schema and role), so no workflow changes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-26 06:00:00+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $do$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
                    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
                    EXECUTE format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM %I', r);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', r);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM %I', r);
                END IF;
            END LOOP;
        END
        $do$
        """
    )


def downgrade() -> None:
    """Deliberately does not re-grant anything: the Data API roles never need these privileges, and restoring them
    would only weaken the database again."""
