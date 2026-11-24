"""Typed domain errors and secret-redaction helpers for Instigate Cafe Bot.

Provides a unified exception hierarchy for configuration validation, input errors,
permission failures, economy balance checks, and external provider failures, along
with deterministic secret redaction for logs and user-facing error summaries.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import discord

    from icbot.config import Settings
    from icbot.theme import EmbedStatus

REDACTED = "[REDACTED]"

# Sensitive query parameter names whose values must always be scrubbed from logs/errors
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|apiKey|apikey|token|access_token|secret|auth|password)=)([^&#\s\"'<>]+)",
    re.IGNORECASE,
)

# Authorization / API key / Cookie headers in loggable snippets
_HEADER_SECRET_RE = re.compile(
    r"((?:Authorization|Proxy-Authorization|X-API-Key|x-api-key|api-key|X-Goog-Api-Key|X-Auth-Token|Cookie|Set-Cookie)\s*:\s*(?:(?:Bearer|Basic|Bot)\s+)?)([^\s,;\"'\r\n]+)",
    re.IGNORECASE,
)

# Standalone auth token prefixes in free text
_BEARER_TOKEN_RE = re.compile(r"\b(Bearer|Bot)\s+([A-Za-z0-9._\-~+/]{8,}=*)")

# Host filesystem paths (Unix absolute system/app paths and Windows drive paths); excludes URLs and /ic commands
_HOST_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9:/._-])/(?:home|Users|var|tmp|etc|opt|srv|data|app|root|mnt|proc|usr)/[^\s\"'<>),;]+|"
    r"\b[A-Za-z]:\\[^\s\"'<>),;]+)"
)

# Bound SQL parameter tuples/lists and raw prompt/resume payload fields in diagnostic strings
_SQL_PARAMS_RE = re.compile(
    r"(\b(?:params|parameters|bindings)\s*=\s*)(\([^)]*\)|\[[^\]]*\]|\{[^}]*\})",
    re.IGNORECASE,
)
_PROMPT_PAYLOAD_RE = re.compile(
    r"(\b(?:prompt|system_instruction|resume_text|resume_content|inline_images)\s*[:=]\s*)"
    r"(?:(['\"])(?:\\.|(?!\2).)*\2|[^\r\n;]{12,})",
    re.IGNORECASE,
)

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


class GuildSetupRequiredError(BotError):
    """Raised when a server has not completed required channel or module setup."""


class FeatureDisabledError(BotError):
    """Raised when a requested command belongs to a module disabled for the server."""


class MissingApiKeyError(BotError):
    """Raised when an optional third-party integration API key is not configured."""


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


class StaleCacheError(ProviderError):
    """Raised when live upstream data is unavailable and only stale cached records exist."""


class RecordNotFoundError(BotError, LookupError):
    """Raised when a requested record, ticker, course, or exam does not exist."""


class StorageUnavailableError(BotError, RuntimeError):
    """Raised when SQLite storage is locked, read-only, or temporarily unavailable."""


class ConcurrencyConflictError(BotError, RuntimeError):
    """Raised when a concurrent update or active session lock blocks a duplicate operation."""


class IdempotencyConflict(ConcurrencyConflictError):
    """Raised when an idempotency key collides with an existing transaction whose semantic identity differs.

    Public messages intentionally omit user IDs, guild IDs, raw keys, balances, and reference strings
    to avoid leaking sensitive or cross-tenant data.
    """

    DEFAULT_MESSAGE = "Idempotency key conflict: request parameters do not match the existing transaction."

    def __init__(
        self,
        message: str = DEFAULT_MESSAGE,
        *,
        mismatched_fields: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.mismatched_fields: tuple[str, ...] = tuple(str(f) for f in mismatched_fields)


class InsufficientBalanceError(BotError, RuntimeError):
    """Raised when a user lacks sufficient virtual currency for an operation."""


def redact_secrets(text: str, secrets: Iterable[str] | None = None) -> str:
    """Scrub API keys, tokens, auth headers, query secrets, host paths, SQL params, and raw payloads."""
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
    out = _SQL_PARAMS_RE.sub(rf"\1{REDACTED}", out)
    out = _PROMPT_PAYLOAD_RE.sub(rf"\1{REDACTED}", out)
    out = _HOST_PATH_RE.sub(REDACTED, out)
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


# ---------------------------------------------------------------------------
# Failure Taxonomy & Shared Interaction Error Routing
# ---------------------------------------------------------------------------

FailureKind = Literal[
    "invalid_arguments",
    "unauthorized",
    "missing_guild_setup",
    "disabled_feature",
    "missing_api_key",
    "provider_unavailable",
    "stale_cache",
    "missing_record",
    "storage_unavailable",
    "concurrency_conflict",
    "discord_permission_failure",
    "unexpected_internal",
]

UxCategory = Literal[
    "temporarily unavailable",
    "not configured",
    "disabled",
    "no matching results",
    "invalid input",
    "unauthorized",
    "conflict",
    "error",
]


@dataclass(frozen=True)
class ClassifiedFailure:
    """Structured classification of an error into user-facing UX, audience, and logging policy."""

    kind: FailureKind
    ux_category: UxCategory
    embed_status: EmbedStatus
    title: str
    user_message: str
    recovery_action: str
    ephemeral: bool = True
    log_level: int = logging.WARNING
    include_correlation_id: bool = False
    retry_after: float | None = None

    def format_text(self, *, correlation_id: str | None = None) -> str:
        """Render a concise single-message text summary with recovery guidance and optional ref ID."""
        if self.kind in ("provider_unavailable", "invalid_arguments", "unauthorized", "unexpected_internal"):
            prefix = "❌"
        else:
            prefix = {
                "disabled": "🚫",
                "warning": "⚠️",
                "normal": "ℹ️",
                "info": "ℹ️",
                "error": "❌",
                "success": "✅",
            }.get(self.embed_status, "❌")
        parts = [f"{prefix} {self.user_message}"]
        if self.recovery_action:
            parts.append(self.recovery_action)
        if self.include_correlation_id and correlation_id:
            parts.append(f"(Ref: `{correlation_id}`)")
        return " ".join(parts)


def _unwrap_exception(exc: BaseException) -> BaseException:
    inner = getattr(exc, "original", exc)
    return inner if isinstance(inner, BaseException) else exc


def _is_sqlite_storage_error(exc: BaseException) -> bool:
    if isinstance(exc, (sqlite3.Error, StorageUnavailableError)):
        return True
    mod = getattr(type(exc), "__module__", "") or ""
    return mod.startswith("aiosqlite")


def classify_failure(
    exc: BaseException,
    *,
    secrets: Iterable[str] | None = None,
    correlation_id: str | None = None,
) -> ClassifiedFailure:
    """Map an exception into the bot's 12-family failure taxonomy without swallowing cancellation.

    Never swallows `asyncio.CancelledError`, `KeyboardInterrupt`, or `SystemExit`.
    """
    inner = _unwrap_exception(exc)
    if isinstance(inner, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        raise inner

    status, retry_after = extract_http_error_info(inner)
    type_name = type(inner).__name__

    # 1. Invalid arguments (UserInputError, TransformerError, ValueError, InsufficientBalanceError)
    if isinstance(inner, (UserInputError, InsufficientBalanceError)) or type_name in (
        "TransformerError",
        "CommandSignatureMismatch",
    ):
        msg = safe_error_summary(inner, secrets, default_message="Invalid command argument.")
        return ClassifiedFailure(
            kind="invalid_arguments",
            ux_category="invalid input",
            embed_status="error",
            title="Invalid Input",
            user_message=msg,
            recovery_action="Check the command parameters and try again.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 2. Unauthorized actions (PermissionDeniedError, MissingPermissions, MissingRole)
    if isinstance(inner, PermissionDeniedError) or type_name in (
        "MissingPermissions",
        "MissingRole",
        "MissingAnyRole",
    ):
        msg = safe_error_summary(
            inner, secrets, default_message="You do not have permission to perform this action."
        )
        return ClassifiedFailure(
            kind="unauthorized",
            ux_category="unauthorized",
            embed_status="error",
            title="Permission Denied",
            user_message=msg,
            recovery_action="Contact a server administrator if you believe you should have access.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 3. Missing guild setup (GuildSetupRequiredError, ConfigValidationError / MisconfigurationError)
    if isinstance(inner, (GuildSetupRequiredError, ConfigValidationError)):
        msg = safe_error_summary(
            inner, secrets, default_message="This server is not configured for this feature yet."
        )
        return ClassifiedFailure(
            kind="missing_guild_setup",
            ux_category="not configured",
            embed_status="warning",
            title="Server Setup Required",
            user_message=msg,
            recovery_action="Run `/ic admin setup` or `/ic admin config` to configure this server.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 4. Disabled feature
    if isinstance(inner, FeatureDisabledError):
        msg = safe_error_summary(
            inner, secrets, default_message="This feature is currently disabled in this server."
        )
        return ClassifiedFailure(
            kind="disabled_feature",
            ux_category="disabled",
            embed_status="disabled",
            title="Module Disabled",
            user_message=msg,
            recovery_action="A server administrator can enable it via `/ic admin features`.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 5. Missing optional API key (MissingApiKeyError, GeminiMisconfigurationError)
    if isinstance(inner, MissingApiKeyError) or type_name == "GeminiMisconfigurationError":
        return ClassifiedFailure(
            kind="missing_api_key",
            ux_category="not configured",
            embed_status="warning",
            title="Integration Not Configured",
            user_message="This optional integration is not configured on this bot instance (missing or invalid API key).",
            recovery_action="Ask the bot operator to configure the optional API key in environment settings.",
            ephemeral=True,
            log_level=logging.WARNING,
            include_correlation_id=True,
        )

    # 6. Stale cache fallback notice
    if isinstance(inner, StaleCacheError):
        msg = safe_error_summary(
            inner,
            secrets,
            default_message="Live upstream data is temporarily unavailable; only cached data exists.",
        )
        return ClassifiedFailure(
            kind="stale_cache",
            ux_category="temporarily unavailable",
            embed_status="warning",
            title="Stale Cached Data",
            user_message=msg,
            recovery_action="Wait a few minutes and retry for refreshed results.",
            ephemeral=True,
            log_level=logging.WARNING,
            include_correlation_id=False,
            retry_after=retry_after,
        )

    # 7. Provider timeout / 429 / 5xx / circuit open / malformed payload
    if (
        isinstance(inner, (ProviderError, TimeoutError))
        or type_name
        in (
            "SchemaDriftError",
            "GeminiRateLimitError",
            "GeminiTransientError",
            "GeminiInvalidResponseError",
            "CircuitBreakerOpenError",
            "ClientError",
            "ClientConnectorError",
            "ServerTimeoutError",
        )
        or (status is not None and status in (429, 500, 502, 503, 504))
    ):
        if status == 429 or type_name == "GeminiRateLimitError":
            wait_hint = (
                f" Please wait about {max(1, round(retry_after))}s and try again."
                if retry_after is not None and retry_after > 0
                else " Please wait a moment and try again."
            )
            return ClassifiedFailure(
                kind="provider_unavailable",
                ux_category="temporarily unavailable",
                embed_status="warning",
                title="Provider Temporarily Rate-Limited",
                user_message="The upstream provider is temporarily rate-limiting requests.",
                recovery_action=wait_hint.strip(),
                ephemeral=True,
                log_level=logging.WARNING,
                include_correlation_id=True,
                retry_after=retry_after,
            )
        if type_name in ("SchemaDriftError", "GeminiInvalidResponseError"):
            return ClassifiedFailure(
                kind="provider_unavailable",
                ux_category="temporarily unavailable",
                embed_status="warning",
                title="Upstream Data Unavailable",
                user_message="The external provider returned malformed or unexpected data and is temporarily unavailable.",
                recovery_action="Try again later; core server commands remain unaffected.",
                ephemeral=True,
                log_level=logging.WARNING,
                include_correlation_id=True,
            )
        detail_summary = (
            safe_error_summary(inner, secrets, max_len=180)
            if isinstance(inner, ProviderError) and str(inner).strip()
            else ""
        )
        prov_msg = (
            f"External provider is temporarily unavailable ({detail_summary})."
            if detail_summary
            else "An external provider is temporarily unavailable or timed out."
        )
        return ClassifiedFailure(
            kind="provider_unavailable",
            ux_category="temporarily unavailable",
            embed_status="warning",
            title="Service Temporarily Unavailable",
            user_message=prov_msg,
            recovery_action="Please try again in a minute.",
            ephemeral=True,
            log_level=logging.WARNING,
            include_correlation_id=True,
            retry_after=retry_after,
        )

    # 8. Missing record / no matching results
    if isinstance(inner, RecordNotFoundError):
        msg = safe_error_summary(inner, secrets, default_message="No matching records were found.")
        return ClassifiedFailure(
            kind="missing_record",
            ux_category="no matching results",
            embed_status="normal",
            title="No Matching Results",
            user_message=msg,
            recovery_action="Double-check your query or broaden your filters.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 9. Storage locked / unavailable (SQLite OperationalError / DatabaseError / StorageUnavailableError)
    if _is_sqlite_storage_error(inner):
        return ClassifiedFailure(
            kind="storage_unavailable",
            ux_category="temporarily unavailable",
            embed_status="warning",
            title="Storage Temporarily Busy",
            user_message="Local storage is temporarily busy or unavailable.",
            recovery_action="Please retry in a few seconds. If this persists, share the reference ID with an admin.",
            ephemeral=True,
            log_level=logging.ERROR,
            include_correlation_id=True,
        )

    # 10. Concurrency conflict
    if isinstance(inner, ConcurrencyConflictError):
        msg = safe_error_summary(
            inner, secrets, default_message="Another operation is already in progress for this session."
        )
        return ClassifiedFailure(
            kind="concurrency_conflict",
            ux_category="conflict",
            embed_status="warning",
            title="Concurrent Action Conflict",
            user_message=msg,
            recovery_action="Finish or wait for the current action to complete before retrying.",
            ephemeral=True,
            log_level=logging.INFO,
            include_correlation_id=False,
        )

    # 11. Discord permission failure (discord.Forbidden / BotMissingPermissions)
    if type_name in ("Forbidden", "BotMissingPermissions"):
        return ClassifiedFailure(
            kind="discord_permission_failure",
            ux_category="unauthorized",
            embed_status="warning",
            title="Missing Discord Permissions",
            user_message="The bot lacks the required Discord channel permissions to complete this action.",
            recovery_action="Ask a server administrator to grant Send Messages and Embed Links permissions.",
            ephemeral=True,
            log_level=logging.WARNING,
            include_correlation_id=True,
        )

    # Other explicit BotError subclasses
    if isinstance(inner, BotError):
        msg = safe_error_summary(inner, secrets, default_message="Request could not be completed.")
        return ClassifiedFailure(
            kind="invalid_arguments",
            ux_category="error",
            embed_status="error",
            title="Request Failed",
            user_message=msg,
            recovery_action="Please review your request and try again.",
            ephemeral=True,
            log_level=logging.WARNING,
            include_correlation_id=False,
        )

    # 12. Unexpected internal exceptions
    return ClassifiedFailure(
        kind="unexpected_internal",
        ux_category="error",
        embed_status="error",
        title="Unexpected Error",
        user_message="An unexpected error occurred.",
        recovery_action="Please try again later or share the reference ID with a server administrator.",
        ephemeral=True,
        log_level=logging.ERROR,
        include_correlation_id=True,
    )


def _interaction_response_is_done(interaction: Any) -> bool:
    resp = getattr(interaction, "response", None)
    if resp is None:
        return False
    is_done_fn = getattr(resp, "is_done", None)
    if not callable(is_done_fn):
        return False
    res = is_done_fn()
    if inspect.iscoroutine(res):
        res.close()
        return False
    return res is True


async def respond_with_failure(
    interaction: discord.Interaction[Any],
    exc: BaseException,
    *,
    logger: logging.Logger | None = None,
    settings: Settings | None = None,
    command_name: str | None = None,
    correlation_id: str | None = None,
    prefer_embed: bool = False,
) -> ClassifiedFailure:
    """Log a redacted incident and send a single deadline-safe ephemeral failure reply.

    - Re-raises `asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit` immediately.
    - Never sends a second initial response (uses `followup.send` if already deferred/responded).
    - Never leaks secrets, URL tokens, headers, prompts, SQL parameters, or file paths.
    """
    inner = _unwrap_exception(exc)
    if isinstance(inner, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        raise inner

    from icbot.logging import get_correlation_id, log_event

    eff_settings = settings or getattr(getattr(interaction, "client", None), "settings", None)
    secrets: tuple[str, ...] = (
        tuple(eff_settings.secret_values())
        if eff_settings is not None and hasattr(eff_settings, "secret_values")
        else ()
    )
    cid = (correlation_id or get_correlation_id() or uuid.uuid4().hex[:8]).strip()
    classified = classify_failure(exc, secrets=secrets, correlation_id=cid)

    cmd_obj = getattr(interaction, "command", None)
    cmd_label = command_name or getattr(cmd_obj, "qualified_name", None) or "unknown"
    guild_id = getattr(interaction, "guild_id", None)
    if guild_id is None and getattr(interaction, "guild", None) is not None:
        guild_id = getattr(interaction.guild, "id", None)

    target_logger = logger or logging.getLogger(__name__)
    log_event(
        target_logger,
        classified.log_level,
        f"interaction_failure_{classified.kind}",
        f"Failure ({classified.kind}) in {cmd_label}: {format_exception_safe(inner, secrets)}",
        guild_id=guild_id,
        correlation_id=cid,
        outcome=classified.kind,
        error_type=type(inner).__name__,
        extra={"command": cmd_label, "ux_category": classified.ux_category},
    )

    text_payload = classified.format_text(correlation_id=cid if classified.include_correlation_id else None)
    embed_payload = None
    if prefer_embed and eff_settings is not None:
        from icbot.embeds import system as system_embed

        desc_lines = [classified.user_message]
        if classified.recovery_action:
            desc_lines.append("")
            desc_lines.append(f"💡 **Next step:** {classified.recovery_action}")
        if classified.include_correlation_id:
            desc_lines.append("")
            desc_lines.append(f"Reference ID: `{cid}`")
        embed_payload = system_embed(
            eff_settings,
            title=classified.title,
            summary="\n".join(desc_lines),
            footer_hint=classified.ux_category.title(),
            status=classified.embed_status,
        )

    try:
        if not _interaction_response_is_done(interaction):
            if embed_payload is not None:
                await interaction.response.send_message(
                    embed=embed_payload,
                    ephemeral=classified.ephemeral,
                )
            else:
                await interaction.response.send_message(
                    text_payload,
                    ephemeral=classified.ephemeral,
                )
        else:
            if embed_payload is not None:
                await interaction.followup.send(
                    embed=embed_payload,
                    ephemeral=classified.ephemeral,
                )
            else:
                await interaction.followup.send(
                    text_payload,
                    ephemeral=classified.ephemeral,
                )
    except (RuntimeError, AttributeError, OSError, ValueError, TypeError) as delivery_err:
        target_logger.debug(
            "Failed sending interaction failure response (cid=%s): %s",
            cid,
            format_exception_safe(delivery_err, secrets),
        )
    except Exception as delivery_err:
        # Handle discord.DiscordException / aiohttp.ClientError without hard-importing both at module load
        mod_name = getattr(type(delivery_err), "__module__", "") or ""
        if mod_name.startswith(("discord", "aiohttp")):
            target_logger.debug(
                "Discord/HTTP error sending failure response (cid=%s): %s",
                cid,
                format_exception_safe(delivery_err, secrets),
            )
        else:
            raise

    return classified
