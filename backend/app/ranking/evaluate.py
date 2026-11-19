"""One story's full Stage 3.4 evaluation: verification → timeliness → dimension scores → quality gates.

Pure: given the same cluster, analysis, Gemini evaluation, related stories, window and configuration, the result
is the same.
"""

import uuid
from dataclasses import dataclass, field

from app.intelligence.contract import StoryAnalysis
from app.processing.contract import StoryCluster
from app.ranking.config import RankingConfig, SourceRegistry
from app.ranking.contract import Eligibility, FinalScores, GateResult, SemanticRelation, Verification
from app.ranking.gates import evaluate_gates, strictest
from app.ranking.scoring import TimelinessResult, final_scores, timeliness
from app.ranking.semantic import EvaluationDraft
from app.ranking.verification import VerificationOutcome, verify
from app.ranking.window import EditionWindow


@dataclass
class Evaluated:
    cluster: StoryCluster
    analysis: StoryAnalysis
    outcome: VerificationOutcome
    timely: TimelinessResult
    scores: FinalScores
    gates: list[GateResult]
    eligibility: Eligibility
    organisation: str | None
    coverage_groups: list[str]
    priority: str = "Very Low"
    duplicate_of: uuid.UUID | None = None
    selection_note: str | None = None
    extra_flags: set[str] = field(default_factory=set)

    @property
    def cluster_id(self) -> uuid.UUID:
        return self.cluster.cluster_id

    @property
    def verification(self) -> Verification:
        return self.outcome.verification

    @property
    def development_type(self) -> str:
        return self.analysis.understanding.development_type

    @property
    def relations(self) -> list[SemanticRelation]:
        return self.verification.semantic_relations

    @property
    def final(self) -> float:
        return self.scores.final_priority_score

    @property
    def flags(self) -> set[str]:
        return self.outcome.flags | self.timely.flags | self.extra_flags

    def add_gate(self, gate: GateResult) -> None:
        self.gates.append(gate)
        self.eligibility = strictest(self.gates)

    def review_flags(self) -> list[str]:
        out = {g.gate for g in self.gates}
        out |= {f for f in self.flags if not f.startswith(("hype:", "hype_level:"))}
        out |= {f"hype:{h}" for h in self.verification.hype_flags}
        return sorted(out)


def organisation_of(cluster: StoryCluster, analysis: StoryAnalysis) -> str | None:
    """Rule-detected organisation first (Stage 3.2 entities), else the first party Stage 3.3 named."""
    if cluster.entities.organisations:
        return cluster.entities.organisations[0]
    return analysis.understanding.involved[0] if analysis.understanding.involved else None


def coverage_groups(analysis: StoryAnalysis, config: RankingConfig) -> list[str]:
    keys = {analysis.primary_category_key, *analysis.secondary_category_keys}
    return sorted(name for name, cats in config.shortlist.coverage_groups.items() if keys & set(cats))


def evaluate_story(
    cluster: StoryCluster,
    analysis: StoryAnalysis,
    window: EditionWindow,
    config: RankingConfig,
    registry: SourceRegistry,
    evaluation: EvaluationDraft,
    *,
    relations: list[SemanticRelation] | None = None,
    extra_publishers: set[str] | None = None,
    evaluation_status: str = "completed",
    model_id: str = "",
    model_version: str | None = None,
) -> Evaluated:
    outcome = verify(
        cluster,
        analysis,
        evaluation,
        config,
        registry,
        relations=relations,
        extra_publishers=extra_publishers,
        evaluation_status=evaluation_status,
        model_id=model_id,
        model_version=model_version,
    )
    timely = timeliness(cluster, window, config)
    scores = final_scores(analysis, outcome.verification, evaluation, timely, config)
    gates = evaluate_gates(analysis, outcome.verification, scores, outcome.flags | timely.flags, config)
    return Evaluated(
        cluster=cluster,
        analysis=analysis,
        outcome=outcome,
        timely=timely,
        scores=scores,
        gates=gates,
        eligibility=strictest(gates),
        organisation=organisation_of(cluster, analysis),
        coverage_groups=coverage_groups(analysis, config),
        priority=config.classification.classify(scores.final_priority_score),
    )
