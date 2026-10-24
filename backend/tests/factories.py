"""Minimal row builders for tests. Every value is obviously fictional."""

import itertools
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Briefing, Edition, ResearchPaper, Source, Story, Tool
from app.models.enums import ContentStatus, PublicationStatus

_seq = itertools.count(1)


def now() -> datetime:
    return datetime.now(UTC)


def source(session: Session, **kw) -> Source:
    n = next(_seq)
    row = Source(
        slug=kw.pop("slug", f"test-source-{n}"),
        name=kw.pop("name", f"Test Source {n}"),
        kind=kw.pop("kind", "official_blog"),
        canonical_domain=kw.pop("canonical_domain", f"source{n}.example.test"),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def edition(session: Session, **kw) -> Edition:
    n = next(_seq)
    start = kw.pop("week_start", date(2030, 1, 7) + timedelta(weeks=n))
    row = Edition(
        slug=kw.pop("slug", f"test-edition-{n}"),
        number=kw.pop("number", n),
        title=kw.pop("title", f"Test edition {n}"),
        week_start=start,
        week_end=kw.pop("week_end", start + timedelta(days=6)),
        status=kw.pop("status", PublicationStatus.PUBLISHED),
        published_at=kw.pop("published_at", now() - timedelta(days=1)),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def story(session: Session, *, edition: Edition, source: Source, **kw) -> Story:
    n = next(_seq)
    row = Story(
        slug=kw.pop("slug", f"test-story-{n}"),
        title=kw.pop("title", f"Fictional test story {n}"),
        summary=kw.pop("summary", "A fictional summary used only in automated tests."),
        content=kw.pop("content", "First paragraph.\n\nSecond paragraph."),
        topic=kw.pop("topic", "models"),
        status=kw.pop("status", ContentStatus.PUBLISHED),
        published_at=kw.pop("published_at", now() - timedelta(hours=n)),
        edition_id=edition.id,
        source_id=source.id,
        impact_why_it_matters=kw.pop("impact_why_it_matters", "Why it matters (test)."),
        impact_build_idea=kw.pop("impact_build_idea", "Build idea (test)."),
        impact_explore=kw.pop("impact_explore", ["Exploration (test)"]),
        impact_disciplines=kw.pop("impact_disciplines", ["Computer Science"]),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def briefing(session: Session, *, edition: Edition, **kw) -> Briefing:
    row = Briefing(
        edition_id=edition.id,
        title=kw.pop("title", "Test briefing"),
        transcript=kw.pop("transcript", "Line one.\n\nLine two."),
        status=kw.pop("status", PublicationStatus.PUBLISHED),
        published_at=kw.pop("published_at", now() - timedelta(hours=1)),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def tool(session: Session, **kw) -> Tool:
    n = next(_seq)
    row = Tool(
        slug=kw.pop("slug", f"test-tool-{n}"),
        name=kw.pop("name", f"Test Tool {n}"),
        provider=kw.pop("provider", "Test Provider"),
        category=kw.pop("category", "coding"),
        summary=kw.pop("summary", "A fictional tool for tests."),
        good_for=kw.pop("good_for", ["Testing"]),
        limitations=kw.pop("limitations", "None known (test)."),
        access_notes=kw.pop("access_notes", "Test access."),
        official_url=kw.pop("official_url", f"https://tool{n}.example.test"),
        status=kw.pop("status", PublicationStatus.PUBLISHED),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def paper(session: Session, **kw) -> ResearchPaper:
    n = next(_seq)
    row = ResearchPaper(
        slug=kw.pop("slug", f"test-paper-{n}"),
        title=kw.pop("title", f"A fictional paper {n}"),
        authors=kw.pop("authors", "Tester et al."),
        organisation=kw.pop("organisation", "Test Lab"),
        published_on=kw.pop("published_on", date(2020, 1, 1) + timedelta(days=n)),
        url=kw.pop("url", f"https://papers.example.test/{n}"),
        plain_language_summary=kw.pop("plain_language_summary", "Plain summary (test)."),
        key_findings=kw.pop("key_findings", "Findings (test)."),
        limitations=kw.pop("limitations", "Limitations (test)."),
        status=kw.pop("status", PublicationStatus.PUBLISHED),
        **kw,
    )
    session.add(row)
    session.flush()
    return row
