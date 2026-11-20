"""Stage 3.5 composition with a scripted fake Gemini: strict output validation (refs, numbers, URLs, lengths,
JSON), repair, quota and timeout handling, the deterministic fallback, and content validation rules."""

import asyncio
import json
import uuid
from datetime import date

from app.editions.compose import (
    CompositionInput,
    EditionComposer,
    fallback_introduction,
    response_schema,
    user_prompt,
)
from app.editions.content import EditionContent, StoryText, clean_text, content_hash, has_errors, validate_content
from app.editions.selection import build_story, select_candidates
from app.intelligence.contract import StoryAnalysis
from app.intelligence.providers.base import GenerationResponse, ProviderError
from app.intelligence.quota import MemoryUsageLedger
from tests.conftest import make_settings
from tests.edition_helpers import MONDAY, ScriptedGemini, composer_answer, edition_config, pipeline, ranking_result
from tests.intelligence_helpers import NoSleep

CONFIG = edition_config()


def stories(n=None):
    rows, _ = pipeline()
    ranking = ranking_result()
    analyses = {r["cluster_id"]: StoryAnalysis.model_validate(r["analysis"]) for r in rows if r["analysis"]}
    out = []
    for c in ranking.candidates[:n]:
        out.append(
            build_story(analyses[c.story_cluster_id], c, source="ranking", eligibility="eligible", ranking_run_id=None)
        )
    return out


def compose(provider, n=3, ledger=None, **settings):
    s = make_settings(gemini_api_key="k", **settings)
    composer = EditionComposer(
        s, CONFIG, provider, ledger=ledger or MemoryUsageLedger(), sleep=NoSleep(), clock=lambda: MONDAY
    )
    inp = CompositionInput("2–8 November 2026", {"2", "8", "11", "2026"}, stories(n))
    return asyncio.run(composer.compose(inp)), inp


def answer(**changes):
    return lambda request: composer_answer(request, **changes)


def test_valid_composition_keeps_every_story_and_never_rewrites_ideas():
    result, inp = compose(ScriptedGemini())
    assert result.status == "completed" and result.calls == 1
    assert set(result.texts) == set(inp.refs())
    assert result.title and result.introduction
    for ref, story in inp.refs().items():
        assert result.texts[ref].project_idea == story.original.project_idea


def test_prompt_puts_stage33_text_in_a_random_data_block_and_schema_is_strict():
    _, inp = compose(ScriptedGemini())
    one, two = user_prompt(inp), user_prompt(inp)
    tag = one.split("<", 1)[1].split(">", 1)[0]
    assert tag.startswith("DATA_") and tag not in two  # a fresh delimiter per request
    schema = response_schema()
    assert schema["additionalProperties"] is False and "stories" in schema["required"]


def test_ai_created_story_is_rejected_then_repaired():
    invent = answer(stories=lambda b: b["stories"] + [{**b["stories"][0], "ref": "E99"}])
    provider = ScriptedGemini(invent, composer_answer)
    result, _ = compose(provider)
    assert result.status == "completed" and result.calls == 2
    assert "unknown story refs" in provider.requests[1].messages[-1].content


def test_missing_duplicate_and_unsupported_outputs_fail_validation():
    cases = {
        "missing": answer(stories=lambda b: b["stories"][:-1]),
        "duplicate": answer(stories=lambda b: b["stories"] + [b["stories"][0]]),
        "number": answer(introduction="Models got 73% faster this week, a huge leap for students everywhere today."),
        "url": answer(closing_note="Read more at https://example.org/promo now."),
        "markup": answer(title="<b>Breaking</b> AI news this week for you"),
        "long": answer(introduction="x" * 1300),
        "json": lambda r: GenerationResponse(content="{not json", model=r.model, finish_reason="stop"),
        "types": lambda r: GenerationResponse(content=json.dumps({"title": 5}), model=r.model, finish_reason="stop"),
        "truncated": lambda r: GenerationResponse(content="{}", model=r.model, finish_reason="length"),
    }
    for name, step in cases.items():
        result, _ = compose(ScriptedGemini(step), ai_max_repair_attempts=1)
        assert result.status == "invalid_output", name
        assert result.calls == 2 and result.problems, name


def test_story_numbers_must_come_from_that_story():
    def swap(b):
        first, second = b["stories"][0], b["stories"][1]
        first = {**first, "summary": first["summary"] + " It costs 999 rupees."}
        return [first, second, *b["stories"][2:]]

    result, _ = compose(ScriptedGemini(answer(stories=swap)), ai_max_repair_attempts=0)
    assert result.status == "invalid_output"
    assert any("numbers not in its input" in p for p in result.problems)


def test_quota_exhaustion_stops_without_retry_or_fallback_model():
    ledger = MemoryUsageLedger()
    provider = ScriptedGemini(ProviderError("quota", "daily quota used up", status_code=429))
    result, _ = compose(provider, ledger=ledger)
    assert result.status == "quota_limited" and result.calls == 1
    assert ledger.snapshot("gemini-3.5-flash-lite", MONDAY).quota_exhausted_until is not None
    again, _ = compose(provider, ledger=ledger)
    assert again.status == "quota_limited" and len(provider.requests) == 1  # nothing sent while exhausted


def test_local_daily_limit_is_respected():
    result, _ = compose(ScriptedGemini(), ai_requests_per_day=1, ledger=_used_ledger())
    assert result.status == "quota_limited" and result.calls == 0


