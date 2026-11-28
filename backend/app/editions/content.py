"""The edition snapshot: what one revision of an edition says, and the rules it must satisfy before approval.

A revision's `content` is an `EditionContent` (JSON). Each story has two kinds of fields:
- Derived fields (cluster id, source URL, publisher, dates, category, topic, Stage 3.4 selection signals, the
  verification note and the original Stage 3.3 text). These are always built by backend code from database
  records, never accepted from a client or from the AI.
- Editorial text (headline, summary, technical explanation, why it matters, student relevance, project idea).
  Generated from Stage 3.3 (optionally tightened by Gemini) and editable by an admin, within strict limits.

`validate_content` returns structured issues; any `error` blocks submission, approval and publication.
`public_story_fields` is the single mapping from a snapshot story to the public `stories` columns, used both for
publishing and for the admin preview, so the preview shows exactly what will be published.
"""

import hashlib
import json
import math
import re
import unicodedata
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.editions.config import Limits
from app.intelligence.evidence import urls_in

CONTENT_VERSION = "1.0"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏  ﻿]")
_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!?]")
_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")
_SLUG = re.compile(r"[^a-z0-9]+")
EDITORIAL_FIELDS = (
    "headline",
    "summary",
    "technical_explanation",
    "why_it_matters",
    "student_relevance",
    "project_idea",
)
REQUIRED_STORY_FIELDS = ("headline", "summary", "why_it_matters", "student_relevance")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoryText(_Model):
    headline: str
    summary: str
    technical_explanation: str = ""
    why_it_matters: str
    student_relevance: str
    project_idea: str = ""


class Selection(_Model):
    source: Literal["ranking", "admin_added"]
    eligibility: Literal["eligible", "review_required"]
    rank: int | None = None
    shortlist_rank: int | None = None
    final_priority_score: float | None = None
    priority: str | None = None
    review_flags: list[str] = Field(default_factory=list)
    ranking_run_id: uuid.UUID | None = None
    review_acknowledged_by: uuid.UUID | None = None


class EditionStory(_Model):
    cluster_id: uuid.UUID
    cluster_revision: str
    # Editorial text (current) and the Stage 3.3 original it was generated from.
    text: StoryText
    original: StoryText
    # Derived from backend records only.
    source_url: str
    source_item_id: uuid.UUID | None = None
    publisher: str
    source_kind: str | None = None
    published_date: date | None = None
    category: str | None = None
    category_key: str | None = None
    topic: str
    disciplines: list[str] = Field(default_factory=list)
    explore: list[str] = Field(default_factory=list)
    verification_note: str
    source_count: int = 0
    selection: Selection


class EditionContent(_Model):
    content_version: Literal["1.0"] = CONTENT_VERSION
    title: str
    introduction: str
    editorial_note: str = ""
    week_start: date
    week_end: date
    timezone: str
    stories: list[EditionStory]
    composition: dict = Field(default_factory=dict, description="How the text was composed (ai / stage33_text).")


class Issue(_Model):
    severity: Literal["error", "warning"]
    code: str
    message: str
    path: str = ""


