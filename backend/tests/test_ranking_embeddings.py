"""Stage 3.4 embeddings: semantic representation, content-hash cache keys, vector validation, batching, retries,
quota stops, and the Gemini provider's embedding request (SDK types, fake network). No network, no key, no quota."""

import asyncio
import math

import pytest
from google.genai import types

from app.intelligence.providers.base import EmbeddingResponse, ProviderError
from app.intelligence.providers.gemini import GeminiProvider
from app.intelligence.quota import MemoryUsageLedger
from app.ranking.embeddings import (
    TASK_PREFIX,
    Embedder,
    EmbeddingJob,
    InvalidEmbedding,
    content_hash,
    embedding_text,
    normalise,
)
from tests.intelligence_helpers import INJECTION_ITEM, PAPER, ai_settings
from tests.processing_helpers import NOW, cluster_of
from tests.ranking_helpers import DIMS, FakeGemini, analysed, analysis_of, ranking_settings
from tests.test_intelligence_gemini import FakeClient


def _paper():
    clusters, records = analysed([PAPER])
    cluster = cluster_of(clusters, PAPER)
    return cluster, analysis_of(records, cluster)


def test_semantic_representation_is_deterministic_and_bounded():
    cluster, analysis = _paper()
    a = embedding_text(cluster, analysis, max_chars=6000)
    assert a == embedding_text(cluster, analysis, max_chars=6000)
    assert a.startswith(TASK_PREFIX) and analysis.headline in a and analysis.understanding.main_event in a
    assert "Article: " in a  # the source text, not only the AI's wording
    assert len(embedding_text(cluster, analysis, max_chars=300)) == 300


def test_content_hash_changes_with_text_model_or_dimensions():
    base = content_hash("text", model="gemini-embedding-2", dimensions=768)
    assert base == content_hash("text", model="gemini-embedding-2", dimensions=768)
    assert base != content_hash("text 2", model="gemini-embedding-2", dimensions=768)
    assert base != content_hash("text", model="gemini-embedding-001", dimensions=768)
    assert base != content_hash("text", model="gemini-embedding-2", dimensions=1536)


def test_vectors_are_validated_and_normalised():
    v = normalise([3.0, 4.0] + [0.0] * (DIMS - 2), DIMS)
    assert math.isclose(math.hypot(*v), 1.0) and math.isclose(v[0], 0.6)
    for bad, message in (
        ([1.0] * (DIMS - 1), "dimension"),
        ([float("nan")] + [1.0] * (DIMS - 1), "finite"),
        ([0.0] * DIMS, "zeros"),
    ):
        with pytest.raises(InvalidEmbedding, match=message):
            normalise(bad, DIMS)


def jobs(n):
    return [EmbeddingJob(i, f"task: x | query: story number {i} about topic {i}", f"h{i}") for i in range(n)]


def embed(provider, settings=None, ledger=None, n=7):
    embedder = Embedder(
        settings or ranking_settings(embedding_batch_size=3), provider, ledger=ledger or MemoryUsageLedger(),
        sleep=_no_sleep, clock=lambda: NOW,
    )  # fmt: skip
    return asyncio.run(embedder.embed(jobs(n)))


async def _no_sleep(_seconds):
    return None


def test_batches_respect_the_batch_size_and_every_vector_is_checked():
    provider = FakeGemini()
    report = embed(provider)
    assert [len(r.texts) for r in provider.embed_requests] == [3, 3, 1]
    assert all(r.dimensions == DIMS and r.model == "gemini-embedding-2" for r in provider.embed_requests)
    assert report.calls == 3 and report.requests == 7
    assert all(o.status == "completed" and len(o.vector) == DIMS for o in report.outcomes.values())


def test_wrong_vector_count_or_size_is_invalid_output_not_a_crash():
    short = FakeGemini(embed_steps=[lambda r: EmbeddingResponse(vectors=[[1.0] * DIMS], model=r.model)])
    report = embed(short, n=3)
    assert {o.status for o in report.outcomes.values()} == {"invalid_output"}
    wrong_size = FakeGemini(embed_steps=[lambda r: EmbeddingResponse([[1.0] * 10 for _ in r.texts], r.model)])
    report = embed(wrong_size, n=2)
    assert {o.error_code for o in report.outcomes.values()} == {"invalid_output"}


def test_transient_errors_are_retried_with_backoff():
    provider = FakeGemini(embed_steps=[ProviderError("rate_limited", "slow down", retry_after=2.0), None])
    report = embed(provider, n=2)
    assert {o.status for o in report.outcomes.values()} == {"completed"}
    assert len(provider.embed_requests) == 2


def test_retries_are_bounded():
    provider = FakeGemini(embed_steps=[ProviderError("unavailable", "busy")])
    report = embed(provider, settings=ranking_settings(embedding_batch_size=3, ai_max_retries=2), n=2)
    assert len(provider.embed_requests) == 3  # first try + 2 retries
    assert {o.status for o in report.outcomes.values()} == {"failed"}


@pytest.mark.parametrize("kind", ["quota", "auth", "model_unavailable"])
def test_quota_key_or_model_errors_stop_the_remaining_batches(kind):
    provider = FakeGemini(embed_steps=[None, ProviderError(kind, f"{kind} problem")])
    report = embed(provider)
    assert len(provider.embed_requests) == 2  # the third batch is never sent
    statuses = [report.outcomes[i].status for i in range(7)]
    assert statuses[:3] == ["completed"] * 3
    assert set(statuses[3:]) == {"quota_limited" if kind == "quota" else "failed"}
    assert report.stopped and report.quota_stopped == (kind == "quota")


def test_local_daily_limit_stops_before_any_paid_or_extra_use():
    ledger = MemoryUsageLedger()
    provider = FakeGemini()
    report = embed(
        provider, settings=ranking_settings(embedding_batch_size=3, embedding_requests_per_day=4), ledger=ledger
    )
    assert sum(len(r.texts) for r in provider.embed_requests) == 3  # 3 reserved; the next batch of 3 would exceed 4
    assert report.quota_stopped and ledger.snapshot("gemini-embedding-2", NOW).requests == 3


def test_gemini_provider_sends_one_content_per_text_and_the_dimension():
    response = types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=[0.1] * 768), types.ContentEmbedding(values=[0.2] * 768)]
    )
    client = FakeClient(response)

    async def embed_content(**kwargs):
        client.aio.models.calls.append(kwargs)
        return response

    client.aio.models.embed_content = embed_content
    provider = GeminiProvider(ai_settings(), client=client)
    from app.intelligence.providers.base import EmbeddingRequest

    out = asyncio.run(provider.embed(EmbeddingRequest(model="gemini-embedding-2", texts=["a", "b"], dimensions=768)))
    call = client.aio.models.calls[0]
    assert call["model"] == "gemini-embedding-2"
    assert len(call["contents"]) == 2 and all(isinstance(c, types.Content) for c in call["contents"])
    assert call["config"].output_dimensionality == 768 and call["config"].task_type is None
    assert len(out.vectors) == 2 and len(out.vectors[0]) == 768


def test_instruction_like_source_text_is_only_data_in_the_embedding_input():
    clusters, records = analysed([INJECTION_ITEM])
    cluster = clusters[0]
    text = embedding_text(cluster, analysis_of(records, cluster), max_chars=6000)
    assert text.startswith(TASK_PREFIX)  # the fixed task prefix always comes first; source text follows it