def _used_ledger():
    ledger = MemoryUsageLedger()
    ledger.reserve("gemini-3.5-flash-lite", MONDAY, requests_per_day=0, tokens_per_day=0)
    return ledger


def test_timeouts_are_retried_with_backoff_then_fail():
    timeout = ProviderError("timeout", "Gemini did not respond in time.")
    ok, _ = compose(ScriptedGemini(timeout, composer_answer), ai_max_retries=2)
    assert ok.status == "completed" and ok.calls == 2
    failed, _ = compose(ScriptedGemini(timeout), ai_max_retries=1)
    assert failed.status == "failed" and failed.error_kind == "timeout" and failed.calls == 2


def test_refusal_is_invalid_output():
    refusal = lambda r: GenerationResponse(content=None, model=r.model, refusal="blocked", finish_reason="safety")  # noqa: E731
    result, _ = compose(ScriptedGemini(refusal), ai_max_repair_attempts=0)
    assert result.status == "invalid_output"


def test_fallback_introduction_is_factual():
    sts = stories(3)
    intro = fallback_introduction(sts, "2–8 November 2026")
    assert "3 AI developments" in intro and sts[0].text.headline.rstrip(".") in intro
    assert "No stories" in fallback_introduction([], "2–8 November 2026")


def _content(sts, **kw):
    return EditionContent(
        title=kw.get("title", "A good title for the week"),
        introduction=kw.get("introduction", "An introduction long enough to describe the week."),
        week_start=date(2026, 11, 2),
        week_end=date(2026, 11, 8),
        timezone="Asia/Kolkata",
        stories=sts,
    )


def test_content_validation_rules():
    sts = stories(2)
    assert not has_errors(validate_content(_content(sts), CONFIG.limits))
    empty = validate_content(_content([]), CONFIG.limits)
    assert any(i.code == "no_stories" for i in empty)
    dup = validate_content(_content([sts[0], sts[0]]), CONFIG.limits)
    assert any(i.code == "duplicate_story" for i in dup)
    no_url = sts[0].model_copy(update={"source_url": "javascript:alert(1)"})
    assert any(i.code == "missing_source_url" for i in validate_content(_content([no_url]), CONFIG.limits))
    bad_text = sts[0].model_copy(update={"text": sts[0].text.model_copy(update={"summary": "See https://x.example"})})
    assert any(i.code == "url_in_text" for i in validate_content(_content([bad_text]), CONFIG.limits))
    blank = sts[0].model_copy(update={"text": sts[0].text.model_copy(update={"why_it_matters": " "})})
    assert any(i.code == "missing_field" for i in validate_content(_content([blank]), CONFIG.limits))
    few = validate_content(_content(sts[:1]), CONFIG.limits, min_stories=3)
    assert any(i.code == "few_stories" and i.severity == "warning" for i in few)
    review = sts[0].model_copy(
        update={"selection": sts[0].selection.model_copy(update={"eligibility": "review_required"})}
    )
    assert any(i.code == "review_not_acknowledged" for i in validate_content(_content([review]), CONFIG.limits))


def test_content_hash_is_stable_and_sensitive():
    sts = stories(2)
    a, b = _content(sts), _content(sts)
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(_content(list(reversed(sts))))


def test_clean_text_strips_control_and_zero_width_characters():
    assert clean_text("Hello​ \x00world\r\n\r\n\r\n\nok  ") == "Hello world\n\nok"
    assert clean_text("a\nb", multiline=False) == "a b"


def test_selection_preserves_stage34_order_and_target():
    ranking = ranking_result()
    picked = select_candidates(ranking, 10)
    assert [c.story_cluster_id for c in picked.candidates] == ranking.ranked_candidate_ids
    capped = select_candidates(ranking, 1)
    assert len(capped.candidates) == 1 and capped.candidates[0].shortlist_rank == 1
    assert any(s["reason"] == "beyond the target story count" for s in capped.skipped)


def test_selection_skips_non_eligible_duplicate_and_repeated_shortlist_entries():
    data = ranking_result().model_dump(mode="json")
    first = data["shortlist"][0]
    data["shortlist"] = [
        {**first, "eligibilityStatus": "review_required"},
        data["shortlist"][1],
        data["shortlist"][1],
        {**data["shortlist"][1], "storyClusterId": str(uuid.uuid4()), "duplicateOf": str(uuid.uuid4())},
    ]
    from app.ranking.contract import RankingResult

    picked = select_candidates(RankingResult.model_validate(data), 10)
    assert [c.story_cluster_id for c in picked.candidates] == [uuid.UUID(data["shortlist"][1]["storyClusterId"])]
    assert sorted(x["reason"] for x in picked.skipped) == [
        "duplicate of another story",
        "eligibility is review_required",
        "listed twice",
    ]


def test_story_text_comes_from_stage33_and_source_link_from_records():
    rows, _ = pipeline()
    st = stories(1)[0]
    analysis = next(StoryAnalysis.model_validate(r["analysis"]) for r in rows if r["cluster_id"] == st.cluster_id)
    assert st.source_url in {s.original_url for s in analysis.sources}
    assert st.text.headline == clean_text(analysis.headline, multiline=False)
    assert st.original == st.text and isinstance(st.text, StoryText)
    assert "not an independent fact-check" in st.verification_note
