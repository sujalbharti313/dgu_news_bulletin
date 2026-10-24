"""Repeatable, non-destructive seeding.

Two independent groups:

- reference: the real, public tools, foundational papers, India ecosystem entries and general career
  signals that the frontend already shows (src/data/reference.ts, src/data/sample/campus.ts). Each links to
  an official page; no prices, ratings or benchmark numbers. last_verified_at stays null.
- demo: the frontend's SAMPLE editions, stories, briefing and campus formats. Every row is is_demo = true,
  attributed to a fictional "Sample source" on the reserved example.invalid domain, and never shown
  publicly unless EXPOSE_DEMO_CONTENT=true. Refused outright when APP_ENV=production.

Rows are inserted with ON CONFLICT DO NOTHING, so running twice changes nothing and existing records
(including any later editorial edits) are never overwritten or deleted.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Briefing, CampusItem, CareerSignal, Edition, IndiaEntry, ResearchPaper, Source, Story, Tool
from app.models.enums import (
    INDIA_KIND_LABELS,
    ContentStatus,
    PublicationStatus,
    SourceKind,
    SummaryOrigin,
)

DATA_DIR = Path(__file__).parent / "data"
DEMO_SOURCE_SLUG = "demo-sample-source"
ARXIV_SOURCE_SLUG = "arxiv"
_INDIA_KINDS = {label: kind.value for kind, label in INDIA_KIND_LABELS.items()}


class SeedRefused(RuntimeError):
    pass


@dataclass
class SeedReport:
    inserted: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def add(self, table: str, inserted: bool) -> None:
        bucket = self.inserted if inserted else self.skipped
        bucket[table] = bucket.get(table, 0) + 1


def check_allowed(settings: Settings, *, reference: bool, demo: bool, confirm_production: bool) -> None:
    if not (reference or demo):
        raise SeedRefused("Nothing to do: pass --reference and/or --demo.")
    if settings.is_production:
        if demo:
            raise SeedRefused("Demo content is never seeded when APP_ENV=production.")
        if not confirm_production:
            raise SeedRefused("APP_ENV=production: re-run with --confirm-production to insert reference data.")


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _insert(session: Session, report: SeedReport, model, values: dict) -> None:
    result = session.execute(insert(model).values(**values).on_conflict_do_nothing().returning(model.id))
    report.add(model.__tablename__, result.first() is not None)


def _id_for(session: Session, model, slug: str):
    return session.scalar(select(model.id).where(model.slug == slug))


def _at(day: str, hour: int = 0) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour), tzinfo=UTC)


def seed_reference(session: Session, report: SeedReport) -> None:
    data = _load("reference.json")
    _insert(
        session,
        report,
        Source,
        {
            "slug": ARXIV_SOURCE_SLUG,
            "name": "arXiv",
            "kind": SourceKind.RESEARCH_REPOSITORY,
            "canonical_domain": "arxiv.org",
            "homepage_url": "https://arxiv.org",
        },
    )
    arxiv_id = _id_for(session, Source, ARXIV_SOURCE_SLUG)

    for order, t in enumerate(data["tools"]):
        _insert(
            session,
            report,
            Tool,
            {
                "slug": t["id"],
                "name": t["name"],
                "provider": t["maker"],
                "category": t["category"],
                "summary": t["summary"],
                "good_for": t["goodFor"],
                "limitations": t["limitations"],
                "access_notes": t["access"],
                "official_url": t["officialUrl"],
                "status": PublicationStatus.PUBLISHED,
                "display_order": order,
            },
        )
    for p in data["researchPapers"]:
        _insert(
            session,
            report,
            ResearchPaper,
            {
                "slug": p["id"],
                "title": p["title"],
                "authors": p["authors"],
                "organisation": p["organisation"],
                "published_on": date.fromisoformat(p["published"]),
                "url": p["url"],
                "topic": "research",
                "plain_language_summary": p["plainLanguage"],
                "summary_origin": SummaryOrigin.EDITORIAL,
                "key_findings": p["demonstrated"],
                "limitations": p["limitations"],
                "source_id": arxiv_id if "arxiv.org" in p["url"] else None,
                "status": PublicationStatus.PUBLISHED,
            },
        )
    for order, e in enumerate(data["indiaEcosystem"]):
        _insert(
            session,
            report,
            IndiaEntry,
            {
                "slug": e["id"],
                "name": e["name"],
                "kind": _INDIA_KINDS[e["kind"]],
                "focus": e["focus"],
                "description": e["description"],
                "url": e["url"],
                "status": PublicationStatus.PUBLISHED,
                "display_order": order,
            },
        )
    for order, c in enumerate(data["careerSignals"]):
        _insert(
            session,
            report,
            CareerSignal,
            {
                "slug": c["id"],
                "kind": c["kind"],
                "title": c["title"],
                "description": c["description"],
                "start_with": c["startWith"],
                "related_topics": c["relatedTopics"],
                "status": PublicationStatus.PUBLISHED,
                "display_order": order,
            },
        )


def seed_demo(session: Session, report: SeedReport) -> None:
    data = _load("demo.json")
    _insert(
        session,
        report,
        Source,
        {
            "slug": DEMO_SOURCE_SLUG,
            "name": "Sample source",
            "kind": SourceKind.DEMO,
            # Reserved TLD (RFC 2606): can never resolve to a real publisher.
            "canonical_domain": "example.invalid",
            "is_active": False,
        },
    )
    source_id = _id_for(session, Source, DEMO_SOURCE_SLUG)

    for e in data["editions"]:
        week_start = date.fromisoformat(e["weekOf"])
        _insert(
            session,
            report,
            Edition,
            {
                "slug": e["id"],
                "number": e["number"],
                "title": e["title"],
                "week_start": week_start,
                "week_end": week_start + timedelta(days=6),
                "status": PublicationStatus.PUBLISHED,
                "published_at": _at(e["weekOf"]),
                "is_demo": True,
            },
        )

    positions: dict[str, int] = {}
    for s in data["stories"]:
        edition_id = _id_for(session, Edition, s["editionId"])
        positions[s["editionId"]] = positions.get(s["editionId"], 0) + 1
        image = s.get("image") or {}
        _insert(
            session,
            report,
            Story,
            {
                "slug": s["slug"],
                "title": s["headline"],
                "summary": s["dek"],
                "content": "\n\n".join(s["body"]),
                "topic": s["topic"],
                "tags": [],
                "status": ContentStatus.PUBLISHED,
                "is_demo": True,
                "is_featured": bool(s.get("featured")),
                "edition_id": edition_id,
                "edition_position": positions[s["editionId"]],
                "source_id": source_id,
                "original_url": None,
                "canonical_url": None,
                "published_at": _at(s["publishedAt"], 6),
                "reading_time_minutes": s["readingMinutes"],
                "image_url": image.get("src"),
                "image_alt": image.get("alt"),
                "image_credit": image.get("credit"),
                "impact_why_it_matters": s["impact"]["whyItMatters"],
                "impact_explore": s["impact"]["explore"],
                "impact_build_idea": s["impact"]["buildIdea"],
                "impact_disciplines": s["impact"]["disciplines"],
            },
        )

    b = data["briefing"]
    edition = session.scalars(select(Edition).where(Edition.slug == b["editionId"])).one()
    _insert(
        session,
        report,
        Briefing,
        {
            "edition_id": edition.id,
            "title": b["title"],
            "audio_url": None,
            "duration_seconds": None,
            "transcript": "\n\n".join(b["transcript"]),
            "status": PublicationStatus.PUBLISHED,
            "published_at": edition.published_at,
            "is_demo": True,
        },
    )

    for order, c in enumerate(data["campusItems"]):
        _insert(
            session,
            report,
            CampusItem,
            {
                "slug": c["id"],
                "kind": c["kind"].lower(),
                "title": c["title"],
                "description": c["description"],
                "when_text": c.get("when"),
                "status": PublicationStatus.PUBLISHED,
                "is_demo": True,
                "display_order": order,
            },
        )


def run_seed(session: Session, *, reference: bool, demo: bool) -> SeedReport:
    """Caller owns the transaction (see Database.write_session), so a failure inserts nothing."""
    report = SeedReport()
    if reference:
        seed_reference(session, report)
    if demo:
        seed_demo(session, report)
    return report
