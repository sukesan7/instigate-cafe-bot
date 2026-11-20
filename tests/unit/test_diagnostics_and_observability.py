"""Unit tests for structured logging, secret redaction, diagnostics, and maintainer ergonomics."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import app_commands

from icbot.bot import CampusBot, on_app_command_error
from icbot.config import Settings
from icbot.db import Database
from icbot.errors import UserInputError
from icbot.features.info import status_cmd
from icbot.logging import (
    BOT_VERSION,
    CorrelationContext,
    ProviderOutageTracker,
    RedactingLogFilter,
    StructuredLogFormatter,
    configure_logging,
    log_event,
)

pytestmark = pytest.mark.unit


def _synthetic_discord_token() -> str:
    return "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM" + "." + "AbCdEf" + "." + "1234567890abcdef1234567890abcd"


def _synthetic_gemini_key() -> str:
    return "AIzaSy" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"


# ---------------------------------------------------------------------------
# 1. RedactingLogFilter and Secret Scrubbing
# ---------------------------------------------------------------------------


def test_redacting_log_filter_scrubs_secrets_and_retains_correlation_id():
    """RedactingLogFilter scrubs known API keys and tokens from message and record args."""
    secret_token = _synthetic_discord_token()
    secret_gemini = _synthetic_gemini_key()
    secrets = (secret_token, secret_gemini)

    log_filter = RedactingLogFilter(secrets=secrets)

    with CorrelationContext("test-trace-123"):
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=42,
            msg=f"Failed request with token {secret_token} and key {secret_gemini}",
            args=(),
            exc_info=None,
        )
        assert log_filter.filter(record) is True
        assert secret_token not in record.msg
        assert secret_gemini not in record.msg
        assert "[REDACTED]" in record.msg
        assert record.correlation_id == "test-trace-123"


def test_redacting_log_filter_scrubs_tuple_and_dict_args():
    """RedactingLogFilter scrubs credentials inside tuple and dict args."""
    secret_gemini = _synthetic_gemini_key()
    log_filter = RedactingLogFilter(secrets=(secret_gemini,))

    # Tuple args
    rec_tuple = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Provider error %s",
        args=(f"https://generativelanguage.googleapis.com/v1beta?key={secret_gemini}",),
        exc_info=None,
    )
    log_filter.filter(rec_tuple)
    assert secret_gemini not in str(rec_tuple.args)
    assert "[REDACTED]" in str(rec_tuple.args)

    # Dict args
    rec_dict = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Headers %(auth)s",
        args=({"auth": f"Bearer {secret_gemini}"},),
        exc_info=None,
    )
    log_filter.filter(rec_dict)
    assert secret_gemini not in str(rec_dict.args)
    assert "[REDACTED]" in str(rec_dict.args)


# ---------------------------------------------------------------------------
# 2. StructuredLogFormatter and Event Logging
# ---------------------------------------------------------------------------


def test_structured_log_formatter_prepends_metadata():
    """StructuredLogFormatter formats tags: cid, event, provider, guild."""
    formatter = StructuredLogFormatter("%(levelname)s: %(message)s")

    record = logging.LogRecord(
        name="icbot.tasks",
        level=logging.INFO,
        pathname=__file__,
        lineno=50,
        msg="Poll completed successfully",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "abc12345"
    record.event_name = "poll_success"
    record.provider = "ttc_alerts"
    record.guild_id = 900000000000000001

    formatted = formatter.format(record)
    assert "[cid=abc12345 event=poll_success provider=ttc_alerts guild=900000000000000001]" in formatted
    assert "INFO: Poll completed successfully" in formatted


def test_log_event_helper_records_attributes_consistently():
    """log_event emits record with standard metadata extra attributes."""
    logger = logging.getLogger("test.event")
    recorded: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recorded.append(record)

    h = Handler()
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)

    try:
        log_event(
            logger,
            logging.WARNING,
            "provider_rate_limit",
            "Rate limit triggered",
            guild_id=12345,
            provider="gemini",
            correlation_id="corr-99",
            latency_ms=125.4,
            retry_count=2,
            outcome="retried",
            error_type="GeminiRateLimitError",
        )

        assert len(recorded) == 1
        r = recorded[0]
        assert r.levelno == logging.WARNING
        assert r.event_name == "provider_rate_limit"
        assert r.correlation_id == "corr-99"
        assert r.provider == "gemini"
        assert r.guild_id == 12345
        assert r.latency_ms == 125.4
        assert r.retry_count == 2
        assert r.outcome == "retried"
        assert r.error_type == "GeminiRateLimitError"
        assert "outcome=retried" in r.getMessage()
        assert "latency=125.4ms" in r.getMessage()
        assert "retries=2" in r.getMessage()
    finally:
        logger.removeHandler(h)


# ---------------------------------------------------------------------------
# 3. ProviderOutageTracker Dampening
# ---------------------------------------------------------------------------


def test_provider_outage_tracker_dampens_spam_and_logs_recovery():
    """Initial outage is WARNING, consecutive repeats within interval are DEBUG, recovery is INFO."""
    logger = logging.getLogger("test.outage")
    events: list[tuple[int, str]] = []

    class CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append((record.levelno, getattr(record, "event_name", "")))

    h = CapturingHandler()
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)

    try:
        tracker = ProviderOutageTracker(logger, repeat_alert_interval_seconds=60.0)

        # 1. First failure -> WARNING (outage started)
        tracker.record_failure("ttc_transit", "HTTP 503", now=100.0, correlation_id="c1")
        assert len(events) == 1
        assert events[-1] == (logging.WARNING, "provider_outage_started")
        assert tracker.is_degraded("ttc_transit") is True

        # 2. Second failure immediately after (105s < 100+60) -> DEBUG (throttled dampening)
        tracker.record_failure("ttc_transit", "HTTP 503", now=105.0, correlation_id="c2")
        assert len(events) == 2
        assert events[-1] == (logging.DEBUG, "provider_outage_dampened")

        # 3. Third failure after interval elapsed (170s >= 100+60) -> WARNING (ongoing outage)
        tracker.record_failure("ttc_transit", "HTTP 503", now=170.0, correlation_id="c3")
        assert len(events) == 3
        assert events[-1] == (logging.WARNING, "provider_outage_ongoing")

        # 4. Recovery -> INFO (recovered)
        recovered = tracker.record_success("ttc_transit", correlation_id="c4")
        assert recovered is True
        assert len(events) == 4
        assert events[-1] == (logging.INFO, "provider_recovered")
        assert tracker.is_degraded("ttc_transit") is False

        # 5. Subsequent success without prior failure returns False and emits no log
        assert tracker.record_success("ttc_transit") is False
        assert len(events) == 4
    finally:
        logger.removeHandler(h)


# ---------------------------------------------------------------------------
# 4. configure_logging and Log Rotation
# ---------------------------------------------------------------------------


def test_configure_logging_rotating_file_handler(tmp_path: Path):
    """configure_logging sets up rotating file handler and scrubs secrets."""
    log_file = tmp_path / "bot_test.log"
    token = _synthetic_discord_token()

    configure_logging(
        level=logging.INFO,
        log_file=log_file,
        max_bytes=1024,
        backup_count=2,
        secrets=(token,),
    )

    test_log = logging.getLogger("test.rotation")
    test_log.info("Starting run with token %s", token)

    # Write enough logs to trigger rotation once
    chunk = "A" * 400
    for i in range(3):
        test_log.info("Message %d: %s", i, chunk)

    assert log_file.exists()
    rotated_file = tmp_path / "bot_test.log.1"
    assert rotated_file.exists()
    all_content = log_file.read_text(encoding="utf-8") + rotated_file.read_text(encoding="utf-8")
    assert token not in all_content
    assert "[REDACTED]" in all_content

    # Clean up handlers on root logger
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


# ---------------------------------------------------------------------------
# 5. /ic status Diagnostics & Channel Permission Audit
# ---------------------------------------------------------------------------


async def test_ic_status_channel_permissions_audit(make_interaction, make_member):
    """The /ic status command audits channel existence and bot permissions."""
    guild_id = 900000000000000001
    admin_id = 800000000000000001
    admin_member = make_member(user_id=admin_id, administrator=True)

    # Construct channels: ch1 healthy, ch2 missing embed_links
    channel_ok = SimpleNamespace(
        id=101,
        name="job-alerts",
        mention="<#101>",
        permissions_for=lambda member: SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True
        ),
    )
    channel_restricted = SimpleNamespace(
        id=102,
        name="transit-alerts",
        mention="<#102>",
        permissions_for=lambda member: SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=False
        ),
    )

    fake_guild = SimpleNamespace(
        id=guild_id,
        owner_id=admin_id,
        name="Audit Server",
        me=SimpleNamespace(id=777),
        get_channel=lambda ch_id: (
            channel_ok if ch_id == 101 else (channel_restricted if ch_id == 102 else None)
        ),
    )

    settings = Settings(
        guild_id=guild_id,
        job_alerts_channel_id=101,
        ttc_alerts_channel_id=102,
        go_alerts_channel_id=999,  # missing channel ID
    )

    inter = make_interaction(user=admin_member, guild=fake_guild, settings=settings)
    await status_cmd.callback(inter)

    assert len(inter.response.sent_messages) == 1
    reply = inter.response.sent_messages[0]
    embed_str = str(reply.embed.to_dict())

    # Bot version present
    assert BOT_VERSION in embed_str
    # Channel warnings present for missing channel and restricted channel
    assert "Channel Permission Warnings" in embed_str
    assert "Channel ID `999` not found" in embed_str
    assert "Missing Embed Links" in embed_str


async def test_ic_status_sanitized_issue_report_export(make_interaction, make_member):
    """The /ic status command exports a sanitized markdown report when issue_report=True."""
    guild_id = 900000000000000001
    admin_id = 800000000000000001
    admin_member = make_member(user_id=admin_id, administrator=True)
    fake_guild = SimpleNamespace(id=guild_id, owner_id=admin_id, name="Report Server")

    settings = Settings(guild_id=guild_id)
    inter = make_interaction(user=admin_member, guild=fake_guild, settings=settings)

    await status_cmd.callback(inter, issue_report=True)

    assert len(inter.response.sent_messages) == 1
    reply = inter.response.sent_messages[0]
    assert reply.file is not None
    assert reply.file.filename == "status_issue_report.md"

    content = reply.file.fp.read().decode("utf-8")
    assert "Maintainer Diagnostic Report" in content
    assert BOT_VERSION in content
    assert "Feature Modules" in content


# ---------------------------------------------------------------------------
# 6. on_app_command_error Error Differentiation and Interaction Resilience
# ---------------------------------------------------------------------------


async def test_on_app_command_error_differentiates_domain_and_unexpected_errors():
    """on_app_command_error differentiates BotError (warning) from unexpected (error)."""
    settings = Settings(token=_synthetic_discord_token(), gemini_api_key=_synthetic_gemini_key())
    client = SimpleNamespace(settings=settings)

    # 1. Domain error before response is done -> send_message
    interaction_1 = SimpleNamespace(
        client=client,
        guild_id=123,
        command=SimpleNamespace(qualified_name="ic casino blackjack"),
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    domain_err = app_commands.AppCommandError("wrapper")
    domain_err.original = UserInputError("Insufficient bet amount")

    await on_app_command_error(interaction_1, domain_err)
    interaction_1.response.send_message.assert_awaited_once()
    sent_text = interaction_1.response.send_message.call_args[0][0]
    assert "Insufficient bet amount" in sent_text
    assert interaction_1.followup.send.await_count == 0

    # 2. Unexpected error after response was deferred -> followup.send
    interaction_2 = SimpleNamespace(
        client=client,
        guild_id=123,
        command=SimpleNamespace(qualified_name="ic jobs"),
        response=SimpleNamespace(
            is_done=lambda: True,  # deferred
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    unexpected_err = app_commands.AppCommandError("Database lock timeout")

    await on_app_command_error(interaction_2, unexpected_err)
    interaction_2.response.send_message.assert_not_awaited()
    interaction_2.followup.send.assert_awaited_once()
    followup_text = interaction_2.followup.send.call_args[0][0]
    assert "An unexpected error occurred" in followup_text


# ---------------------------------------------------------------------------
# 7. CampusBot Runtime Cleanup and Lifecycle
# ---------------------------------------------------------------------------


async def test_campus_bot_cleanup_runtime_cleanly_terminates_resources(tmp_db: Database):
    """_cleanup_runtime cancels supervisor tasks, closes HTTP sessions, and closes DB."""
    settings = Settings(allow_gateway=True, db_path=tmp_db.path)
    mock_session = SimpleNamespace(closed=False, close=AsyncMock())

    bot = CampusBot(
        settings=settings,
        db=tmp_db,
        http_session=mock_session,  # type: ignore[arg-type]
        register_commands=False,
        sync_commands=False,
    )
    bot.services.owns_http_session = True
    bot.services.http_session = mock_session  # type: ignore[assignment]

    # Mock supervisor
    bot.supervisor.stop_all = AsyncMock()

    # Trigger cleanup
    await bot._cleanup_runtime()

    bot.supervisor.stop_all.assert_awaited_once()
    mock_session.close.assert_awaited_once()
    assert bot.setup_complete is False


async def test_campus_bot_on_ready_is_idempotent():
    """on_ready increments ready_count without re-registering or re-syncing commands."""
    settings = Settings(allow_gateway=True)
    bot = CampusBot(
        settings=settings,
        register_commands=False,
        sync_commands=False,
    )
    bot._user = SimpleNamespace(id=12345, name="InstigateBot")  # type: ignore[assignment]

    await bot.on_ready()
    assert bot.ready_count == 1

    await bot.on_ready()
    assert bot.ready_count == 2
