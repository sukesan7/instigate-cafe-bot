"""Unit tests for icbot.config.Settings parsing and .env.example hygiene."""

from __future__ import annotations

from pathlib import Path

import pytest

from icbot.config import MAX_SNOWFLAKE, Settings, _to_bool, _to_color, _to_int, _to_str_list

pytestmark = pytest.mark.unit


def test_settings_from_empty_env_is_safe():
    settings = Settings.from_env()
    assert settings.token == ""
    assert settings.guild_id == 0
    assert settings.guild_id_invalid is False
    assert settings.llm_enabled is False
    assert settings.market_watch_enabled is False
    assert settings.btc_watch_enabled is False
    assert settings.embed_accent_color == 0xA64DFF
    assert settings.embed_footer_text == "Instigate Cafe"
    assert settings.workday_poll_seconds == settings.jobs_poll_seconds


def test_snowflake_clamping_flags_invalid_guild_id(monkeypatch: pytest.MonkeyPatch):
    too_big = str(MAX_SNOWFLAKE + 100)
    monkeypatch.setenv("GUILD_ID", too_big)
    monkeypatch.setenv("STOCK_CHANNEL_ID", too_big)

    settings = Settings.from_env()
    assert settings.guild_id == 0
    assert settings.guild_id_invalid is True
    assert settings.stock_channel_id == 0

    monkeypatch.setenv("GUILD_ID", "900000000000000001")
    valid = Settings.from_env()
    assert valid.guild_id == 900000000000000001
    assert valid.guild_id_invalid is False


def test_workday_poll_seconds_override_and_role_parsing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOBS_POLL_SECONDS", "900")
    monkeypatch.setenv("WORKDAY_POLL_SECONDS", "300")
    monkeypatch.setenv("ALLOWED_ROLE_IDS", "111, \\, 222, not_an_int, 111")

    settings = Settings.from_env()
    assert settings.workday_poll_seconds == 300
    assert settings.allowed_role_ids == [111, 222]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#A64DFF", 0xA64DFF),
        ("0xff1122", 0xFF1122),
        ("112233", 112233),
        ("00ff00", 0x00FF00),
        (0x123456, 0x123456),
        ("invalid", 0xA64DFF),
        ("", 0xA64DFF),
    ],
)
def test_to_color_formats(raw, expected):
    assert _to_color(raw, 0xA64DFF) == expected


def test_scalar_and_list_helpers():
    assert _to_bool("TRUE") is True
    assert _to_bool("0") is False
    assert _to_bool("maybe", default=True) is True
    assert _to_int(" 42 ") == 42
    assert _to_int("abc", default=7) == 7
    assert _to_str_list("SPY, \\, QQQ, spy, \\\\, NVDA") == ["SPY", "QQQ", "NVDA"]


def test_env_example_has_empty_secrets_and_disabled_pollers():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    raw = env_example.read_text(encoding="utf-8")
    kv = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        kv[k.strip()] = v.strip()

    assert kv["DISCORD_TOKEN"] == ""
    assert kv["GEMINI_API_KEY"] == ""
    assert kv["GO_API_KEY"] == ""
    assert kv["ODDS_API_KEY"] == ""
    assert kv["GUILD_ID"] == "0"
    assert kv["LLM_ENABLED"] == "false"
    assert kv["MARKET_WATCH_ENABLED"] == "false"
    assert kv["BTC_WATCH_ENABLED"] == "false"
