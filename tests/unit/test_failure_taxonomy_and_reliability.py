"""Comprehensive offline tests for failure taxonomy, cancellation, retries/circuit-breaker, view errors, and redaction."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from icbot.academics.parsers import SchemaDriftError
from icbot.bot import on_app_command_error
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.errors import (
    REDACTED,
    ConcurrencyConflictError,
    FeatureDisabledError,
    GuildSetupRequiredError,
    MissingApiKeyError,
    PermissionDeniedError,
    ProviderError,
    RecordNotFoundError,
    StaleCacheError,
    StorageUnavailableError,
    UserInputError,
    classify_failure,
    format_exception_safe,
    redact_secrets,
    respond_with_failure,
)
from icbot.features.jobs import JobPaginationView
from icbot.features.sports import _send_sport
from icbot.logging import RedactingLogFilter, StructuredLogFormatter
from icbot.services.gemini_client import (
    GeminiInvalidResponseError,
    GeminiMisconfigurationError,
    GeminiRateLimitError,
)
from icbot.services.http_transport import (
    BoundedHttpTransport,
    CircuitBreakerOpenError,
    ProviderCircuitBreaker,
    TransportRateLimitError,
    TransportServerError,
)
from tests.helpers import FakeClient


def _fake_discord_forbidden() -> discord.Forbidden:
    resp = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(resp, "Missing Permissions")  # type: ignore[arg-type]


@pytest.mark.unit
def test_complete_12_family_failure_taxonomy_and_ux_distinctions() -> None:
    """Verify every one of the 12 failure families maps to distinct UX category, B1 status color, and recovery action."""
    cases: list[tuple[BaseException, str, str, str, bool]] = [
        # (exception, expected_kind, expected_ux_category, expected_embed_status, include_correlation_id)
        (UserInputError("Bet must be >= 10"), "invalid_arguments", "invalid input", "error", False),
        (PermissionDeniedError("Admins only"), "unauthorized", "unauthorized", "error", False),
        (
            GuildSetupRequiredError("No channel configured"),
            "missing_guild_setup",
            "not configured",
            "warning",
            False,
        ),
        (FeatureDisabledError("Casino is turned off"), "disabled_feature", "disabled", "disabled", False),
        (MissingApiKeyError("ODDS_API_KEY missing"), "missing_api_key", "not configured", "warning", True),
        (
            GeminiMisconfigurationError("Bad API key", status=401),
            "missing_api_key",
            "not configured",
            "warning",
            True,
        ),
        (
            TransportRateLimitError("HTTP 429", status=429, retry_after=15.0),
            "provider_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            GeminiRateLimitError("Quota exceeded", status=429, retry_after=20.0),
            "provider_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            TransportServerError("HTTP 503", status=503),
            "provider_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (TimeoutError("socket timeout"), "provider_unavailable", "temporarily unavailable", "warning", True),
        (
            SchemaDriftError("Unexpected GraphQL shape"),
            "provider_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            GeminiInvalidResponseError("No candidates"),
            "provider_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            StaleCacheError("Showing cached snapshot"),
            "stale_cache",
            "temporarily unavailable",
            "warning",
            False,
        ),
        (
            RecordNotFoundError("No course found for XYZ999"),
            "missing_record",
            "no matching results",
            "normal",
            False,
        ),
        (
            sqlite3.OperationalError("database is locked"),
            "storage_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            StorageUnavailableError("Read-only volume"),
            "storage_unavailable",
            "temporarily unavailable",
            "warning",
            True,
        ),
        (
            ConcurrencyConflictError("Hand already in progress"),
            "concurrency_conflict",
            "conflict",
            "warning",
            False,
        ),
        (
            _fake_discord_forbidden(),
            "discord_permission_failure",
            "unauthorized",
            "warning",
            True,
        ),
        (
            RuntimeError("Unhandled internal invariant"),
            "unexpected_internal",
            "error",
            "error",
            True,
        ),
    ]

    seen_kinds: set[str] = set()
    seen_ux: set[str] = set()
    for exc, exp_kind, exp_ux, exp_status, exp_cid in cases:
        classified = classify_failure(exc)
        seen_kinds.add(classified.kind)
        seen_ux.add(classified.ux_category)
        assert classified.kind == exp_kind, f"Mismatch for {exc!r}: got {classified.kind}"
        assert classified.ux_category == exp_ux
        assert classified.embed_status == exp_status
        assert classified.include_correlation_id is exp_cid
        assert classified.ephemeral is True
        assert classified.recovery_action != ""
        rendered = classified.format_text(correlation_id="abc12345")
        if exp_cid:
            assert "Ref: `abc12345`" in rendered
        else:
            assert "Ref:" not in rendered

    # Confirm all 12 failure families and all 4 required UX distinctions were exercised
    assert len(seen_kinds) == 12
    assert {"temporarily unavailable", "not configured", "disabled", "no matching results"}.issubset(seen_ux)


@pytest.mark.asyncio
async def test_cancellation_is_never_swallowed_by_error_routing_or_view_handlers(
    make_interaction,
    make_member,
) -> None:
    """Verify asyncio.CancelledError propagates immediately out of classify_failure, respond_with_failure, on_app_command_error, and view.on_error."""
    settings = Settings()
    user = make_member(user_id=850000000000000001)
    inter = make_interaction(user=user, settings=settings)

    with pytest.raises(asyncio.CancelledError):
        classify_failure(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await respond_with_failure(inter, asyncio.CancelledError())

    wrapped_cancel = app_commands.AppCommandError("cancelled")
    wrapped_cancel.original = asyncio.CancelledError()  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await on_app_command_error(inter, wrapped_cancel)

    view = JobPaginationView(
        settings=settings,
        postings=[],
        preset_name="tech_swe",
        total_matched=0,
        last_updated_str="",
        owner_id=user.id,
    )
    with pytest.raises(asyncio.CancelledError):
        await view.on_error(inter, asyncio.CancelledError(), view.next_button)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retryable_status_and_circuit_breaker_without_fabricated_fallback(mock_aiohttp) -> None:
    """Verify BoundedHttpTransport retries 429/503 with Retry-After and ProviderCircuitBreaker opens/recovers without fabricating data."""
    now_ref = [1000.0]
    slept: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        slept.append(sec)
        now_ref[0] += sec

    breaker = ProviderCircuitBreaker(
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
        clock=lambda: now_ref[0],
    )
    url = "https://api.example.com/v1/feed?apiKey=secret-retry-key"

    # 1. Transient 429 followed by 200 succeeds after 1 bounded retry respecting Retry-After
    mock_aiohttp.get(url, status=429, headers={"Retry-After": "3"}, text="rate limited", repeat=False)
    mock_aiohttp.get(url, status=200, json_data={"ok": True, "items": [1, 2]}, repeat=False)

    async with BoundedHttpTransport(
        source_name="test_feed",
        max_retries=2,
        circuit_breaker=breaker,
        enforce_ssrf_protection=False,
        clock=lambda: now_ref[0],
        sleep_func=_fake_sleep,
    ) as transport:
        res = await transport.get_json(url, secrets=("secret-retry-key",))
        assert res.data == {"ok": True, "items": [1, 2]}
        assert slept == [3.0]
        assert breaker.state == "closed"

        # 2. Two sustained 503 failures trip the circuit breaker open (no fabricated fallback data)
        for _ in range(6):
            mock_aiohttp.get(url, status=503, text="service down", repeat=False)

        with pytest.raises(TransportServerError) as exc1:
            await transport.get_json(url, secrets=("secret-retry-key",))
        assert "secret-retry-key" not in str(exc1.value)
        assert breaker.state == "closed"  # 1 failure < threshold 2

        with pytest.raises(TransportServerError):
            await transport.get_json(url, secrets=("secret-retry-key",))
        assert breaker.state == "open"

        # 3. While breaker is open, requests fail fast with CircuitBreakerOpenError without hitting network
        calls_before = len(mock_aiohttp.calls)
        with pytest.raises(CircuitBreakerOpenError) as open_exc:
            await transport.get_json(url, secrets=("secret-retry-key",))
        assert len(mock_aiohttp.calls) == calls_before
        assert open_exc.value.status == 503
        assert classify_failure(open_exc.value).ux_category == "temporarily unavailable"

        # 4. After recovery_timeout_seconds elapses, breaker enters half_open and closes on probe success
        now_ref[0] += 35.0
        assert breaker.state == "half_open"
        mock_aiohttp.get(url, status=200, json_data={"ok": True, "recovered": True}, repeat=False)
        recovered = await transport.get_json(url, secrets=("secret-retry-key",))
        assert recovered.data == {"ok": True, "recovered": True}
        assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_permission_denied_view_callbacks_and_no_double_acknowledgments(
    make_interaction,
    make_member,
) -> None:
    """Verify BasePaginationView.on_error routes discord.Forbidden ephemerally with Ref ID and never double-acks."""
    settings = Settings(embed_accent_color=0x004C97, embed_footer_text="TMU Hub")
    user = make_member(user_id=851000000000000001)

    view = JobPaginationView(
        settings=settings,
        postings=[],
        preset_name="tech_swe",
        total_matched=0,
        last_updated_str="",
        owner_id=user.id,
    )

    # Case A: Interaction not yet responded -> send_message(ephemeral=True)
    inter1 = make_interaction(user=user, settings=settings)
    await view.on_error(inter1, _fake_discord_forbidden(), view.next_button)
    assert len(inter1.response.sent_messages) == 1
    assert len(inter1.followup.messages) == 0
    assert inter1.response.sent_messages[0].ephemeral is True
    assert "Missing Discord Permissions" in (inter1.response.sent_messages[0].content or "") or (
        "lacks the required Discord channel permissions" in (inter1.response.sent_messages[0].content or "")
    )
    assert "Ref: `" in (inter1.response.sent_messages[0].content or "")

    # Case B: Interaction already deferred -> followup.send(ephemeral=True), zero second initial response
    inter2 = make_interaction(user=user, settings=settings)
    await inter2.response.defer(ephemeral=True)
    classified = await respond_with_failure(
        inter2,
        sqlite3.OperationalError("database is locked on /data/ic_stock.db"),
        settings=settings,
        prefer_embed=True,
    )
    assert classified.kind == "storage_unavailable"
    assert len(inter2.response.sent_messages) == 0
    assert len(inter2.followup.messages) == 1
    reply2 = inter2.followup.messages[0]
    assert reply2.ephemeral is True
    assert reply2.embed is not None
    assert reply2.embed.color.value == 0xF39C12  # warning semantic color
    assert "/data/ic_stock.db" not in (reply2.embed.description or "")
    assert "Reference ID:" in (reply2.embed.description or "")


@pytest.mark.asyncio
async def test_sports_command_defers_before_network_and_distinguishes_not_configured_vs_unavailable(
    make_interaction,
    make_member,
    monkeypatch,
) -> None:
    """Verify /ic sports defers before HTTP calls and clearly separates not configured vs temporarily unavailable."""
    user = make_member(user_id=852000000000000001)

    # 1. Missing ODDS_API_KEY -> 'not configured'
    settings_no_key = Settings(odds_api_key="")
    client_no_key = FakeClient(settings=settings_no_key)
    inter1 = make_interaction(user=user, client=client_no_key, command_name="ic sports nba")

    async def _empty_news(*args: Any, **kwargs: Any) -> list[tuple[str, str]]:
        return []

    monkeypatch.setattr("icbot.features.sports._get_news", _empty_news)
    await _send_sport(inter1, "nba", "🏀 NBA")
    assert inter1.response.deferred is True
    assert len(inter1.response.sent_messages) == 0
    assert len(inter1.followup.messages) == 1
    emb1 = inter1.followup.messages[0].embed
    assert emb1 is not None
    assert "not configured" in emb1.fields[0].value.lower()
    assert emb1.color.value == settings_no_key.embed_accent_color

    # 2. Configured ODDS_API_KEY but upstream times out -> 'temporarily unavailable' + warning color (0xF39C12)
    secret_key = "odds-secret-key-99999"
    settings_key = Settings(odds_api_key=secret_key)
    container_key = ServiceContainer.from_settings(settings_key)

    class _FailingOddsClient:
        async def get_upcoming(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise ProviderError(
                f"HTTP 503 https://api.the-odds-api.com/v4/sports/nba?apiKey={secret_key} /data/cache.db",
                status=503,
            )

    container_key.build_odds_client = lambda _s=None: _FailingOddsClient()  # type: ignore[method-assign]
    client_key = FakeClient(settings=settings_key, services=container_key)
    inter2 = make_interaction(user=user, client=client_key, command_name="ic sports nba")
    await _send_sport(inter2, "nba", "🏀 NBA")
    assert inter2.response.deferred is True
    emb2 = inter2.followup.messages[0].embed
    assert emb2 is not None
    assert emb2.color.value == 0xF39C12
    assert "temporarily unavailable" in emb2.fields[0].value.lower()
    assert secret_key not in emb2.fields[0].value
    assert "/data/cache.db" not in emb2.fields[0].value


@pytest.mark.unit
def test_sensitive_logging_and_provider_error_redaction() -> None:
    """Verify tokens, headers, resume content, raw prompts, SQL params, and host file paths are redacted from errors and logs."""
    secret_tok = "super-live-secret-token-xyz"
    raw = (
        f"GET https://api.provider.invalid/v1/scan?api_key={secret_tok}&token=inlineTok888 "
        "Authorization: Bearer authHdrSecret123 "
        "Cookie: session_id=cookieVal999 "
        "X-Goog-Api-Key: googKey777 "
        "prompt='Analyze confidential student Jane Doe SSN 123-45-6789' "
        "resume_text='John Smith 416-555-0199 GPA 3.9' "
        "parameters=(888000111, 'private_note_text', 42) "
        "sqlite3.OperationalError: unable to open /data/ic_stock.db or /home/operator/.env or C:\\Users\\admin\\bot.db"
    )

    scrubbed = redact_secrets(raw, secrets=(secret_tok,))
    for forbidden in (
        secret_tok,
        "inlineTok888",
        "authHdrSecret123",
        "cookieVal999",
        "googKey777",
        "Jane Doe SSN",
        "John Smith 416-555-0199",
        "private_note_text",
        "/data/ic_stock.db",
        "/home/operator/.env",
        "C:\\Users\\admin\\bot.db",
    ):
        assert forbidden not in scrubbed, f"Sensitive data leaked in redacted output: {forbidden!r}"

    assert REDACTED in scrubbed
    # Safe slash commands and non-secret URL hostnames remain readable for diagnostics
    assert "https://api.provider.invalid/v1/scan" in scrubbed
    assert "/ic admin setup" in redact_secrets("Run /ic admin setup to configure this server.")

    # Structured logger + RedactingLogFilter pipeline scrubs formatted exception records
    rec = logging.LogRecord(
        name="icbot.test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="Provider failure: %s",
        args=(format_exception_safe(RuntimeError(raw), secrets=(secret_tok,)),),
        exc_info=None,
    )
    filt = RedactingLogFilter(secrets=(secret_tok,))
    formatter = StructuredLogFormatter("%(message)s")
    assert filt.filter(rec) is True
    formatted_line = formatter.format(rec)
    for forbidden in (secret_tok, "cookieVal999", "Jane Doe SSN", "/data/ic_stock.db", "private_note_text"):
        assert forbidden not in formatted_line
