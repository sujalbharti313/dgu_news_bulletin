"""Stage 3.4 configuration: `config/ranking.toml` (weights, thresholds, gates, diversity) and
`config/source_registry.toml` (reviewed source classifications). Both are validated strictly at load time, and
each carries a version that is stored with every result so that results under different rules never mix."""

import hashlib
import json
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.intelligence.categories import BACKEND_DIR

DIMENSIONS = (
    "global_impact",
    "technical_significance",
    "student_relevance",
    "evidence_quality",
    "source_credibility",
    "timeliness",
)
CredibilityLevel = Literal["primary", "established", "provisional", "aggregator", "unknown", "low"]
RegistryLevel = Literal["primary", "established", "aggregator", "low"]
SourceKindValue = Literal[
    "official_channel",
    "research_repository",
    "government",
    "news_outlet",
    "technical_publication",
    "newsletter",
    "press_release_wire",
    "community",
]
GateAction = Literal["review_required", "ineligible"]


class RankingConfigError(Exception):
    """Invalid or missing ranking configuration. `safe_message` is loggable."""

    def __init__(self, message: str):
        super().__init__(message)
        self.safe_message = message


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Weights(_Strict):
    global_impact: float = Field(ge=0, le=1)
    technical_significance: float = Field(ge=0, le=1)
    student_relevance: float = Field(ge=0, le=1)
    evidence_quality: float = Field(ge=0, le=1)
    source_credibility: float = Field(ge=0, le=1)
    timeliness: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> "Weights":
        total = sum(getattr(self, d) for d in DIMENSIONS)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0 (got {total:.4f})")
        return self

    def as_dict(self) -> dict[str, float]:
        return {d: getattr(self, d) for d in DIMENSIONS}


class Classification(_Strict):
    exceptional: int = Field(ge=1, le=100)
    high: int = Field(ge=1, le=100)
    medium: int = Field(ge=1, le=100)
    low: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "Classification":
        if not self.exceptional > self.high > self.medium > self.low:
            raise ValueError("classification bounds must be strictly decreasing: exceptional > high > medium > low")
        return self

    def classify(self, score: float) -> str:
        for name, floor in (
            ("Exceptional", self.exceptional),
            ("High", self.high),
            ("Medium", self.medium),
            ("Low", self.low),
        ):
            if score >= floor:
                return name
        return "Very Low"


class Timeliness(_Strict):
    timezone: str = "Asia/Kolkata"
    in_window_daily_decay: int = Field(ge=0, le=14)
    late_arrival_days: int = Field(ge=0, le=14)
    late_arrival_score: int = Field(ge=0, le=100)
    stale_days: int = Field(ge=1, le=365)
    stale_score: int = Field(ge=0, le=100)
    very_stale_days: int = Field(ge=1, le=730)
    very_stale_score: int = Field(ge=0, le=100)
    newsletter_date_cap: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "Timeliness":
        if not self.late_arrival_days <= self.stale_days <= self.very_stale_days:
            raise ValueError("timeliness: late_arrival_days <= stale_days <= very_stale_days")
        return self


class Semantic(_Strict):
    """Embedding retrieval. Thresholds are cosine similarities (1 = same direction). They decide which stories
    are *candidates* for a relationship; whether two stories are the same event is decided by the Gemini
    evaluation of each pair, never by a similarity score alone."""

    lookback_days: int = Field(ge=0, le=180)
    retrieval_limit: int = Field(ge=1, le=20)
    related_threshold: float = Field(ge=0, le=1)
    same_event_min_similarity: float = Field(ge=0, le=1)
    topic_similarity: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Semantic":
        if not self.related_threshold <= self.same_event_min_similarity:
            raise ValueError("semantic: related_threshold <= same_event_min_similarity")
        if not self.related_threshold <= self.topic_similarity:
            raise ValueError("semantic: related_threshold <= topic_similarity")
        return self


class Caps(_Strict):
    """Deterministic limits applied by code to the AI's dimension scores, each recorded as an adjustment."""

    no_article_text_evidence: int = Field(ge=0, le=100)
    missing_url_evidence: int = Field(ge=0, le=100)
    stage3_insufficient_evidence: int = Field(ge=0, le=100)
    stage3_partial_evidence: int = Field(ge=0, le=100)
    claim_problem_penalty: int = Field(ge=0, le=50)
    max_claim_penalty: int = Field(ge=0, le=100)
    weak_evidence_below: int = Field(ge=0, le=100)
    weak_evidence_impact: int = Field(ge=0, le=100)
    single_unknown_source_impact: int = Field(ge=0, le=100)
    significant_hype_impact: int = Field(ge=0, le=100)
    significant_hype_technical: int = Field(ge=0, le=100)
    mild_hype_technical: int = Field(ge=0, le=100)


class Credibility(_Strict):
    primary: int = Field(ge=0, le=100)
    established: int = Field(ge=0, le=100)
    provisional: int = Field(ge=0, le=100)
    aggregator: int = Field(ge=0, le=100)
    unknown: int = Field(ge=0, le=100)
    low: int = Field(ge=0, le=100)
    corroboration_bonus: int = Field(ge=0, le=20)
    max_corroboration_bonus: int = Field(ge=0, le=30)
    newsletter_penalty: int = Field(ge=0, le=50)

    def score(self, level: str) -> int:
        return getattr(self, level)


