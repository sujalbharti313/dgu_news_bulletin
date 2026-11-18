"""Analysis of one story cluster: evidence pack → prompt → Gemini (quota-checked, paced, bounded retries) →
validation → bounded repair → contract. Pure apart from the provider call and usage accounting; persistence
belongs to the runner. Logs carry ids, codes and counts only, never prompts or source text.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.intelligence.categories import CategorySet
from app.intelligence.contract import Generation, StoryAnalysis
from app.intelligence.draft import response_json_schema
from app.intelligence.evidence import build_evidence, estimate_tokens, fit_to_budget
from app.intelligence.prompts import PROMPT_VERSION, repair_prompt, system_prompt, user_prompt
from app.intelligence.providers.base import (
    GenerationRequest,
    GenerationResponse,
    IntelligenceProvider,
    Message,
    ProviderError,
    Usage,
)
from app.intelligence.quota import MemoryUsageLedger, RateGate, RequestPacer, UsageLedger, next_reset
from app.intelligence.validation import Issue, build_analysis, review_flags, validate_response
from app.models.enums import AnalysisStatus
from app.processing.contract import StoryCluster

logger = logging.getLogger("app.intelligence")
MAX_ECHO_CHARS = 12_000
LOCAL_LIMIT_MESSAGE = (
    "The daily limit configured in AI_REQUESTS_PER_DAY / AI_TOKENS_PER_DAY is reached; no more Gemini requests "
    "are sent today. Remaining stories wait for the next quota day (midnight Pacific time)."
)


def engine_key(categories: CategorySet, settings: Settings) -> str:
    """Identifies everything that shapes an analysis except the model id (changing the model is not, by itself,
    a reason to spend quota re-analysing every story; the model id is stored with each analysis)."""
    return f"prompt-{PROMPT_VERSION}+cat-{categories.fingerprint}+ideas-{settings.ai_max_project_ideas}"


@dataclass
class AnalysisOutcome:
    status: AnalysisStatus
    analysis: StoryAnalysis | None = None
    issues: list[Issue] = field(default_factory=list)
    error: ProviderError | None = None
    usage: Usage = field(default_factory=Usage)
    calls: int = 0
    model: str | None = None
    review_flags: list[str] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return self.error is not None and self.error.fatal

    @property
    def quota_limited(self) -> bool:
        return self.error is not None and self.error.kind == "quota"

    def errors_json(self) -> list[dict] | None:
        if self.error is not None:
            return [
                {
                    "code": f"provider_{self.error.kind}",
                    "path": "$",
                    "message": self.error.safe_message,
                    "severity": "error",
                }
            ]
        return [i.as_dict() for i in self.issues] or None


class StoryAnalyzer:
    def __init__(
        self,
        settings: Settings,
        provider: IntelligenceProvider,
        categories: CategorySet,
        *,
        ledger: UsageLedger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        gate: RateGate | None = None,
        pacer: RequestPacer | None = None,
        rng: random.Random | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.categories = categories
        self.ledger = ledger or MemoryUsageLedger()
        self.sleep = sleep
        self.clock = clock
        self.gate = gate or RateGate(sleep)
        self.pacer = pacer or RequestPacer(settings.ai_requests_per_minute, sleep)
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self.engine_key = engine_key(categories, settings)
        self.schema = response_json_schema(categories.keys, settings.ai_max_project_ideas)
        self.system = system_prompt(categories, settings.ai_max_project_ideas)
        self.quota_stopped = False

    async def _call(self, request: GenerationRequest) -> tuple[GenerationResponse, int]:
        """One logical call: quota reservation, pacing, and bounded retries with exponential backoff for
        timeouts, per-minute rate limits and outages. Never switches models."""
        s = self.settings
        calls = 0
        attempt = 0
        while True:
            if self.quota_stopped:
                raise ProviderError("quota", LOCAL_LIMIT_MESSAGE)
            await self.gate.wait()
            await self.pacer.acquire()
            now = self.clock()
            if not self.ledger.reserve(
                request.model, now, requests_per_day=s.ai_requests_per_day, tokens_per_day=s.ai_tokens_per_day
            ):
                self.quota_stopped = True
                raise ProviderError("quota", LOCAL_LIMIT_MESSAGE)
            calls += 1
            try:
                response = await self.provider.generate(request)
            except ProviderError as exc:
                self.ledger.record(
                    request.model,
                    now,
                    input_tokens=0,
                    output_tokens=0,
                    outcome="rate_limited" if exc.kind == "rate_limited" else "error",
                )
                if exc.kind == "quota":
                    self.quota_stopped = True
                    self.ledger.mark_exhausted(request.model, now, next_reset(now))
                if not exc.retryable or attempt == s.ai_max_retries:
                    exc.calls = calls
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(2.0 * 2**attempt, 30.0)
                delay = min(delay, s.ai_max_retry_after_seconds) + self.rng.uniform(0, 0.5)
                logger.warning("Gemini %s, retrying in %.1fs (attempt %d)", exc.kind, delay, attempt + 1)
                if exc.kind == "rate_limited":
                    self.gate.block(delay)  # every worker waits at the top of the loop, this one included
                else:
                    await self.sleep(delay)
                attempt += 1
                continue
            self.ledger.record(
                request.model,
                now,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                outcome="ok",
            )
            return response, calls

    async def analyse(self, cluster: StoryCluster, now: datetime) -> AnalysisOutcome:
        s = self.settings
        pack = build_evidence(cluster)
        if pack.word_count < s.ai_min_evidence_words:
            return AnalysisOutcome(
                AnalysisStatus.INSUFFICIENT_EVIDENCE,
                issues=[
                    Issue(
                        "insufficient_evidence",
                        "$",
                        f"Only {pack.word_count} distinct words of source text; not sent to the model.",
                        "warning",
                    )
                ],
                review_flags=["insufficient_evidence", "not_sent_to_model"],
            )
        overhead = estimate_tokens(self.system) + 400
        pack = fit_to_budget(
            pack,
            max_sources=s.ai_max_sources_per_story,
            max_input_tokens=s.ai_max_input_tokens,
            overhead_tokens=overhead,
        )

        messages = [Message("system", self.system), Message("user", user_prompt(pack, analysis_date=now))]
        usage, calls, issues = Usage(), 0, []
        version: str | None = None
        for round_ in range(s.ai_max_repair_attempts + 1):
            request = GenerationRequest(
                model=s.ai_model,
                messages=list(messages),
                schema_name="story_analysis",
                json_schema=self.schema,
                max_output_tokens=s.ai_max_output_tokens,
                thinking_level=s.ai_thinking_level,
            )
            try:
                response, n = await self._call(request)
            except ProviderError as exc:
                calls += exc.calls
                status = AnalysisStatus.QUOTA_LIMITED if exc.kind == "quota" else AnalysisStatus.FAILED
                return AnalysisOutcome(status, error=exc, usage=usage + exc.usage, calls=calls, model=s.ai_model)
            calls += n
            usage = usage + response.usage
            version = response.model_version or version
            if response.refusal:
                result_issues = [Issue("refusal", "$", response.refusal)]
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
                    model_id=s.ai_model,
                    model_version=version,
                    engine_key=self.engine_key,
                    generated_at=now,
                    calls=calls,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                analysis = build_analysis(result.draft, pack, self.categories, flags=flags, generation=generation)
                return AnalysisOutcome(
                    AnalysisStatus(analysis.analysis_status),
                    analysis=analysis,
                    issues=result.warnings,
                    usage=usage,
                    calls=calls,
                    model=s.ai_model,
                    review_flags=flags,
                )
            issues = result_issues
            logger.info(
                "analysis failed validation (round %d): %s",
                round_ + 1,
                # Field paths and rule messages only; never the model's text or the source text.
                [f"{i.code} {i.path}: {i.message}"[:200] for i in issues if i.severity == "error"],
            )
            if round_ < s.ai_max_repair_attempts:
                messages.append(Message("assistant", (response.content or "")[:MAX_ECHO_CHARS]))
                messages.append(Message("user", repair_prompt([str(i) for i in issues if i.severity == "error"])))
        return AnalysisOutcome(
            AnalysisStatus.INVALID_OUTPUT,
            issues=issues,
            usage=usage,
            calls=calls,
            model=s.ai_model,
            review_flags=["invalid_output"],
        )
