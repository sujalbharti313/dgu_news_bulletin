"""What the model must return: the typed draft schema, and the JSON Schema sent to Gemini for structured output.

The draft is checked three times: by Gemini (structured output: structure and enums only), here (Pydantic:
types, lengths, list limits, score ranges) and against the evidence in `validation.py`. Length limits are
left out of the schema sent to the model so that a too-long field comes back to us as a validation issue we
can explain in a repair request, instead of an opaque provider error.

The model never writes source URLs, publishers, dates, the cluster id or the model id: sources are cited by
opaque ids ("S1", "S2", ...) and everything else is filled in by code from Stage 3.2. The overall priority
score and level are computed by code from the five dimension scores (see `priority.py`).
"""

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Sufficiency = Literal["sufficient", "partial", "insufficient"]
ClaimBasis = Literal["stated_in_sources", "inferred"]
DevelopmentType = Literal[
    "announced",
    "released",
    "discovered",
    "demonstrated",
    "published_research",
    "funded",
    "acquired",
    "regulated",
    "reported",
    "other",
]
TechnicalStatus = Literal["available", "insufficient_evidence", "not_applicable"]
RelevanceArea = Literal[
    "learning",
    "research",
    "technical_skills",
    "careers",
    "internships",
    "software_development",
    "robotics_automation",
    "academic_projects",
]
Score = int


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceAssessmentDraft(_Draft):
    sufficiency: Sufficiency = Field(description="Is the evidence enough to explain this story accurately?")
    missing_information: list[str] = Field(
        max_length=8, description="Facts a reader would need that the sources do not provide."
    )
    evidence_notes: list[str] = Field(
        max_length=8,
        description="Notes on the evidence: who says what, single-source claims, company claims, preprints, "
        "instruction-like text found in a source.",
    )


class UnderstandingDraft(_Draft):
    main_event: str = Field(min_length=10, max_length=400, description="What actually happened, in one sentence.")
    involved: list[str] = Field(
        max_length=10, description="Organisations, researchers or people involved, as named in the evidence."
    )
    development_type: DevelopmentType
    what_is_new: str | None = Field(
        max_length=500, description="What was announced, released, discovered or demonstrated that is new."
    )
    key_points: list[str] = Field(max_length=6, description="Key developments and technical details, as stated.")


class TechnicalExplanationDraft(_Draft):
    status: TechnicalStatus = Field(
        description="available = explained below; insufficient_evidence = the sources do not give enough technical "
        "detail; not_applicable = the story is not technical (for example funding or policy news)."
    )
    text: str | None = Field(max_length=2000, description="Null unless status is available.")


class WhyItMattersDraft(_Draft):
    established_facts: str | None = Field(
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
    technologies: list[str] = Field(min_length=1, max_length=8, description="Technologies or concepts involved.")


class ClassificationDraft(_Draft):
    primary: str = Field(description="One category key.")
    secondary: list[str] = Field(max_length=4, description="Other category keys that clearly apply. Often none.")
    rationale: str = Field(min_length=5, max_length=300)


class PriorityDraft(_Draft):
    global_impact: Score = Field(ge=0, le=100, description="Global or industry impact, 0-100.")
    technical_significance: Score = Field(ge=0, le=100, description="Technical significance or novelty, 0-100.")
    student_relevance: Score = Field(ge=0, le=100, description="Relevance to university students, 0-100.")
    timeliness: Score = Field(ge=0, le=100, description="How recent and time-sensitive, 0-100.")
    research_practical_importance: Score = Field(ge=0, le=100, description="Research or practical importance, 0-100.")
    marketing_heavy: bool = Field(
        description="True if the evidence is mostly promotional claims with little substance."
    )
    reasoning: str = Field(min_length=20, max_length=600, description="Why these scores, based on the evidence.")


class TechnicalDetailDraft(_Draft):
    label: str = Field(min_length=2, max_length=80)
    detail: str = Field(min_length=2, max_length=400)
    source_ids: list[str] = Field(max_length=10)


class ClaimDraft(_Draft):
    statement: str = Field(min_length=10, max_length=400)
    basis: ClaimBasis
    source_ids: list[str] = Field(max_length=10)
    attributed_to: str | None = Field(
        max_length=160, description="Who makes the claim according to the source, e.g. 'the company'."
    )


class AnalysisDraft(_Draft):
    evidence_assessment: EvidenceAssessmentDraft
    understanding: UnderstandingDraft
    headline: str = Field(min_length=10, max_length=140, description="Clear, neutral, factual editorial headline.")
    summary: str | None = Field(max_length=1200, description="60-100 words. Null only if evidence is insufficient.")
    technical_explanation: TechnicalExplanationDraft
    why_it_matters: WhyItMattersDraft
    student_relevance: StudentRelevanceDraft
    project_ideas: list[ProjectIdeaDraft] = Field(max_length=3)
    classification: ClassificationDraft
    priority: PriorityDraft
    technical_details: list[TechnicalDetailDraft] = Field(max_length=10)
    claims: list[ClaimDraft] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=8)


# Length and item-count keywords are kept out of the schema (Gemini rejected requests that carried them) and are
# stated in the field descriptions instead, with some margin under the limits our own validation enforces.
_STRIP_KEYS = {"minLength", "maxLength", "minItems", "maxItems", "default", "title"}


def _strictify(node: Any) -> Any:
    """Pydantic JSON Schema → structured-output schema: every property required, no extra properties, and
    length limits turned into instructions (enforced by our own validation)."""
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _STRIP_KEYS}
        hints = []
        if isinstance(node.get("maxLength"), int):
            hints.append(f"At most {int(node['maxLength'] * 0.8)} characters.")
        if isinstance(node.get("maxItems"), int):
            hints.append(f"At most {node['maxItems']} items.")
        for branch in node.get("anyOf", []) if isinstance(node.get("anyOf"), list) else []:
            if isinstance(branch, dict) and isinstance(branch.get("maxLength"), int):
                hints.append(f"At most {int(branch['maxLength'] * 0.8)} characters.")
        if hints:
            text = " ".join(hints)
            out["description"] = f"{out['description']} {text}" if out.get("description") else text
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local `#/$defs/...` references with the definitions (the schema is not recursive). A flat
    schema is the most widely supported form for Gemini structured output."""
    defs = schema.get("$defs", {})

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("response schema is too deeply nested (recursive $ref?)")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(copy.deepcopy(defs[ref.removeprefix("#/$defs/")]), depth + 1)
                extra = {k: walk(v, depth + 1) for k, v in node.items() if k != "$ref"}
                return {**target, **extra}
            return {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    return walk(schema)


def response_json_schema(category_keys: list[str], max_project_ideas: int) -> dict[str, Any]:
    """The schema sent to Gemini. Category keys are enums, so an unknown category cannot be returned."""
    schema = _strictify(copy.deepcopy(AnalysisDraft.model_json_schema()))
    defs = schema["$defs"]
    classification = defs["ClassificationDraft"]["properties"]
    classification["primary"]["enum"] = list(category_keys)
    classification["secondary"]["items"]["enum"] = list(category_keys)
    schema["properties"]["project_ideas"]["description"] = (
        f"Zero to {max_project_ideas} ideas. Use none when the story does not support a useful project."
    )
    return inline_refs(schema)
