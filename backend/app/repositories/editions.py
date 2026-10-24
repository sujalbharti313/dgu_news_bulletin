from sqlalchemy import func, select
from sqlalchemy.orm import Session, contains_eager

from app.models import Briefing, Edition
from app.repositories.visibility import public_briefing, public_edition


class EditionRepository:
    def __init__(self, session: Session, include_demo: bool):
        self.session = session
        self.include_demo = include_demo

    def page(self, *, offset: int, limit: int) -> tuple[list[Edition], int]:
        where = public_edition(self.include_demo)
        total = self.session.scalar(select(func.count()).select_from(Edition).where(where)) or 0
        rows = self.session.scalars(
            select(Edition).where(where).order_by(Edition.week_start.desc(), Edition.id).offset(offset).limit(limit)
        ).all()
        return list(rows), total

    def current(self) -> Edition | None:
        """The public edition with the latest week_start."""
        return self.session.scalars(
            select(Edition)
            .where(public_edition(self.include_demo))
            .order_by(Edition.week_start.desc(), Edition.id)
            .limit(1)
        ).first()

    def by_slug(self, slug: str) -> Edition | None:
        return self.session.scalars(
            select(Edition).where(public_edition(self.include_demo), Edition.slug == slug)
        ).first()

    def latest_briefing(self) -> Briefing | None:
        return self.session.scalars(
            select(Briefing)
            .join(Briefing.edition)
            .options(contains_eager(Briefing.edition))
            .where(public_briefing(self.include_demo))
            .order_by(Edition.week_start.desc(), Briefing.id)
            .limit(1)
        ).first()
