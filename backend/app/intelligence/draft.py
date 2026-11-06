"""What the model must return: the typed draft schema, and its JSON Schema for structured output.

The draft is validated twice: by the provider (strict JSON-schema mode, structure and enums only) and here
(Pydantic: types, lengths, list limits), then against the evidence in `validation.py`. Length limits are
left out of the schema sent to the provider so that a too-long field comes back to us as a validation
issue we can explain in a repair request, instead of an opaque provider error.

Source references are opaque ids ("S1", "S2", ...) assigned by the engine. The model never writes URLs,
publishers or dates of sources; those are copied from Stage 3.2 by code.
"""

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Sufficiency = Literal["sufficient", "partial", "insufficient"]
ClaimBasis = Literal["stated_in_sources", "inferred"]
RelevanceArea = Literal["learning", "research", "internships", "career_skills", "practical_applications"]
Difficulty = Literal["beginner", "intermediate", "advanced"]

Text = str
SourceIds = list[str]


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceAssessmentDraft(_Draft):
    sufficiency: Sufficiency = Field(description="Is the evidence enough to explain this story accurately?")
    missing_information: list[str] = Field(
        max_length=8, description="Facts a reader would need that the sources do not provide."
    )
    notes: str | None = Field(max_length=400, description="Anything else about the evidence quality.")


class WhyItMattersDraft(_Draft):
    confirmed_impact: str | None = Field(
        max_length=700, description="Significance the sources themselves state or directly show."
    )
    interpretation: str | None = Field(
        max_length=700, description="Your reasoned interpretation of broader significance, clearly hedged."
    )


class RelevanceAngleDraft(_Draft):
    area: RelevanceArea
    detail: str = Field(min_length=10, max_length=400)


class StudentRelevanceDraft(_Draft):
    summary: str | None = Field(max_length=700)
    angles: list[RelevanceAngleDraft] = Field(max_length=4)


class ProjectIdeaDraft(_Draft):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=20, max_length=500)
    technical_direction: str = Field(min_length=10, max_length=500)
    difficulty: Difficulty


class ClassificationDraft(_Draft):
    primary: str = Field(description="One category key.")
    secondary: list[str] = Field(max_length=4, description="Other category keys that clearly apply.")
    rationale: str = Field(min_length=5, max_length=300)


class EntitiesDraft(_Draft):
    organisations: list[str] = Field(max_length=15)
    people: list[str] = Field(max_length=15)
    products: list[str] = Field(max_length=15, description="Models, products, services, datasets.")
    technologies: list[str] = Field(max_length=15, description="Methods, architectures, techniques.")
    benchmarks: list[str] = Field(max_length=15)


class TechnicalDetailDraft(_Draft):
    label: str = Field(min_length=2, max_length=80)
    detail: str = Field(min_length=2, max_length=400)
    source_ids: SourceIds = Field(max_length=10)


class ClaimDraft(_Draft):
    statement: str = Field(min_length=10, max_length=400)
    basis: ClaimBasis
    source_ids: SourceIds = Field(max_length=10)
    attributed_to: str | None = Field(
        max_length=160, description="Who makes the claim according to the source, e.g. 'the company'."
    )


class IntelligenceDraft(_Draft):
    evidence_assessment: EvidenceAssessmentDraft
    headline: str = Field(min_length=10, max_length=140, description="Neutral, factual headline.")
    summary: str | None = Field(max_length=1200, description="60-100 words. Null only if evidence is insufficient.")
    technical_explanation: str | None = Field(max_length=2000)
    why_it_matters: WhyItMattersDraft
    student_relevance: StudentRelevanceDraft
    project_ideas: list[ProjectIdeaDraft] = Field(max_length=3)
    classification: ClassificationDraft
    entities: EntitiesDraft
    technical_details: list[TechnicalDetailDraft] = Field(max_length=10)
    claims: list[ClaimDraft] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=8)


_STRIP_KEYS = {"minLength", "maxLength", "minItems", "maxItems", "default", "title"}


def _strictify(node: Any) -> Any:
    """Pydantic JSON Schema → OpenAI strict structured-output schema: every property required, no extras,
    no length keywords (enforced by our own validation instead)."""
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _STRIP_KEYS}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def response_json_schema(category_keys: list[str], max_project_ideas: int) -> dict[str, Any]:
    """The schema sent to the provider. Category keys are enums, so an unknown category cannot be returned."""
    schema = _strictify(copy.deepcopy(IntelligenceDraft.model_json_schema()))
    defs = schema["$defs"]
    classification = defs["ClassificationDraft"]["properties"]
    classification["primary"]["enum"] = list(category_keys)
    classification["secondary"]["items"]["enum"] = list(category_keys)
    schema["properties"]["project_ideas"]["description"] = (
        f"Zero to {max_project_ideas} ideas. Use none when the story does not support a useful project."
    )
    return schema