def clean_text(value: str | None, *, multiline: bool = True) -> str:
    """Normalise user/AI text: NFC, no control or zero-width characters, trimmed whitespace."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _CONTROL.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines) if multiline else " ".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text).strip()


def content_hash(content: EditionContent) -> str:
    data = content.model_dump(mode="json")
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _limit(field: str, limits: Limits) -> int:
    return getattr(limits, field)


def _text_issues(value: str, field: str, path: str, limits: Limits, *, required: bool) -> list[Issue]:
    issues = []
    if required and not value.strip():
        issues.append(Issue(severity="error", code="missing_field", message=f"{field} is required.", path=path))
    if len(value) > _limit(field, limits):
        issues.append(
            Issue(
                severity="error",
                code="too_long",
                message=f"{field} is longer than {_limit(field, limits)} characters.",
                path=path,
            )
        )
    if _TAG.search(value):
        issues.append(
            Issue(severity="error", code="markup_not_allowed", message=f"{field} must be plain text.", path=path)
        )
    if urls_in(value):
        issues.append(
            Issue(
                severity="error",
                code="url_in_text",
                message=f"{field} must not contain links; the original source link is shown separately.",
                path=path,
            )
        )
    return issues


def validate_content(
    content: EditionContent, limits: Limits, *, min_stories: int = 0, max_stories: int | None = None
) -> list[Issue]:
    """Structural and editorial rules. `max_stories` is the edition's story limit (`selection.target_stories`):
    admins may add candidates while editing, but an edition above the limit cannot be approved or published.
    Database checks (source links exist, candidates are eligible, URLs not already published) are done separately by
    the service before approval and publication."""
    issues: list[Issue] = []
    for field in ("title", "introduction"):
        issues += _text_issues(getattr(content, field), field, field, limits, required=True)
    issues += _text_issues(content.editorial_note, "editorial_note", "editorial_note", limits, required=False)
    if not content.stories:
        issues.append(Issue(severity="error", code="no_stories", message="The edition has no stories.", path="stories"))
    elif max_stories is not None and len(content.stories) > max_stories:
        issues.append(
            Issue(
                severity="error",
                code="too_many_stories",
                message=f"{len(content.stories)} stories: an edition has at most {max_stories}. Remove some.",
                path="stories",
            )
        )
    elif len(content.stories) < min_stories:
        issues.append(
            Issue(
                severity="warning",
                code="few_stories",
                message=f"Only {len(content.stories)} stories (the target minimum is {min_stories}).",
                path="stories",
            )
        )
    seen: set[uuid.UUID] = set()
    for i, story in enumerate(content.stories):
        path = f"stories[{i}]"
        if story.cluster_id in seen:
            issues.append(
                Issue(severity="error", code="duplicate_story", message="This story appears twice.", path=path)
            )
        seen.add(story.cluster_id)
        for field in EDITORIAL_FIELDS:
            issues += _text_issues(
                getattr(story.text, field), field, f"{path}.{field}", limits, required=field in REQUIRED_STORY_FIELDS
            )
        if not re.match(r"^https?://[^\s]+$", story.source_url or ""):
            issues.append(
                Issue(severity="error", code="missing_source_url", message="No valid source link.", path=path)
            )
        if not story.publisher.strip():
            issues.append(Issue(severity="error", code="missing_publisher", message="No publisher.", path=path))
        if story.selection.eligibility == "review_required":
            if story.selection.review_acknowledged_by is None:
                issues.append(
                    Issue(
                        severity="error",
                        code="review_not_acknowledged",
                        message="Stage 3.4 marked this story review_required; an admin must confirm it was reviewed.",
                        path=path,
                    )
                )
            else:
                issues.append(
                    Issue(
                        severity="warning",
                        code="review_required_story",
                        message="Added by an admin although Stage 3.4 marked it review_required.",
                        path=path,
                    )
                )
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def slugify(value: str, max_length: int = 90) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = _SLUG.sub("-", ascii_text).strip("-")[:max_length].strip("-")
    return slug or "story"


def story_slug(edition_slug: str, position: int, headline: str) -> str:
    return f"{edition_slug}-{position:02d}-{slugify(headline)}"[:160].rstrip("-")


def body_paragraphs(story: EditionStory) -> list[str]:
    paragraphs = []
    for block in (story.text.technical_explanation, story.text.why_it_matters):
        paragraphs += [p.strip() for p in block.split("\n\n") if p.strip()]
    paragraphs.append(story.verification_note)
    return paragraphs


def reading_minutes(story: EditionStory) -> int:
    words = sum(len(getattr(story.text, f).split()) for f in EDITORIAL_FIELDS)
    return max(1, min(120, math.ceil(words / 200)))


def public_story_fields(story: EditionStory, *, edition_slug: str, position: int) -> dict:
    """The `stories` columns for one published story. Used by publishing and by the admin preview."""
    return {
        "slug": story_slug(edition_slug, position, story.text.headline),
        "title": story.text.headline,
        "summary": story.text.summary,
        "content": "\n\n".join(body_paragraphs(story)),
        "topic": story.topic,
        "tags": [story.category] if story.category else [],
        "is_featured": position == 1,
        "edition_position": position,
        "original_url": story.source_url,
        "original_published_at_date": story.published_date,
        "reading_time_minutes": reading_minutes(story),
        "impact_why_it_matters": story.text.student_relevance,
        "impact_explore": story.explore,
        "impact_build_idea": story.text.project_idea or None,
        "impact_disciplines": story.disciplines,
    }
