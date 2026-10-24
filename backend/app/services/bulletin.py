"""Use cases for stories, editions, briefings and search."""

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.repositories.editions import EditionRepository
from app.repositories.stories import StoryFilter, StoryRepository
from app.schemas.common import Page, PageParams
from app.schemas.edition import BriefingOut, EditionDetailOut, EditionOut
from app.schemas.story import SearchQuery, StoryOut, StoryQuery, StorySearchPage
from app.services import presenters


class BulletinService:
    def __init__(self, session: Session, include_demo: bool):
        self.stories = StoryRepository(session, include_demo)
        self.editions = EditionRepository(session, include_demo)

    def list_stories(self, params: StoryQuery) -> Page[StoryOut]:
        query = params.q.strip() if params.q else None
        sort = params.sort or ("relevance" if query else "newest")
        f = StoryFilter(
            topic=params.topic,
            edition_slug=params.edition,
            featured=params.featured,
            query=query or None,
            published_from=params.published_from,
            published_to=params.published_to,
            sort=sort,
        )
        rows, total = self.stories.page(f, offset=params.offset, limit=params.page_size)
        return Page[StoryOut].build(
            [presenters.story_out(s) for s in rows], page=params.page, page_size=params.page_size, total=total
        )

    def search(self, params: SearchQuery) -> StorySearchPage:
        query = params.q.strip()
        f = StoryFilter(topic=params.topic, edition_slug=params.edition, query=query or None, sort="relevance")
        if not query:
            rows, total = [], 0
        else:
            rows, total = self.stories.page(f, offset=params.offset, limit=params.page_size)
        page = Page[StoryOut].build(
            [presenters.story_out(s) for s in rows], page=params.page, page_size=params.page_size, total=total
        )
        return StorySearchPage(**page.model_dump(), query=query)

    def get_story(self, id_or_slug: str) -> StoryOut:
        story = self.stories.get(id_or_slug)
        if story is None:
            raise NotFoundError("No published story matches that id or slug.")
        return presenters.story_out(story)

    def list_editions(self, params: PageParams) -> Page[EditionOut]:
        rows, total = self.editions.page(offset=params.offset, limit=params.page_size)
        return Page[EditionOut].build(
            [presenters.edition_out(e) for e in rows], page=params.page, page_size=params.page_size, total=total
        )

    def current_edition(self) -> EditionDetailOut:
        edition = self.editions.current()
        if edition is None:
            raise NotFoundError("No editions have been published yet.")
        return presenters.edition_detail_out(edition, self.stories.for_edition(edition.id))

    def edition(self, slug: str) -> EditionDetailOut:
        edition = self.editions.by_slug(slug)
        if edition is None:
            raise NotFoundError("No published edition has that slug.")
        return presenters.edition_detail_out(edition, self.stories.for_edition(edition.id))

    def latest_briefing(self) -> BriefingOut:
        briefing = self.editions.latest_briefing()
        if briefing is None:
            raise NotFoundError("No briefing has been published yet.")
        return presenters.briefing_out(briefing)