class Gate(_Strict):
    enabled: bool = True
    action: GateAction = "review_required"


class InsufficientEvidenceGate(Gate):
    min_evidence_quality: int = Field(ge=0, le=100)
    ineligible_below: int = Field(ge=0, le=100)


class InconsistencyGate(Gate):
    min_claim_consistency: int = Field(ge=0, le=100)


class MajorClaimGate(Gate):
    major_impact_score: int = Field(ge=0, le=100)
    min_evidence_quality: int = Field(ge=0, le=100)


class LowSubstanceGate(Gate):
    max_technical: int = Field(ge=0, le=100)
    max_impact: int = Field(ge=0, le=100)


class Gates(_Strict):
    missing_source: Gate
    unclear_publisher: Gate
    insufficient_evidence: InsufficientEvidenceGate
    severe_inconsistency: InconsistencyGate
    unverified_major_claim: MajorClaimGate
    misleading_claims: Gate
    low_technical_substance: LowSubstanceGate
    duplicate_event: Gate
    previously_shortlisted: Gate
    out_of_window: Gate
    unknown_single_source: Gate
    analysis_needs_review: Gate
    instruction_like_text: Gate


class Shortlist(_Strict):
    target: int = Field(ge=1, le=50)
    min_score: int = Field(ge=0, le=100)
    max_per_category: int = Field(ge=1, le=50)
    max_per_organisation: int = Field(ge=1, le=50)
    max_per_development_type: int = Field(ge=1, le=50)
    max_per_topic: int = Field(ge=1, le=50)
    exceptional_score: int = Field(ge=0, le=100)
    coverage_swap_margin: int = Field(ge=0, le=50)
    coverage_groups: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("coverage_groups")
    @classmethod
    def _groups(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, keys in value.items():
            if not keys:
                raise ValueError(f"coverage group {name!r} lists no category keys")
        return value


class RankingConfig(_Strict):
    version: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9._-]+$")
    weights: Weights
    classification: Classification
    timeliness: Timeliness
    semantic: Semantic
    caps: Caps
    credibility: Credibility
    gates: Gates
    shortlist: Shortlist

    @cached_property
    def fingerprint(self) -> str:
        """Content hash: catches an edit made without bumping `version`."""
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:8]

    @property
    def ranking_version(self) -> str:
        return f"{self.version}+{self.fingerprint}"


class RegistryEntry(_Strict):
    domain: str | None = Field(
        default=None, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
    )
    source_key: str | None = Field(default=None, max_length=80)
    kind: SourceKindValue
    level: RegistryLevel
    organisation: str | None = Field(default=None, max_length=120)
    reviewed_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _has_key(self) -> "RegistryEntry":
        if not self.domain and not self.source_key:
            raise ValueError("a registry entry needs a domain, a source_key, or both")
        return self


class SourceRegistry(_Strict):
    version: str = Field(min_length=1, max_length=20)
    sources: list[RegistryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "SourceRegistry":
        domains = [s.domain for s in self.sources if s.domain]
        keys = [s.source_key for s in self.sources if s.source_key]
        if len(set(domains)) != len(domains) or len(set(keys)) != len(keys):
            raise ValueError("registry domains and source keys must be unique")
        return self

    @cached_property
    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:8]

    def by_site(self, site: str | None) -> RegistryEntry | None:
        """Longest domain-suffix match: `blog.openai.com` → `openai.com`; `meity.gov.in` → `gov.in`."""
        if not site:
            return None
        host = site.casefold().removeprefix("www.")
        best: RegistryEntry | None = None
        for entry in self.sources:
            d = entry.domain
            if d and (host == d or host.endswith("." + d)) and (best is None or len(d) > len(best.domain or "")):
                best = entry
        return best

    def by_source_key(self, key: str | None) -> RegistryEntry | None:
        return next((e for e in self.sources if key and e.source_key == key), None)


def _load_toml(path: str | Path, what: str) -> dict:
    file = Path(path)
    if not file.is_absolute():
        file = BACKEND_DIR / file
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankingConfigError(f"{what} not found: {file.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RankingConfigError(f"{what} is not valid TOML: {exc}") from exc


def _validate[T: BaseModel](model: type[T], data: dict, what: str) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        loc = ".".join(str(p) for p in first["loc"]) or "$"
        raise RankingConfigError(f"Invalid {what} ({loc}): {first['msg']}") from exc


def load_ranking_config(path: str | Path) -> RankingConfig:
    return _validate(RankingConfig, _load_toml(path, "Ranking config"), "ranking config")


def load_source_registry(path: str | Path) -> SourceRegistry:
    return _validate(SourceRegistry, _load_toml(path, "Source registry"), "source registry")


def verification_version(config: RankingConfig, registry: SourceRegistry, evaluation_key: str) -> str:
    """Identifies every rule that shapes a verification result (thresholds, registry, evaluation prompt)."""
    return f"v{config.version}+r{registry.version}-{registry.fingerprint}+{evaluation_key}"
