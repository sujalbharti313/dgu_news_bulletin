"""The Stage 3.3 → Stage 3.4 interface: validated, evidence-aware AI analysis of one story cluster.

Stage 3.4 (verification and ranking) reads it with `app.intelligence.service.list_story_analyses`
(in-process) or, for inspection, the token-protected `GET /api/v1/admin/intelligence/analyses`.

Rules every consumer must follow:
- Every text field written by the model (`headline`, `summary`, `technical_explanation`, `why_it_matters`,
  `student_relevance`, `project_ideas`, `understanding`, `priority_assessment.reasoning`, `claims`,
  `evidence_notes`, `uncertainties`) is AI-GENERATED and unreviewed. Verify and edit it before publication.
- `project_ideas` are AI suggestions inspired by the story, never projects announced by a source
  (`kind` is always `ai_generated_suggestion`).
- `sources` are copied from Stage 3.2 by code, never written by the model. Each has a stable `ref_id`
  (S1, S2, ...) that `claims` and `technical_details` cite. URLs, publishers and dates are exactly what the
  sources provided; missing values stay null.
- Nothing here is independently verified (`verification` is always `not_independently_verified`).
  `corroboration` counts independent publishers; syndicated or duplicate copies count once.
- `priority_assessment` is PRELIMINARY. The overall score is computed by code from the dimension scores with
  fixed weights and evidence caps; Stage 3.4 makes the final ranking.
- `analysis_status = needs_review` means a human must look at `review_flags` first;
  `insufficient_evidence` means the sources were too thin to analyse responsibly.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.intelligence.categories import FrontendTopic
from app.intelligence.draft import ClaimBasis, DevelopmentType, RelevanceArea, Sufficiency, TechnicalStatus
from app.processing.contract import EventType, Relation, SourceKind
from app.schemas.common import ApiModel

CONTRACT_VERSION = "1.0"
Corroboration = Literal["no_sources_cited", "single_report", "multiple_independent_reports"]
PriorityLevel = Literal["Critical", "High", "Medium", "Low"]
AnalysisStatusValue = Literal["completed", "needs_review", "insufficient_evidence"]


class _Frozen(ApiModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnalysisSource(_Frozen):
    """A Stage 3.2 source reference, metadata only (no excerpt). Values a source did not provide are null."""

    ref_id: str = Field(description="Stable id within this analysis (S1, S2, ...), cited by claims.")
    item_id: uuid.UUID
    source_key: str
    source_name: str | None = None
    source_kind: SourceKind
    relation: Relation
    duplicate_of: uuid.UUID | None = None
    title: str
    publisher: str | None = None
    site: str | None = None
    original_url: str
    canonical_url: str
    published_at: datetime | None = None
    newsletter_date: datetime | None = None
    sent_to_model: bool = Field(description="False when dropped to stay within the input budget.")
    content_flags: list[str] = Field(default_factory=list)


class Understanding(_Frozen):
    main_event: str
    involved: list[str]
    development_type: DevelopmentType
    what_is_new: str | None = None
    key_points: list[str]


class WhyItMatters(_Frozen):
    established_facts: str | None = Field(default=None, description="Significance the sources state.")
    interpretation: str | None = Field(default=None, description="AI interpretation, not a reported fact.")


class RelevanceAngle(_Frozen):
    area: RelevanceArea
    detail: str


class StudentRelevance(_Frozen):
    summary: str | None = None
    angles: list[RelevanceAngle] = Field(default_factory=list)


class ProjectIdea(_Frozen):
    kind: Literal["ai_generated_suggestion"] = "ai_generated_suggestion"
    title: str
    description: str
    technologies: list[str]


class PriorityAssessment(_Frozen):
    global_impact: int = Field(ge=0, le=100)
    technical_significance: int = Field(ge=0, le=100)
    student_relevance: int = Field(ge=0, le=100)
    timeliness: int = Field(ge=0, le=100)
    research_practical_importance: int = Field(ge=0, le=100)
    weighted_score: int = Field(ge=0, le=100, description="Weighted sum of the dimensions, before caps.")
    overall_score: int = Field(ge=0, le=100, description="Preliminary priority score after evidence caps.")
    priority_level: PriorityLevel
    adjustments: list[str] = Field(default_factory=list, description="Caps applied by code, with reasons.")
    marketing_heavy: bool
    reasoning: str
    weights: dict[str, float]
    preliminary: Literal[True] = True


class Claim(_Frozen):
    statement: str
    basis: ClaimBasis = Field(description="stated_in_sources = a source says it; inferred = the model's inference.")
    source_ids: list[str]
    attributed_to: str | None = None
    corroboration: Corroboration
    independent_publishers: int
    cites_official_source: bool
    verification: Literal["not_independently_verified"] = "not_independently_verified"


class TechnicalDetail(_Frozen):
    label: str
    detail: str
    source_ids: list[str]


class Evidence(_Frozen):
    sufficiency: Sufficiency
    missing_information: list[str]
    corroboration: Corroboration
    independent_publishers: int
    official_source_present: bool
    verification: Literal["not_independently_verified"] = "not_independently_verified"


class Generation(_Frozen):
    ai_generated: Literal[True] = True
    provider: Literal["gemini"] = "gemini"
    model_id: str = Field(description="The model id requested (exactly AI_MODEL).")
    model_version: str | None = Field(default=None, description="The model version Gemini reported, if any.")
    engine_key: str
    generated_at: datetime
    calls: int = Field(description="Gemini calls for this analysis, including retries and repairs.")
    input_tokens: int
    output_tokens: int


class StoryAnalysis(_Frozen):
    """AI analysis of one Stage 3.2 story cluster."""

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    story_cluster_id: uuid.UUID
    cluster_revision: str = Field(description="Stage 3.2 `processing.revision` this analysis was made from.")
    cluster_contract_version: str
    analysis_status: AnalysisStatusValue
    review_flags: list[str] = Field(default_factory=list)
    representative_item_id: uuid.UUID
    event_type: EventType | None = Field(default=None, description="Stage 3.2's rule-based event type.")
    earliest_published_at: datetime | None = None
    latest_published_at: datetime | None = None
    headline: str
    summary: str | None = None
    understanding: Understanding
    technical_explanation: str | None = None
    technical_explanation_status: TechnicalStatus
    why_it_matters: WhyItMatters
    student_relevance: StudentRelevance
    project_ideas: list[ProjectIdea] = Field(max_length=3)
    primary_category: str = Field(description="Category label, e.g. 'AI Research'.")
    primary_category_key: str
    secondary_categories: list[str] = Field(description="Category labels.")
    secondary_category_keys: list[str]
    category_rationale: str
    topic: FrontendTopic | None = Field(default=None, description="Frontend topic mapped from the primary category.")
    priority_assessment: PriorityAssessment
    technical_details: list[TechnicalDetail]
    claims: list[Claim]
    evidence: Evidence
    evidence_notes: list[str]
    uncertainties: list[str]
    sources: list[AnalysisSource] = Field(min_length=1)
    model_id: str
    generation: Generation
    content_is_ai_generated: Literal[True] = True
