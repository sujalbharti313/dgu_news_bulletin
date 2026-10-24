from datetime import date, datetime

from app.schemas.common import ApiModel, Provenance
from app.schemas.story import StoryOut


class EditionOut(ApiModel):
    """Mirrors `Edition` in src/lib/types.ts. `id` is the edition slug."""

    id: str
    slug: str
    number: int
    title: str
    week_of: date
    week_end: date
    published_at: datetime
    provenance: Provenance


class EditionDetailOut(EditionOut):
    stories: list[StoryOut]


class BriefingOut(ApiModel):
    """Mirrors `Briefing` in src/lib/types.ts."""

    edition_id: str
    title: str
    audio_url: str | None
    duration_seconds: int | None
    transcript: list[str]
    provenance: Provenance
