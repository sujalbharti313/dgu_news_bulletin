"""Semantic representation of a story and its Gemini embedding (EMBEDDING_MODEL, default gemini-embedding-2).

What is embedded: the meaning of the story, built from the Stage 3.3 analysis and the article itself (headline,
main event, what is new, summary, technical explanation, the organisations involved and the lead article's
excerpt), with repeated sentences dropped and the text bounded by EMBEDDING_MAX_INPUT_CHARS. The task instruction
is part of the text, as Google recommends for gemini-embedding-2 (it does not accept `task_type`).

Caching: `content_hash` covers the model, the requested dimension, the input format version and the exact text.
An embedding is generated again only when one of those changes, so re-runs cost no quota.

Free tier: every text counts as one request in the shared `ai_usage_daily` ledger under the embedding model,
paced per minute and capped per day (EMBEDDING_REQUESTS_PER_MINUTE / _PER_DAY). Retries are bounded with
exponential backoff. When the quota is used up no further requests are sent and the remaining stories stay
pending until the next run. There is no other embedding provider and no paid fallback.

Validation: the response must hold exactly one vector per text, each with exactly the requested dimension and
only finite numbers, and not all zero. Vectors are L2-normalised before storage so that cosine distance in
pgvector is well defined whatever the model's own normalisation.
"""

import asyncio
import hashlib
import logging
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.intelligence.contract import StoryAnalysis
from app.intelligence.evidence import estimate_tokens
from app.intelligence.providers.base import EmbeddingProvider, EmbeddingRequest, ProviderError
from app.intelligence.quota import MemoryUsageLedger, RateGate, RequestPacer, UsageLedger, next_reset
from app.processing.contract import StoryCluster

logger = logging.getLogger("app.ranking")
EMBEDDING_INPUT_VERSION = "emb-1"
TASK_PREFIX = "task: sentence similarity | query: "
EXCERPT_CHARS = 1500
LOCAL_LIMIT_MESSAGE = (
    "The daily embedding limit (EMBEDDING_REQUESTS_PER_DAY) is reached; no more embedding requests are sent "
    "today. Stories without an embedding stay pending until the quota resets (midnight Pacific time)."
)
_SPACE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _SPACE.sub(" ", text or "").strip()


def embedding_text(cluster: StoryCluster, analysis: StoryAnalysis, *, max_chars: int) -> str:
    """The semantic representation of one story. Deterministic for the same inputs."""
    u = analysis.understanding
    parts: list[str] = [
        analysis.headline,
        u.main_event,
        u.what_is_new or "",
        analysis.summary or "",
        analysis.technical_explanation or "" if analysis.technical_explanation_status == "available" else "",
    ]
    if u.involved:
        parts.append("Involved: " + ", ".join(u.involved[:6]))
    excerpt = _clean(cluster.description)[:EXCERPT_CHARS]
    if excerpt:
        parts.append("Article: " + excerpt)
    seen: set[str] = set()
    kept: list[str] = []
    for part in (_clean(p) for p in parts):
        key = part.casefold().rstrip(".")
        if part and key not in seen and not any(key in s for s in seen if len(key) > 40):
            seen.add(key)
            kept.append(part if part.endswith((".", "!", "?", ":")) else part + ".")
    body = " ".join(kept)
    return (TASK_PREFIX + body)[:max_chars]


def content_hash(text: str, *, model: str, dimensions: int) -> str:
    raw = "\x1f".join([model, str(dimensions), EMBEDDING_INPUT_VERSION, text])
    return hashlib.sha256(raw.encode()).hexdigest()


class InvalidEmbedding(ValueError):
    """The provider answered, but the vectors are unusable. Never stored."""


