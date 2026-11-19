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


class VerificationThresholds(_Strict):
    min_words_for_substance: int = Field(ge=0, le=500)
    claim_support_threshold: float = Field(ge=0, le=1)
    headline_support_threshold: float = Field(ge=0, le=1)
    repeat_novelty_threshold: float = Field(ge=0, le=1)
    preliminary_overreach_score: int = Field(ge=0, le=100)
    preliminary_overreach_evidence: int = Field(ge=0, le=100)


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


class DuplicateGate(Gate):
    action: GateAction = "ineligible"
    product_window_days: int = Field(ge=0, le=60)
    title_window_days: int = Field(ge=0, le=60)
    title_similarity: float = Field(ge=0.1, le=1)


class MajorClaimGate(Gate):
    major_impact_score: int = Field(ge=0, le=100)
    min_evidence_quality: int = Field(ge=0, le=100)


class Gates(_Strict):
    missing_source: Gate
    insufficient_evidence: InsufficientEvidenceGate
    severe_inconsistency: InconsistencyGate
    duplicate_event: DuplicateGate
    out_of_window: Gate
    unverified_major_claim: MajorClaimGate
    unknown_single_source: Gate
    analysis_needs_review: Gate


class AiCheck(_Strict):
    min_score: int = Field(ge=0, le=100)


class Shortlist(_Strict):
    target: int = Field(ge=1, le=50)
    min_score: int = Field(ge=0, le=100)
    max_per_category: int = Field(ge=1, le=50)
    max_per_organisation: int = Field(ge=1, le=50)
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
    verification: VerificationThresholds
    credibility: Credibility
    gates: Gates
    ai_check: AiCheck
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


def verification_version(config: RankingConfig, registry: SourceRegistry, ai_check_key: str) -> str:
    """Identifies every rule that shapes a verification result (thresholds, registry, AI check prompt)."""
    return f"v{config.version}+r{registry.version}-{registry.fingerprint}+{ai_check_key}"
