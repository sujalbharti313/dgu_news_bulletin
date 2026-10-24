"""Map database rows to the public response contracts (src/lib/types.ts)."""

from app.models import Briefing, CampusItem, CareerSignal, Edition, IndiaEntry, ResearchPaper, Story, Tool
from app.models.enums import INDIA_KIND_LABELS, IndiaEntryKind
from app.schemas.catalogue import CampusItemOut, CareerSignalOut, IndiaEntryOut, ResearchPaperOut, ToolOut
from app.schemas.edition import BriefingOut, EditionDetailOut, EditionOut
from app.schemas.story import StoryImage, StoryOut, StudentImpact


def provenance(is_demo: bool) -> str:
    return "sample" if is_demo else "live"


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def story_out(story: Story) -> StoryOut:
    shown_date = story.original_published_at or story.published_at
    return StoryOut(
        id=story.id,
        slug=story.slug,
        provenance=provenance(story.is_demo),
        edition_id=story.edition.slug,
        headline=story.title,
        dek=story.summary,
        body=paragraphs(story.content),
        topic=story.topic,
        source_name=story.source.name,
        source_url=story.original_url,
        published_at=shown_date.date(),
        original_published_at=story.original_published_at,
        reading_minutes=story.reading_time_minutes,
        impact=StudentImpact(
            why_it_matters=story.impact_why_it_matters or "",
            explore=story.impact_explore,
            build_idea=story.impact_build_idea or "",
            disciplines=story.impact_disciplines,
        ),
        image=(
            StoryImage(src=story.image_url, alt=story.image_alt, credit=story.image_credit) if story.image_url else None
        ),
        featured=story.is_featured,
        tags=story.tags,
    )


def edition_out(edition: Edition) -> EditionOut:
    return EditionOut(
        id=edition.slug,
        slug=edition.slug,
        number=edition.number,
        title=edition.title,
        week_of=edition.week_start,
        week_end=edition.week_end,
        published_at=edition.published_at,
        provenance=provenance(edition.is_demo),
    )


def edition_detail_out(edition: Edition, stories: list[Story]) -> EditionDetailOut:
    return EditionDetailOut(**edition_out(edition).model_dump(), stories=[story_out(s) for s in stories])


def briefing_out(briefing: Briefing) -> BriefingOut:
    return BriefingOut(
        edition_id=briefing.edition.slug,
        title=briefing.title,
        audio_url=briefing.audio_url,
        duration_seconds=briefing.duration_seconds,
        transcript=paragraphs(briefing.transcript),
        provenance=provenance(briefing.is_demo),
    )


def tool_out(tool: Tool) -> ToolOut:
    return ToolOut(
        id=tool.slug,
        name=tool.name,
        maker=tool.provider,
        category=tool.category,
        summary=tool.summary,
        good_for=tool.good_for,
        limitations=tool.limitations,
        access=tool.access_notes,
        official_url=tool.official_url,
        pricing_notes=tool.pricing_notes,
        last_verified_at=tool.last_verified_at,
    )


def paper_out(paper: ResearchPaper) -> ResearchPaperOut:
    return ResearchPaperOut(
        id=paper.slug,
        title=paper.title,
        authors=paper.authors or "",
        organisation=paper.organisation or "",
        published=paper.published_on,
        plain_language=paper.plain_language_summary,
        summary_origin=paper.summary_origin,
        demonstrated=paper.key_findings,
        limitations=paper.limitations,
        url=paper.url,
        doi=paper.doi,
        topic=paper.topic,
    )


def india_entry_out(entry: IndiaEntry) -> IndiaEntryOut:
    return IndiaEntryOut(
        id=entry.slug,
        name=entry.name,
        kind=INDIA_KIND_LABELS[IndiaEntryKind(entry.kind)],
        focus=entry.focus,
        description=entry.description,
        url=entry.url,
    )


def career_signal_out(signal: CareerSignal) -> CareerSignalOut:
    return CareerSignalOut(
        id=signal.slug,
        kind=signal.kind,
        title=signal.title,
        description=signal.description,
        start_with=signal.start_with,
        related_topics=signal.related_topics,
    )


def campus_item_out(item: CampusItem) -> CampusItemOut:
    return CampusItemOut(
        id=item.slug,
        provenance=provenance(item.is_demo),
        kind=item.kind.capitalize(),
        title=item.title,
        description=item.description,
        when=item.when_text,
    )
