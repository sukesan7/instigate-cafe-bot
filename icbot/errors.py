"""Typed domain errors and secret-redaction helpers for Instigate Cafe Bot.

Provides a unified exception hierarchy for configuration validation, input errors,
permission failures, economy balance checks, and external provider failures, along
with deterministic secret redaction for logs and user-facing error summaries.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

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
    Optionally carries ``status`` (HTTP status code) and ``retry_after`` (seconds)
    so supervised pollers can apply RFC-compliant backoff without coupling to transport classes.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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


def parse_retry_after(raw: str | int | float | None, *, now: float | None = None) -> float | None:
    """Parse HTTP ``Retry-After`` (delta-seconds or IMF-fixdate) into non-negative seconds."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return max(0.0, val)

    text = str(raw).strip()
    if not text:
        return None

    try:
        val = float(text)
    except ValueError:
        pass
    else:
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return max(0.0, val)

    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    now_ts = float(time.time() if now is None else now)
    now_dt = datetime.fromtimestamp(now_ts, tz=UTC)
    return max(0.0, (dt - now_dt).total_seconds())


def extract_http_error_info(
    exc: BaseException, *, now: float | None = None
) -> tuple[int | None, float | None]:
    """Extract ``(status, retry_after_seconds)`` from provider, aiohttp, or Discord exceptions."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status_raw = getattr(cur, "status", None)
        status = int(status_raw) if isinstance(status_raw, int) and not isinstance(status_raw, bool) else None

        retry_raw = getattr(cur, "retry_after", None)
        retry_after = parse_retry_after(retry_raw, now=now)

        if retry_after is None:
            headers = getattr(cur, "headers", None)
            if isinstance(headers, Mapping):
                for key in ("Retry-After", "retry-after"):
                    if key in headers:
                        retry_after = parse_retry_after(headers[key], now=now)
                        if retry_after is not None:
                            break
            response = getattr(cur, "response", None)
            resp_headers = getattr(response, "headers", None)
            if retry_after is None and isinstance(resp_headers, Mapping):
                for key in ("Retry-After", "retry-after"):
                    if key in resp_headers:
                        retry_after = parse_retry_after(resp_headers[key], now=now)
                        if retry_after is not None:
                            break

        if status is not None or retry_after is not None:
            return status, retry_after

        next_exc = getattr(cur, "original", None)
        if not isinstance(next_exc, BaseException):
            next_exc = cur.__cause__ or cur.__context__
        cur = next_exc if isinstance(next_exc, BaseException) else None

    return None, None


def format_exception_safe(
    exc: BaseException, secrets: Iterable[str] | None = None, *, max_len: int = 400
) -> str:
    """Format an exception with type name and redacted message for diagnostic logging."""
    detail = redact_secrets(repr(exc), secrets)
    if max_len > 3 and len(detail) > max_len:
        detail = detail[: max_len - 1].rstrip() + "…"
    return detail
