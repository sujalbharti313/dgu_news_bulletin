"""The edition state machine. Every status change goes through `transition`, which rejects anything not listed
here; the API never accepts a status from a client. Publication is only reachable from `approved`.

    generating        → draft | review_required | generation_failed
    generation_failed → generating                       (automatic or admin retry)
    draft             → review_required | generating | archived
    review_required   → approved | rejected | draft | generating
    approved          → published | review_required       (a material edit invalidates the approval)
    rejected          → draft | generating | archived
    published         → (final in Stage 3.5; content is read-only)
    archived          → (final)
"""

from app.models.enums import EditionStatus as S


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"An edition cannot move from '{current}' to '{target}'.")
        self.current = current
        self.target = target
        self.safe_message = str(self)


TRANSITIONS: dict[S, frozenset[S]] = {
    S.GENERATING: frozenset({S.DRAFT, S.REVIEW_REQUIRED, S.GENERATION_FAILED}),
    S.GENERATION_FAILED: frozenset({S.GENERATING}),
    S.DRAFT: frozenset({S.REVIEW_REQUIRED, S.GENERATING, S.ARCHIVED}),
    S.REVIEW_REQUIRED: frozenset({S.APPROVED, S.REJECTED, S.DRAFT, S.GENERATING}),
    S.APPROVED: frozenset({S.PUBLISHED, S.REVIEW_REQUIRED}),
    S.REJECTED: frozenset({S.DRAFT, S.GENERATING, S.ARCHIVED}),
    S.PUBLISHED: frozenset(),
    S.ARCHIVED: frozenset(),
    S.WITHDRAWN: frozenset(),
}

# Content (text, selection, order) can be revised in these states. Editing an approved edition is allowed but
# invalidates the approval (approved → review_required); editing a rejected one reopens it as a draft.
EDITABLE = frozenset({S.DRAFT, S.REVIEW_REQUIRED, S.APPROVED, S.REJECTED})
REGENERATABLE = frozenset({S.GENERATION_FAILED, S.DRAFT, S.REVIEW_REQUIRED, S.REJECTED})


def can_transition(current: str, target: str) -> bool:
    try:
        return S(target) in TRANSITIONS[S(current)]
    except ValueError:
        return False


def transition(current: str, target: str) -> str:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
    return S(target).value


def after_edit(current: str) -> str:
    """Status after a content revision is saved."""
    status = S(current)
    if status not in EDITABLE:
        raise InvalidTransition(current, "edited")
    if status is S.APPROVED:
        return S.REVIEW_REQUIRED.value
    if status is S.REJECTED:
        return S.DRAFT.value
    return status.value


def allowed_actions(status: str, *, has_issues: bool) -> list[str]:
    """What the admin UI may offer. Informational only: every action is re-checked by the backend."""
    s = S(status)
    actions = []
    if s in EDITABLE:
        actions.append("edit")
    if s is S.DRAFT and not has_issues:
        actions.append("submit")
    if s is S.REVIEW_REQUIRED and not has_issues:
        actions.append("approve")
    if s is S.REVIEW_REQUIRED:
        actions += ["reject", "return_for_revision"]
    if s is S.APPROVED:
        actions.append("publish")
    if s in REGENERATABLE:
        actions.append("regenerate")
    if S.ARCHIVED in TRANSITIONS[s]:
        actions.append("archive")
    return actions
