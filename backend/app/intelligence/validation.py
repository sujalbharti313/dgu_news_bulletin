"""Validation of a model response against the schema AND the evidence, then assembly of the contract.

Errors (the response cannot be used) trigger a bounded repair attempt; warnings become review flags.
Nothing missing is ever filled in by code: an unusable response is rejected, not patched. The model's JSON is
never trusted: it is parsed strictly, every field is typed and bounded, every cited source id must exist,
and numbers or URLs that do not appear in the evidence are rejected.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from app.intelligence.categories import CategorySet
from app.intelligence.contract import (
    Claim,
    Evidence,
    Generation,
    PriorityAssessment,
    ProjectIdea,
    RelevanceAngle,
    StoryAnalysis,
    StudentRelevance,
    TechnicalDetail,
    Understanding,
    WhyItMatters,
)
from app.intelligence.draft import AnalysisDraft
from app.intelligence.evidence import EvidencePack, numbers_in, urls_in
from app.intelligence.priority import WEIGHTS, compute_priority

SUMMARY_MIN_WORDS, SUMMARY_MAX_WORDS = 45, 120  # hard limits
SUMMARY_TARGET = (60, 100)  # outside this: a review warning
EXPLANATION_MIN_WORDS, EXPLANATION_MAX_WORDS = 20, 300
_SOURCE_ID = re.compile(r"^S[1-9]\d{0,2}$")
_INJECTION_ECHO = re.compile(
    r"(?i)\b(ignore (all |any )?(previous|prior|above) instructions|system prompt|you are now|admin mode|"
    r"as an ai language model)"
)
_HYPE = re.compile(r"(?i)\b(revolutionary|game[- ]chang\w*|groundbreaking|mind[- ]blowing|unprecedented)\b")
_VERIFIED = re.compile(r"(?i)\b(is|are|was|were|been|be|now)\s+(officially |independently )?(confirmed|verified)\b")

# Review flags that require a human before Stage 3.4 uses the analysis.
BLOCKING_FLAGS = {
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
    draft: AnalysisDraft | None
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
        return ValidationResult(AnalysisDraft.model_validate(data))
    except ValidationError as exc:
        issues = [
            Issue("schema", ".".join(str(p) for p in e["loc"]) or "$", e["msg"])
            for e in exc.errors(include_url=False, include_input=False)[:20]
        ]
        return ValidationResult(None, issues)


def check_draft(
    draft: AnalysisDraft, pack: EvidencePack, categories: CategorySet, *, max_project_ideas: int
) -> list[Issue]:
    issues: list[Issue] = []
    add = issues.append
    sufficiency = draft.evidence_assessment.sufficiency
    tech = draft.technical_explanation

    if sufficiency != "insufficient":
        words = _words(draft.summary)
        if not draft.summary:
            add(Issue("missing_field", "summary", "A summary is required unless evidence is insufficient."))
        elif not SUMMARY_MIN_WORDS <= words <= SUMMARY_MAX_WORDS:
            add(Issue("length", "summary", f"Summary has {words} words; write 60 to 100."))
        elif not SUMMARY_TARGET[0] <= words <= SUMMARY_TARGET[1]:
            add(Issue("length", "summary", f"Summary has {words} words (target 60-100).", "warning"))
        if not (draft.why_it_matters.established_facts or draft.why_it_matters.interpretation):
            add(Issue("missing_field", "why_it_matters", "Give established facts, interpretation, or both."))
        if not draft.student_relevance.summary:
            add(Issue("missing_field", "student_relevance.summary", "Required unless evidence is insufficient."))
        if not draft.claims:
            add(Issue("missing_field", "claims", "List at least one claim with its source ids."))
    elif draft.summary and _words(draft.summary) > SUMMARY_MAX_WORDS:
        add(Issue("length", "summary", "Summary is too long."))

    if tech.status == "available":
        exp_words = _words(tech.text)
        if not tech.text:
            add(Issue("missing_field", "technical_explanation.text", "Text is required when status is available."))
        elif not EXPLANATION_MIN_WORDS <= exp_words <= EXPLANATION_MAX_WORDS:
            add(Issue("length", "technical_explanation.text", f"Has {exp_words} words; write 20 to 300."))
        if sufficiency == "insufficient":
            add(
                Issue(
                    "consistency",
                    "technical_explanation.status",
                    "Evidence is insufficient, so the status must be insufficient_evidence or not_applicable.",
                )
            )
    elif tech.text:
        add(Issue("consistency", "technical_explanation.text", "Text must be null unless status is available."))

    # Categories (also enforced by the response schema enum; checked again here).
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

    # No invented URLs anywhere in generated text; no echoed instructions; no hype or "verified" language.
    allowed_urls = pack.allowed_urls()
    for path, text in _generated_texts(draft):
        for url in urls_in(text):
            if url not in allowed_urls:
                add(Issue("unsupported_url", path, "Do not write URLs; cite sources by id."))
                break
        if _INJECTION_ECHO.search(text):
            add(Issue("instruction_like_text", path, "Output contains instruction-like text.", "warning"))
    for path, text in _reader_texts(draft):
        if _HYPE.search(text):
            add(Issue("promotional_language", path, "Remove promotional words such as 'groundbreaking'."))
        if _VERIFIED.search(text):
            add(
                Issue("verification_claim", path, "Do not call facts confirmed or verified; attribute them.", "warning")
            )

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


def _factual_texts(d: AnalysisDraft) -> list[tuple[str, str]]:
    u = d.understanding
    out = [
        ("headline", d.headline),
        ("summary", d.summary or ""),
        ("technical_explanation.text", d.technical_explanation.text or ""),
        ("why_it_matters.established_facts", d.why_it_matters.established_facts or ""),
        ("understanding.main_event", u.main_event),
        ("understanding.what_is_new", u.what_is_new or ""),
    ]
    out += [(f"understanding.key_points.{i}", p) for i, p in enumerate(u.key_points)]
    out += [(f"technical_details.{i}.detail", t.detail) for i, t in enumerate(d.technical_details)]
    out += [(f"claims.{i}.statement", c.statement) for i, c in enumerate(d.claims) if c.basis == "stated_in_sources"]
    return [(p, t) for p, t in out if t]


def _reader_texts(d: AnalysisDraft) -> list[tuple[str, str]]:
    """Text a student would read in the bulletin."""
    out = [
        ("headline", d.headline),
        ("summary", d.summary or ""),
        ("technical_explanation.text", d.technical_explanation.text or ""),
        ("why_it_matters.established_facts", d.why_it_matters.established_facts or ""),
        ("why_it_matters.interpretation", d.why_it_matters.interpretation or ""),
        ("student_relevance.summary", d.student_relevance.summary or ""),
    ]
    return [(p, t) for p, t in out if t]


def _generated_texts(d: AnalysisDraft) -> list[tuple[str, str]]:
    out = _factual_texts(d)
    out.append(("why_it_matters.interpretation", d.why_it_matters.interpretation or ""))
    out.append(("student_relevance.summary", d.student_relevance.summary or ""))
    out += [(f"student_relevance.angles.{i}", a.detail) for i, a in enumerate(d.student_relevance.angles)]
    for i, p in enumerate(d.project_ideas):
        out += [(f"project_ideas.{i}", f"{p.title} {p.description} {' '.join(p.technologies)}")]
    out.append(("classification.rationale", d.classification.rationale))
    out.append(("priority.reasoning", d.priority.reasoning))
    out += [(f"evidence_assessment.evidence_notes.{i}", n) for i, n in enumerate(d.evidence_assessment.evidence_notes)]
    out += [(f"uncertainties.{i}", n) for i, n in enumerate(d.uncertainties)]
    return [(p, t) for p, t in out if t]


def validate_response(
    content: str | None, pack: EvidencePack, categories: CategorySet, *, max_project_ideas: int
) -> ValidationResult:
    result = parse_draft(content)
    if result.draft is not None:
        result.issues += check_draft(result.draft, pack, categories, max_project_ideas=max_project_ideas)
    return result


def review_flags(draft: AnalysisDraft, pack: EvidencePack, warnings: list[Issue]) -> list[str]:
    flags: set[str] = set()
    sufficiency = draft.evidence_assessment.sufficiency
    if sufficiency == "insufficient":
        flags.add("insufficient_evidence")
    elif sufficiency == "partial":
        flags.add("partial_evidence")
    if pack.cluster.content_flags or any(s.ref.content_flags for s in pack.sent_sources):
        flags.add("source_content_flagged")
    notes = " ".join(draft.evidence_assessment.evidence_notes).casefold()
    if "instruction" in notes and ("source" in notes or "evidence" in notes):
        flags.add("model_reported_injection")
    if draft.priority.marketing_heavy:
        flags.add("marketing_heavy")
    for w in warnings:
        flags.add("instruction_like_text_in_output" if w.code == "instruction_like_text" else f"warning_{w.code}")
    level, _, _ = pack.corroboration([s.ref_id for s in pack.sources])
    if level == "single_report":
        flags.add("single_report")
    if len(pack.sent_sources) < len(pack.sources):
        flags.add("sources_trimmed_for_budget")
    return sorted(flags)


def build_analysis(
    draft: AnalysisDraft,
    pack: EvidencePack,
    categories: CategorySet,
    *,
    flags: list[str],
    generation: Generation,
) -> StoryAnalysis:
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
    story_level, story_publishers, _ = pack.corroboration([s.ref_id for s in pack.sources])
    official_present = any(s.is_official for s in pack.sources)
    sufficiency = draft.evidence_assessment.sufficiency

    p = draft.priority
    scores = {k: getattr(p, k) for k in WEIGHTS}
    priority = compute_priority(
        scores,
        sufficiency=sufficiency,
        marketing_heavy=p.marketing_heavy,
        single_unofficial_report=story_level != "multiple_independent_reports" and not official_present,
    )
    primary = categories.by_key[draft.classification.primary]
    secondary = list(dict.fromkeys(draft.classification.secondary))
    if sufficiency == "insufficient":
        status = "insufficient_evidence"
    elif set(flags) & BLOCKING_FLAGS:
        status = "needs_review"
    else:
        status = "completed"
    u = draft.understanding
    return StoryAnalysis(
        story_cluster_id=cluster.cluster_id,
        cluster_revision=cluster.processing.revision,
        cluster_contract_version=cluster.contract_version,
        analysis_status=status,
        review_flags=flags,
        representative_item_id=cluster.representative.item_id,
        event_type=cluster.event_type,
        earliest_published_at=cluster.earliest_published_at,
        latest_published_at=cluster.latest_published_at,
        headline=draft.headline.strip(),
        summary=draft.summary,
        understanding=Understanding(
            main_event=u.main_event,
            involved=list(dict.fromkeys(u.involved)),
            development_type=u.development_type,
            what_is_new=u.what_is_new,
            key_points=u.key_points,
        ),
        technical_explanation=draft.technical_explanation.text
        if draft.technical_explanation.status == "available"
        else None,
        technical_explanation_status=draft.technical_explanation.status,
        why_it_matters=WhyItMatters(**draft.why_it_matters.model_dump()),
        student_relevance=StudentRelevance(
            summary=draft.student_relevance.summary,
            angles=[RelevanceAngle(**a.model_dump()) for a in draft.student_relevance.angles],
        ),
        project_ideas=[
            ProjectIdea(title=i.title, description=i.description, technologies=list(dict.fromkeys(i.technologies)))
            for i in draft.project_ideas
        ],
        primary_category=primary.label,
        primary_category_key=primary.key,
        secondary_categories=[categories.label(k) for k in secondary],
        secondary_category_keys=secondary,
        category_rationale=draft.classification.rationale,
        topic=primary.topic,
        priority_assessment=PriorityAssessment(
            **scores,
            weighted_score=priority.weighted_score,
            overall_score=priority.overall_score,
            priority_level=priority.priority_level,
            adjustments=priority.adjustments,
            marketing_heavy=p.marketing_heavy,
            reasoning=p.reasoning,
            weights=dict(WEIGHTS),
        ),
        technical_details=[
            TechnicalDetail(label=t.label, detail=t.detail, source_ids=list(dict.fromkeys(t.source_ids)))
            for t in draft.technical_details
        ],
        claims=claims,
        evidence=Evidence(
            sufficiency=sufficiency,
            missing_information=draft.evidence_assessment.missing_information,
            corroboration=story_level,
            independent_publishers=story_publishers,
            official_source_present=official_present,
        ),
        evidence_notes=draft.evidence_assessment.evidence_notes,
        uncertainties=draft.uncertainties,
        sources=pack.contract_sources(),
        model_id=generation.model_id,
        generation=generation,
    )
