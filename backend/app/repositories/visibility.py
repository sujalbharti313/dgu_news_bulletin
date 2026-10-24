"""The publication rules, in one place. Every public query goes through these filters.

- An edition is public when status = 'published' and published_at <= now().
- A story is public when it is published, published_at <= now(), and its edition is public.
  Stories without an edition (for example Phase 3 drafts) are never public.
- A briefing is public when it is published and its edition is public.
- Reference records (tools, papers, India entries, career signals, campus items) are public when published.
- Demonstration records (is_demo = true) are hidden unless EXPOSE_DEMO_CONTENT is on,
  which settings validation forbids in production.
"""

from sqlalchemy import ColumnElement, and_, func, true

from app.models import Briefing, CampusItem, Edition, Story
from app.models.enums import ContentStatus, PublicationStatus


def _demo_ok(column, include_demo: bool) -> ColumnElement[bool]:
    return true() if include_demo else column.is_(False)


def public_edition(include_demo: bool) -> ColumnElement[bool]:
    return and_(
        Edition.status == PublicationStatus.PUBLISHED,
        Edition.published_at <= func.now(),
        _demo_ok(Edition.is_demo, include_demo),
    )


def public_story(include_demo: bool) -> ColumnElement[bool]:
    """Assumes the query joins Story.edition."""
    return and_(
        Story.status == ContentStatus.PUBLISHED,
        Story.published_at <= func.now(),
        _demo_ok(Story.is_demo, include_demo),
        public_edition(include_demo),
    )


def public_briefing(include_demo: bool) -> ColumnElement[bool]:
    """Assumes the query joins Briefing.edition."""
    return and_(
        Briefing.status == PublicationStatus.PUBLISHED,
        Briefing.published_at <= func.now(),
        _demo_ok(Briefing.is_demo, include_demo),
        public_edition(include_demo),
    )


def published(model) -> ColumnElement[bool]:
    return model.status == PublicationStatus.PUBLISHED


def public_campus_item(include_demo: bool) -> ColumnElement[bool]:
    return and_(published(CampusItem), _demo_ok(CampusItem.is_demo, include_demo))
