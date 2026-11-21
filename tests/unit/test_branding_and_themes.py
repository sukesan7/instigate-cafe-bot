"""Unit tests for generic theming, accessible branding, text sanitization, and embed contracts."""

from __future__ import annotations

import pytest

from icbot.config import Settings
from icbot.embeds import (
    EmbedFactory,
    alert_role_allowed_mentions,
    embed_to_text_snapshot,
    format_footer,
    format_safe_link,
    paginated_embed,
    resolve_embed_color,
    safe_allowed_mentions,
    safe_http_url,
    sanitize_text,
    sanitize_untrusted_text,
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


@pytest.mark.unit
def test_visual_contract_across_multiple_themes_semantics_and_restrained_footers():
    """Verify deterministic snapshots across instigate_cafe, toronto_tmu, and emerald_academic themes."""
    s_default = Settings.from_env(environ={"THEME_PRESET": "instigate_cafe"})
    s_tmu = Settings.from_env(environ={"THEME_PRESET": "toronto_tmu"})
    s_emerald = Settings.from_env(environ={"THEME_PRESET": "emerald_academic"})

    # 1. Normal accent honors active theme without forcing purple onto other presets
    f_default = EmbedFactory(s_default)
    f_tmu = EmbedFactory(s_tmu)
    f_emerald = EmbedFactory(s_emerald)

    e_def = f_default.paginated(
        title="Campus Job Board",
        description="Summary before detail fields.",
        fields=[
            ("Role 1", "Details 1", False),
            ("Role 2", "Details 2", False),
            ("Role 3", "Details 3", False),
        ],
        page=0,
        total_pages=3,
        kind="jobs",
    )
    e_tmu = f_tmu.paginated(
        title="Campus Job Board",
        description="Summary before detail fields.",
        fields=[
            ("Role 1", "Details 1", False),
            ("Role 2", "Details 2", False),
            ("Role 3", "Details 3", False),
        ],
        page=0,
        total_pages=3,
        kind="jobs",
    )
    e_em = f_emerald.empty_state(
        title="No Courses Found",
        message="No study links matched your filter.",
        suggestion="Try /ic notes course:CPS109",
        category="Academics",
    )
    e_dis = f_tmu.disabled_feature("Cafe Casino", module_id="casino", category="Gaming")

    assert e_def.color.value == 0xA64DFF
    assert e_def.author.name == "InstigateAI"
    assert e_def.footer.text == "Instigate Cafe • Jobs • Page 1/3"
    assert e_def.thumbnail.url is None  # No unnecessary duplicate logo thumbnail
    assert e_def.timestamp is None  # No noisy wall-clock timestamp on static/paginated embed

    assert e_tmu.color.value == 0x004C97
    assert e_tmu.author.name == "TMU Campus Bot"
    assert e_tmu.footer.text == "TMU Student Hub • Jobs • Page 1/3"

    assert e_em.color.value == 0x059669
    assert e_em.author.name == "Academic Bot"
    assert e_em.footer.text == "Academic Commons • System • Academics"

    # Disabled feature uses muted disabled semantic color (0x64748B)
    assert e_dis.color.value == 0x64748B
    assert "Module Disabled • Gaming" in (e_dis.title or "")

    # Semantic color resolution (normal, success, warning, error, disabled)
    assert resolve_embed_color(s_tmu, status="normal") == 0x004C97
    assert resolve_embed_color(s_tmu, status="success") == 0x2ECC71
    assert resolve_embed_color(s_tmu, status="warning") == 0xF39C12
    assert resolve_embed_color(s_tmu, status="error") == 0xE74C3C
    assert resolve_embed_color(s_tmu, status="disabled") == 0x64748B

    # format_footer deduplicates repeated brand or category segments cleanly
    assert format_footer(s_default, "Instigate Cafe", "Jobs", "Jobs", page=1, total_pages=3) == (
        "Instigate Cafe • Jobs • Page 1/3"
    )


@pytest.mark.unit
def test_unicode_truncation_malformed_values_and_safe_links():
    """Verify Unicode-safe truncation (ZWJ emoji, combining marks, lone surrogates, NUL) and safe URL schemes."""
    s = Settings()

    # 1. Lone surrogates and NUL bytes never fail UTF-8 encoding
    malformed = "Bad\x00Text\ud800WithSurrogate and @everyone <@&999888777>"
    cleaned = sanitize_untrusted_text(malformed)
    assert "\x00" not in cleaned
    cleaned.encode("utf-8")  # Must not raise UnicodeEncodeError
    assert "@everyone" not in cleaned
    assert "<@&999888777>" not in cleaned

    # 2. Bot formatting vs untrusted text escaping
    bot_formatted = sanitize_text("**Bold Label:** `code` @here")
    assert "**Bold Label:** `code`" in (bot_formatted or "")
    assert "@here" not in (bot_formatted or "")

    untrusted_md = sanitize_untrusted_text("**Fake Bold** [spoof](https://evil.invalid) @everyone")
    assert "\\*\\*Fake Bold\\*\\*" in untrusted_md
    assert "@everyone" not in untrusted_md

    # 3. Truncation at ZWJ / combining mark boundary does not leave dangling ZWJ (\u200d) or combining accent
    zwj_family = "A" * 18 + "👩‍💻_extra"  # '👩' + '\u200d' + '💻' + '_extra'
    truncated_zwj = sanitize_text(zwj_family, limit=21)
    assert truncated_zwj is not None
    assert len(truncated_zwj) <= 21
    assert truncated_zwj.endswith("…")
    assert not truncated_zwj[:-1].endswith("\u200d")

    combining_str = "B" * 18 + "e\u0301_extra"
    truncated_comb = sanitize_text(combining_str, limit=21)
    assert truncated_comb is not None
    assert len(truncated_comb) <= 21
    assert truncated_comb.endswith("…")
    assert not truncated_comb[:-1].endswith("\u0301")

    # 4. Safe URL schemes
    assert safe_http_url("https://torontomu.ca/courses?id=1") == "https://torontomu.ca/courses?id=1"
    assert safe_http_url("http://example.com/path") == "http://example.com/path"
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("data:text/html,<script>alert(1)</script>") is None
    assert safe_http_url("file:///etc/passwd") is None
    assert safe_http_url("https://") is None
    assert safe_http_url("https://evil.com/foo) [click](https://phish.com") is None

    assert (
        format_safe_link("Apply [Now]", "https://example.com/job")
        == "[Apply \\[Now\\]](https://example.com/job)"
    )
    assert format_safe_link("Apply", "javascript:evil()", fallback="No link") == "No link"

    # 5. Long fields and 6000-char total budget across multiple oversized fields
    huge_fields = [(f"Field {i} " + ("N" * 300), "V" * 1500, False) for i in range(10)]
    emb = paginated_embed(
        s,
        title="T" * 300,
        description="D" * 4500,
        fields=huge_fields,
        page=0,
        total_pages=2,
        kind="jobs",
    )
    assert len(emb.title or "") <= 256
    assert len(emb.description or "") <= 4096
    for f in emb.fields:
        assert len(f.name) <= 256
        assert len(f.value) <= 1024
    assert len(emb) <= 6000


@pytest.mark.asyncio
async def test_client_default_allowed_mentions_and_intentional_alert_role_exception(tmp_db):
    """Verify CampusBot defaults to AllowedMentions.none() and job alert outbox permits only configured role_id."""
    from types import SimpleNamespace

    import discord

    from icbot.bot import CampusBot
    from icbot.container import ServiceContainer
    from icbot.services.jobs.models import JobPosting
    from icbot.services.jobs.pipeline import JobsIngestionPipeline
    from tests.helpers import FakeTextChannel

    # 1. CampusBot defaults to AllowedMentions.none()
    bot = CampusBot(settings=Settings(), register_commands=False, sync_commands=False)
    assert bot.allowed_mentions is not None
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.replied_user is False

    # 2. alert_role_allowed_mentions helper validates snowflake and scopes strictly to that single role
    none_am = alert_role_allowed_mentions(None)
    assert none_am.roles is False
    invalid_am = alert_role_allowed_mentions(0)
    assert invalid_am.roles is False
    role_id = 900000000000000055
    scoped_am = alert_role_allowed_mentions(role_id)
    assert scoped_am.everyone is False
    assert scoped_am.users is False
    assert scoped_am.replied_user is False
    assert isinstance(scoped_am.roles, list)
    assert [r.id for r in scoped_am.roles] == [role_id]

    # 3. Scheduled job alert outbox resolves guild theme override AND applies intentional role mention exception
    guild_tmu_id = 900000000000000101
    guild_default_id = 900000000000000102
    base_settings = Settings(db_path=":memory:")
    container = ServiceContainer.from_settings(base_settings, db=tmp_db)
    assert container.guild_settings is not None
    await container.guild_settings.set_overrides(guild_tmu_id, {"theme_preset": "toronto_tmu"})

    # Guild TMU subscribes channel 301 WITH alert role_id; Guild Default subscribes channel 302 WITHOUT role_id
    await tmp_db.set_guild_job_subscription(
        guild_tmu_id, 301, preset="tech_swe", role_id=role_id, enabled=True
    )
    await tmp_db.set_guild_job_subscription(
        guild_default_id, 302, preset="tech_swe", role_id=None, enabled=True
    )

    ch_tmu = FakeTextChannel(channel_id=301, name="tmu-careers")
    ch_def = FakeTextChannel(channel_id=302, name="cafe-careers")
    fake_bot = SimpleNamespace(
        settings=base_settings,
        db=tmp_db,
        services=container,
        get_channel=lambda cid: {301: ch_tmu, 302: ch_def}.get(cid),
    )

    pipeline = JobsIngestionPipeline(clock=lambda: 1760000000.0)
    posting = JobPosting(
        provider="greenhouse",
        employer="Stripe",
        external_id="role-1",
        title="Software Engineer Intern @everyone",
        location="Toronto, ON",
        url="https://example.com/jobs/1",
        posted_unix=1760000000,
        text_blob="software engineer intern toronto",
    )
    await pipeline.ingest_and_enqueue_postings(tmp_db, [posting])
    delivered = await pipeline.dispatch_outbox(fake_bot, tmp_db)  # type: ignore[arg-type]
    assert delivered == 2

    # Channel 301 (TMU guild): received toronto_tmu theme color (0x004C97), footer "TMU Student Hub", AND scoped role ping
    msg_tmu = ch_tmu.sent[0]
    assert msg_tmu.embed is not None
    assert msg_tmu.embed.color.value == 0x004C97
    assert "TMU Student Hub" in (msg_tmu.embed.footer.text or "")
    assert "@everyone" not in (msg_tmu.embed.title or "")
    assert msg_tmu.content == f"<@&{role_id}>"
    assert isinstance(msg_tmu.allowed_mentions, discord.AllowedMentions)
    assert msg_tmu.allowed_mentions.everyone is False
    assert msg_tmu.allowed_mentions.users is False
    assert [r.id for r in msg_tmu.allowed_mentions.roles] == [role_id]

    # Channel 302 (Default guild): received instigate_cafe theme color (0xA64DFF) and AllowedMentions.none()
    msg_def = ch_def.sent[0]
    assert msg_def.embed is not None
    assert msg_def.embed.color.value == 0xA64DFF
    assert "Instigate Cafe" in (msg_def.embed.footer.text or "")
    assert msg_def.content is None
    assert isinstance(msg_def.allowed_mentions, discord.AllowedMentions)
    assert msg_def.allowed_mentions.roles is False
    await container.close()
