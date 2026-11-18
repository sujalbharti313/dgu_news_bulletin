"""The Gemini provider on top of the official google-genai SDK, with the SDK's network layer replaced by a fake
client. Real SDK request and response types are used; nothing leaves the process."""

import asyncio
import logging

import httpx
import pytest
from google.genai import errors, types

from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers.base import GenerationRequest, Message, ProviderError
from app.intelligence.providers.gemini import GeminiProvider, classify_api_error
from tests.intelligence_helpers import FAKE_KEY, ai_settings


class FakeModels:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def get(self, *, model):
        self.calls.append({"get": model})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return types.Model(name=f"models/{model}")


class FakeClient:
    def __init__(self, outcome):
        self.aio = type("Aio", (), {})()
        self.aio.models = FakeModels(outcome)
        self.closed = False

        async def aclose():
            self.closed = True

        self.aio.aclose = aclose


def request(**overrides) -> GenerationRequest:
    values = {
        "model": "gemini-3.5-flash-lite",
        "messages": [Message("system", "rules"), Message("user", "evidence")],
        "schema_name": "story_analysis",
        "json_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        "max_output_tokens": 4096,
        "thinking_level": "minimal",
    }
    values.update(overrides)
    return GenerationRequest(**values)


def sdk_response(text='{"a": "b"}', finish="STOP", thought=False):
    parts = [types.Part(text="thinking...", thought=True)] if thought else []
    parts.append(types.Part(text=text))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200, candidates_token_count=300, thoughts_token_count=20
        ),
        model_version="gemini-3.5-flash-lite-001",
        response_id="resp-1",
    )


def provider_with(outcome):
    client = FakeClient(outcome)
    return GeminiProvider(ai_settings(), client=client), client


def test_request_is_structured_output_with_exact_model_and_no_paid_tools():
    provider, client = provider_with(sdk_response(thought=True))
    result = asyncio.run(provider.generate(request()))
    call = client.aio.models.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    config: types.GenerateContentConfig = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema["required"] == ["a"]
    assert config.system_instruction == "rules"
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert config.max_output_tokens == 4096
    # No tools (no search grounding), no deprecated sampling parameters.
    assert config.tools is None and config.temperature is None and config.candidate_count is None
    assert [c.role for c in call["contents"]] == ["user"]
    assert result.content == '{"a": "b"}'  # thought parts are dropped
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 320)
    assert result.model_version == "gemini-3.5-flash-lite-001" and result.finish_reason == "stop"


def test_repair_turns_map_to_model_and_user_roles():
    provider, client = provider_with(sdk_response())
    msgs = [Message("system", "r"), Message("user", "e"), Message("assistant", "{}"), Message("user", "fix")]
    asyncio.run(provider.generate(request(messages=msgs)))
    assert [c.role for c in client.aio.models.calls[0]["contents"]] == ["user", "model", "user"]


def test_max_tokens_and_safety_blocks_are_reported():
    provider, _ = provider_with(sdk_response(finish="MAX_TOKENS"))
    assert asyncio.run(provider.generate(request())).finish_reason == "length"
    provider, _ = provider_with(sdk_response(finish="SAFETY"))
    assert asyncio.run(provider.generate(request())).refusal


def quota_body(quota_id: str, retry: str | None = None, message="Quota exceeded"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message, "details": details}}


@pytest.mark.parametrize(
    "code,body,kind",
    [
        (429, quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "17s"), "rate_limited"),
        (429, quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier"), "quota"),
        (429, quota_body("X", message="Quota exceeded, limit: 0"), "quota"),
        (400, {"error": {"status": "INVALID_ARGUMENT", "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}, "auth"),
        (403, {"error": {"status": "PERMISSION_DENIED"}}, "auth"),
        (404, {"error": {"status": "NOT_FOUND", "message": "models/x is not found"}}, "model_unavailable"),
        (400, {"error": {"status": "FAILED_PRECONDITION"}}, "auth"),
        (500, {"error": {"status": "INTERNAL"}}, "unavailable"),
        (503, {"error": {"status": "UNAVAILABLE"}}, "unavailable"),
        (400, {"error": {"status": "INVALID_ARGUMENT"}}, "bad_request"),
    ],
)  # fmt: skip
def test_api_errors_are_classified(code, body, kind):
    error = errors.ClientError(code, body) if code < 500 else errors.ServerError(code, body)
    provider, _ = provider_with(error)
    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(request()))
    assert info.value.kind == kind
    if kind == "rate_limited":
        assert info.value.retry_after == 17.0


def test_timeouts_and_network_errors_are_mapped():
    provider, _ = provider_with(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(request()))
    assert info.value.kind == "timeout"
    provider, _ = provider_with(httpx.ConnectError("down"))
    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(request()))
    assert info.value.kind == "unavailable"


def test_model_check_uses_metadata_only():
    provider, client = provider_with(sdk_response())
    assert asyncio.run(provider.check_model("gemini-3.5-flash-lite")) == "models/gemini-3.5-flash-lite"
    assert client.aio.models.calls == [{"get": "gemini-3.5-flash-lite"}]
    missing, _ = provider_with(errors.ClientError(404, {"error": {"status": "NOT_FOUND"}}))
    with pytest.raises(ProviderError) as info:
        asyncio.run(missing.check_model("gemini-9-imaginary"))
    assert info.value.kind == "model_unavailable" and "No other model" in info.value.safe_message


def test_invalid_model_names_are_refused_locally():
    provider, client = provider_with(sdk_response())
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate(request(model="../../v1/secret")))
    assert client.aio.models.calls == []


def test_missing_key_is_a_configuration_error():
    with pytest.raises(IntelligenceConfigError) as info:
        GeminiProvider(ai_settings(gemini_api_key=None))
    assert "GEMINI_API_KEY" in info.value.safe_message


def test_real_sdk_client_gets_the_key_without_exposing_it(caplog):
    caplog.set_level(logging.DEBUG)
    provider = GeminiProvider(ai_settings(ai_request_timeout_seconds=42))
    client = provider._client
    assert client._api_client.api_key == FAKE_KEY  # passed explicitly, not read from other env vars
    assert client._api_client._http_options.timeout == 42_000
    assert client._api_client._http_options.retry_options.attempts == 1  # the engine owns retries
    asyncio.run(provider.aclose())
    assert FAKE_KEY not in caplog.text
    settings = ai_settings()
    assert FAKE_KEY not in repr(settings) and FAKE_KEY not in str(settings.model_dump())


def test_error_messages_never_contain_the_key_or_prompt():
    body = {"error": {"status": "INVALID_ARGUMENT", "message": f"bad key {FAKE_KEY} for prompt SECRET-PROMPT"}}
    err = classify_api_error(400, body)
    assert FAKE_KEY not in err.safe_message and "SECRET-PROMPT" not in err.safe_message


def test_schema_rejections_keep_googles_reason_without_secrets():
    body = {
        "error": {"status": "INVALID_ARGUMENT", "message": f"Invalid response_json_schema: too many states {FAKE_KEY}"}
    }
    err = classify_api_error(400, body)
    assert err.kind == "bad_request" and "too many states" in err.safe_message and FAKE_KEY not in err.safe_message
