"""The Stage 3.3 → Stage 3.4 interface: validated, evidence-aware intelligence for one story cluster.

Stage 3.4 (verification and ranking) reads it with `app.intelligence.service.list_story_intelligence`
(in-process) or, for inspection, the token-protected `GET /api/v1/admin/intelligence/stories`.

Rules every consumer must follow:
- Every text field written by the model (`headline`, `summary`, `technical_explanation`, `why_it_matters`,
  `student_relevance`, `project_ideas`, `classification.rationale`, `evidence.claims`) is AI-GENERATED and
  unreviewed. It must be verified and edited before publication.
- `project_ideas` are AI suggestions, not facts from any article (`kind` is always `ai_generated_suggestion`).
- `sources` are copied from Stage 3.2 by code, never written by the model. Each has a stable `ref_id`
  (S1, S2, ...) that `claims` and `technical_details` cite. URLs, publishers and dates are exactly what the
  sources provided; missing values stay null.
- `evidence.verification` is always `not_independently_verified`: this stage only records what sources
  *report*. `corroboration` counts independent publishers; syndicated or duplicate copies of one article
  count once.
- `status = needs_review` means a human must look at the `review_flags` before the story is used.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.intelligence.categories import FrontendTopic
from app.intelligence.draft import ClaimBasis, Difficulty, RelevanceArea, Sufficiency
from app.processing.contract import EventType, Relation, SourceKind
from app.schemas.common import ApiModel

CONTRACT_VERSION = "1.0"
Corroboration = Literal["no_sources_cited", "single_report", "multiple_independent_reports"]


class _Frozen(ApiModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntelligenceSource(_Frozen):
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
    content_flags: list[str] = Field(default_factory=list)


class Claim(_Frozen):
    statement: str
    basis: ClaimBasis = Field(description="stated_in_sources = a source says it; inferred = the model's inference.")
    source_ids: list[str]
    attributed_to: str | None = None
    corroboration: Corroboration
    independent_publishers: int = Field(description="Distinct publishers among the cited, non-duplicate sources.")
    cites_official_source: bool
    verification: Literal["not_independently_verified"] = "not_independently_verified"


class TechnicalDetail(_Frozen):
    label: str
    detail: str
    source_ids: list[str]


class Evidence(_Frozen):
    sufficiency: Sufficiency
    claims: list[Claim]
    missing_information: list[str]
    uncertainties: list[str]
    notes: str | None = None
    corroboration: Corroboration = Field(description="For the story as a whole, across all cited sources.")
    independent_publishers: int
    official_source_present: bool
    verification: Literal["not_independently_verified"] = "not_independently_verified"


class WhyItMatters(_Frozen):
    confirmed_impact: str | None = Field(default=None, description="Significance the sources state.")
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
    technical_direction: str
    difficulty: Difficulty


class Classification(_Frozen):
    primary: str
    primary_label: str
    secondary: list[str]
    topic: FrontendTopic = Field(description="Frontend topic mapped from the primary category.")
    rationale: str


class Entities(_Frozen):
    organisations: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    benchmarks: list[str] = Field(default_factory=list)


class Generation(_Frozen):
    ai_generated: Literal[True] = True
    provider: str
    model: str
    engine_key: str
    generated_at: datetime
    calls: int = Field(description="Provider calls for this analysis, including repair attempts.")
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None = None


class StoryIntelligence(_Frozen):
    """AI intelligence for one Stage 3.2 story cluster."""

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    cluster_id: uuid.UUID
    cluster_revision: str = Field(description="Stage 3.2 `processing.revision` this analysis was made from.")
    cluster_contract_version: str
    status: Literal["analysed", "needs_review"]
    review_flags: list[str] = Field(default_factory=list)
    representative_item_id: uuid.UUID
    event_type: EventType | None = Field(default=None, description="Stage 3.2's rule-based event type.")
    earliest_published_at: datetime | None = None
    latest_published_at: datetime | None = None
    headline: str
    summary: str | None = None
    technical_explanation: str | None = None
    why_it_matters: WhyItMatters
    student_relevance: StudentRelevance
    project_ideas: list[ProjectIdea] = Field(max_length=3)
    classification: Classification
    entities: Entities
    technical_details: list[TechnicalDetail]
    evidence: Evidence
    sources: list[IntelligenceSource] = Field(min_length=1)
    generation: Generation
    content_is_ai_generated: Literal[True] = True
