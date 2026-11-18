"""Unit tests for generic theming, accessible branding, text sanitization, and embed contracts."""

from __future__ import annotations

import pytest

from icbot.config import Settings
from icbot.embeds import (
    embed_to_text_snapshot,
    safe_allowed_mentions,
    sanitize_text,
)
from icbot.embeds import (
    game as game_embed,
)
from icbot.embeds import (
    go_alert as go_alert_embed,
)
from icbot.embeds import (
    job_alert as job_alert_embed,
)
from icbot.embeds import (
    stock as stock_embed,
)
from icbot.embeds import (
    system as system_embed,
)
from icbot.embeds import (
    ttc_alert as ttc_alert_embed,
)
from icbot.features.help import build_dynamic_help_categories, build_help_embed
from icbot.guild_config import GuildSettings, resolve_effective_settings
from icbot.theme import (
    DEFAULT_THEME_NAME,
    KNOWN_THEME_NAMES,
    calculate_luminance,
    get_theme_preset,
    is_accessible_accent,
    list_theme_presets,
)


@pytest.mark.unit
def test_theme_presets_coverage():
    """Verify all 5 required presets are defined with accessible accent colors."""
    expected = {"instigate_cafe", "campus_generic", "student_clean", "toronto_tmu", "emerald_academic"}
    assert expected.issubset(KNOWN_THEME_NAMES)

    presets = list_theme_presets()
    assert len(presets) >= 5

    for p in presets:
        assert p.name
        assert p.display_name
        assert p.footer_text
        assert p.stock_exchange_name
        assert p.currency_name
        assert p.currency_symbol
        assert p.primary_stock_symbol
        assert p.primary_stock_name
        assert p.source_attribution
        assert is_accessible_accent(p.accent_color), f"Preset {p.name} accent color is not accessible"


@pytest.mark.unit
def test_get_theme_preset_fallback():
    """Verify graceful fallback to default preset for unknown or empty names."""
    default = get_theme_preset(DEFAULT_THEME_NAME)
    assert default.name == "instigate_cafe"

    assert get_theme_preset(None).name == "instigate_cafe"
    assert get_theme_preset("").name == "instigate_cafe"
    assert get_theme_preset("non_existent_preset").name == "instigate_cafe"
    assert get_theme_preset("  CAMPUS_GENERIC  ").name == "campus_generic"


@pytest.mark.unit
def test_luminance_and_accessibility_checks():
    """Verify WCAG 2.1 relative luminance calculation and boundary thresholds."""
    assert calculate_luminance(0x000000) == pytest.approx(0.0, abs=1e-4)
    assert calculate_luminance(0xFFFFFF) == pytest.approx(1.0, abs=1e-4)

    # Pure black and pure white fail contrast against matching dark/light themes
    assert not is_accessible_accent(0x000000)
    assert not is_accessible_accent(0xFFFFFF)

    # Midtones pass
    assert is_accessible_accent(0xA64DFF)  # Instigate Purple
    assert is_accessible_accent(0x2563EB)  # Royal Blue
    assert is_accessible_accent(0x059669)  # Emerald Green


@pytest.mark.unit
def test_settings_from_env_loads_preset():
    """Verify loading from environment populates preset defaults for branding."""
    env = {
        "THEME_PRESET": "campus_generic",
    }
    s = Settings.from_env(environ=env)
    assert s.theme_preset == "campus_generic"
    assert s.bot_display_name == "Campus Assistant"
    assert s.stock_exchange_name == "Campus Stock Exchange"
    assert s.currency_name == "Campus Coins"
    assert s.primary_stock_symbol == "CAMP"
    assert s.embed_accent_color == 0x2563EB


@pytest.mark.unit
def test_guild_override_switches_theme_preset():
    """Verify per-guild theme_preset override applies preset defaults to effective settings."""
    base = Settings(
        theme_preset="instigate_cafe",
        bot_display_name="InstigateAI",
        stock_exchange_name="Instigate Cafe Stock Exchange",
    )
    guild_cfg = GuildSettings.from_overrides(
        guild_id=123456789012345678,
        overrides={"theme_preset": "student_clean"},
    )
    eff = resolve_effective_settings(base, guild_cfg)
    assert eff.theme_preset == "student_clean"
    assert eff.bot_display_name == "Student Bot"
    assert eff.currency_name == "Credits"
    assert eff.primary_stock_symbol == "STUD"
    assert eff.embed_accent_color == 0x0284C7


@pytest.mark.unit
def test_guild_override_custom_field_takes_precedence_over_preset():
    """Verify specific guild field override takes precedence over preset defaults."""
    base = Settings()
    guild_cfg = GuildSettings.from_overrides(
        guild_id=123456789012345678,
        overrides={
            "theme_preset": "student_clean",
            "bot_display_name": "Custom Student Union Bot",
            "currency_symbol": "💎",
        },
    )
    eff = resolve_effective_settings(base, guild_cfg)
    assert eff.theme_preset == "student_clean"
    assert eff.bot_display_name == "Custom Student Union Bot"
    assert eff.currency_symbol == "💎"
    assert eff.currency_name == "Credits"  # From student_clean preset


