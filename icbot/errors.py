"""Typed domain errors and secret-redaction helpers for Instigate Cafe Bot.

Provides a unified exception hierarchy for configuration validation, input errors,
permission failures, economy balance checks, and external provider failures, along
with deterministic secret redaction for logs and user-facing error summaries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Sensitive query parameter names whose values must always be scrubbed from logs/errors
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|apiKey|apikey|token|access_token|secret|auth|password)=)([^&#\s\"'<>]+)",
    re.IGNORECASE,
)

# Authorization / API key headers in loggable snippets
_HEADER_SECRET_RE = re.compile(
    r"((?:Authorization|X-API-Key|x-api-key|api-key)\s*:\s*(?:(?:Bearer|Basic|Bot)\s+)?)([^\s,;\"']+)",
    re.IGNORECASE,
)

# Standalone auth token prefixes in free text
_BEARER_TOKEN_RE = re.compile(r"\b(Bearer|Bot)\s+([A-Za-z0-9._\-~+/]{8,}=*)")

# Well-known credential token shapes (matching tools/scan_secrets.py defensively)
_KNOWN_TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:[MN][A-Za-z\d]{23,26}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,38}|mfa\.[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(r"\bAIzaSy[A-Za-z0-9_-]{33}\b"),
    re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{82})\b"),
    re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9_]+/B[A-Z0-9_]+/[A-Za-z0-9_]+"),
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
)


class BotError(Exception):
    """Base domain exception for Instigate Cafe Bot."""


class ConfigValidationError(BotError, ValueError):
    """Raised when configuration fails schema, bounds, or isolation validation."""


# Clear synonym for invalid bot/guild configuration
MisconfigurationError = ConfigValidationError


class UserInputError(BotError, ValueError):
    """Raised when a user supplies an invalid command parameter or argument."""


class PermissionDeniedError(BotError, PermissionError):
    """Raised when a user lacks permission for a guarded command or action."""


class ProviderError(BotError, RuntimeError):
    """Raised when an external HTTP or API integration fails cleanly.

    Inherits ``RuntimeError`` for backwards compatibility with legacy callers and tests.
    """


class InsufficientBalanceError(BotError, RuntimeError):
    """Raised when a user lacks sufficient virtual currency for an operation."""


def redact_secrets(text: str, secrets: Iterable[str] | None = None) -> str:
    """Scrub API keys, tokens, auth headers, and sensitive query parameters from ``text``."""
    out = str(text or "")
    if not out:
        return ""

    # Replace explicit secret values first (longest-first to avoid partial leak remnants)
    if secrets:
        cleaned_secrets = sorted(
            {str(s).strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= 4},
            key=len,
            reverse=True,
        )
        for sec in cleaned_secrets:
            out = out.replace(sec, REDACTED)

    out = _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", out)
    out = _HEADER_SECRET_RE.sub(rf"\1{REDACTED}", out)
    out = _BEARER_TOKEN_RE.sub(rf"\1 {REDACTED}", out)
    for pat in _KNOWN_TOKEN_RES:
        out = pat.sub(REDACTED, out)
    return out


def safe_error_summary(
    exc: BaseException,
    secrets: Iterable[str] | None = None,
    *,
    default_message: str = "An unexpected error occurred.",
    max_len: int = 300,
) -> str:
    """Return a user-safe, secret-redacted single-line error summary for Discord replies/logs."""
    inner = getattr(exc, "original", exc)
    if not isinstance(inner, BaseException):
        inner = exc

    if isinstance(inner, BotError):
        raw = redact_secrets(str(inner), secrets).strip()
        msg = " ".join(raw.split()) if raw else redact_secrets(type(inner).__name__, secrets)
    elif isinstance(inner, TimeoutError):
        msg = "Upstream request timed out."
    else:
        msg = default_message

    if max_len > 3 and len(msg) > max_len:
        msg = msg[: max_len - 1].rstrip() + "…"
    return msg


def format_exception_safe(
    exc: BaseException, secrets: Iterable[str] | None = None, *, max_len: int = 400
) -> str:
    """Format an exception with type name and redacted message for diagnostic logging."""
    detail = redact_secrets(repr(exc), secrets)
    if max_len > 3 and len(detail) > max_len:
        detail = detail[: max_len - 1].rstrip() + "…"
    return detail
