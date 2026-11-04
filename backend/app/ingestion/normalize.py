"""RawItem -> NormalizedItem: validate, clean, canonicalise. Never invents missing values."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.ingestion.errors import UnsafeUrlError
from app.ingestion.models import NormalizedItem, RawItem
from app.ingestion.text import clean_text
from app.ingestion.url_safety import validate_url
from app.models.enums import IngestionSourceType
from app.services.urls import canonicalize_url

MAX_TITLE = 500
MAX_EXCERPT = 600
MAX_PUBLISHER = 200
MAX_EXTRA_STRING = 300
MAX_EXTRA_LIST = 10
# Feeds sometimes carry clock skew; anything further in the future than this is not trusted.
FUTURE_TOLERANCE = timedelta(days=1)
EARLIEST_PLAUSIBLE = datetime(1990, 1, 1, tzinfo=UTC)


class InvalidItem(ValueError):
    """The item is unusable (no title, no safe URL). Counted, never stored."""


def dedup_key_for(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _clean_extra(extra: dict) -> dict:
    """Keep only small scalar metadata; everything is treated as plain text."""
    cleaned: dict = {}
    for key, value in list(extra.items())[:20]:
        key = str(key)[:40]
        if isinstance(value, bool | int | float) or value is None:
            cleaned[key] = value
        elif isinstance(value, str):
            text = clean_text(value, MAX_EXTRA_STRING, is_html=False)
            if text:
                cleaned[key] = text
        elif isinstance(value, list | tuple):
            items = [clean_text(str(v), 80, is_html=False) for v in value[:MAX_EXTRA_LIST]]
            cleaned[key] = [v for v in items if v]
    return cleaned


def _plausible_date(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if value < EARLIEST_PLAUSIBLE or value > now + FUTURE_TOLERANCE:
        return None
    return value


def normalize_item(
    raw: RawItem,
    *,
    source_key: str,
    source_type: IngestionSourceType,
    collected_at: datetime,
    allow_http: bool = True,
) -> NormalizedItem:
    title = clean_text(raw.title, MAX_TITLE)
    if not title:
        raise InvalidItem("missing title")
    if not raw.url:
        raise InvalidItem("missing url")
    try:
        # Article links are stored, not fetched, in Stage 3.1; http is accepted for them but internal
        # hosts, IP literals in private ranges, credentials and odd ports are not.
        original_url = validate_url(raw.url.strip(), allow_http=allow_http)
        canonical_url = canonicalize_url(original_url)
    except (UnsafeUrlError, ValueError) as exc:
        raise InvalidItem("unsafe or malformed url") from exc

    extra = _clean_extra(raw.extra)
    published_at = _plausible_date(raw.published_at, collected_at)
    if raw.published_at is not None and published_at is None:
        extra["date_rejected"] = True

    return NormalizedItem(
        source_key=source_key,
        source_type=source_type,
        title=title,
        original_url=original_url,
        canonical_url=canonical_url,
        dedup_key=dedup_key_for(canonical_url),
        publisher=clean_text(raw.publisher, MAX_PUBLISHER),
        published_at=published_at,
        excerpt=clean_text(raw.description, MAX_EXCERPT),
        feed_identifier=raw.feed_identifier[:500],
        collected_at=collected_at,
        extra=extra,
    )
