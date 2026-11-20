"""The edition state machine: only listed transitions, publication only from approved, edits invalidate approval."""

import pytest

from app.editions.states import InvalidTransition, after_edit, allowed_actions, can_transition, transition
from app.models.enums import EditionStatus as S

VALID = [
    (S.GENERATING, S.DRAFT),
    (S.GENERATING, S.REVIEW_REQUIRED),
    (S.GENERATING, S.GENERATION_FAILED),
    (S.GENERATION_FAILED, S.GENERATING),
    (S.DRAFT, S.REVIEW_REQUIRED),
    (S.REVIEW_REQUIRED, S.APPROVED),
    (S.REVIEW_REQUIRED, S.REJECTED),
    (S.REVIEW_REQUIRED, S.DRAFT),
    (S.APPROVED, S.PUBLISHED),
    (S.APPROVED, S.REVIEW_REQUIRED),
    (S.REJECTED, S.DRAFT),
    (S.DRAFT, S.ARCHIVED),
]


@pytest.mark.parametrize(("current", "target"), VALID)
def test_valid_transitions(current, target):
    assert transition(current, target) == target.value


@pytest.mark.parametrize(
    "current", [S.GENERATING, S.DRAFT, S.REVIEW_REQUIRED, S.GENERATION_FAILED, S.REJECTED, S.ARCHIVED, S.PUBLISHED]
)
def test_publication_is_only_reachable_from_approved(current):
    assert not can_transition(current, S.PUBLISHED)
    with pytest.raises(InvalidTransition):
        transition(current, S.PUBLISHED)


def test_approval_needs_review_required():
    for current in (S.GENERATING, S.DRAFT, S.GENERATION_FAILED, S.REJECTED, S.PUBLISHED):
        assert not can_transition(current, S.APPROVED)


def test_published_and_archived_are_final_and_unknown_values_rejected():
    for target in S:
        assert not can_transition(S.PUBLISHED, target)
        assert not can_transition(S.ARCHIVED, target)
    assert not can_transition("published", "hacked")
    assert not can_transition("bogus", "draft")


def test_edits_invalidate_approval_and_reopen_rejected():
    assert after_edit(S.APPROVED) == S.REVIEW_REQUIRED
    assert after_edit(S.REJECTED) == S.DRAFT
    assert after_edit(S.REVIEW_REQUIRED) == S.REVIEW_REQUIRED
    assert after_edit(S.DRAFT) == S.DRAFT
    for frozen in (S.PUBLISHED, S.GENERATING, S.ARCHIVED, S.GENERATION_FAILED):
        with pytest.raises(InvalidTransition):
            after_edit(frozen)


def test_allowed_actions_are_informational_and_respect_issues():
    assert "approve" in allowed_actions("review_required", has_issues=False)
    assert "approve" not in allowed_actions("review_required", has_issues=True)
    assert "publish" in allowed_actions("approved", has_issues=False)
    assert allowed_actions("published", has_issues=False) == []
    assert "regenerate" in allowed_actions("generation_failed", has_issues=True)
    assert "submit" in allowed_actions("draft", has_issues=False)
