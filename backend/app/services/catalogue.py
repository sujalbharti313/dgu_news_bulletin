"""Use cases for tools, research papers and the home-page reference collections."""

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.repositories.catalogue import CatalogueRepository
from app.schemas.catalogue import (
    CampusItemOut,
    CareerSignalOut,
    CareerSignalQuery,
    IndiaEntryOut,
    ResearchPaperOut,
    ResearchQuery,
    ToolOut,
    ToolQuery,
)
from app.schemas.common import Page, PageParams
from app.services import presenters


def _clean(q: str | None) -> str | None:
    return q.strip() or None if q else None


class CatalogueService:
    def __init__(self, session: Session, include_demo: bool):
        self.repo = CatalogueRepository(session, include_demo)

    def tools(self, p: ToolQuery) -> Page[ToolOut]:
        rows, total = self.repo.tools(category=p.category, query=_clean(p.q), offset=p.offset, limit=p.page_size)
        return Page[ToolOut].build(
            [presenters.tool_out(t) for t in rows], page=p.page, page_size=p.page_size, total=total
        )

    def tool(self, slug: str) -> ToolOut:
        tool = self.repo.tool(slug)
        if tool is None:
            raise NotFoundError("No published tool has that slug.")
        return presenters.tool_out(tool)

    def papers(self, p: ResearchQuery) -> Page[ResearchPaperOut]:
        rows, total = self.repo.papers(
            topic=p.topic, query=_clean(p.q), sort=p.sort, offset=p.offset, limit=p.page_size
        )
        return Page[ResearchPaperOut].build(
            [presenters.paper_out(r) for r in rows], page=p.page, page_size=p.page_size, total=total
        )

    def paper(self, id_or_slug: str) -> ResearchPaperOut:
        paper = self.repo.paper(id_or_slug)
        if paper is None:
            raise NotFoundError("No published research record matches that id or slug.")
        return presenters.paper_out(paper)

    def india_entries(self, p: PageParams) -> Page[IndiaEntryOut]:
        rows, total = self.repo.india_entries(offset=p.offset, limit=p.page_size)
        return Page[IndiaEntryOut].build(
            [presenters.india_entry_out(e) for e in rows], page=p.page, page_size=p.page_size, total=total
        )

    def career_signals(self, p: CareerSignalQuery) -> Page[CareerSignalOut]:
        rows, total = self.repo.career_signals(kind=p.kind, topic=p.topic, offset=p.offset, limit=p.page_size)
        return Page[CareerSignalOut].build(
            [presenters.career_signal_out(c) for c in rows], page=p.page, page_size=p.page_size, total=total
        )

    def campus_items(self, p: PageParams) -> Page[CampusItemOut]:
        rows, total = self.repo.campus_items(offset=p.offset, limit=p.page_size)
        return Page[CampusItemOut].build(
            [presenters.campus_item_out(c) for c in rows], page=p.page, page_size=p.page_size, total=total
        )
