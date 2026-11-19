"""One story's full Stage 3.4 evaluation: verification → timeliness → dimension scores → quality gates.

Pure: given the same cluster, analysis, AI check result, window and configuration, the result is the same.
"""

import uuid
from dataclasses import dataclass, field

from app.intelligence.contract import StoryAnalysis
from app.processing.contract import StoryCluster
from app.ranking.ai_check import AiCheckDraft
from app.ranking.config import RankingConfig, SourceRegistry
from app.ranking.contract import Eligibility, FinalScores, GateResult, Verification
from app.ranking.gates import evaluate_gates, strictest
from app.ranking.scoring import TimelinessResult, final_scores, timeliness
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
        out |= {f for f in self.flags if not f.startswith(("hype:", "ai_cleared:"))}
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
    *,
    ai: AiCheckDraft | None = None,
    ai_status: str = "disabled",
) -> Evaluated:
    outcome = verify(cluster, analysis, config, registry, ai=ai, ai_status=ai_status)
    timely = timeliness(cluster, window, config)
    scores = final_scores(analysis, outcome.verification, timely, config)
    gates = evaluate_gates(analysis, outcome.verification, outcome.flags | timely.flags, config)
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
