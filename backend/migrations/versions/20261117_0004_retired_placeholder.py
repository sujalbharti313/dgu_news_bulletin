"""Retired revision (no-op). Kept so that every database can reach Phase 3.3 with `alembic upgrade head`.

An earlier Phase 3.3 attempt used revision 0004 (table story_intelligence). That attempt was rolled back and its
migration removed. Some databases may still be at that old 0004 (if the rollback script
migrations/rollback/0004_drop_story_intelligence.sql was not run); others are back at 0003. With this
placeholder, both upgrade cleanly: 0003 → 0004 (nothing happens) → 0005, or old 0004 → 0005.

This revision creates, changes and deletes nothing. It never touches story_intelligence: if that old table
still exists it is left alone (drop it with the rollback script when convenient).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17 12:00:00+00:00

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
