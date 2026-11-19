from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.services.gemini_client import (
    DEFAULT_MAX_PROMPT_CHARS,
    AIBudgetGovernor,
    GeminiBudgetExceededError,
    GeminiClient,
    GeminiError,
    GeminiInvalidResponseError,
    GeminiMisconfigurationError,
    GeminiRateLimitError,
    GeminiSafetyBlockError,
    GeminiTransientError,
    GeminiUnsupportedContentError,
    ModelRole,
    gemini_generate_text,
)


@pytest.mark.unit
async def test_client_initialization_and_graceful_disable():
    """GeminiClient disabled gracefully when key is absent, raising typed misconfiguration."""
    client = GeminiClient(api_key="")
    assert not client.is_available

    diag = client.get_diagnostics()
    assert diag["available"] is False
    assert diag["key_configured"] is False

    with pytest.raises(GeminiMisconfigurationError) as exc_info:
        await client.generate_text(prompt="Hello")
    assert "missing api key" in str(exc_info.value).lower()
    assert exc_info.value.status == 0

    # Backwards compatibility functional wrapper
    with pytest.raises(GeminiMisconfigurationError):
        await gemini_generate_text(api_key="", model="gemini-2.5-flash", prompt="Hello")


@pytest.mark.unit
def test_role_specific_model_resolution():
    """Model role tiers map deterministically to fast, heavy, or custom models."""
    client = GeminiClient(
        api_key="AIzaSySyntheticKey",
        default_model="gemini-2.5-flash",
        resume_model="gemini-2.5-pro",
    )

    assert client.resolve_model(role=ModelRole.DEFAULT) == "gemini-2.5-flash"
    assert client.resolve_model(role=ModelRole.CHAT) == "gemini-2.5-flash"
    assert client.resolve_model(role=ModelRole.FAST) == "gemini-2.5-flash"
    assert client.resolve_model(role=ModelRole.RESUME) == "gemini-2.5-pro"
    assert client.resolve_model(role=ModelRole.HEAVY) == "gemini-2.5-pro"

    # Explicit string override takes priority over role
    assert client.resolve_model(model="gemini-1.5-flash", role=ModelRole.RESUME) == "gemini-1.5-flash"


@pytest.mark.unit
def test_request_payload_validation_bounds():
    """Client bounds check prompt size, attachment size, count, and MIME types."""
    client = GeminiClient(api_key="AIzaSySyntheticKey")

    # Prompt exceeds maximum allowed length
    with pytest.raises(GeminiUnsupportedContentError) as exc_info:
        client.validate_request_payload("x" * (DEFAULT_MAX_PROMPT_CHARS + 1))
    assert "exceeds limit" in str(exc_info.value)

    # System instruction exceeds limit
    with pytest.raises(GeminiUnsupportedContentError):
        client.validate_request_payload("test", system_instruction="s" * 25_000)

    # Too many attachments
    fake_imgs = [("image/png", b"fake")] * 6
    with pytest.raises(GeminiUnsupportedContentError) as exc_info:
        client.validate_request_payload("test", inline_images=fake_imgs)
    assert "exceeds limit" in str(exc_info.value)

    # Unsupported MIME type
    with pytest.raises(GeminiUnsupportedContentError) as exc_info:
        client.validate_request_payload("test", inline_images=[("application/x-sh", b"echo hi")])
    assert "unsupported attachment mime" in str(exc_info.value).lower()

    # Single attachment size limit (5MB)
    with pytest.raises(GeminiUnsupportedContentError) as exc_info:
        client.validate_request_payload("test", inline_images=[("image/png", b"0" * 6_000_000)])
    assert "exceeds per-item limit" in str(exc_info.value).lower()

    # Temperature and max output token clamping
    _, _, _, temp, max_out = client.validate_request_payload(
        "test", temperature=3.5, max_output_tokens=20_000
    )
    assert temp == 2.0
    assert max_out == 8192

    _, _, _, temp_low, max_out_low = client.validate_request_payload(
        "test", temperature=-0.5, max_output_tokens=-10
    )
    assert temp_low == 0.0
    assert max_out_low == 1


@pytest.mark.unit
async def test_2xx_successful_response_and_usage_tracking(mock_aiohttp):
    """Successful REST call extracts candidate text and increments usage metrics."""
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=AIzaSySyntheticKey"
    mock_aiohttp.post(
        endpoint,
        json_data={
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Synthetic response line 1\n"}, {"text": "Synthetic line 2"}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 150,
                "candidatesTokenCount": 50,
                "totalTokenCount": 200,
            },
        },
    )

    client = GeminiClient(api_key="AIzaSySyntheticKey")
    res = await client.generate_text(prompt="Hello", user_id=101, guild_id=202)
    assert "Synthetic response line 1" in res
    assert "Synthetic line 2" in res

    diag = client.get_diagnostics()
    assert diag["total_requests"] == 1
    assert diag["successful_requests"] == 1
    assert diag["total_input_tokens"] == 150
    assert diag["total_output_tokens"] == 50
    assert diag["total_cost_usd"] > 0.0


