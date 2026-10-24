import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, contains_eager

from app.models import Edition, Story
from app.repositories.visibility import public_story

SEARCH_CONFIG = "english"


@dataclass(frozen=True)
class StoryFilter:
    topic: str | None = None
    edition_slug: str | None = None
    featured: bool | None = None
    query: str | None = None
    published_from: date | None = None
    published_to: date | None = None
    sort: str = "newest"


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


class StoryRepository:
    def __init__(self, session: Session, include_demo: bool):
        self.session = session
        self.include_demo = include_demo

    def _base(self) -> Select:
        # Edition and source are many-to-one: one joined query, no N+1.
        return (
            select(Story)
            .join(Story.edition)
            .join(Story.source)
            .options(contains_eager(Story.edition), contains_eager(Story.source))
            .where(public_story(self.include_demo))
        )

    def _filtered(self, f: StoryFilter) -> Select:
        stmt = self._base()
        if f.topic:
            stmt = stmt.where(Story.topic == f.topic)
        if f.edition_slug:
            stmt = stmt.where(Edition.slug == f.edition_slug)
        if f.featured is not None:
            stmt = stmt.where(Story.is_featured.is_(f.featured))
        if f.published_from:
            stmt = stmt.where(Story.published_at >= _start_of(f.published_from))
        if f.published_to:
            stmt = stmt.where(Story.published_at < _start_of(f.published_to + timedelta(days=1)))
        if f.query:
            stmt = stmt.where(Story.search_vector.op("@@")(self._tsquery(f.query)))
        return stmt

    @staticmethod
    def _tsquery(query: str):
        # websearch_to_tsquery never raises on user syntax and is fully parameterised.
        return func.websearch_to_tsquery(SEARCH_CONFIG, query)

    def page(self, f: StoryFilter, *, offset: int, limit: int) -> tuple[list[Story], int]:
        stmt = self._filtered(f)
        total = self.session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        if f.sort == "relevance" and f.query:
            rank = func.ts_rank_cd(Story.search_vector, self._tsquery(f.query))
            order = (rank.desc(), Story.published_at.desc(), Story.id)
        elif f.sort == "oldest":
            order = (Story.published_at.asc(), Story.id)
        else:
            order = (Story.published_at.desc(), Story.id)
        rows = self.session.scalars(stmt.order_by(*order).offset(offset).limit(limit)).all()
        return list(rows), total

    def for_edition(self, edition_id: uuid.UUID) -> list[Story]:
        """Every public story of one edition: featured first, then newest, matching the frontend's sample adapter."""
        stmt = (
            self._base()
            .where(Story.edition_id == edition_id)
            .order_by(
                Story.is_featured.desc(),
                Story.published_at.desc(),
                Story.edition_position.asc().nulls_last(),
                Story.id,
            )
        )
        return list(self.session.scalars(stmt).all())

    def get(self, id_or_slug: str) -> Story | None:
        try:
            key = Story.id == uuid.UUID(id_or_slug)
        except ValueError:
            key = Story.slug == id_or_slug
        return self.session.scalars(self._base().where(key)).first()
