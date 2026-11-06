"""Analysis of one story cluster: evidence pack → prompt → provider (bounded retries) → validation →
bounded repair → contract. Pure apart from the provider call; persistence belongs to the runner."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings
from app.intelligence.categories import CategorySet
from app.intelligence.contract import Generation, StoryIntelligence
from app.intelligence.draft import response_json_schema
from app.intelligence.evidence import build_evidence
from app.intelligence.prompts import PROMPT_VERSION, repair_prompt, system_prompt, user_prompt
from app.intelligence.providers.base import (
    GenerationRequest,
    GenerationResponse,
    IntelligenceProvider,
    Message,
    ProviderError,
    Usage,
)
from app.intelligence.validation import Issue, build_intelligence, review_flags, validate_response
from app.models.enums import IntelligenceStatus
from app.processing.contract import StoryCluster

logger = logging.getLogger("app.intelligence")
SCHEMA_NAME = "story_intelligence"
MAX_ECHO_CHARS = 12_000


def engine_key(categories: CategorySet, settings: Settings) -> str:
    """Identifies everything that shapes an analysis except the model (changing models is not, by itself,
    a reason to pay for re-analysing every story)."""
    return f"prompt-{PROMPT_VERSION}+cat-{categories.fingerprint}+ideas-{settings.ai_max_project_ideas}"


def estimate_cost(settings: Settings, usage: Usage) -> float | None:
    if settings.ai_input_cost_per_million_tokens is None or settings.ai_output_cost_per_million_tokens is None:
        return None
    return round(
        usage.input_tokens * settings.ai_input_cost_per_million_tokens / 1_000_000
        + usage.output_tokens * settings.ai_output_cost_per_million_tokens / 1_000_000,
        6,
    )


class RateGate:
    """Shared pause for every worker after a rate-limit response, so concurrent calls back off together."""

    def __init__(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock=time.monotonic):
        self._until = 0.0
        self._sleep = sleep
        self._clock = clock

    def block(self, seconds: float) -> None:
        self._until = max(self._until, self._clock() + seconds)

    async def wait(self) -> None:
        delay = self._until - self._clock()
        if delay > 0:
            await self._sleep(delay)


@dataclass
class AnalysisOutcome:
    status: IntelligenceStatus
    intelligence: StoryIntelligence | None = None
    issues: list[Issue] = field(default_factory=list)
    error: ProviderError | None = None
    usage: Usage = field(default_factory=Usage)
    calls: int = 0
    model: str | None = None
    review_flags: list[str] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return self.error is not None and self.error.fatal

    def errors_json(self) -> list[dict] | None:
        if self.error is not None:
            return [{"code": f"provider_{self.error.kind}", "path": "$", "message": self.error.safe_message,
                     "severity": "error"}]  # fmt: skip
        return [i.as_dict() for i in self.issues] or None


class StoryAnalyzer:
    def __init__(
        self,
        settings: Settings,
        provider: IntelligenceProvider,
        categories: CategorySet,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gate: RateGate | None = None,
        rng: random.Random | None = None,
    ):
        if not settings.ai_model:
            raise ValueError("AI_MODEL is not set")
        self.settings = settings
        self.provider = provider
        self.categories = categories
        self.sleep = sleep
        self.gate = gate or RateGate(sleep)
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self.engine_key = engine_key(categories, settings)
        self.schema = response_json_schema(categories.keys, settings.ai_max_project_ideas)
        self.system = system_prompt(categories, settings.ai_max_project_ideas)

    async def _call(self, request: GenerationRequest) -> tuple[GenerationResponse, int]:
        """One logical call with bounded retries for timeouts, rate limits and outages."""
        s = self.settings
        for attempt in range(s.ai_max_retries + 1):
            await self.gate.wait()
            try:
                return await self.provider.generate(request), attempt + 1
            except ProviderError as exc:
                if not exc.retryable or attempt == s.ai_max_retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(2.0 * 2**attempt, 30.0)
                delay = min(delay, s.ai_max_retry_after_seconds) + self.rng.uniform(0, 0.5)
                if exc.kind == "rate_limited":
                    self.gate.block(delay)
                logger.warning("AI provider %s, retrying in %.1fs (attempt %d)", exc.kind, delay, attempt + 1)
                await self.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def analyse(self, cluster: StoryCluster, now: datetime) -> AnalysisOutcome:
        s = self.settings
        pack = build_evidence(cluster)
        if pack.word_count < s.ai_min_evidence_words:
            return AnalysisOutcome(
                IntelligenceStatus.INSUFFICIENT_EVIDENCE,
                issues=[Issue("insufficient_evidence", "$",
                              f"Only {pack.word_count} distinct words of source text; not sent to the model.",
                              "warning")],
                review_flags=["insufficient_evidence"],
            )  # fmt: skip

        messages = [Message("system", self.system), Message("user", user_prompt(pack))]
        model = s.ai_model
        usage, calls, issues = Usage(), 0, []
        last_model = model
        for round_ in range(s.ai_max_repair_attempts + 1):
            request = GenerationRequest(
                model=model,
                messages=list(messages),
                schema_name=SCHEMA_NAME,
                json_schema=self.schema,
                max_output_tokens=s.ai_max_output_tokens,
                temperature=s.ai_temperature,
            )
            try:
                response, n = await self._call(request)
            except ProviderError as exc:
                return AnalysisOutcome(
                    IntelligenceStatus.FAILED, error=exc, usage=usage + exc.usage, calls=calls, model=last_model
                )
            calls += n
            usage = usage + response.usage
            last_model = response.model or model
            if response.refusal:
                result_issues = [Issue("refusal", "$", "The model refused to answer.")]
                result = None
            else:
                result = validate_response(
                    response.content, pack, self.categories, max_project_ideas=s.ai_max_project_ideas
                )
                result_issues = list(result.issues)
                if response.finish_reason == "length":
                    result_issues.insert(0, Issue("output_truncated", "$", "The response hit the output token limit."))
            if result is not None and result.ok and response.finish_reason != "length":
                flags = review_flags(result.draft, pack, result.warnings)
                generation = Generation(
                    provider=self.provider.name,
                    model=last_model,
                    engine_key=self.engine_key,
                    generated_at=now,
                    calls=calls,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    estimated_cost_usd=estimate_cost(s, usage),
                )
                intelligence = build_intelligence(
                    result.draft, pack, self.categories, flags=flags, generation=generation
                )
                return AnalysisOutcome(
                    IntelligenceStatus(intelligence.status),
                    intelligence=intelligence,
                    issues=result.warnings,
                    usage=usage,
                    calls=calls,
                    model=last_model,
                    review_flags=flags,
                )
            issues = result_issues
            logger.info("analysis failed validation (round %d): %s", round_ + 1, [i.code for i in issues])
            if round_ < s.ai_max_repair_attempts:
                messages.append(Message("assistant", (response.content or "")[:MAX_ECHO_CHARS]))
                messages.append(Message("user", repair_prompt([str(i) for i in issues if i.severity == "error"])))
                model = s.ai_repair_model or s.ai_model
        return AnalysisOutcome(
            IntelligenceStatus.INVALID_OUTPUT, issues=issues, usage=usage, calls=calls, model=last_model,
            review_flags=["invalid_output"],
        )  # fmt: skip