@pytest.mark.unit
async def test_2xx_empty_or_no_candidates(mock_aiohttp):
    """Empty candidate lists or empty text output raise GeminiInvalidResponseError."""
    endpoint1 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-empty-candidates"
    client1 = GeminiClient(api_key="k-empty-candidates")

    # Empty candidate list
    mock_aiohttp.post(endpoint1, json_data={"candidates": []})
    with pytest.raises(GeminiInvalidResponseError) as exc_info:
        await client1.generate_text(prompt="Hi")
    assert "no candidates" in str(exc_info.value)

    # Candidate with empty parts
    endpoint2 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-empty-parts"
    client2 = GeminiClient(api_key="k-empty-parts")
    mock_aiohttp.post(
        endpoint2,
        json_data={"candidates": [{"content": {"parts": [{"text": "   "}]}, "finishReason": "MAX_TOKENS"}]},
    )
    with pytest.raises(GeminiInvalidResponseError) as exc_info:
        await client2.generate_text(prompt="Hi")
    assert "empty response" in str(exc_info.value)
    assert "MAX_TOKENS" in str(exc_info.value)


@pytest.mark.unit
async def test_safety_blocked_handling(mock_aiohttp):
    """Candidate or prompt-level safety blocks raise GeminiSafetyBlockError."""
    endpoint1 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-safety-cand"
    client1 = GeminiClient(api_key="k-safety-cand")

    # Candidate level finishReason = SAFETY
    mock_aiohttp.post(
        endpoint1,
        json_data={"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]},
    )
    with pytest.raises(GeminiSafetyBlockError) as exc_info:
        await client1.generate_text(prompt="Hi")
    assert "safety filter" in str(exc_info.value)
    assert "SAFETY" in exc_info.value.details

    # Prompt feedback blockReason = SAFETY
    endpoint2 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-safety-prompt"
    client2 = GeminiClient(api_key="k-safety-prompt")
    mock_aiohttp.post(
        endpoint2,
        json_data={
            "promptFeedback": {"blockReason": "SAFETY"},
            "candidates": [],
        },
    )
    with pytest.raises(GeminiSafetyBlockError) as exc_info:
        await client2.generate_text(prompt="Hi")
    assert "prompt blocked by safety filter" in str(exc_info.value)


@pytest.mark.unit
async def test_malformed_json_response(mock_aiohttp):
    """Malformed or invalid JSON bodies raise GeminiInvalidResponseError."""
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k"
    )
    mock_aiohttp.post(
        endpoint,
        status=200,
        body=b"<html><body>502 Cloudflare Gateway Error</body></html>",
        headers={"Content-Type": "application/json"},
    )
    client = GeminiClient(api_key="k")
    with pytest.raises(GeminiInvalidResponseError) as exc_info:
        await client.generate_text(prompt="Hi")
    assert "invalid json" in str(exc_info.value)


@pytest.mark.unit
async def test_http_error_differentiation(mock_aiohttp):
    """HTTP 400, 401/403, 429, and 500+ map to distinct typed domain exceptions."""
    base = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

    # 400 invalid key
    mock_aiohttp.post(f"{base}?key=k400", status=400, text='{"error": {"message": "API_KEY_INVALID"}}')
    client400 = GeminiClient(api_key="k400", max_retries=0)
    with pytest.raises(GeminiMisconfigurationError) as exc_info:
        await client400.generate_text(prompt="Hi")
    assert exc_info.value.status == 400

    # 401 unauthenticated
    mock_aiohttp.post(f"{base}?key=k401", status=401, text='{"error": "Unauthorized"}')
    client401 = GeminiClient(api_key="k401", max_retries=0)
    with pytest.raises(GeminiMisconfigurationError) as exc_info:
        await client401.generate_text(prompt="Hi")
    assert exc_info.value.status == 401

    # 403 forbidden
    mock_aiohttp.post(f"{base}?key=k403", status=403, text='{"error": "Forbidden"}')
    client403 = GeminiClient(api_key="k403", max_retries=0)
    with pytest.raises(GeminiMisconfigurationError) as exc_info:
        await client403.generate_text(prompt="Hi")
    assert exc_info.value.status == 403

    # 429 rate limited
    mock_aiohttp.post(
        f"{base}?key=k429", status=429, headers={"Retry-After": "10"}, text='{"error": "Quota exceeded"}'
    )
    client429 = GeminiClient(api_key="k429", max_retries=0)
    with pytest.raises(GeminiRateLimitError) as exc_info:
        await client429.generate_text(prompt="Hi")
    assert exc_info.value.status == 429
    assert exc_info.value.retry_after == 10.0

    # 503 service unavailable
    mock_aiohttp.post(f"{base}?key=k503", status=503, text="Service Unavailable")
    client503 = GeminiClient(api_key="k503", max_retries=0)
    with pytest.raises(GeminiTransientError) as exc_info:
        await client503.generate_text(prompt="Hi")
    assert exc_info.value.status == 503


