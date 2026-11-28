"""Builds a persistent, disposable LOCAL database for the Phase 4.2 Scenario M browser test (frontend + backend).

    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/aicampus_e2e_test \
        python -m tests.e2e.frontend_fixture

Refuses anything but a local database whose name contains "test" (the same guard as tests/conftest.py), rebuilds its
schema with the real migrations, then runs the real Stage 3.5 service on the shared test week to generate, approve
and publish one edition, and adds unpublished "canary" content (an edition in review and a draft story) that must
never appear on the public site. Fictional data only; no network, no key, no quota.
"""

import os
import sys
import uuid
from datetime import date

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

CANARY = "CANARY unpublished headline must never appear publicly"


def main() -> int:
    from app.core.config import LOCAL_HOSTS, normalise_database_url
    from tests.conftest import BACKEND_DIR, make_settings

    raw = os.environ.get("TEST_DATABASE_URL", "")
    url = normalise_database_url(raw)
    if "test" not in (url.database or "") or url.host not in LOCAL_HOSTS:
        print("TEST_DATABASE_URL must be a local database whose name contains 'test'.", file=sys.stderr)
        return 2
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["settings"] = make_settings(database_url=raw)
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        conn.commit()

    from app.models import Edition, Story
    from tests.cleanup_helpers import published_edition

    with Session(engine) as s:
        eid, selected, run, _ = published_edition(s)
        edition = s.get(Edition, eid)
        draft = Edition(
            slug="2026-w46",
            number=2,
            title=CANARY,
            week_start=date(2026, 11, 9),
            week_end=date(2026, 11, 15),
            status="review_required",
            introduction=CANARY,
            current_revision=0,
            is_demo=False,
        )
        s.add(draft)
        template = s.scalars(select(Story).where(Story.edition_id == eid)).first()
        values = {
            c.key: getattr(template, c.key)
            for c in Story.__table__.columns
            if c.key not in ("id", "created_at", "updated_at", "search_vector")
        }
        values.update(
            id=uuid.uuid4(),
            slug="canary-draft-story",
            title=CANARY,
            status="draft",
            edition_id=None,
            edition_position=None,
            is_featured=False,
            published_at=None,
            original_url="https://canary.example.org/draft",
            canonical_url="https://canary.example.org/draft",
        )
        s.add(Story(**values))
        s.commit()
        print(f"published edition {edition.slug} ({len(selected)} stories) and canary content")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
