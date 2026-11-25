"""Publication confirmation: the gate in front of every Stage 3.6 deletion.

`verify_publication` decides, from database records only (never from AI output or a client), whether an edition is
confirmed published and its public archive is complete and independent of the working data cleanup may remove.
It returns safe reason codes for every failed check; any failure means no deletion for that week. It is run when a
job is created, again when its manifest is staged, and again right before every deletion pass.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.editions.content import EditionContent, content_hash
from app.editions.schedule import edition_slug
from app.models import Edition, EditionAuditEntry, EditionItem, EditionRevision, Story
from app.models.enums import EditionStatus
from app.ranking.window import EditionWindow
from app.repositories.editions import EditionRepository
from app.repositories.stories import StoryRepository
from app.services.urls import canonicalize_url


@dataclass
class PublicationCheck:
    edition_id: uuid.UUID
    reasons: list[str] = field(default_factory=list)
    window: EditionWindow | None = None
    published_at: datetime | None = None
    story_count: int = 0
    selected_cluster_ids: list[uuid.UUID] = field(default_factory=list)
    ranking_run_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "weekStart": self.window.start_date.isoformat() if self.window else None,
            "publishedAt": self.published_at.isoformat() if self.published_at else None,
            "stories": self.story_count,
        }


def verify_publication(s: Session, edition_id: uuid.UUID, now: datetime) -> PublicationCheck:
    check = PublicationCheck(edition_id)
    fail = check.reasons.append
    edition = s.get(Edition, edition_id)
    # 1. The edition exists, is a real (non-demo) Stage 3.5 edition.
    if edition is None:
        fail("edition_missing")
        return check
    if edition.is_demo:
        fail("demo_edition")
    if edition.current_revision <= 0:
        fail("not_a_stage35_edition")
    # 2. The week identifier and collection period are consistent.
    window = EditionWindow(edition.week_start, edition.timezone or "Asia/Kolkata")
    check.window = window
    if edition.week_start.weekday() != 0 or edition.week_end != window.end_date:
        fail("week_period_inconsistent")
    elif edition.slug != edition_slug(window):
        fail("week_identifier_inconsistent")
    if now < window.end:
        fail("week_not_ended")
    # 3./4./9. Published, with a timestamp in the past, and not in a failed or uncertain state.
    if edition.status != EditionStatus.PUBLISHED:
        fail("not_published")
    if edition.published_at is None:
        fail("published_at_missing")
    elif edition.published_at > now:
        fail("published_at_in_future")
    check.published_at = edition.published_at
    if edition.published_revision is None or edition.published_revision != edition.approved_revision:
        fail("published_revision_uncertain")
    if check.reasons:
        return check

    published_audit = s.scalar(
        select(func.max(EditionAuditEntry.id)).where(
            EditionAuditEntry.edition_id == edition.id,
            EditionAuditEntry.action == "published",
            EditionAuditEntry.revision == edition.published_revision,
        )
    )
    if published_audit is None:
        fail("publication_not_audited")
    else:
        later_failure = s.scalar(
            select(func.count())
            .select_from(EditionAuditEntry)
            .where(
                EditionAuditEntry.edition_id == edition.id,
                EditionAuditEntry.action == "publish_failed",
                EditionAuditEntry.id > published_audit,
            )
        )
        if later_failure:
            fail("publication_failure_after_publish")

    # 5. The published snapshot is complete and unchanged.
    rev = s.scalar(
        select(EditionRevision).where(
            EditionRevision.edition_id == edition.id, EditionRevision.revision == edition.published_revision
        )
    )
    if rev is None:
        fail("snapshot_missing")
        return check
    try:
        content = EditionContent.model_validate(rev.content)
    except Exception:  # noqa: BLE001 - an unreadable snapshot is a failed check, never an exception
        fail("snapshot_unreadable")
        return check
    if content_hash(content) != rev.content_hash or rev.content_hash != edition.approved_content_hash:
        fail("snapshot_hash_mismatch")
    if not content.stories:
        fail("snapshot_has_no_stories")
    if content.week_start != edition.week_start:
        fail("snapshot_week_mismatch")
    check.selected_cluster_ids = [st.cluster_id for st in content.stories]
    check.ranking_run_ids = sorted(
        {st.selection.ranking_run_id for st in content.stories if st.selection.ranking_run_id}, key=str
    )
    if edition.ranking_run_id:
        check.ranking_run_ids = sorted({*check.ranking_run_ids, edition.ranking_run_id}, key=str)

    # 6./7. Every selected story exists as a published public story, in order, with its source link and source.
    stories = s.scalars(select(Story).where(Story.edition_id == edition.id).order_by(Story.edition_position)).all()
    check.story_count = len(stories)
    if len(stories) != len(content.stories):
        fail("published_story_count_mismatch")
    for pos, (snap, row) in enumerate(zip(content.stories, stories, strict=False), start=1):
        if row.edition_position != pos or row.status != "published" or row.published_at is None:
            fail("published_story_incomplete")
            break
        if not row.original_url or row.original_url != snap.source_url or row.source_id is None:
            fail("source_link_missing")
            break
        try:
            if row.canonical_url != canonicalize_url(snap.source_url):
                fail("source_link_mismatch")
                break
        except ValueError:
            fail("source_link_invalid")
            break
        if not row.title or not row.summary or not row.impact_why_it_matters:
            fail("published_story_incomplete")
            break
    items = s.scalars(select(EditionItem).where(EditionItem.edition_id == edition.id)).all()
    if {i.cluster_id for i in items} != set(check.selected_cluster_ids):
        fail("selection_mismatch")
    if any(i.story_id is None for i in items):
        fail("selection_not_linked_to_public_story")

    # 8. Publicly retrievable through the same filters the public API uses.
    public = EditionRepository(s, include_demo=False).by_slug(edition.slug)
    if public is None:
        fail("not_publicly_retrievable")
    elif len(StoryRepository(s, include_demo=False).for_edition(edition.id)) != len(content.stories):
        fail("public_stories_incomplete")
    return check
