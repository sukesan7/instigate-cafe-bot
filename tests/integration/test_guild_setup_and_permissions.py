"""Integration tests for guild setup, server-side permission checks, and status UX.

Covers:
- Server-side Manage Guild / Admin permission verification independent of Discord UI.
- Two distinct guilds with different admin IDs and role IDs (cross-guild isolation).
- Privilege escalation defense, DM rejection, and stale interaction checks.
- Migration 5 guild settings audit ledger tracking mutations and resets.
- Interactive GuildSetupView, CampusTimezoneModal, and ChannelsConfigModal.
- /ic status output differentiating working, degraded, disabled, and missing config.
- /ic admin config, features, roles, and audit command workflows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord import app_commands

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.admin import (
    CampusTimezoneModal,
    ChannelsConfigModal,
    GuildSetupView,
    _is_valid_tz,
    audit_cmd,
    config_cmd,
    features_cmd,
    roles_cmd,
    setup_cmd,
)
from icbot.features.info import status_cmd
from icbot.guild_config import GuildSettingsStore
from icbot.permissions import can_manage_guild

pytestmark = pytest.mark.discord_mock


# ---------------------------------------------------------------------------
# 1. Server-side permission authorization checks (can_manage_guild)
# ---------------------------------------------------------------------------


def test_can_manage_guild_authorization_matrix(make_interaction, make_member):
    """Server-side permission checks enforce Manage Guild / Owner / Admin / Allowed Roles."""
    guild_id = 900000000000000001
    owner_id = 800000000000000001
    admin_id = 800000000000000002
    manager_id = 800000000000000003
    roled_user_id = 800000000000000004
    regular_user_id = 800000000000000005

    allowed_role_id = 700000000000000001
    other_role_id = 700000000000000002

    settings = Settings(guild_id=guild_id, allowed_role_ids=[allowed_role_id])
    fake_guild = SimpleNamespace(id=guild_id, owner_id=owner_id, name="Test Guild")

    # 1. Guild owner (even without explicit admin permission flags)
    owner = make_member(user_id=owner_id, administrator=False)
    inter_owner = make_interaction(user=owner, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_owner, settings) is True
    assert can_manage_guild(owner, settings, guild=fake_guild) is True

    # 2. Administrator permission
    admin_mem = make_member(user_id=admin_id, administrator=True)
    inter_admin = make_interaction(user=admin_mem, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_admin, settings) is True

    # 3. Manage Guild permission (without full administrator)
    manager_mem = make_member(user_id=manager_id, administrator=False)
    manager_mem.guild_permissions.manage_guild = True
    inter_manager = make_interaction(user=manager_mem, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_manager, settings) is True

    # 4. Configured allowed role
    roled_mem = make_member(user_id=roled_user_id, administrator=False, role_ids=[allowed_role_id])
    inter_roled = make_interaction(user=roled_mem, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_roled, settings) is True

    # 5. Regular member with non-allowed role: rejected
    other_roled = make_member(user_id=regular_user_id, administrator=False, role_ids=[other_role_id])
    inter_other = make_interaction(user=other_roled, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_other, settings) is False

    # 6. Regular member with zero roles / permissions: rejected
    plain_mem = make_member(user_id=regular_user_id, administrator=False, role_ids=[])
    inter_plain = make_interaction(user=plain_mem, guild=fake_guild, settings=settings)
    assert can_manage_guild(inter_plain, settings) is False

    # 7. DM Context (guild is None): must fail closed
    dm_inter = make_interaction(user=owner, guild=None, settings=settings)
    dm_inter.guild = None
    assert can_manage_guild(dm_inter, settings) is False

    # 8. Non-member user object without guild_permissions: must fail closed
    non_member = SimpleNamespace(id=999999, name="NonMember")
    inter_non_member = SimpleNamespace(user=non_member, guild=fake_guild)
    assert can_manage_guild(inter_non_member, settings) is False


# ---------------------------------------------------------------------------
# 2. Two distinct guilds with different admin IDs and role IDs
# ---------------------------------------------------------------------------


async def test_two_distinct_guilds_permission_and_config_isolation(
    tmp_db: Database, make_interaction, make_member
):
    """Guild A and Guild B have different admins and roles; mutations stay strictly isolated."""
    guild_a_id = 900000000000000001
    guild_b_id = 900000000000000002

    admin_a_id = 800000000000000001
    admin_b_id = 800000000000000002

    role_a_id = 700000000000000001
    role_b_id = 700000000000000002

    fake_guild_a = SimpleNamespace(id=guild_a_id, owner_id=admin_a_id, name="Campus Guild A")
    fake_guild_b = SimpleNamespace(id=guild_b_id, owner_id=admin_b_id, name="Campus Guild B")

    base_settings = Settings(guild_id=0, db_path=tmp_db.path)
    container = ServiceContainer(settings=base_settings, db=tmp_db)
    store = GuildSettingsStore(tmp_db)

    # Initialize overrides for Guild A and Guild B
    await store.set_overrides(
        guild_a_id,
        {
            "rmp_university": "University A",
            "exams_tz": "America/Toronto",
            "allowed_role_ids": [role_a_id],
            "feature_profile": "student_core",
        },
        actor_id=admin_a_id,
    )
    await store.set_overrides(
        guild_b_id,
        {
            "rmp_university": "University B",
            "exams_tz": "America/Vancouver",
            "allowed_role_ids": [role_b_id],
            "feature_profile": "community_full",
        },
        actor_id=admin_b_id,
    )

    settings_a = await container.resolve_settings_for_guild(guild_a_id)
    settings_b = await container.resolve_settings_for_guild(guild_b_id)

    assert settings_a.rmp_university == "University A"
    assert settings_a.exams_tz == "America/Toronto"
    assert settings_b.rmp_university == "University B"
    assert settings_b.exams_tz == "America/Vancouver"

    # Admin of Guild A is authorized in Guild A (as owner)
    admin_a_in_a = make_interaction(
        user=make_member(user_id=admin_a_id, administrator=False, role_ids=[]),
        guild=fake_guild_a,
        settings=settings_a,
        db=tmp_db,
    )
    assert can_manage_guild(admin_a_in_a, settings_a) is True

    # Member having Role A in Guild B is NOT authorized in Guild B
    user_with_role_a = make_member(user_id=800000000000000099, administrator=False, role_ids=[role_a_id])
    inter_cross = make_interaction(user=user_with_role_a, guild=fake_guild_b, settings=settings_b, db=tmp_db)
    assert can_manage_guild(inter_cross, settings_b) is False

    # Admin of Guild A attempting to run config update on Guild B is rejected
    admin_a_in_b = make_interaction(
        user=make_member(user_id=admin_a_id, administrator=False, role_ids=[]),
        guild=fake_guild_b,
        settings=settings_b,
        db=tmp_db,
    )
    assert can_manage_guild(admin_a_in_b, settings_b) is False

    # Calling config command as Admin A in Guild B is rejected ephemerally
    await config_cmd.callback(admin_a_in_b, campus="Compromised University")
    assert len(admin_a_in_b.response.sent_messages) == 1
    assert "You do not have permission" in admin_a_in_b.response.sent_messages[0].content

    # Assert Guild B configuration was unchanged
    b_cfg_fresh = await store.get(guild_b_id)
    assert b_cfg_fresh is not None
    assert b_cfg_fresh.overrides["rmp_university"] == "University B"


# ---------------------------------------------------------------------------
# 3. Database Migration 5 & Audit Trail (guild_settings_audit)
# ---------------------------------------------------------------------------


async def test_migration_0005_audit_ledger_and_history(tmp_db: Database):
    """Migration 5 creates audit table, logging mutations, resets, and query history."""
    store = GuildSettingsStore(tmp_db)
    guild_id = 900000000000000001
    actor_id = 800000000000000055

    # Verify table existence in SQLite schema
    table_row = await tmp_db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_settings_audit'"
    )
    assert table_row is not None and table_row[0] == "guild_settings_audit"

    # 1. Update overrides with actor_id -> records audit entry
    await store.update_overrides(
        guild_id,
        {"rmp_university": "Ryerson / TMU", "exams_tz": "America/Toronto"},
        actor_id=actor_id,
    )
    history = await store.get_audit_history(guild_id)
    assert len(history) == 1
    entry1 = history[0]
    assert entry1.guild_id == guild_id
    assert entry1.actor_id == actor_id
    assert entry1.action == "update"
    assert "rmp_university" in entry1.changed_keys
    assert "exams_tz" in entry1.changed_keys
    assert entry1.created_at > 0

    # 2. Second update -> records second audit entry with chronological order
    await store.update_overrides(
        guild_id,
        {"user_command_channel_id": 123456789},
        actor_id=actor_id,
    )
    history2 = await store.get_audit_history(guild_id)
    assert len(history2) == 2
    assert history2[0].action == "update"
    assert "user_command_channel_id" in history2[0].changed_keys

    # 3. Reset overrides -> records reset audit entry
    reset_ok = await store.reset_overrides(guild_id, actor_id=actor_id)
    assert reset_ok is True
    history3 = await store.get_audit_history(guild_id)
    assert len(history3) == 3
    assert history3[0].action == "reset"
    assert history3[0].new_json == "{}"

    # Guild settings row is cleared
    assert await store.get(guild_id) is None


# ---------------------------------------------------------------------------
# 4. Timezone validation
# ---------------------------------------------------------------------------


def test_timezone_validation():
    """Validates IANA timezones and rejects invalid strings cleanly."""
    assert _is_valid_tz("America/Toronto") is True
    assert _is_valid_tz("America/New_York") is True
    assert _is_valid_tz("America/Vancouver") is True
    assert _is_valid_tz("UTC") is True

    assert _is_valid_tz("Mars/Olympus_Mons") is False
    assert _is_valid_tz("") is False
    assert _is_valid_tz("Invalid/TimeZone") is False
    assert _is_valid_tz("12345") is False


# ---------------------------------------------------------------------------
# 5. Interactive Setup View & Modals
# ---------------------------------------------------------------------------


async def test_guild_setup_view_and_modals(tmp_db: Database, make_interaction, make_member):
    """GuildSetupView and modals validate inputs, enforce permissions, and persist updates."""
    guild_id = 900000000000000001
    admin_id = 800000000000000001
    attacker_id = 800000000000000099

    fake_guild = SimpleNamespace(id=guild_id, owner_id=admin_id, name="Test Campus Guild")
    admin_member = make_member(user_id=admin_id, administrator=True)
    attacker_member = make_member(user_id=attacker_id, administrator=False)

    base_settings = Settings(guild_id=0, db_path=tmp_db.path)
    container = ServiceContainer(settings=base_settings, db=tmp_db)
    store = GuildSettingsStore(tmp_db)

    view = GuildSetupView(guild_id, admin_id, container, base_settings)

    # 1. Attacker clicking view button fails interaction check
    inter_attacker = make_interaction(
        user=attacker_member, guild=fake_guild, settings=base_settings, db=tmp_db
    )
    allowed = await view.interaction_check(inter_attacker)
    assert allowed is False
    assert len(inter_attacker.response.sent_messages) == 1
    assert "You do not have permission" in inter_attacker.response.sent_messages[0].content

    # 2. Admin clicking view button passes interaction check
    inter_admin = make_interaction(user=admin_member, guild=fake_guild, settings=base_settings, db=tmp_db)
    allowed_admin = await view.interaction_check(inter_admin)
    assert allowed_admin is True

    # 3. ProfileSelect component updates profile in SQLite
    view.profile_select._values = ["community_full"]

    inter_select = make_interaction(user=admin_member, guild=fake_guild, settings=base_settings, db=tmp_db)
    await view.profile_select.callback(inter_select)
    assert len(inter_select.response.edit_messages) == 1

    saved = await store.get(guild_id)
    assert saved is not None and saved.overrides["feature_profile"] == "community_full"

    # 4. CampusTimezoneModal submit with invalid timezone rejected
    modal = CampusTimezoneModal(current_campus="Old U", current_tz="America/Toronto", guild_id=guild_id)
    modal.university._value = "New Campus"
    modal.timezone._value = "Invalid/Zone"

    inter_modal_bad = make_interaction(user=admin_member, guild=fake_guild, settings=base_settings, db=tmp_db)
    await modal.on_submit(inter_modal_bad)
    assert len(inter_modal_bad.response.sent_messages) == 1
    assert "Invalid IANA timezone" in inter_modal_bad.response.sent_messages[0].content

    # 5. CampusTimezoneModal submit with valid timezone accepted
    modal.timezone._value = "America/Vancouver"
    inter_modal_good = make_interaction(
        user=admin_member, guild=fake_guild, settings=base_settings, db=tmp_db
    )
    await modal.on_submit(inter_modal_good)
    assert len(inter_modal_good.response.sent_messages) == 1
    assert "Configuration Updated" in inter_modal_good.response.sent_messages[0].content

    saved2 = await store.get(guild_id)
    assert saved2 is not None
    assert saved2.overrides["rmp_university"] == "New Campus"
    assert saved2.overrides["exams_tz"] == "America/Vancouver"

    # 6. ChannelsConfigModal submit updates channels
    ch_modal = ChannelsConfigModal(current_cmd=0, current_jobs=0, current_transit=0, guild_id=guild_id)
    ch_modal.command_channel._value = "111222333"
    ch_modal.job_alerts_channel._value = "444555666"
    ch_modal.transit_alerts_channel._value = "777888999"

    inter_ch = make_interaction(user=admin_member, guild=fake_guild, settings=base_settings, db=tmp_db)
    await ch_modal.on_submit(inter_ch)
    assert len(inter_ch.response.sent_messages) == 1
    assert "Channels Updated" in inter_ch.response.sent_messages[0].content

    saved3 = await store.get(guild_id)
    assert saved3 is not None
    assert saved3.overrides["user_command_channel_id"] == 111222333
    assert saved3.overrides["job_alerts_channel_id"] == 444555666
    assert saved3.overrides["ttc_alerts_channel_id"] == 777888999


# ---------------------------------------------------------------------------
# 6. /ic status diagnostic output and UX
# ---------------------------------------------------------------------------


async def test_ic_status_command_diagnostics(tmp_db: Database, make_interaction, make_member):
    """The /ic status command differentiates working, degraded, disabled, and deployment tracks."""
    guild_id = 900000000000000001
    admin_id = 800000000000000001
    fake_guild = SimpleNamespace(id=guild_id, owner_id=admin_id, name="Diagnostic Server")
    admin_member = make_member(user_id=admin_id, administrator=True)

    # 1. Multi-guild hosted mode with standard settings
    hosted_settings = Settings(
        guild_id=0,
        db_path=tmp_db.path,
        ttc_alerts_channel_id=123,
        go_api_key="",  # Degrades transit
        odds_api_key="",  # Degrades sports if enabled
        disabled_features=["casino"],
    )
    inter_status = make_interaction(user=admin_member, guild=fake_guild, settings=hosted_settings, db=tmp_db)
    await status_cmd.callback(inter_status)

    assert len(inter_status.response.sent_messages) == 1
    reply = inter_status.response.sent_messages[0]
    assert reply.ephemeral is True
    assert reply.embed is not None

    embed_text = str(reply.embed.to_dict())
    # Multi-guild track recognized
    assert "Multi-Guild Hosted" in embed_text
    # Status badges present
    assert "Working" in embed_text
    assert "Degraded" in embed_text
    assert "Disabled" in embed_text
    # Actionable fix present for degraded transit
    assert "GO_API_KEY" in embed_text

    # 2. Single-guild self-hosted mode
    single_guild_settings = Settings(
        guild_id=guild_id,
        db_path=tmp_db.path,
    )
    inter_single = make_interaction(
        user=admin_member, guild=fake_guild, settings=single_guild_settings, db=tmp_db
    )
    await status_cmd.callback(inter_single)
    reply_single = inter_single.response.sent_messages[0]
    assert "Single-Guild Self-Hosted" in str(reply_single.embed.to_dict())

    # 3. Non-admin caller is rejected ephemerally
    regular_user = make_member(user_id=800000000000000099, administrator=False)
    inter_non_admin = make_interaction(
        user=regular_user, guild=fake_guild, settings=hosted_settings, db=tmp_db
    )
    await status_cmd.callback(inter_non_admin)
    assert len(inter_non_admin.response.sent_messages) == 1
    assert "Manage Server permissions" in inter_non_admin.response.sent_messages[0].content


# ---------------------------------------------------------------------------
# 7. /ic admin setup & config slash commands
# ---------------------------------------------------------------------------


async def test_admin_config_slash_commands(tmp_db: Database, make_interaction, make_member):
    """The /ic admin config, features, roles, and audit commands operate correctly."""
    guild_id = 900000000000000001
    admin_id = 800000000000000001
    fake_guild = SimpleNamespace(id=guild_id, owner_id=admin_id, name="Admin Command Server")
    admin_member = make_member(user_id=admin_id, administrator=True)

    settings = Settings(guild_id=guild_id, db_path=tmp_db.path)

    # 1. /ic admin setup launches view ephemerally
    inter_setup = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await setup_cmd.callback(inter_setup)
    assert len(inter_setup.response.sent_messages) == 1
    assert inter_setup.response.sent_messages[0].view is not None
    assert inter_setup.response.sent_messages[0].ephemeral is True

    # 2. /ic admin config action=update campus & timezone
    inter_cfg_set = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await config_cmd.callback(
        inter_cfg_set,
        campus="Metropolitan University",
        timezone="America/Toronto",
    )
    assert len(inter_cfg_set.response.sent_messages) == 1
    assert "Configuration Updated" in inter_cfg_set.response.sent_messages[0].content

    # 3. /ic admin config action=view
    inter_cfg_view = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await config_cmd.callback(inter_cfg_view)
    assert len(inter_cfg_view.response.sent_messages) == 1
    assert "Configuration Preview" in str(inter_cfg_view.response.sent_messages[0].embed.to_dict())

    # 4. /ic admin features enable and disable
    inter_feat = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await features_cmd.callback(inter_feat, enable="casino", disable="sports")
    assert len(inter_feat.response.sent_messages) == 1
    assert "Feature Configuration Updated" in inter_feat.response.sent_messages[0].content

    # 5. /ic admin roles add role
    fake_role = SimpleNamespace(id=777000000000000001, mention="<@&777000000000000001>")
    inter_role_add = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    choice_add = app_commands.Choice(name="Add", value="add")
    await roles_cmd.callback(inter_role_add, action=choice_add, role=fake_role)
    assert len(inter_role_add.response.sent_messages) == 1
    assert "Added" in inter_role_add.response.sent_messages[0].content

    # 6. /ic admin audit displays recorded entries
    inter_audit = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await audit_cmd.callback(inter_audit)
    assert len(inter_audit.response.sent_messages) == 1
    assert "Configuration Audit Log" in str(inter_audit.response.sent_messages[0].embed.to_dict())

    # 7. /ic admin config action=reset clears overrides
    choice_reset = app_commands.Choice(name="Reset to Defaults", value="reset")
    inter_reset = make_interaction(user=admin_member, guild=fake_guild, settings=settings, db=tmp_db)
    await config_cmd.callback(inter_reset, action=choice_reset)
    assert len(inter_reset.response.sent_messages) == 1
    assert "Configuration Reset" in inter_reset.response.sent_messages[0].content
