"""Read queries for tools, research papers and the home-page reference collections."""

import uuid

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import CampusItem, CareerSignal, IndiaEntry, ResearchPaper, Tool
from app.repositories.visibility import public_campus_item, published


def contains_text(query: str, *columns) -> ColumnElement[bool]:
    """Case-insensitive substring match. LIKE wildcards in user input are escaped."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(*(col.ilike(pattern, escape="\\") for col in columns))


def _page(session: Session, stmt: Select, order, offset: int, limit: int) -> tuple[list, int]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = session.scalars(stmt.order_by(*order).offset(offset).limit(limit)).all()
    return list(rows), total


class CatalogueRepository:
    def __init__(self, session: Session, include_demo: bool):
        self.session = session
        self.include_demo = include_demo

    def tools(self, *, category: str | None, query: str | None, offset: int, limit: int):
        stmt = select(Tool).where(published(Tool))
        if category:
            stmt = stmt.where(Tool.category == category)
        if query:
            stmt = stmt.where(contains_text(query, Tool.name, Tool.provider, Tool.summary))
        return _page(self.session, stmt, (Tool.display_order, Tool.name, Tool.id), offset, limit)

    def tool(self, slug: str) -> Tool | None:
        return self.session.scalars(select(Tool).where(published(Tool), Tool.slug == slug)).first()

    def papers(self, *, topic: str | None, query: str | None, sort: str, offset: int, limit: int):
        stmt = select(ResearchPaper).where(published(ResearchPaper))
        if topic:
            stmt = stmt.where(ResearchPaper.topic == topic)
        if query:
            stmt = stmt.where(
                contains_text(query, ResearchPaper.title, ResearchPaper.plain_language_summary, ResearchPaper.authors)
            )
        date_order = ResearchPaper.published_on.asc() if sort == "oldest" else ResearchPaper.published_on.desc()
        return _page(self.session, stmt, (date_order, ResearchPaper.id), offset, limit)

    def paper(self, id_or_slug: str) -> ResearchPaper | None:
        try:
            key = ResearchPaper.id == uuid.UUID(id_or_slug)
        except ValueError:
            key = ResearchPaper.slug == id_or_slug
        return self.session.scalars(select(ResearchPaper).where(published(ResearchPaper), key)).first()

    def india_entries(self, *, offset: int, limit: int):
        stmt = select(IndiaEntry).where(published(IndiaEntry))
        return _page(self.session, stmt, (IndiaEntry.display_order, IndiaEntry.name, IndiaEntry.id), offset, limit)

    def career_signals(self, *, kind: str | None, topic: str | None, offset: int, limit: int):
        stmt = select(CareerSignal).where(published(CareerSignal))
        if kind:
            stmt = stmt.where(CareerSignal.kind == kind)
        if topic:
            stmt = stmt.where(CareerSignal.related_topics.contains([topic]))
        order = (CareerSignal.display_order, CareerSignal.title, CareerSignal.id)
        return _page(self.session, stmt, order, offset, limit)

    def campus_items(self, *, offset: int, limit: int):
        stmt = select(CampusItem).where(public_campus_item(self.include_demo))
        return _page(self.session, stmt, (CampusItem.display_order, CampusItem.title, CampusItem.id), offset, limit)