@pytest.mark.unit
def test_text_sanitization_and_allowed_mentions():
    """Verify @everyone, @here, and user/role mentions are neutralized."""
    raw = "Hey @everyone and @here! ping <@123456> role <@&789012>"
    sanitized = sanitize_text(raw)
    assert "@everyone" not in sanitized
    assert "@\u200beveryone" in sanitized
    assert "@here" not in sanitized
    assert "@\u200bhere" in sanitized
    assert "<@123456>" not in sanitized
    assert "<@\u200b123456>" in sanitized

    # AllowedMentions.none() prevents all outbound pings
    am = safe_allowed_mentions()
    assert am.everyone is False
    assert am.roles is False
    assert am.users is False


@pytest.mark.unit
def test_embed_limits_enforced():
    """Verify Discord limits (title <= 256, desc <= 4096, field val <= 1024, total <= 6000)."""
    s = Settings()
    e = system_embed(
        s,
        title="T" * 500,
        summary="D" * 5000,
        fields=[("F" * 400, "V" * 2000, False) for _ in range(30)],
    )
    assert len(e.title or "") <= 256
    assert len(e.description or "") <= 4096
    assert len(e.fields) <= 25
    for f in e.fields:
        assert len(f.name) <= 256
        assert len(f.value) <= 1024
    assert len(e) <= 6000


@pytest.mark.unit
def test_dynamic_help_categories_filtering():
    """Verify help categories filter disabled modules and adapt to permissions."""
    # 1. Full features enabled
    s_full = Settings(feature_profile="community_full")
    cats_full = build_dynamic_help_categories(None, s_full)
    cat_titles = [c["title"] for c in cats_full]
    assert "📚 Main Utilities" in cat_titles
    assert "🎓 Academic Toolkit" in cat_titles
    assert any("Stock Exchange" in t for t in cat_titles)
    assert "🎰 Casino Games" in cat_titles
    assert "ℹ️ Info & Updates" in cat_titles
    # Admin category omitted when interaction is None
    assert not any("Administration" in t for t in cat_titles)

    # 2. Disabled academics and casino
    s_core = Settings(disabled_features=["academics", "casino"])
    cats_core = build_dynamic_help_categories(None, s_core)
    cat_titles_core = [c["title"] for c in cats_core]
    assert "🎓 Academic Toolkit" not in cat_titles_core
    assert "🎰 Casino Games" not in cat_titles_core

    # 3. Dynamic help embed builds cleanly
    emb = build_help_embed(0, len(cats_full), s_full, categories=cats_full)
    assert emb.title
    assert s_full.bot_display_name in emb.title
    assert len(emb) <= 6000


@pytest.mark.unit
def test_embed_to_text_snapshot_all_builders(synthetic_settings: Settings):
    """Verify plain-text structural snapshot formatting across all embed builders."""
    # 1. Stock embed
    stk = stock_embed(
        synthetic_settings, title="Stock Quote", headline="$150 / share", reason="Strong earnings"
    )
    stk_snap = embed_to_text_snapshot(stk)
    assert "[EMBED]" in stk_snap
    assert "Title: Stock Quote" in stk_snap
    assert "Description: $150 / share" in stk_snap
    assert "Fields (1):" in stk_snap
    assert "Reason: Strong earnings" in stk_snap

    # 2. Game embed
    gm = game_embed(
        synthetic_settings, title="Blackjack Hand", summary="Dealer showing 10", controls="Hit | Stand"
    )
    gm_snap = embed_to_text_snapshot(gm)
    assert "Title: Blackjack Hand" in gm_snap
    assert "Controls: Hit | Stand" in gm_snap

    # 3. TTC Alert embed
    ttc = ttc_alert_embed(
        synthetic_settings, headline="Line 1 Subway Delay", details="Track maintenance at Bloor"
    )
    ttc_snap = embed_to_text_snapshot(ttc)
    assert "TTC Service Alert" in ttc_snap
    assert "Line 1 Subway Delay" in ttc_snap

    # 4. GO Alert embed
    go = go_alert_embed(synthetic_settings, headline="Lakeshore West Delay", lines=["LW"])
    go_snap = embed_to_text_snapshot(go)
    assert "GO Transit Service Alert" in go_snap
    assert "Lakeshore West Delay" in go_snap

    # 5. Job Alert embed
    jb = job_alert_embed(
        synthetic_settings,
        title="Software Engineer Intern",
        company="Stripe",
        location="Toronto, ON",
        url="https://example.invalid/jobs/1",
    )
    jb_snap = embed_to_text_snapshot(jb)
    assert "Software Engineer Intern" in jb_snap
    assert "🏢 Company: **Stripe**" in jb_snap
