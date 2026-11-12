"""Unit tests for typed sub-configs, strict validation, capabilities, and deprecated aliases."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from icbot.config import (
    MAX_SNOWFLAKE,
    POLL_INTERVAL_BOUNDS,
    AcademicsConfig,
    CapabilityStatus,
    CareersConfig,
    DiscordConfig,
    EconomyConfig,
    FinancialAlertsConfig,
    GeminiConfig,
    SchedulerConfig,
    Settings,
    SportsConfig,
    StorageConfig,
    TransitConfig,
    load_settings,
)
from icbot.errors import ConfigValidationError, MisconfigurationError

pytestmark = pytest.mark.unit


def _make_fake_gemini_secret() -> str:
    # Constructed dynamically so tools/scan_secrets.py never sees a literal token prefix+body
    return "synth-gem-" + "secret-value-12345"


def _make_fake_bot_token() -> str:
    return "synth-discord-" + "token-value-98765"


def test_from_components_round_trips_all_nine_subconfigs():
    discord_cfg = DiscordConfig(
        token=_make_fake_bot_token(),
        guild_id=900000000000000001,
        stock_channel_id=900000000000000010,
        user_command_channel_id=900000000000000020,
        allowed_role_ids=(900000000000000099,),
        embed_logo_url="https://example.invalid/logo.png",
        embed_accent_color=0x112233,
        embed_footer_text="Cafe Custom",
        force_guild_resync=True,
        allow_gateway=False,
    )
    storage_cfg = StorageConfig(
        db_path="custom_stock.db",
        grades_db_path="custom_grades.db",
        lore_path="tests/fixtures/synthetic_lore.json",
    )
    academics_cfg = AcademicsConfig(
        exams_tz="America/Vancouver",
        notes_sites=("Notes|https://example.invalid/notes",),
        rmp_university="Test University",
    )
    careers_cfg = CareersConfig(
        job_alerts_channel_id=900000000000000040,
        jobs_poll_seconds=300,
        greenhouse_boards=("acme",),
        lever_sites=("globex",),
        jobs_location_allowlist=("Toronto",),
        jobs_keywords_allowlist=("Python",),
        jobs_keywords_denylist=("Senior",),
        jobs_bootstrap_post=True,
        jobs_require_early_career=True,
        workday_sites=("Initech|https://example.invalid/jobs",),
        workday_poll_seconds=450,
        workday_timeout_seconds=15,
    )
    gemini_cfg = GeminiConfig(
        llm_enabled=True,
        llm_cooldown_seconds=5,
        llm_max_messages=30,
        llm_max_chars=8000,
        llm_max_image_bytes=1_000_000,
        llm_max_output_tokens=1024,
        llm_debug=True,
        gemini_api_key=_make_fake_gemini_secret(),
        gemini_model="gemini-2.5-flash",
        gemini_resume_model="gemini-2.5-pro",
        gemini_resume_prompt="SWE base",
        gemini_resume_prompt_hw="HW base",
        gemini_resume_prompt_ele="ELE base",
    )
    economy_cfg = EconomyConfig(
        daily_claim_amount=250,
        bj_starting_bankroll=2000,
        bj_dealer_hits_soft_17=False,
        bj_max_splits=2,
        bj_session_ttl_minutes=15,
    )
    transit_cfg = TransitConfig(
        ttc_alerts_channel_id=900000000000000030,
        ttc_mastodon_base="https://example.invalid",
        ttc_mastodon_acct="ttcalerts",
        ttc_poll_seconds=90,
        ttc_alert_level="all",
        ttc_bootstrap_post_latest=True,
        ttc_include_replies=False,
        ttc_include_reblogs=True,
        ttc_debug=True,
        go_api_base="https://example.invalid/go/v1",
        go_api_key="synth-go-key",
        go_alerts_channel_id=900000000000000050,
        go_poll_seconds=120,
        go_alert_level="all",
        go_bootstrap_post_latest=False,
        go_include_buses=True,
        go_lines_allowlist=("LW", "KI"),
        go_ssl_verify=False,
        go_ca_bundle_path="/tmp/ca.pem",
        go_debug=True,
    )
    financial_cfg = FinancialAlertsConfig(
        market_watch_enabled=True,
        market_watch_poll_seconds=120,
        market_watch_channel_id=900000000000000060,
        market_watch_vix_level=22.5,
        market_watch_index_move_pct=1.75,
        market_watch_stock_move_pct=8.5,
        market_watch_tickers=("SPY", "NVDA"),
        market_watch_cooldown_minutes=20,
        market_watch_debug=True,
        market_daily_report_enabled=True,
        market_daily_report_channel_id=900000000000000061,
        market_daily_report_time="15:45",
        market_daily_report_tz="America/New_York",
        market_daily_report_top_n=6,
        market_daily_report_tickers=("AAPL", "MSFT"),
        market_daily_report_debug=True,
        btc_watch_enabled=True,
        btc_watch_poll_seconds=180,
        btc_watch_channel_id=900000000000000062,
        btc_watch_label="Treasury Watch",
        btc_watch_addresses=("bc1qsynthetic000000000000000000000",),
        btc_esplora_base_url="https://example.invalid/esplora",
        btc_watch_cooldown_minutes=45,
        btc_watch_debug=True,
    )
    sports_cfg = SportsConfig(
        odds_api_key="synth-odds-key",
        sports_rss_nba="https://example.invalid/nba.rss",
        sports_rss_nfl="https://example.invalid/nfl.rss",
        sports_rss_mma="https://example.invalid/mma.rss",
        sports_rss_ufc="https://example.invalid/ufc.rss",
        sports_rss_boxing="https://example.invalid/boxing.rss",
    )
    scheduler_cfg = SchedulerConfig(
        task_startup_jitter_seconds=7.5,
        task_max_backoff_seconds=900.0,
    )

    settings = Settings.from_components(
        discord=discord_cfg,
        storage=storage_cfg,
        academics=academics_cfg,
        careers=careers_cfg,
        gemini=gemini_cfg,
        economy=economy_cfg,
        transit=transit_cfg,
        financial=financial_cfg,
        sports=sports_cfg,
        scheduler=scheduler_cfg,
    )

    assert settings.discord == discord_cfg
    assert settings.storage == storage_cfg
    assert settings.academics == academics_cfg
    assert settings.careers == careers_cfg
    assert settings.gemini == gemini_cfg
    assert settings.economy == economy_cfg
    assert settings.transit == transit_cfg
    assert settings.financial == financial_cfg
    assert settings.sports == sports_cfg
    assert settings.scheduler == scheduler_cfg
    assert settings.sports_rss_urls("NBA") == ["https://example.invalid/nba.rss"]
    assert settings.effective_workday_poll_seconds == 450


def test_load_validated_accepts_empty_dev_environment():
    settings = Settings.load_validated(environ={})
    assert isinstance(settings, Settings)
    report = settings.validate(strict=False)
    assert report.is_valid is True
    assert report.errors == ()

    loaded = load_settings(strict=True, environ={})
    assert loaded == settings


@pytest.mark.parametrize(
    ("env", "expected_field"),
    [
        ({"GUILD_ID": "-1"}, "guild_id"),
        ({"GUILD_ID": str(MAX_SNOWFLAKE + 1)}, "guild_id"),
        ({"GUILD_ID": "not_an_int"}, "guild_id"),
        ({"STOCK_CHANNEL_ID": "abc"}, "stock_channel_id"),
        ({"ALLOWED_ROLE_IDS": "0"}, "allowed_role_ids"),
        ({"ALLOWED_ROLE_IDS": "bad-role"}, "allowed_role_ids"),
        ({"LLM_ENABLED": "definitely"}, "llm_enabled"),
        ({"EMBED_ACCENT_COLOR": "not-a-color"}, "embed_accent_color"),
        ({"EMBED_LOGO_URL": "ftp://example.invalid/logo.png"}, "embed_logo_url"),
        ({"GO_API_BASE": "not a valid url"}, "go_api_base"),
        ({"TTC_MASTODON_BASE": "/relative/path"}, "ttc_mastodon_base"),
        ({"BTC_ESPLORA_BASE_URL": "javascript:alert(1)"}, "btc_esplora_base_url"),
        ({"SPORTS_RSS_NBA": "not-a-url"}, "sports_rss_nba"),
        ({"NOTES_SITES": "MissingPipe"}, "notes_sites"),
        ({"NOTES_SITES": "Course|ftp://invalid.example"}, "notes_sites"),
        ({"WORKDAY_SITES": "NoPipe"}, "workday_sites"),
        ({"WORKDAY_SITES": "Acme|ftp://invalid.example"}, "workday_sites"),
        ({"TTC_ALERT_LEVEL": "verbose"}, "ttc_alert_level"),
        ({"GO_ALERT_LEVEL": "emergency"}, "go_alert_level"),
        ({"EXAMS_TZ": "Invalid/Timezone"}, "exams_tz"),
        ({"MARKET_DAILY_REPORT_TZ": "Mars/Phobos"}, "market_daily_report_tz"),
        ({"MARKET_DAILY_REPORT_TIME": "25:99"}, "market_daily_report_time"),
        ({"MARKET_DAILY_REPORT_TIME": "1630"}, "market_daily_report_time"),
        ({"JOBS_POLL_SECONDS": "-5"}, "jobs_poll_seconds"),
        ({"JOBS_POLL_SECONDS": "30"}, "jobs_poll_seconds"),
        ({"WORKDAY_POLL_SECONDS": "30"}, "workday_poll_seconds"),
        ({"TTC_POLL_SECONDS": "29"}, "ttc_poll_seconds"),
        ({"TTC_POLL_SECONDS": "86401"}, "ttc_poll_seconds"),
        ({"GO_POLL_SECONDS": "59"}, "go_poll_seconds"),
        ({"MARKET_WATCH_POLL_SECONDS": "9"}, "market_watch_poll_seconds"),
        ({"BTC_WATCH_POLL_SECONDS": "29"}, "btc_watch_poll_seconds"),
        ({"TASK_STARTUP_JITTER_SECONDS": "-1"}, "task_startup_jitter_seconds"),
        ({"TASK_STARTUP_JITTER_SECONDS": "301"}, "task_startup_jitter_seconds"),
        ({"TASK_MAX_BACKOFF_SECONDS": "29"}, "task_max_backoff_seconds"),
        ({"TASK_MAX_BACKOFF_SECONDS": "86401"}, "task_max_backoff_seconds"),
        ({"WORKDAY_TIMEOUT_SECONDS": "0"}, "workday_timeout_seconds"),
        ({"LLM_COOLDOWN_SECONDS": "-1"}, "llm_cooldown_seconds"),
        ({"LLM_MAX_MESSAGES": "0"}, "llm_max_messages"),
        ({"LLM_MAX_CHARS": "10"}, "llm_max_chars"),
        ({"LLM_MAX_IMAGE_BYTES": "-10"}, "llm_max_image_bytes"),
        ({"LLM_MAX_OUTPUT_TOKENS": "0"}, "llm_max_output_tokens"),
        ({"DAILY_CLAIM_AMOUNT": "-1"}, "daily_claim_amount"),
        ({"BJ_STARTING_BANKROLL": "0"}, "bj_starting_bankroll"),
        ({"BJ_MAX_SPLITS": "-1"}, "bj_max_splits"),
        ({"BJ_SESSION_TTL_MINUTES": "0"}, "bj_session_ttl_minutes"),
        ({"MARKET_WATCH_VIX_LEVEL": "0"}, "market_watch_vix_level"),
        ({"MARKET_WATCH_INDEX_MOVE_PCT": "nan"}, "market_watch_index_move_pct"),
        ({"MARKET_WATCH_STOCK_MOVE_PCT": "-2.5"}, "market_watch_stock_move_pct"),
        ({"MARKET_WATCH_COOLDOWN_MINUTES": "-1"}, "market_watch_cooldown_minutes"),
        ({"MARKET_DAILY_REPORT_TOP_N": "0"}, "market_daily_report_top_n"),
        ({"BTC_WATCH_COOLDOWN_MINUTES": "-1"}, "btc_watch_cooldown_minutes"),
    ],
)
def test_strict_env_validation_rejects_malformed_values_and_never_leaks_secrets(
    env: dict[str, str], expected_field: str
):
    secret_env = {
        "DISCORD_TOKEN": _make_fake_bot_token(),
        "GEMINI_API_KEY": _make_fake_gemini_secret(),
        "GO_API_KEY": "synth-go-" + "key-77777",
        "ODDS_API_KEY": "synth-odds-" + "key-88888",
        **env,
    }
    with pytest.raises(ConfigValidationError) as exc_info:
        Settings.from_env(strict=True, environ=secret_env)

    assert isinstance(exc_info.value, MisconfigurationError)
    msg = str(exc_info.value)
    assert expected_field in msg
    for sec in (
        secret_env["DISCORD_TOKEN"],
        secret_env["GEMINI_API_KEY"],
        secret_env["GO_API_KEY"],
        secret_env["ODDS_API_KEY"],
    ):
        assert sec not in msg


@pytest.mark.parametrize(
    ("kwargs", "expected_field"),
    [
        ({"embed_accent_color": 0x1000000}, "embed_accent_color"),
        ({"embed_accent_color": True}, "embed_accent_color"),
        ({"db_path": ""}, "db_path"),
        ({"grades_db_path": "   "}, "grades_db_path"),
        ({"go_api_base": "", "go_alerts_channel_id": 900000000000000050, "go_api_key": "k"}, "go_api_base"),
    ],
)
def test_settings_validate_strict_catches_direct_constructor_invariants(
    kwargs: dict[str, Any], expected_field: str
):
    settings = Settings(**kwargs)
    with pytest.raises(ConfigValidationError, match=expected_field):
        settings.validate(strict=True)


def test_deprecated_env_vars_and_properties_emit_deprecation_warnings():
    with pytest.warns(DeprecationWarning, match="LLM_MODEL"):
        s_llm = Settings.from_env(strict=True, environ={"LLM_MODEL": "gemini-2.5-pro"})
    assert s_llm.gemini_model == "gemini-2.5-pro"

    with pytest.warns(DeprecationWarning, match="TTC_CHANNEL_ID"):
        s_ttc = Settings.from_env(strict=True, environ={"TTC_CHANNEL_ID": "900000000000000031"})
    assert s_ttc.ttc_alerts_channel_id == 900000000000000031

    with pytest.warns(DeprecationWarning, match="JOBS_CHANNEL_ID"):
        s_jobs = Settings.from_env(strict=True, environ={"JOBS_CHANNEL_ID": "900000000000000041"})
    assert s_jobs.job_alerts_channel_id == 900000000000000041

    with pytest.warns(DeprecationWarning, match="ALPHAVANTAGE_API_KEY"):
        Settings.from_env(strict=True, environ={"ALPHAVANTAGE_API_KEY": "legacy-ignored"})

    # Canonical names win cleanly and emit no warning
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        canonical = Settings.from_env(
            strict=True,
            environ={
                "GEMINI_MODEL": "gemini-2.5-flash",
                "LLM_MODEL": "legacy-model",
                "TTC_ALERTS_CHANNEL_ID": "900000000000000032",
                "TTC_CHANNEL_ID": "900000000000000031",
                "JOB_ALERTS_CHANNEL_ID": "900000000000000042",
                "JOBS_CHANNEL_ID": "900000000000000041",
            },
        )
    assert caught == []
    assert canonical.gemini_model == "gemini-2.5-flash"
    assert canonical.ttc_alerts_channel_id == 900000000000000032
    assert canonical.job_alerts_channel_id == 900000000000000042

    s_props = Settings(
        greenhouse_boards=["acme"],
        lever_sites=["globex"],
        job_alerts_channel_id=900000000000000042,
    )
    with pytest.warns(DeprecationWarning, match="greenhouse_board_tokens"):
        assert s_props.greenhouse_board_tokens == ["acme"]
    with pytest.warns(DeprecationWarning, match="lever_site_tokens"):
        assert s_props.lever_site_tokens == ["globex"]
    with pytest.warns(DeprecationWarning, match="jobs_channel_id"):
        assert s_props.jobs_channel_id == 900000000000000042


def test_capabilities_report_degradation_and_warnings_without_leaking_secrets():
    token = _make_fake_bot_token()
    gemini_key = _make_fake_gemini_secret()
    partial = Settings(
        token=token,
        allow_gateway=False,
        llm_enabled=True,
        gemini_api_key="",
        odds_api_key="",
        ttc_alerts_channel_id=900000000000000030,
        market_watch_enabled=True,
        btc_watch_enabled=True,
        btc_watch_channel_id=900000000000000060,
    )

    report = partial.validate(strict=False)
    assert report.is_valid is True
    warning_fields = {w.field for w in report.warnings}
    assert {"llm_assistant", "ttc_alerts", "market_watch", "btc_watch"}.issubset(warning_fields)

    caps = partial.capabilities()
    assert isinstance(caps["discord_gateway"], CapabilityStatus)
    assert caps["discord_gateway"].configured is True
    assert caps["discord_gateway"].available is False
    assert "opt-in disabled" in caps["discord_gateway"].reason
    assert caps["odds_api"].configured is False
    assert caps["odds_api"].available is False
    assert "RSS" in caps["odds_api"].reason

    ready = Settings(
        token=token,
        allow_gateway=True,
        llm_enabled=True,
        gemini_api_key=gemini_key,
        odds_api_key="synth-odds-key-111",
    )
    ready_caps = ready.capabilities()
    assert ready_caps["discord_gateway"].available is True
    assert ready_caps["llm_assistant"].available is True
    assert ready_caps["resume_review"].available is True
    assert ready_caps["odds_api"].available is True

    # Ensure zero secrets appear in any CapabilityStatus text
    rendered = repr(caps) + repr(ready_caps)
    assert token not in rendered
    assert gemini_key not in rendered
    assert "synth-odds-key-111" not in rendered


def test_poll_interval_bounds_and_scheduler_zero_opt_out():
    zero_ok = Settings.from_env(
        strict=True,
        environ={
            "JOBS_POLL_SECONDS": "0",
            "WORKDAY_POLL_SECONDS": "0",
            "TASK_STARTUP_JITTER_SECONDS": "0",
            "TASK_MAX_BACKOFF_SECONDS": "120",
        },
    )
    assert zero_ok.jobs_poll_seconds == 0
    assert zero_ok.workday_poll_seconds == 0
    assert zero_ok.task_startup_jitter_seconds == 0.0
    assert zero_ok.task_max_backoff_seconds == 120.0

    assert POLL_INTERVAL_BOUNDS["jobs_poll_seconds"].clamp(0) == 0
    assert POLL_INTERVAL_BOUNDS["jobs_poll_seconds"].clamp(10) == 60
    assert POLL_INTERVAL_BOUNDS["workday_poll_seconds"].clamp(0, inherited=300) == 300
    assert POLL_INTERVAL_BOUNDS["ttc_poll_seconds"].clamp(5) == 30
    assert POLL_INTERVAL_BOUNDS["ttc_poll_seconds"].clamp(200000) == 86400
