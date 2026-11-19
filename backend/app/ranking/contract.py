"""The Stage 3.4 → Stage 3.5 interface: verification results, final editorial scores, eligibility and the
diversity-aware shortlist for one weekly edition.

Stage 3.5 reads it with `app.ranking.service.latest_ranking` (in-process) or, for inspection, the
token-protected `GET /api/v1/admin/ranking/runs/latest`.

Rules every consumer must follow:
- Nothing here is externally fact-checked (`externally_fact_checked` is always false). Verification compares the
  Stage 3.3 analysis with the source material Stage 3.1 collected, using rules in code and, where enabled, one
  Gemini consistency check. Source material is the evidence; AI analysis is never counted as evidence.
- Only stories with `eligibility_status = eligible` are in `shortlist`. `review_required` stories need an admin
  first; `ineligible` stories are kept for inspection only. Nothing is deleted or published by Stage 3.4.
- A run whose `status` is `incomplete` is NOT a finished ranking: some stories were still pending (not yet
  analysed, analysis out of date, or the AI check waiting for quota). Stage 3.5 must not treat it as final.
- Scores are computed by code from the configured weights (see `ranking_config_version`). A dimension that
  could not be assessed is null, listed in `unavailable_dimensions`, and contributes 0 to the final score.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel

CONTRACT_VERSION = "1.0"
CredibilityLevel = Literal["primary", "established", "provisional", "aggregator", "unknown", "low"]
CorroborationLevel = Literal[
    "multiple_independent",
    "official_plus_independent",
    "repeated_announcement",
    "syndicated_single_source",
    "single_source",
    "unclear",
]
ContentNature = Literal[
    "research_publication", "official_announcement", "factual_report", "opinion", "speculation", "promotional"
]
VerificationStatus = Literal["completed", "pending_ai_check", "ai_check_failed", "insufficient_input"]
AiCheckStatus = Literal["disabled", "not_selected", "completed", "cached", "pending", "failed", "invalid_output"]
Eligibility = Literal["eligible", "review_required", "ineligible"]
Priority = Literal["Exceptional", "High", "Medium", "Low", "Very Low"]
ClaimSupport = Literal["supported", "weakly_supported", "uncited", "inferred", "not_in_sources", "contradicted"]
RunStatus = Literal["completed", "incomplete", "failed"]


class _Frozen(ApiModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceAssessment(_Frozen):
    """Credibility of one source reference. `basis` says where the level came from."""

    ref_id: str
    item_id: uuid.UUID
    publisher: str | None = None
    site: str | None = None
    source_key: str
    source_kind: str
    relation: str
    is_copy: bool = Field(description="A duplicate or syndicated copy of another reference (counted once).")
    original_url_valid: bool
    credibility_level: CredibilityLevel
    credibility_score: int = Field(ge=0, le=100)
    basis: Literal["registry", "ingestion_category", "none"]
    reviewed_on: str | None = Field(default=None, description="When a person reviewed the registry entry.")
    reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CorroborationResult(_Frozen):
    level: CorroborationLevel
    source_count: int = Field(description="All source references in the cluster, copies included.")
    report_count: int = Field(description="Distinct reports after grouping duplicates and syndicated copies.")
    independent_source_count: int | None = Field(
        default=None, description="Estimated independent publishers; null when independence cannot be judged."
    )
    official_source_present: bool
    explanation: str
    limitations: list[str] = Field(default_factory=list)


class ClaimCheck(_Frozen):
    index: int
    statement: str
    basis: str
    source_ids: list[str]
    support: ClaimSupport
    lexical_coverage: float = Field(ge=0, le=1, description="Share of the claim's words found in its sources.")
    ai_verdict: str | None = None
    note: str | None = None


class Verification(_Frozen):
    source_credibility_score: int = Field(ge=0, le=100)
    source_credibility_level: CredibilityLevel
    source_assessments: list[SourceAssessment]
    evidence_quality_score: int = Field(ge=0, le=100)
    evidence_reasons: list[str]
    claim_consistency_score: int = Field(ge=0, le=100)
    consistency_reasons: list[str]
    claim_checks: list[ClaimCheck]
    corroboration: CorroborationResult
    corroboration_level: CorroborationLevel
    source_count: int
    independent_source_count: int | None = None
    technical_substance_score: int | None = Field(default=None, ge=0, le=100, description="Null = unknown.")
    technical_signals: list[str]
    content_nature: ContentNature
    hype_flags: list[str]
    unsupported_claims: list[str]
    conflicting_claims: list[str]
    verification_notes: list[str]
    limitations: list[str]
    verification_status: VerificationStatus
    ai_check_status: AiCheckStatus
    checked_by: list[Literal["rules", "gemini"]]
    externally_fact_checked: Literal[False] = False


class FinalScores(_Frozen):
    global_impact: int | None = Field(default=None, ge=0, le=100)
    technical_significance: int | None = Field(default=None, ge=0, le=100)
    student_relevance: int | None = Field(default=None, ge=0, le=100)
    evidence_quality: int | None = Field(default=None, ge=0, le=100)
    source_credibility: int | None = Field(default=None, ge=0, le=100)
    timeliness: int | None = Field(default=None, ge=0, le=100)
    final_priority_score: float = Field(ge=0, le=100)
    unavailable_dimensions: list[str] = Field(default_factory=list)
    weights: dict[str, float]
    adjustments: list[str] = Field(default_factory=list, description="How each dimension was derived or capped.")


class GateResult(_Frozen):
    gate: str
    action: Eligibility
    reason: str


class Timing(_Frozen):
    event_date: datetime | None = Field(default=None, description="Earliest known date of the story.")
    date_basis: Literal["published", "newsletter", "none"]
    in_window: bool | None = None
    days_outside_window: int | None = None


class RankedCandidate(_Frozen):
    story_cluster_id: uuid.UUID
    headline: str = Field(description="Stage 3.3 headline (AI-generated, unreviewed).")
    primary_category: str | None = None
    primary_category_key: str | None = None
    organisation: str | None = None
    coverage_groups: list[str] = Field(default_factory=list)
    final_scores: FinalScores
    priority_classification: Priority
    verification_status: VerificationStatus
    eligibility_status: Eligibility
    review_flags: list[str]
    gates: list[GateResult]
    ranking_explanation: str
    rank: int = Field(ge=1, description="Position among all evaluated stories by final score.")
    shortlisted: bool = False
    shortlist_rank: int | None = None
    selection_note: str | None = Field(default=None, description="Why it was or was not shortlisted.")
    duplicate_of: uuid.UUID | None = None
    timing: Timing
    cluster_revision: str
    analysis_generated_at: datetime
    verification: Verification


class PendingStory(_Frozen):
    story_cluster_id: uuid.UUID
    reason: Literal[
        "not_analysed", "analysis_out_of_date", "analysis_failed", "analysis_quota_limited", "ai_check_pending"
    ]
    detail: str


class ExcludedStory(_Frozen):
    """Evaluated but not rankable: Stage 3.3 had too little source text to analyse it. Kept for inspection."""

    story_cluster_id: uuid.UUID
    reason: Literal["insufficient_source_text"]
    detail: str


class FailedEvaluation(_Frozen):
    story_cluster_id: uuid.UUID
    error: str


class RankingTotals(_Frozen):
    clusters_in_window: int
    evaluated: int
    eligible: int
    review_required: int
    ineligible: int
    pending: int
    failed: int
    shortlisted: int


class RankingResult(_Frozen):
    contract_version: Literal["1.0"] = CONTRACT_VERSION
    run_id: uuid.UUID | None = None
    edition: dict[str, Any]
    processed_at: datetime
    status: RunStatus
    status_reason: str | None = None
    totals: RankingTotals
    ranked_candidate_ids: list[uuid.UUID] = Field(description="The shortlist, in order.")
    shortlist: list[RankedCandidate]
    candidates: list[RankedCandidate] = Field(description="Every evaluated story, by rank.")
    pending: list[PendingStory]
    excluded: list[ExcludedStory]
    failed: list[FailedEvaluation]
    ranking_config_version: str
    verification_config_version: str
    ai_check: dict[str, Any]
    notes: list[str] = Field(default_factory=list)
    dry_run: bool = False
    externally_fact_checked: Literal[False] = False
