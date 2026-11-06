"""Validation of a model response against the schema AND the evidence, then assembly of the contract.

Errors (the response cannot be used) trigger a bounded repair attempt; warnings become review flags.
Nothing missing is ever filled in by code: an unusable response is flagged, not patched.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from app.intelligence.categories import CategorySet
from app.intelligence.contract import (
    Claim,
    Classification,
    Entities,
    Evidence,
    Generation,
    ProjectIdea,
    RelevanceAngle,
    StoryIntelligence,
    StudentRelevance,
    TechnicalDetail,
    WhyItMatters,
)
from app.intelligence.draft import IntelligenceDraft
from app.intelligence.evidence import EvidencePack, numbers_in, urls_in

SUMMARY_MIN_WORDS, SUMMARY_MAX_WORDS = 40, 130  # hard limits
SUMMARY_TARGET = (60, 100)  # outside this: a review warning
EXPLANATION_MIN_WORDS, EXPLANATION_MAX_WORDS = 25, 300
_SOURCE_ID = re.compile(r"^S[1-9]\d{0,2}$")
_INJECTION_ECHO = re.compile(
    r"(?i)\b(ignore (all |any )?(previous|prior|above) instructions|system prompt|you are now|as an ai language model)"
)

# Review flags that require a human before Stage 3.4 uses the analysis.
BLOCKING_FLAGS = {
    "insufficient_evidence",
    "source_content_flagged",
    "instruction_like_text_in_output",
    "model_reported_injection",
}


@dataclass
class Issue:
    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"

    def as_dict(self) -> dict:
        return {"code": self.code, "path": self.path, "message": self.message, "severity": self.severity}

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class ValidationResult:
    draft: IntelligenceDraft | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return self.draft is not None and not self.errors


def _words(text: str | None) -> int:
    return len((text or "").split())


def parse_draft(content: str | None) -> ValidationResult:
    """Empty, malformed or schema-violating responses → errors (never partially accepted)."""
    if content is None or not content.strip():
        return ValidationResult(None, [Issue("empty_response", "$", "The response was empty.")])
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return ValidationResult(None, [Issue("malformed_json", "$", "The response was not valid JSON.")])
    if not isinstance(data, dict):
        return ValidationResult(None, [Issue("malformed_json", "$", "The response must be a JSON object.")])
    try:
        return ValidationResult(IntelligenceDraft.model_validate(data))
    except ValidationError as exc:
        issues = [
            Issue("schema", ".".join(str(p) for p in e["loc"]) or "$", e["msg"])
            for e in exc.errors(include_url=False, include_input=False)[:20]
        ]
        return ValidationResult(None, issues)


def check_draft(
    draft: IntelligenceDraft, pack: EvidencePack, categories: CategorySet, *, max_project_ideas: int
) -> list[Issue]:
    issues: list[Issue] = []
    add = issues.append
    sufficient = draft.evidence_assessment.sufficiency != "insufficient"

    # Required content when the model says the evidence is enough.
    if sufficient:
        words = _words(draft.summary)
        if not draft.summary:
            add(Issue("missing_field", "summary", "A summary is required unless evidence is insufficient."))
        elif not SUMMARY_MIN_WORDS <= words <= SUMMARY_MAX_WORDS:
            add(Issue("length", "summary", f"Summary has {words} words; write 60 to 100."))
        elif not SUMMARY_TARGET[0] <= words <= SUMMARY_TARGET[1]:
            add(Issue("length", "summary", f"Summary has {words} words (target 60-100).", "warning"))
        exp_words = _words(draft.technical_explanation)
        if not draft.technical_explanation:
            add(Issue("missing_field", "technical_explanation", "Required unless evidence is insufficient."))
        elif not EXPLANATION_MIN_WORDS <= exp_words <= EXPLANATION_MAX_WORDS:
            add(Issue("length", "technical_explanation", f"Has {exp_words} words; write 25 to 300."))
        if not (draft.why_it_matters.confirmed_impact or draft.why_it_matters.interpretation):
            add(Issue("missing_field", "why_it_matters", "Give confirmed impact, interpretation, or both."))
        if not draft.student_relevance.summary:
            add(Issue("missing_field", "student_relevance.summary", "Required unless evidence is insufficient."))
        if not draft.claims:
            add(Issue("missing_field", "claims", "List at least one claim with its source ids."))

    # Categories (also enforced by the provider schema enum; checked again here).
    cls = draft.classification
    if cls.primary not in categories.by_key:
        add(Issue("category", "classification.primary", f"Unknown category {cls.primary!r}."))
    unknown = [k for k in cls.secondary if k not in categories.by_key]
    if unknown:
        add(Issue("category", "classification.secondary", f"Unknown categories {unknown!r}."))
    if cls.primary in cls.secondary:
        add(Issue("category", "classification.secondary", "Must not repeat the primary category."))
    if len(set(cls.secondary)) != len(cls.secondary):
        add(Issue("category", "classification.secondary", "Duplicate categories."))
    if len(cls.secondary) > categories.max_secondary:
        add(Issue("category", "classification.secondary", f"At most {categories.max_secondary} allowed."))

    if len(draft.project_ideas) > max_project_ideas:
        add(Issue("limit", "project_ideas", f"At most {max_project_ideas} project ideas allowed."))

    # Source reference integrity: every cited id must exist; stated claims and details must cite something.
    valid = pack.ref_ids
    for i, claim in enumerate(draft.claims):
        bad = [s for s in claim.source_ids if s not in valid or not _SOURCE_ID.match(s)]
        if bad:
            add(Issue("source_reference", f"claims.{i}.source_ids", f"Unknown source ids {bad!r}."))
        if claim.basis == "stated_in_sources" and not claim.source_ids:
            add(Issue("source_reference", f"claims.{i}.source_ids", "A stated claim must cite its sources."))
    for i, detail in enumerate(draft.technical_details):
        bad = [s for s in detail.source_ids if s not in valid]
        if bad:
            add(Issue("source_reference", f"technical_details.{i}.source_ids", f"Unknown source ids {bad!r}."))
        if not detail.source_ids:
            add(Issue("source_reference", f"technical_details.{i}.source_ids", "Cite the sources that state it."))

    # No invented URLs anywhere in generated text.
    allowed_urls = pack.allowed_urls()
    for path, text in _generated_texts(draft):
        for url in urls_in(text):
            if url not in allowed_urls:
                add(Issue("unsupported_url", path, "Do not write URLs; cite sources by id."))
                break
        if _INJECTION_ECHO.search(text):
            add(Issue("instruction_like_text", path, "Output contains instruction-like text.", "warning"))

    # No numbers that the evidence does not contain, in the factual fields.
    allowed_numbers = pack.allowed_numbers()
    for path, text in _factual_texts(draft):
        unsupported = sorted(numbers_in(text) - allowed_numbers)
        if unsupported:
            add(
                Issue(
                    "unsupported_number",
                    path,
                    f"Numbers not found in the evidence: {', '.join(unsupported[:6])}. Remove or correct them.",
                )
            )
    return issues


def _factual_texts(d: IntelligenceDraft) -> list[tuple[str, str]]:
    out = [
        ("headline", d.headline),
        ("summary", d.summary or ""),
        ("technical_explanation", d.technical_explanation or ""),
    ]
    out.append(("why_it_matters.confirmed_impact", d.why_it_matters.confirmed_impact or ""))
    out += [(f"technical_details.{i}.detail", t.detail) for i, t in enumerate(d.technical_details)]
    out += [(f"claims.{i}.statement", c.statement) for i, c in enumerate(d.claims) if c.basis == "stated_in_sources"]
    return [(p, t) for p, t in out if t]


def _generated_texts(d: IntelligenceDraft) -> list[tuple[str, str]]:
    out = _factual_texts(d)
    out.append(("why_it_matters.interpretation", d.why_it_matters.interpretation or ""))
    out.append(("student_relevance.summary", d.student_relevance.summary or ""))
    out += [(f"student_relevance.angles.{i}", a.detail) for i, a in enumerate(d.student_relevance.angles)]
    for i, p in enumerate(d.project_ideas):
        out += [(f"project_ideas.{i}", f"{p.title} {p.description} {p.technical_direction}")]
    out.append(("classification.rationale", d.classification.rationale))
    out.append(("evidence_assessment.notes", d.evidence_assessment.notes or ""))
    return [(p, t) for p, t in out if t]


def validate_response(
    content: str | None, pack: EvidencePack, categories: CategorySet, *, max_project_ideas: int
) -> ValidationResult:
    result = parse_draft(content)
    if result.draft is not None:
        result.issues += check_draft(result.draft, pack, categories, max_project_ideas=max_project_ideas)
    return result


def review_flags(draft: IntelligenceDraft, pack: EvidencePack, warnings: list[Issue]) -> list[str]:
    flags: set[str] = set()
    sufficiency = draft.evidence_assessment.sufficiency
    if sufficiency == "insufficient":
        flags.add("insufficient_evidence")
    elif sufficiency == "partial":
        flags.add("partial_evidence")
    if pack.cluster.content_flags:
        flags.add("source_content_flagged")
    notes = (draft.evidence_assessment.notes or "").casefold()
    if "instruction" in notes and ("source" in notes or "evidence" in notes):
        flags.add("model_reported_injection")
    for w in warnings:
        flags.add("instruction_like_text_in_output" if w.code == "instruction_like_text" else f"warning_{w.code}")
    level, _, _ = pack.corroboration([s.ref_id for s in pack.sources])
    if level == "single_report":
        flags.add("single_report")
    return sorted(flags)


def build_intelligence(
    draft: IntelligenceDraft,
    pack: EvidencePack,
    categories: CategorySet,
    *,
    flags: list[str],
    generation: Generation,
) -> StoryIntelligence:
    """Validated draft + Stage 3.2 provenance → the Stage 3.4 contract. Source metadata comes from code only."""
    cluster = pack.cluster
    claims = []
    for c in draft.claims:
        ids = list(dict.fromkeys(c.source_ids))
        level, publishers, official = pack.corroboration(ids)
        claims.append(
            Claim(
                statement=c.statement,
                basis=c.basis,
                source_ids=ids,
                attributed_to=c.attributed_to,
                corroboration=level,
                independent_publishers=publishers,
                cites_official_source=official,
            )
        )
    cited = sorted(
        {s for c in claims for s in c.source_ids} | {s for t in draft.technical_details for s in t.source_ids}
    )
    level, publishers, _ = pack.corroboration(cited)
    primary = categories.by_key[draft.classification.primary]
    status = "needs_review" if set(flags) & BLOCKING_FLAGS else "analysed"
    return StoryIntelligence(
        cluster_id=cluster.cluster_id,
        cluster_revision=cluster.processing.revision,
        cluster_contract_version=cluster.contract_version,
        status=status,
        review_flags=flags,
        representative_item_id=cluster.representative.item_id,
        event_type=cluster.event_type,
        earliest_published_at=cluster.earliest_published_at,
        latest_published_at=cluster.latest_published_at,
        headline=draft.headline.strip(),
        summary=draft.summary,
        technical_explanation=draft.technical_explanation,
        why_it_matters=WhyItMatters(**draft.why_it_matters.model_dump()),
        student_relevance=StudentRelevance(
            summary=draft.student_relevance.summary,
            angles=[RelevanceAngle(**a.model_dump()) for a in draft.student_relevance.angles],
        ),
        project_ideas=[ProjectIdea(**p.model_dump()) for p in draft.project_ideas],
        classification=Classification(
            primary=primary.key,
            primary_label=primary.label,
            secondary=list(dict.fromkeys(draft.classification.secondary)),
            topic=primary.topic,
            rationale=draft.classification.rationale,
        ),
        entities=Entities(**{k: list(dict.fromkeys(v)) for k, v in draft.entities.model_dump().items()}),
        technical_details=[
            TechnicalDetail(label=t.label, detail=t.detail, source_ids=list(dict.fromkeys(t.source_ids)))
            for t in draft.technical_details
        ],
        evidence=Evidence(
            sufficiency=draft.evidence_assessment.sufficiency,
            claims=claims,
            missing_information=draft.evidence_assessment.missing_information,
            uncertainties=draft.uncertainties,
            notes=draft.evidence_assessment.notes,
            corroboration=level,
            independent_publishers=publishers,
            official_source_present=any(s.is_official for s in pack.sources),
        ),
        sources=pack.contract_sources(),
        generation=generation,
    )
