from sqlalchemy import Boolean, CheckConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_check, slug_check, url_check
from app.models.enums import SourceKind


class Source(UUIDPrimaryKey, Timestamps, Base):
    """A publisher or feed that stories, tools and papers are attributed to."""

    __tablename__ = "sources"
    __table_args__ = (
        slug_check(),
        enum_check("kind", SourceKind),
        url_check("homepage_url"),
        url_check("feed_url"),
        # Bare, lower-case host such as "arxiv.org": no scheme, path or port.
        CheckConstraint(
            "canonical_domain = lower(canonical_domain) AND canonical_domain ~ '^[a-z0-9.-]+$'",
            name="canonical_domain_format",
        ),
        # One source of each kind per domain (an official blog and its RSS feed may both exist).
        UniqueConstraint("canonical_domain", "kind"),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    canonical_domain: Mapped[str] = mapped_column(String(253))
    homepage_url: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str | None] = mapped_column(Text, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
