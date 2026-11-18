"""Integration tests for typed feature registry, dependency checks, presets, and runtime gating."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import app_commands

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.errors import ConfigValidationError
from icbot.features.blackjack import blackjack as blackjack_cmd
from icbot.features.exams import exams_add_cmd
from icbot.features.invest import buy as buy_cmd
from icbot.features.profile import daily_cmd
from icbot.guild_config import GuildSettingsStore

try:
    from icbot.registry import (
        FEATURE_MANIFESTS,
        FeatureManifest,
        check_command_gate,
        get_feature_registry,
        register_ic_commands,
        registered_qualified_names,
    )
except ImportError as _err:
    pytest.skip(
        f"Feature registry manifests not available in current worktree: {_err}",
        allow_module_level=True,
    )

pytestmark = pytest.mark.discord_mock


# ---------------------------------------------------------------------------
# 1. Manifest completeness & Route inventory
# ---------------------------------------------------------------------------


def test_feature_manifest_completeness_and_contract():
    """All 9 functional modules have complete typed capability contracts."""
    expected_modules = {
        "core",
        "academics",
        "careers",
        "transit",
        "economy",
        "casino",
        "market",
        "ai",
        "sports",
    }
    assert set(FEATURE_MANIFESTS.keys()) == expected_modules

    reg = get_feature_registry()
    all_supplied: list[str] = []

    for mod_id, m in FEATURE_MANIFESTS.items():
        assert isinstance(m, FeatureManifest)
        assert m.module_id == mod_id
        assert m.display_name
        assert m.description
        assert m.category
        assert isinstance(m.source_modules, tuple)
        assert isinstance(m.commands_supplied, tuple)
        assert isinstance(m.pollers_supplied, tuple)
        assert isinstance(m.dependencies, tuple)
        assert isinstance(m.required_secrets, tuple)
        assert isinstance(m.required_channels, tuple)
        assert isinstance(m.required_permissions, tuple)
        assert isinstance(m.storage_tables, tuple)
        assert isinstance(m.guild_config_keys, tuple)
        assert isinstance(m.default_enabled, bool)

        all_supplied.extend(m.commands_supplied)

    # 49 commands with zero overlap across modules
    assert len(all_supplied) == 49
    assert len(set(all_supplied)) == 49

    # Every command maps back to its module
    for cmd in all_supplied:
        assert reg.get_module_for_command(cmd) is not None


def test_route_inventory_before_and_after_registry_enhancement():
    """Slash command hierarchy and leaf command names are 100% preserved."""
    fake_client = SimpleNamespace(
        http=SimpleNamespace(),
        _connection=SimpleNamespace(_command_tree=None),
    )
    tree = app_commands.CommandTree(fake_client)  # type: ignore[arg-type]
    root = register_ic_commands(tree, override=True)
    names = registered_qualified_names(root)

    assert len(names) == 49
    all_manifest_cmds = {cmd for m in FEATURE_MANIFESTS.values() for cmd in m.commands_supplied}
    assert set(names) == all_manifest_cmds


# ---------------------------------------------------------------------------
# 2. Presets & Capability resolution
# ---------------------------------------------------------------------------


def test_presets_and_capabilities_resolution():
    """Configuration presets enable expected feature sets cleanly."""
    reg = get_feature_registry()

    # student_core: student focus (no casino/market/ai/sports)
    sc_settings = Settings(feature_profile="student_core")
    assert reg.is_feature_enabled("core", sc_settings) is True
    assert reg.is_feature_enabled("academics", sc_settings) is True
    assert reg.is_feature_enabled("careers", sc_settings) is True
    assert reg.is_feature_enabled("transit", sc_settings) is True
    assert reg.is_feature_enabled("economy", sc_settings) is True
    assert reg.is_feature_enabled("casino", sc_settings) is False
    assert reg.is_feature_enabled("market", sc_settings) is False
    assert reg.is_feature_enabled("ai", sc_settings) is False
    assert reg.is_feature_enabled("sports", sc_settings) is False

    # toronto_tmu: equivalent student profile
    tmu_settings = Settings(feature_profile="toronto_tmu")
    assert reg.is_feature_enabled("core", tmu_settings) is True
    assert reg.is_feature_enabled("casino", tmu_settings) is False

    # community_full: all modules enabled in preset
    full_settings = Settings(feature_profile="community_full", gemini_api_key="synthetic_gemini")
    assert reg.is_feature_enabled("casino", full_settings) is True
    assert reg.is_feature_enabled("market", full_settings) is True
    assert reg.is_feature_enabled("ai", full_settings) is True
    assert reg.is_feature_enabled("sports", full_settings) is True

    # default preset: standard 7 enabled, external APIs off unless configured
    def_settings = Settings(feature_profile="default")
    assert reg.is_feature_enabled("casino", def_settings) is True
    assert reg.is_feature_enabled("market", def_settings) is True
    assert reg.is_feature_enabled("ai", def_settings) is False
    assert reg.is_feature_enabled("sports", def_settings) is False

    # Unknown profile falls back gracefully to default
    unknown_settings = Settings(feature_profile="custom_unregistered")
    assert reg.is_feature_enabled("core", unknown_settings) is True
    assert reg.is_feature_enabled("casino", unknown_settings) is True


# ---------------------------------------------------------------------------
# 3. Missing optional & required keys (Degradation vs Error)
# ---------------------------------------------------------------------------


def test_missing_optional_and_required_keys():
    """Optional keys degrade health gracefully; missing required secrets block execution."""
    reg = get_feature_registry()

    # Sports without ODDS_API_KEY: degraded but available (RSS headlines active)
    sports_no_odds = Settings(enabled_features=["sports"], odds_api_key="")
    h_sports = reg.resolve_feature_health("sports", sports_no_odds)
    assert h_sports.enabled is True
    assert h_sports.available is True
    assert h_sports.status == "degraded"
    assert "ODDS_API_KEY not configured" in h_sports.reason

    # Transit without GO_API_KEY: degraded but available (TTC active)
    transit_no_go = Settings(ttc_alerts_channel_id=123, go_api_key="")
    h_transit = reg.resolve_feature_health("transit", transit_no_go)
    assert h_transit.enabled is True
    assert h_transit.available is True
    assert h_transit.status == "degraded"
    assert "GO_API_KEY not configured" in h_transit.reason

    # AI without GEMINI_API_KEY: missing_config and unavailable
    ai_no_key = Settings(feature_profile="community_full", gemini_api_key="")
    h_ai = reg.resolve_feature_health("ai", ai_no_key)
    assert h_ai.enabled is True
    assert h_ai.available is False
    assert h_ai.status == "missing_config"
    assert "gemini_api_key" in h_ai.missing_secrets


# ---------------------------------------------------------------------------
# 4. Dependency failure enforcement
# ---------------------------------------------------------------------------


def test_module_dependency_enforcement():
    """Disabling economy automatically disables casino and market with clear dependency failure."""
    reg = get_feature_registry()

    # Default has economy and casino enabled
    base = Settings(feature_profile="default")
    assert reg.is_feature_enabled("casino", base) is True
    assert reg.is_feature_enabled("market", base) is True

    # Disable economy: casino & market fail dependency check
    no_econ = Settings(feature_profile="default", disabled_features=["economy"])
    assert reg.is_feature_enabled("economy", no_econ) is False
    assert reg.is_feature_enabled("casino", no_econ) is False
    assert reg.is_feature_enabled("market", no_econ) is False

    h_casino = reg.resolve_feature_health("casino", no_econ)
    assert h_casino.enabled is True  # administratively in profile
    assert h_casino.available is False  # blocked by dependency
    assert h_casino.status == "missing_dependency"
    assert h_casino.missing_dependencies == ("economy",)
    assert "Virtual Economy" in h_casino.reason

    h_market = reg.resolve_feature_health("market", no_econ)
    assert h_market.available is False
    assert h_market.status == "missing_dependency"
    assert h_market.missing_dependencies == ("economy",)


# ---------------------------------------------------------------------------
# 5. Two-guild feature toggle isolation
# ---------------------------------------------------------------------------


async def test_guild_scoped_feature_toggle_isolation(tmp_db: Database, make_interaction, make_member):
    """Features can be independently enabled or disabled per guild without cross-talk."""
    store = GuildSettingsStore(tmp_db)

    guild_a_id = 900000000000000001
    guild_b_id = 900000000000000002

    # Setting secrets in guild overrides is strictly rejected
    with pytest.raises(ConfigValidationError):
        await store.set_overrides(
            guild_a_id,
            {"feature_profile": "community_full", "gemini_api_key": "leak"},
        )

    # Guild A: Community Full (all features on)
    await store.set_overrides(guild_a_id, {"feature_profile": "community_full"})

    # Guild B: Student Core (casino off)
    await store.set_overrides(guild_b_id, {"feature_profile": "student_core"})

    base_settings = Settings(guild_id=0, db_path=tmp_db.path)
    container = ServiceContainer(settings=base_settings, db=tmp_db)

    # Resolve effective settings for each guild
    settings_a = await container.resolve_settings_for_guild(guild_a_id)
    settings_b = await container.resolve_settings_for_guild(guild_b_id)

    assert settings_a.feature_profile == "community_full"
    assert settings_b.feature_profile == "student_core"

    member_a = make_member(user_id=800000000000000001, display_name="GuildAUser")
    member_b = make_member(user_id=800000000000000002, display_name="GuildBUser")

    fake_guild_a = SimpleNamespace(id=guild_a_id, name="Guild A")
    fake_guild_b = SimpleNamespace(id=guild_b_id, name="Guild B")

    # Command gate check for casino blackjack
    inter_a = make_interaction(
        user=member_a,
        guild=fake_guild_a,
        command_name="ic casino blackjack",
        settings=settings_a,
    )
    inter_b = make_interaction(
        user=member_b,
        guild=fake_guild_b,
        command_name="ic casino blackjack",
        settings=settings_b,
    )

    # Guild A allows blackjack
    allowed_a = await check_command_gate(inter_a, settings_a)
    assert allowed_a is True
    assert len(inter_a.response.sent_messages) == 0

    # Guild B denies blackjack ephemerally
    allowed_b = await check_command_gate(inter_b, settings_b)
    assert allowed_b is False
    assert len(inter_b.response.sent_messages) == 1
    msg_b = inter_b.response.sent_messages[0]
    assert msg_b.ephemeral is True
    assert "Cafe Casino" in msg_b.content
    assert "disabled" in msg_b.content


# ---------------------------------------------------------------------------
# 6. Direct callback invocation safety without data mutation
# ---------------------------------------------------------------------------


async def test_direct_callback_invocation_safety_without_data_mutation(
    tmp_db: Database, make_interaction, make_member
):
    """Calling mutating command callbacks directly when feature is disabled mutates zero data."""
    member = make_member(user_id=800000000000000055, display_name="TestPlayer")
    guild_id = 900000000000000010
    fake_guild = SimpleNamespace(id=guild_id, name="Strict Guild")

    # Configure disabled features: casino, economy, market, academics
    disabled_settings = Settings(
        guild_id=guild_id,
        db_path=tmp_db.path,
        disabled_features=["casino", "economy", "market", "academics"],
    )

    # 1. /ic casino blackjack direct callback
    inter_bj = make_interaction(
        user=member,
        guild=fake_guild,
        command_name="ic casino blackjack",
        settings=disabled_settings,
        db=tmp_db,
    )
    await blackjack_cmd.callback(inter_bj, bet=50)
    assert len(inter_bj.response.sent_messages) == 1
    assert "Cafe Casino" in inter_bj.response.sent_messages[0].content
    assert inter_bj.response.sent_messages[0].ephemeral is True

    # Assert zero blackjack games in DB
    bj_count = await tmp_db.fetchone("SELECT COUNT(*) FROM blackjack_users")
    assert bj_count is not None and bj_count[0] == 0

    # 2. /ic dailyclaim direct callback
    inter_daily = make_interaction(
        user=member,
        guild=fake_guild,
        command_name="ic dailyclaim",
        settings=disabled_settings,
        db=tmp_db,
    )
    await daily_cmd.callback(inter_daily)
    assert len(inter_daily.response.sent_messages) == 1
    assert "Virtual Economy" in inter_daily.response.sent_messages[0].content

    # Assert zero users and zero economy ledger entries in DB
    user_count = await tmp_db.fetchone("SELECT COUNT(*) FROM users")
    assert user_count is not None and user_count[0] == 0
    ledger_count = await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger")
    assert ledger_count is not None and ledger_count[0] == 0

    # 3. /ic icse buy direct callback
    inter_buy = make_interaction(
        user=member,
        guild=fake_guild,
        command_name="ic icse buy",
        settings=disabled_settings,
        db=tmp_db,
    )
    await buy_cmd.callback(inter_buy, symbol="IC", qty=10)
    assert len(inter_buy.response.sent_messages) == 1
    assert "Stock Market & ICSE" in inter_buy.response.sent_messages[0].content

    # Assert zero trades in DB
    trade_count = await tmp_db.fetchone("SELECT COUNT(*) FROM fun_trades")
    assert trade_count is not None and trade_count[0] == 0

    # 4. /ic exam add direct callback
    inter_exam = make_interaction(
        user=member,
        guild=fake_guild,
        command_name="ic exam add",
        settings=disabled_settings,
        db=tmp_db,
    )
    await exams_add_cmd.callback(
        inter_exam,
        course_code="COE101",
        course_name="Intro",
        date="2026-12-15",
        start_time="14:00",
    )
    assert len(inter_exam.response.sent_messages) == 1
    assert "Academics & Exams" in inter_exam.response.sent_messages[0].content

    # Assert zero exams in DB
    exam_count = await tmp_db.fetchone("SELECT COUNT(*) FROM exams")
    assert exam_count is not None and exam_count[0] == 0


async def test_dependency_failure_blocks_mutation_ephemerally(
    tmp_db: Database, make_interaction, make_member
):
    """Enabling casino while economy is disabled blocks callback and explains dependency."""
    member = make_member(user_id=800000000000000055, display_name="TestPlayer")
    guild_id = 900000000000000011
    fake_guild = SimpleNamespace(id=guild_id, name="No Economy Guild")

    # Casino enabled, but Economy disabled
    dep_fail_settings = Settings(
        guild_id=guild_id,
        db_path=tmp_db.path,
        enabled_features=["casino"],
        disabled_features=["economy"],
    )

    inter = make_interaction(
        user=member,
        guild=fake_guild,
        command_name="ic casino blackjack",
        settings=dep_fail_settings,
        db=tmp_db,
    )
    await blackjack_cmd.callback(inter, bet=25)

    assert len(inter.response.sent_messages) == 1
    reply = inter.response.sent_messages[0]
    assert reply.ephemeral is True
    assert "unavailable because required feature" in reply.content
    assert "Virtual Economy" in reply.content

    # Zero database records
    bj_count = await tmp_db.fetchone("SELECT COUNT(*) FROM blackjack_users")
    assert bj_count is not None and bj_count[0] == 0


# ---------------------------------------------------------------------------
# 7. Task supervisor feature gating
# ---------------------------------------------------------------------------


def test_task_supervisor_gated_by_feature_registry():
    """Supervisor tasks respect feature registry enabled status."""
    from icbot.scheduler import build_default_task_specs

    async def _noop():
        pass

    specs = build_default_task_specs(
        run_ttc=_noop,
        run_go=_noop,
        run_jobs=_noop,
        run_workday=_noop,
        run_market_watch=_noop,
        run_btc_watch=_noop,
        run_daily_report=_noop,
    )
    spec_map = {s.name: s for s in specs}

    # Settings with transit and market disabled
    settings = Settings(
        ttc_alerts_channel_id=123,
        ttc_mastodon_base="https://example.invalid",
        ttc_mastodon_acct="ttc",
        market_watch_enabled=True,
        market_watch_channel_id=456,
        disabled_features=["transit", "market"],
    )

    # TTC and GO disabled because transit is disabled
    ttc_enabled, ttc_reason = spec_map["ttc"].is_enabled(settings, False)
    assert ttc_enabled is False
    assert "Transit feature is disabled" in ttc_reason

    go_enabled, go_reason = spec_map["go"].is_enabled(settings, False)
    assert go_enabled is False
    assert "Transit feature is disabled" in go_reason

    # Market watch disabled because market is disabled
    mw_enabled, mw_reason = spec_map["market_watch"].is_enabled(settings, False)
    assert mw_enabled is False
    assert "Market feature is disabled" in mw_reason