@pytest.mark.unit
async def test_bounded_retries_and_backoff_on_transient_failure(mock_aiohttp):
    """Client performs bounded retries on 503 / 429 and succeeds when upstream recovers."""
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-retry"
    )

    call_count = 0
    delays_slept: list[float] = []

    async def _fake_sleep(d: float) -> None:
        delays_slept.append(d)

    def _handler(_call: Any):
        nonlocal call_count
        call_count += 1
        from tests.helpers import FakeAiohttpResponse

        if call_count < 3:
            return FakeAiohttpResponse(
                url=_call.url,
                method=_call.method,
                status=503,
                body=b"Server Busy",
            )
        resp_data = {"candidates": [{"content": {"parts": [{"text": "Recovered!"}]}, "finishReason": "STOP"}]}
        return FakeAiohttpResponse(
            url=_call.url,
            method=_call.method,
            status=200,
            body=json.dumps(resp_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    mock_aiohttp.post(endpoint, handler=_handler)

    client = GeminiClient(api_key="k-retry", max_retries=3, sleep_fn=_fake_sleep)
    res = await client.generate_text(prompt="Hi")
    assert res == "Recovered!"
    assert call_count == 3
    assert len(delays_slept) == 2


@pytest.mark.unit
async def test_request_cancellation_preserves_cancelled_error(mock_aiohttp):
    """Client disconnect or task cancellation cleanly bubbles asyncio.CancelledError."""
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k-cancel"
    mock_aiohttp.post(endpoint, exc=asyncio.CancelledError)

    client = GeminiClient(api_key="k-cancel")
    with pytest.raises(asyncio.CancelledError):
        await client.generate_text(prompt="Hi")


@pytest.mark.unit
async def test_budget_governor_cost_and_request_guardrails():
    """Budget governor refuses unbounded requests when cost or rate limits are reached."""
    governor = AIBudgetGovernor(
        daily_cost_limit_usd=1.0,
        weekly_cost_limit_usd=5.0,
        user_requests_per_minute=2,
        user_requests_per_day=5,
        guild_requests_per_day=10,
    )

    # 1. User rate limit per minute
    governor.record_request_start(user_id=1)
    governor.record_request_start(user_id=1)
    with pytest.raises(GeminiBudgetExceededError) as exc_info:
        governor.check_budget(user_id=1)
    assert "User AI rate limit exceeded" in str(exc_info.value)

    # User 2 is independent and passes
    governor.check_budget(user_id=2)

    # 2. Guild rate limit per day
    for _ in range(10):
        governor.record_request_start(guild_id=999)
    with pytest.raises(GeminiBudgetExceededError) as exc_info:
        governor.check_budget(guild_id=999)
    assert "Guild daily AI request quota exhausted" in str(exc_info.value)

    # 3. Global daily cost limit
    # Record usage that costs $1.50
    governor.record_usage(
        model="gemini-2.5-pro",
        input_tokens=1_000_000,
        output_tokens=100_000,
    )
    with pytest.raises(GeminiBudgetExceededError) as exc_info:
        governor.check_budget()
    assert "Daily AI cost budget exhausted" in str(exc_info.value)


@pytest.mark.unit
def test_secret_redaction_in_diagnostics_and_errors():
    """Secret API keys and sensitive query parameters never leak in errors or diagnostics."""
    secret_key = "AIzaSyLeakedSecretKey99999"
    client = GeminiClient(api_key=secret_key)

    diag = client.get_diagnostics()
    diag_str = str(diag)
    assert secret_key not in diag_str
    assert diag["key_configured"] is True

    formatted = client.format_diagnostics()
    assert secret_key not in formatted
    assert "AI Provider Diagnostics" in formatted

    # Direct error redaction
    err = GeminiError(
        f"Failed call to https://generativelanguage.googleapis.com?key={secret_key}",
        status=500,
        details=f"authorization: Bearer {secret_key}",
    )
    err_str = str(err)
    from icbot.errors import redact_secrets

    clean = redact_secrets(err_str, (secret_key,))
    assert secret_key not in clean


@pytest.mark.unit
async def test_service_container_wiring():
    """ServiceContainer binds GeminiClient and exposes diagnostics cleanly."""
    settings = Settings(
        token="",
        gemini_api_key="AIzaSyContainerKey",
        gemini_model="gemini-2.5-flash",
        gemini_resume_model="gemini-2.5-pro",
    )
    container = ServiceContainer.from_settings(settings)
    assert container.gemini_client is not None
    assert container.gemini_client.is_available is True

    diag = container.get_ai_diagnostics()
    assert diag["available"] is True
    assert diag["model_default"] == "gemini-2.5-flash"
    assert diag["model_resume"] == "gemini-2.5-pro"