def normalise(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions:
        raise InvalidEmbedding(f"expected {dimensions} dimensions, got {len(vector)}")
    if not all(math.isfinite(x) for x in vector):
        raise InvalidEmbedding("the vector contains non-finite values")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm < 1e-9:
        raise InvalidEmbedding("the vector is all zeros")
    return [x / norm for x in vector]


@dataclass
class EmbeddingJob:
    key: object
    text: str
    content_hash: str


@dataclass
class EmbeddingOutcome:
    status: str  # completed | failed | invalid_output | quota_limited
    vector: list[float] | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass
class EmbeddingBatchReport:
    outcomes: dict[object, EmbeddingOutcome] = field(default_factory=dict)
    requests: int = 0
    calls: int = 0
    stopped: str | None = None  # safe message when quota/auth/model stopped the run
    quota_stopped: bool = False


class Embedder:
    """Quota-aware, paced, batched embedding calls with bounded retries."""

    def __init__(
        self,
        settings: Settings,
        provider: EmbeddingProvider,
        *,
        ledger: UsageLedger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.ledger = ledger or MemoryUsageLedger()
        self.sleep = sleep
        self.clock = clock
        self.gate = RateGate(sleep)
        self.pacer = RequestPacer(settings.embedding_requests_per_minute, sleep)
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not security

    def _reserve(self, n: int) -> bool:
        s = self.settings
        now = self.clock()
        # Whole batch or nothing: never count requests that are then not sent.
        if self.ledger.snapshot(s.embedding_model, now).requests + n > s.embedding_requests_per_day:
            return False
        for _ in range(n):
            if not self.ledger.reserve(
                s.embedding_model, now, requests_per_day=s.embedding_requests_per_day, tokens_per_day=0
            ):
                return False
        return True

    async def _call(self, texts: list[str], report: EmbeddingBatchReport) -> list[list[float]]:
        s = self.settings
        attempt = 0
        tokens = sum(estimate_tokens(t) for t in texts)
        while True:
            await self.gate.wait()
            for _ in texts:
                await self.pacer.acquire()
            now = self.clock()
            if not self._reserve(len(texts)):
                report.quota_stopped = True
                raise ProviderError("quota", LOCAL_LIMIT_MESSAGE)
            report.calls += 1
            report.requests += len(texts)
            try:
                response = await self.provider.embed(
                    EmbeddingRequest(model=s.embedding_model, texts=texts, dimensions=s.embedding_dimensions)
                )
            except ProviderError as exc:
                self.ledger.record(
                    s.embedding_model,
                    now,
                    input_tokens=0,
                    output_tokens=0,
                    outcome="rate_limited" if exc.kind == "rate_limited" else "error",
                )
                if exc.kind == "quota":
                    report.quota_stopped = True
                    self.ledger.mark_exhausted(s.embedding_model, now, next_reset(now))
                if not exc.retryable or attempt >= s.ai_max_retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(2.0 * 2**attempt, 30.0)
                delay = min(delay, s.ai_max_retry_after_seconds) + self.rng.uniform(0, 0.5)
                logger.warning("Gemini embedding %s, retrying in %.1fs (attempt %d)", exc.kind, delay, attempt + 1)
                if exc.kind == "rate_limited":
                    self.gate.block(delay)
                else:
                    await self.sleep(delay)
                attempt += 1
                continue
            self.ledger.record(s.embedding_model, now, input_tokens=tokens, output_tokens=0, outcome="ok")
            if len(response.vectors) != len(texts):
                raise InvalidEmbedding(f"expected {len(texts)} vectors, got {len(response.vectors)}")
            return response.vectors

    async def embed(self, jobs: list[EmbeddingJob]) -> EmbeddingBatchReport:
        """Embeds every job in batches of EMBEDDING_BATCH_SIZE. A failed batch fails only its own stories; a
        quota, key or model error stops the rest (left `quota_limited` / `failed` with the reason)."""
        s = self.settings
        report = EmbeddingBatchReport()
        size = s.embedding_batch_size
        for start in range(0, len(jobs), size):
            batch = jobs[start : start + size]
            if report.stopped:
                for job in batch:
                    report.outcomes[job.key] = EmbeddingOutcome(
                        "quota_limited" if report.quota_stopped else "failed",
                        error_code="stopped",
                        error=report.stopped,
                    )
                continue
            try:
                vectors = await self._call([j.text for j in batch], report)
            except ProviderError as exc:
                status = "quota_limited" if exc.kind == "quota" else "failed"
                for job in batch:
                    report.outcomes[job.key] = EmbeddingOutcome(
                        status, error_code=f"provider_{exc.kind}", error=exc.safe_message
                    )
                if exc.fatal:
                    report.stopped = exc.safe_message
                logger.warning("embedding batch failed (%s): %s", exc.kind, exc.safe_message)
                continue
            except InvalidEmbedding as exc:
                for job in batch:
                    report.outcomes[job.key] = EmbeddingOutcome(
                        "invalid_output", error_code="invalid_output", error=str(exc)
                    )
                logger.warning("embedding batch invalid: %s", exc)
                continue
            for job, vector in zip(batch, vectors, strict=True):
                try:
                    report.outcomes[job.key] = EmbeddingOutcome(
                        "completed", vector=normalise(vector, s.embedding_dimensions)
                    )
                except InvalidEmbedding as exc:
                    report.outcomes[job.key] = EmbeddingOutcome(
                        "invalid_output", error_code="invalid_output", error=str(exc)
                    )
        return report
