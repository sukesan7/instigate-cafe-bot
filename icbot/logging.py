"""Structured, privacy-respecting logging and maintainer diagnostics for Instigate Cafe Bot.

Provides:
1. Deterministic secret redaction filter (`RedactingLogFilter`) for loggers and handlers.
2. Structured log formatting (`StructuredLogFormatter`) supporting correlation IDs and metadata.
3. High-level event logging helper (`log_event`).
4. Rotating log policy and stream handler configuration (`configure_logging`).
5. Outage dampening / deduplication tracker (`ProviderOutageTracker`).
6. Sanitized issue report generator (`generate_sanitized_issue_report`).
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from icbot.errors import redact_secrets

BOT_VERSION: str = "v1.0.0-rc1"


# Context variable for tracing asynchronous operations across coroutines
_CORRELATION_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the current correlation ID, generating a 8-char hex token if none is active."""
    cid = _CORRELATION_ID_CTX.get()
    if not cid:
        cid = uuid.uuid4().hex[:8]
        _CORRELATION_ID_CTX.set(cid)
    return cid


def set_correlation_id(correlation_id: str | None = None) -> str:
    """Set or generate the active correlation ID in context."""
    cid = correlation_id.strip() if correlation_id and correlation_id.strip() else uuid.uuid4().hex[:8]
    _CORRELATION_ID_CTX.set(cid)
    return cid


class CorrelationContext:
    """Context manager for scoping correlation IDs to specific tasks or interactions."""

    def __init__(self, correlation_id: str | None = None) -> None:
        self.cid = (
            correlation_id.strip() if correlation_id and correlation_id.strip() else uuid.uuid4().hex[:8]
        )
        self._token: contextvars.Token[str] | None = None

    def __enter__(self) -> str:
        self._token = _CORRELATION_ID_CTX.set(self.cid)
        return self.cid

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._token is not None:
            _CORRELATION_ID_CTX.reset(self._token)


class RedactingLogFilter(logging.Filter):
    """Logging filter that scrubs tokens, auth headers, query keys, and secrets from all log records."""

    def __init__(self, secrets: tuple[str, ...] | list[str] | None = None) -> None:
        super().__init__()
        self.secrets = tuple(secrets or ())

    def _redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return redact_secrets(obj, self.secrets)
        if isinstance(obj, BaseException):
            return redact_secrets(str(obj), self.secrets)
        if isinstance(obj, dict):
            return {k: self._redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, tuple):
            return tuple(self._redact_obj(v) for v in obj)
        if isinstance(obj, list):
            return [self._redact_obj(v) for v in obj]
        return obj

    def filter(self, record: logging.LogRecord) -> bool:
        record._redaction_secrets = self.secrets
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg, self.secrets)
        elif isinstance(record.msg, BaseException):
            record.msg = redact_secrets(str(record.msg), self.secrets)

        if record.args:
            record.args = self._redact_obj(record.args)

        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self.secrets)

        # Ensure correlation ID is present on the record
        if not hasattr(record, "correlation_id") or not record.correlation_id:
            record.correlation_id = _CORRELATION_ID_CTX.get() or ""

        return True


class StructuredLogFormatter(logging.Formatter):
    """Formatter that prepends structured tags (correlation ID, event, provider) to log lines."""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", "") or _CORRELATION_ID_CTX.get() or ""
        event = getattr(record, "event_name", "")
        provider = getattr(record, "provider", "")
        guild_id = getattr(record, "guild_id", None)

        prefix_parts: list[str] = []
        if cid:
            prefix_parts.append(f"cid={cid}")
        if event:
            prefix_parts.append(f"event={event}")
        if provider:
            prefix_parts.append(f"provider={provider}")
        if guild_id:
            prefix_parts.append(f"guild={guild_id}")

        tag_str = f"[{' '.join(prefix_parts)}] " if prefix_parts else ""
        original_msg = super().format(record)
        secrets = getattr(record, "_redaction_secrets", ())
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, secrets)
        return f"{tag_str}{redact_secrets(original_msg, secrets)}"


