"""Unit tests for typed domain errors and centralized secret redaction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import app_commands

from icbot.errors import (
    REDACTED,
    BotError,
    ConfigValidationError,
    InsufficientBalanceError,
    MisconfigurationError,
    PermissionDeniedError,
    ProviderError,
    UserInputError,
    format_exception_safe,
    redact_secrets,
    safe_error_summary,
)

pytestmark = pytest.mark.unit


def _synthetic_discord_token() -> str:
    return "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM" + "." + "AbCdEf" + "." + "1234567890abcdef1234567890abcd"


def _synthetic_gemini_key() -> str:
    return "AIzaSy" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"


def _synthetic_aws_key() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _synthetic_github_pat() -> str:
    return "ghp_" + ("x" * 36)


def _synthetic_discord_webhook() -> str:
    return "https://discord" + ".com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz"


def test_exception_hierarchy_preserves_legacy_base_classes():
    assert issubclass(ConfigValidationError, BotError)
    assert issubclass(ConfigValidationError, ValueError)
    assert MisconfigurationError is ConfigValidationError
    assert issubclass(UserInputError, BotError)
    assert issubclass(UserInputError, ValueError)
    assert issubclass(PermissionDeniedError, BotError)
    assert issubclass(PermissionDeniedError, PermissionError)
    assert issubclass(ProviderError, BotError)
    assert issubclass(ProviderError, RuntimeError)
    assert issubclass(InsufficientBalanceError, BotError)
    assert issubclass(InsufficientBalanceError, RuntimeError)


def test_redact_secrets_scrubs_explicit_query_headers_and_token_shapes():
    explicit_long = "super-secret-value-longer"
    explicit_short_prefix = "super-secret"
    discord_tok = _synthetic_discord_token()
    gemini_key = _synthetic_gemini_key()
    aws_key = _synthetic_aws_key()
    gh_pat = _synthetic_github_pat()
    webhook = _synthetic_discord_webhook()

    raw = (
        f"explicit={explicit_long} short={explicit_short_prefix} ignored=x "
        f"url=https://api.example.invalid/v1/odds?sport=nba&apiKey=secretQ123&token=tok999&auth=pass1 "
        f"hdr1='Authorization: Bearer headerTok123' hdr2='X-API-Key: apiHdrKey456' "
        f"bare='Bearer bareTok99999 Bot {discord_tok}' "
        f"gem={gemini_key} aws={aws_key} gh={gh_pat} wh={webhook}"
    )
    redacted = redact_secrets(raw, secrets=["x", explicit_short_prefix, explicit_long])

    assert REDACTED in redacted
    for sensitive in (
        explicit_long,
        explicit_short_prefix,
        "secretQ123",
        "tok999",
        "pass1",
        "headerTok123",
        "apiHdrKey456",
        "bareTok99999",
        discord_tok,
        gemini_key,
        aws_key,
        gh_pat,
        webhook,
    ):
        assert sensitive not in redacted

    assert "sport=nba" in redacted
    assert "x" in redacted


def test_safe_error_summary_distinguishes_domain_errors_timeouts_and_internal_errors():
    secret = _synthetic_gemini_key()
    prov = ProviderError(f"Upstream rejected request with key={secret} on url?apiKey={secret}")
    summary = safe_error_summary(prov, secrets=[secret])
    assert secret not in summary
    assert REDACTED in summary
    assert "Upstream rejected request" in summary

    wrapped = app_commands.CommandInvokeError(SimpleNamespace(name="synthetic"), prov)  # type: ignore[arg-type]
    wrapped_summary = safe_error_summary(wrapped, secrets=[secret])
    assert secret not in wrapped_summary
    assert REDACTED in wrapped_summary

    assert safe_error_summary(TimeoutError("raw socket wait"), [secret]) == "Upstream request timed out."

    unexpected = KeyError(f"internal_detail_{secret}")
    assert safe_error_summary(unexpected, [secret]) == "An unexpected error occurred."

    long_err = BotError("A" * 120)
    truncated = safe_error_summary(long_err, max_len=30)
    assert len(truncated) <= 30
    assert truncated.endswith("…")


def test_format_exception_safe_includes_type_and_scrubs_secrets():
    secret = "custom-go-open-data-" + "secret-999"
    exc = RuntimeError(f"GET https://api.example.invalid/feed?key={secret}&X-API-Key: {secret}")
    formatted = format_exception_safe(exc, secrets=[secret], max_len=120)
    assert "RuntimeError" in formatted
    assert secret not in formatted
    assert REDACTED in formatted
