"""Database-level guarantees: these hold even for writes that bypass the API (SQL editor, future jobs)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import CareerSignal, IngestionJob
from tests import factories as f


def expect_integrity_error(session, fn):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            fn()
            session.flush()


def test_duplicate_slugs_are_rejected(db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    f.story(db_session, edition=ed, source=src, slug="same")
    expect_integrity_error(db_session, lambda: f.story(db_session, edition=ed, source=src, slug="same"))
    expect_integrity_error(db_session, lambda: f.edition(db_session, slug=ed.slug))


def test_duplicate_articles_are_rejected_by_canonical_url(db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    url = "https://lab.example.test/post"
    f.story(db_session, edition=ed, source=src, original_url=url + "?utm_source=a", canonical_url=url)
    expect_integrity_error(
        db_session,
        lambda: f.story(db_session, edition=ed, source=src, original_url=url + "?utm_source=b", canonical_url=url),
    )


def test_duplicate_sources_are_rejected(db_session):
    f.source(db_session, canonical_domain="blog.example.test", kind="official_blog")
    expect_integrity_error(
        db_session, lambda: f.source(db_session, canonical_domain="blog.example.test", kind="official_blog")
    )
    f.source(db_session, canonical_domain="blog.example.test", kind="rss")  # a feed of the same site is fine
    expect_integrity_error(db_session, lambda: f.source(db_session, canonical_domain="https://Blog.example.test/"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"topic": "gossip"},
        {"status": "live"},
        {"slug": "Not A Slug"},
        {"status": "published", "published_at": None},
        {"impact_why_it_matters": None},
        {"reading_time_minutes": 0},
        {"original_url": "javascript:alert(1)", "canonical_url": "x"},
        {"original_url": "https://a.example.test"},  # canonical_url missing
        {"image_url": "https://img.example.test/a.png"},  # alt and credit missing
    ],
)
def test_story_check_constraints(db_session, overrides):
    src, ed = f.source(db_session), f.edition(db_session)
    expect_integrity_error(db_session, lambda: f.story(db_session, edition=ed, source=src, **overrides))


def test_published_story_needs_a_source(db_session):
    ed = f.edition(db_session)

    class NoSource:
        id = None

    expect_integrity_error(db_session, lambda: f.story(db_session, edition=ed, source=NoSource()))
    f.story(db_session, edition=ed, source=NoSource(), status="draft", published_at=None)  # drafts may lack one


def test_one_featured_story_per_edition_and_unique_positions(db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    f.story(db_session, edition=ed, source=src, is_featured=True, edition_position=1)
    expect_integrity_error(db_session, lambda: f.story(db_session, edition=ed, source=src, is_featured=True))
    expect_integrity_error(db_session, lambda: f.story(db_session, edition=ed, source=src, edition_position=1))


def test_edition_constraints(db_session):
    expect_integrity_error(
        db_session, lambda: f.edition(db_session, week_start=date(2031, 1, 8), week_end=date(2031, 1, 1))
    )
    expect_integrity_error(db_session, lambda: f.edition(db_session, status="published", published_at=None))
    ed = f.edition(db_session, number=500)
    expect_integrity_error(db_session, lambda: f.edition(db_session, number=500))
    f.edition(db_session, number=500, is_demo=True)  # demo numbering is independent
    assert ed.id


def test_restrict_and_cascade_rules(db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    f.story(db_session, edition=ed, source=src)
    expect_integrity_error(
        db_session, lambda: db_session.execute(text("DELETE FROM editions WHERE id = :id"), {"id": ed.id})
    )
    expect_integrity_error(
        db_session, lambda: db_session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": src.id})
    )

    lonely = f.edition(db_session)
    f.briefing(db_session, edition=lonely)
    db_session.execute(text("DELETE FROM editions WHERE id = :id"), {"id": lonely.id})
    assert (
        db_session.scalar(text("SELECT count(*) FROM audio_briefings WHERE edition_id = :id"), {"id": lonely.id}) == 0
    )

    job_source = f.source(db_session)
    job = IngestionJob(source_id=job_source.id, job_type="source_fetch")
    db_session.add(job)
    db_session.flush()
    db_session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": job_source.id})
    db_session.refresh(job)
    assert job.source_id is None  # job history survives


def test_tool_pricing_requires_verification(db_session):
    expect_integrity_error(db_session, lambda: f.tool(db_session, pricing_notes="Free tier"))
    f.tool(db_session, pricing_notes="Free tier", last_verified_at=f.now())


def test_career_signal_topics_are_controlled(db_session):
    def bad():
        db_session.add(
            CareerSignal(
                slug="bad-topics",
                kind="skill",
                title="T",
                description="D",
                start_with="S",
                related_topics=["astrology"],
            )
        )

    expect_integrity_error(db_session, bad)


def test_ingestion_job_constraints(db_session):
    expect_integrity_error(db_session, lambda: db_session.add(IngestionJob(job_type="scrape_everything")))
    expect_integrity_error(db_session, lambda: db_session.add(IngestionJob(job_type="source_fetch", attempt_count=-1)))
    expect_integrity_error(
        db_session,
        lambda: db_session.add(
            IngestionJob(job_type="source_fetch", started_at=f.now(), completed_at=f.now() - timedelta(hours=1))
        ),
    )


def test_updated_at_trigger_overrides_stale_values(db_session):
    src = f.source(db_session)
    db_session.execute(text("UPDATE sources SET updated_at = '2000-01-01' WHERE id = :id"), {"id": src.id})
    updated = db_session.scalar(text("SELECT updated_at FROM sources WHERE id = :id"), {"id": src.id})
    assert updated.year > 2000  # the trigger replaced the stale value with now()


def test_row_level_security_is_enabled_on_every_table(db_session):
    rows = db_session.execute(
        text(
            "SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )
    ).all()
    assert len(rows) == 11  # ten tables plus alembic_version
    assert all(enabled for _, enabled in rows), rows