def log_event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    message: str = "",
    *,
    guild_id: int | None = None,
    provider: str | None = None,
    module: str | None = None,
    correlation_id: str | None = None,
    latency_ms: float | None = None,
    retry_count: int | None = None,
    outcome: str = "success",
    error_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a structured log entry with consistent metadata attributes."""
    cid = correlation_id or _CORRELATION_ID_CTX.get() or uuid.uuid4().hex[:8]

    meta_parts: list[str] = [f"outcome={outcome}"]
    if latency_ms is not None:
        meta_parts.append(f"latency={latency_ms:.1f}ms")
    if retry_count is not None and retry_count > 0:
        meta_parts.append(f"retries={retry_count}")
    if error_type:
        meta_parts.append(f"error_type={error_type}")

    meta_suffix = f" ({' '.join(meta_parts)})" if meta_parts else ""
    full_msg = f"{message}{meta_suffix}" if message else " ".join(meta_parts)

    record_extra: dict[str, Any] = {
        "event_name": event_name,
        "correlation_id": cid,
        "guild_id": guild_id,
        "provider": provider,
        "component": module,
        "latency_ms": latency_ms,
        "retry_count": retry_count,
        "outcome": outcome,
        "error_type": error_type,
    }
    if extra:
        record_extra.update(extra)

    logger.log(level, full_msg, extra=record_extra)


class ProviderOutageTracker:
    """Dampens repetitive provider outage logs to prevent log spam during downtime.

    Logs the initial failure at WARNING, logs subsequent consecutive failures at DEBUG
    or throttled intervals, and logs an INFO recovery announcement when the provider recovers.
    """

    def __init__(self, logger: logging.Logger, *, repeat_alert_interval_seconds: float = 300.0) -> None:
        self.logger = logger
        self.interval = repeat_alert_interval_seconds
        self._failures: dict[str, int] = {}
        self._last_logged: dict[str, float] = {}

    def record_failure(
        self,
        provider: str,
        error_msg: str,
        *,
        now: float | None = None,
        correlation_id: str | None = None,
    ) -> None:
        ref_now = now if now is not None else time.time()
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        last_time = self._last_logged.get(provider, 0.0)

        cid = correlation_id or get_correlation_id()

        if count == 1:
            log_event(
                self.logger,
                logging.WARNING,
                event_name="provider_outage_started",
                message=f"Provider {provider} outage detected: {error_msg}",
                provider=provider,
                correlation_id=cid,
                outcome="error",
            )
            self._last_logged[provider] = ref_now
        elif (ref_now - last_time) >= self.interval:
            log_event(
                self.logger,
                logging.WARNING,
                event_name="provider_outage_ongoing",
                message=f"Provider {provider} still failing ({count} consecutive errors): {error_msg}",
                provider=provider,
                correlation_id=cid,
                outcome="error",
                retry_count=count,
            )
            self._last_logged[provider] = ref_now
        else:
            log_event(
                self.logger,
                logging.DEBUG,
                event_name="provider_outage_dampened",
                message=f"Provider {provider} consecutive failure #{count} (throttled): {error_msg}",
                provider=provider,
                correlation_id=cid,
                outcome="error",
            )

    def record_success(
        self,
        provider: str,
        *,
        correlation_id: str | None = None,
    ) -> bool:
        """Record a successful call, returning True if this resolved a previous outage."""
        count = self._failures.pop(provider, 0)
        self._last_logged.pop(provider, 0.0)
        if count > 0:
            cid = correlation_id or get_correlation_id()
            log_event(
                self.logger,
                logging.INFO,
                event_name="provider_recovered",
                message=f"Provider {provider} recovered after {count} consecutive failures.",
                provider=provider,
                correlation_id=cid,
                outcome="success",
            )
            return True
        return False

    def is_degraded(self, provider: str) -> bool:
        return self._failures.get(provider, 0) > 0


def configure_logging(
    level: int = logging.INFO,
    *,
    log_file: str | Path | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
    secrets: tuple[str, ...] | list[str] | None = None,
    telemetry_enabled: bool = False,
) -> None:
    """Configure structured logging, redacting filter, and optional rotating file handler."""
    root = logging.getLogger()
    root.setLevel(level)

    # Clean existing handlers
    for h in list(root.handlers):
        root.removeHandler(h)

    redacting_filter = RedactingLogFilter(secrets=secrets)
    formatter = StructuredLogFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stream Handler (stdout)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.addFilter(redacting_filter)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Optional Rotating File Handler (Docker / Railway / local host)
    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.addFilter(redacting_filter)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    if telemetry_enabled:
        logging.getLogger(__name__).info("Telemetry: ENABLED (opt-in active).")


def generate_sanitized_issue_report(
    *,
    version: str = "v2.0.0",
    db_schema_version: int = 4,
    db_journal_mode: str = "wal",
    feature_statuses: dict[str, str] | None = None,
    recent_logs: list[str] | None = None,
    secrets: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Compile a sanitized markdown diagnostic report suitable for GitHub issue reporting."""
    lines: list[str] = [
        "### Instigate Cafe Bot — Maintainer Diagnostic Report",
        "",
        "#### System Environment",
        f"- **Bot Version:** `{version}`",
        f"- **Python Runtime:** `{platform.python_version()} ({platform.python_implementation()})`",
        f"- **Operating System:** `{platform.system()} {platform.release()}`",
        f"- **Storage:** Schema v{db_schema_version} ({db_journal_mode.upper()} mode)",
        "",
        "#### Feature Modules",
    ]

    statuses = feature_statuses or {}
    if statuses:
        for mod, stat in sorted(statuses.items()):
            lines.append(f"- **{mod}**: `{stat}`")
    else:
        lines.append("- *(No feature status data provided)*")

    lines.extend(
        [
            "",
            "#### Sanitized Log Excerpt",
            "```text",
        ]
    )

    if recent_logs:
        for entry in recent_logs[-20:]:
            sanitized = redact_secrets(entry, secrets)
            lines.append(sanitized)
    else:
        lines.append("[No recent errors recorded]")

    lines.extend(
        [
            "```",
            "",
            "> *Note: All credentials, tokens, guild IDs, and user attachments are automatically redacted.*",
        ]
    )

    return "\n".join(lines)
