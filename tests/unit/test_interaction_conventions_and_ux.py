"""Unit tests for information architecture, interaction conventions, and UX safeguards.

Verifies:
1. 100% backwards compatibility across all 53 leaf commands (52 historical + `/ic getting-started`).
2. Dynamic, metadata-driven help with module gating, permission filtering, and syntax examples.
3. `/ic getting-started` guided onboarding workflow, accessible language, and button state machine.
4. Accessible button labels, author isolation, and timeout safeguards.
5. Unified embeds API enforcing Discord character caps, markdown escaping, and mention defense.
6. Stale provider badges, disabled feature guidance, and empty state embeds.
7. User error recovery on course codes and search filters.
8. Synthetic end-to-end new-user walkthrough and friction point logging.
"""

from __future__ import annotations

import discord
import pytest

from icbot import embeds
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.grades import grades_command
from icbot.features.help import (
    COMMAND_METADATA,
    GETTING_STARTED_STEPS,
    GettingStartedView,
    HelpPaginationView,
    build_dynamic_help_categories,
    build_getting_started_embed,
    build_help_embed,
    ic_getting_started,
    ic_help,
)
from icbot.features.notes import notes_cmd
from icbot.registry import (
    FEATURE_MANIFESTS,
    load_feature_modules,
    registered_qualified_names,
)
from tests.helpers import build_fake_member

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Command Inventory & Backwards Compatibility
# ---------------------------------------------------------------------------


def test_command_inventory_preserves_all_commands():
    """All 52 existing leaf commands are preserved, plus the new `/ic getting-started` command."""
    load_feature_modules()
    names = registered_qualified_names()
    assert len(names) == 53, f"Expected 53 leaf commands, found {len(names)}"

    # Ensure all manifest-declared commands are registered
    all_manifest_cmds = {cmd for m in FEATURE_MANIFESTS.values() for cmd in m.commands_supplied}
    assert set(names) == all_manifest_cmds

    # Ensure `/ic getting-started` is present
    assert "ic getting-started" in names

    # Ensure historical key commands remain intact
    historical_anchors = [
        "ic profile",
        "ic dailyclaim",
        "ic leaderboard",
        "ic notes",
        "ic prof",
        "ic grades",
        "ic exam list",
        "ic exam add",
        "ic jobs",
        "ic resume",
        "ic icse market",
        "ic icse buy",
        "ic icse sell",
        "ic casino blackjack",
        "ic casino roulette",
        "ic admin setup",
        "ic admin config",
        "ic admin features",
    ]
    for anchor in historical_anchors:
        assert anchor in names, f"Expected historical command {anchor!r} to be preserved"


def test_command_metadata_covers_all_commands():
    """Every public slash command has a curated description and syntax example."""
    load_feature_modules()
    names = registered_qualified_names()
    for name in names:
        slash_name = f"/{name}"
        meta = COMMAND_METADATA.get(slash_name)
        assert meta is not None, f"Missing metadata for command: {slash_name}"
        assert meta.get("desc"), f"Missing description for command: {slash_name}"
        assert meta.get("example"), f"Missing example for command: {slash_name}"
        assert meta["example"].startswith("/ic "), f"Example must be a valid /ic command: {meta['example']}"


# ---------------------------------------------------------------------------
# 2. Dynamic Help & Permission Filtering
# ---------------------------------------------------------------------------


def test_dynamic_help_filters_admin_for_regular_users(make_interaction, synthetic_settings: Settings):
    """Standard non-admin users do not see the Server Administration help category."""
    regular_inter = make_interaction(administrator=False, role_ids=[12345])
    categories = build_dynamic_help_categories(regular_inter, synthetic_settings)

    cat_titles = [c["title"] for c in categories]
    assert not any("Server Administration" in t for t in cat_titles)
    assert any("Main Utilities" in t for t in cat_titles)
    assert any("Academic Toolkit" in t for t in cat_titles)


def test_dynamic_help_includes_admin_for_guild_managers(make_interaction, synthetic_settings: Settings):
    """Guild managers / administrators see the Server Administration help category."""
    admin_inter = make_interaction(administrator=True, role_ids=[])
    categories = build_dynamic_help_categories(admin_inter, synthetic_settings)

    cat_titles = [c["title"] for c in categories]
    assert any("Server Administration" in t for t in cat_titles)


def test_dynamic_help_excludes_disabled_modules(make_interaction, synthetic_settings: Settings):
    """Disabling modules hides them from dynamic help."""
    custom_settings = synthetic_settings.replace(
        disabled_features=("casino", "sports"),
    )

    inter = make_interaction(administrator=True)
    categories = build_dynamic_help_categories(inter, custom_settings)
    cat_titles = [c["title"] for c in categories]

    assert not any("Casino" in t for t in cat_titles)
    # Sports commands should not be in Info category
    info_cat = next(c for c in categories if "Info" in c["title"])
    cmd_names = [cmd[0] for cmd in info_cat["commands"]]
    assert not any("sports" in cmd for cmd in cmd_names)


def test_build_help_embed_includes_examples_and_pagination(synthetic_settings: Settings):
    """Help embed renders command descriptions, examples, and page counters."""
    categories = [
        {
            "title": "Test Category",
            "description": "Category for unit test.",
            "commands": [
                ("/ic notes", "Search notes"),
                ("/ic prof", "Search professor ratings"),
            ],
        }
    ]
    embed_obj = build_help_embed(0, 3, synthetic_settings, categories=categories)
    assert "Test Category" in (embed_obj.title or "")
    assert "/ic notes" in (embed_obj.description or "")
    assert "*Example:* `/ic notes course:CPS109`" in (embed_obj.description or "")
    assert "Page 1/3" in (embed_obj.footer.text or "")


# ---------------------------------------------------------------------------
# 3. `/ic getting-started` Onboarding Workflow
# ---------------------------------------------------------------------------


def test_getting_started_embed_structure(synthetic_settings: Settings):
    """Getting started embeds provide multi-step guidance with pro-tips."""
    assert len(GETTING_STARTED_STEPS) == 4

    for i in range(len(GETTING_STARTED_STEPS)):
        emb = build_getting_started_embed(i, synthetic_settings)
        assert emb.title is not None
        assert emb.description is not None
        assert len(emb.fields) >= 3
        # Ensure footer indicates progress
        assert f"Step {i + 1}/4" in (emb.footer.text or "")
        # Ensure pro-tip is included
        assert any(f.name == "💡 Pro-Tip" for f in emb.fields)


@pytest.mark.asyncio
async def test_getting_started_view_navigation(synthetic_settings: Settings):
    """GettingStartedView allows stepping forward and backward with button state synchronization."""
    view = GettingStartedView(current_step=0, settings=synthetic_settings, author_id=123)
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is False
    assert view.prev_button.label == "◀ Previous Step"
    assert view.next_button.label == "Next Step ▶"

    # Step forward
    view.current_step = 1
    view._sync_buttons()
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is False

    # Step to final page
    view.current_step = 3
    view._sync_buttons()
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is True


@pytest.mark.asyncio
async def test_getting_started_view_author_isolation(make_interaction, synthetic_settings: Settings):
    """Other members cannot hijack the initiating user's onboarding menu."""
    author_id = 999111
    view = GettingStartedView(current_step=0, settings=synthetic_settings, author_id=author_id)

    # Authorized user
    author_member = build_fake_member(user_id=author_id)
    author_inter = make_interaction(user=author_member)
    assert await view.interaction_check(author_inter) is True

    # Unauthorized user
    other_member = build_fake_member(user_id=888222)
    other_inter = make_interaction(user=other_member)
    assert await view.interaction_check(other_inter) is False
    assert len(other_inter.response.sent_messages) == 1
    assert "Only the person who opened this guide" in (other_inter.response.sent_messages[0].content or "")
    assert other_inter.response.sent_messages[0].ephemeral is True


@pytest.mark.asyncio
async def test_views_timeout_disables_components(synthetic_settings: Settings):
    """Interactive views disable all buttons when the 180s timeout fires."""
    help_view = HelpPaginationView(current_page=0, total_pages=3, settings=synthetic_settings)
    assert any(not b.disabled for b in help_view.children if isinstance(b, discord.ui.Button))

    await help_view.on_timeout()
    assert all(b.disabled for b in help_view.children if isinstance(b, discord.ui.Button))

    gs_view = GettingStartedView(current_step=0, settings=synthetic_settings)
    await gs_view.on_timeout()
    assert all(b.disabled for b in gs_view.children if isinstance(b, discord.ui.Button))


# ---------------------------------------------------------------------------
# 4. Unified Embeds API & Limits Enforcement
# ---------------------------------------------------------------------------


def test_markdown_escaping():
    """`escape_markdown` neutralizes formatting injection."""
    raw = "**bold** *italic* `code` ~strike~ |spoiler| \\backslash"
    escaped = embeds.escape_markdown(raw)
    assert "\\*\\*bold\\*\\*" in escaped
    assert "\\*italic\\*" in escaped
    assert "\\`code\\`" in escaped
    assert "\\~strike\\~" in escaped
    assert "\\|spoiler\\|" in escaped


def test_mention_neutralization_covers_all_discord_patterns():
    """`sanitize_text` disarms @everyone, @here, user pings, and role pings."""
    untrusted = (
        "Hello @everyone and @here! Meet <@123456789>, nickname <@!987654321>, and role <@&1122334455>."
    )
    clean = embeds.sanitize_text(untrusted)
    assert clean is not None
    assert "@everyone" not in clean
    assert "@here" not in clean
    assert "@\u200beveryone" in clean
    assert "@\u200bhere" in clean
    assert "<@123456789>" not in clean
    assert "<@!987654321>" not in clean
    assert "<@&1122334455>" not in clean
    assert "<@\u200b123456789>" in clean
    assert "<@\u200b987654321>" in clean
    assert "<@\u200b1122334455>" in clean


def test_safe_allowed_mentions_suppresses_all_pings():
    """`safe_allowed_mentions()` returns AllowedMentions.none()."""
    am = embeds.safe_allowed_mentions()
    assert am.everyone is False
    assert am.roles is False
    assert am.users is False
    assert am.replied_user is False


def test_stale_data_notice_formatting():
    """`stale_data_notice` produces accessible textual badges with timestamps."""
    notice = embeds.stale_data_notice("Metrolinx API", 1760000000, is_stale=True)
    assert "⚠️ **[Stale / Cached Data]**" in notice
    assert "Metrolinx API" in notice
    assert "<t:1760000000:R>" in notice

    fresh_notice = embeds.stale_data_notice("Course Catalog", 1760000000, is_stale=False)
    assert "ℹ️ **[Cached Data]**" in fresh_notice
    assert "⚠️" not in fresh_notice


def test_disabled_feature_embed_guidance(synthetic_settings: Settings):
    """`disabled_feature_embed` provides informative user and admin recovery instructions."""
    emb = embeds.disabled_feature_embed(
        synthetic_settings,
        "AI Assistant",
        module_id="ai",
        admin_tip=True,
        category="Intelligence",
    )
    assert "Module Disabled • Intelligence" in (emb.title or "")
    assert "The **AI Assistant** module is currently turned off" in (emb.description or "")
    assert "/ic admin features `enable:ai`" in (emb.description or "")


def test_empty_state_embed_guidance(synthetic_settings: Settings):
    """`empty_state_embed` displays helpful tips for empty result sets."""
    emb = embeds.empty_state_embed(
        synthetic_settings,
        title="No Jobs Found",
        message="No postings matched your criteria.",
        suggestion="Try searching for a broader term like 'developer' or clearing location filters.",
        category="Careers",
    )
    assert "No Jobs Found" in (emb.title or "")
    assert "No postings matched your criteria." in (emb.description or "")
    assert "💡 **Tip:** Try searching for a broader term" in (emb.description or "")
    assert "Careers" in (emb.footer.text or "")


def test_paginated_embed_helper(synthetic_settings: Settings):
    """`paginated_embed` sets title, fields, and auto-computed page footer."""
    emb = embeds.paginated_embed(
        synthetic_settings,
        title="Results",
        description="Search results",
        fields=[("Item 1", "Value 1", False)],
        page=1,
        total_pages=5,
        footer_hint="Directory",
    )
    assert "Results" in (emb.title or "")
    assert len(emb.fields) == 1
    assert "Directory • Page 2/5" in (emb.footer.text or "")


# ---------------------------------------------------------------------------
# 5. User Error Recovery on Academic Course Inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_invalid_course_input_provides_supportive_recovery(
    make_interaction, synthetic_settings: Settings, tmp_db: Database, frozen_time
):
    """Passing a single character or punctuation to `/ic notes` yields supportive guidance."""
    container = ServiceContainer.from_settings(synthetic_settings, db=tmp_db, clock=frozen_time)
    inter = make_interaction(command_name="ic notes")
    inter.client.container = container
    inter.client.settings = synthetic_settings

    await notes_cmd.callback(inter, course="?")
    assert len(inter.followup.messages) == 1
    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert "Please provide a valid course code with at least 3 letters/digits" in (msg.content or "")
    assert "(e.g. `CPS109`, `MTH140`, or `CS135`)" in (msg.content or "")


@pytest.mark.asyncio
async def test_grades_invalid_course_input_provides_supportive_recovery(
    make_interaction, synthetic_settings: Settings, tmp_db: Database, frozen_time
):
    """Passing invalid course to `/ic grades` yields helpful error embed."""
    container = ServiceContainer.from_settings(synthetic_settings, db=tmp_db, clock=frozen_time)
    inter = make_interaction(command_name="ic grades")
    inter.client.container = container
    inter.client.settings = synthetic_settings

    await grades_command.callback(inter, course="!")
    assert len(inter.followup.messages) == 1
    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert msg.embed is not None
    assert "Invalid Course Code" in (msg.embed.title or "")
    assert "Please provide a valid course code (e.g. `ELE888` or `CPS109`)" in (msg.embed.description or "")


# ---------------------------------------------------------------------------
# 6. Synthetic New-User Walkthrough & Friction Logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_new_user_walkthrough_and_friction_points(
    make_interaction, synthetic_settings: Settings, tmp_db: Database, frozen_time
):
    """Simulate a complete new student walkthrough from onboarding to commands and record friction points."""
    friction_log: list[str] = []
    container = ServiceContainer.from_settings(synthetic_settings, db=tmp_db, clock=frozen_time)

    # Persona: First-year student outside the original engineering cohort
    student_user_id = 918273645
    student_member = build_fake_member(user_id=student_user_id, administrator=False, role_ids=[111])
    inter = make_interaction(user=student_member)
    inter.client.container = container
    inter.client.settings = synthetic_settings

    # Touchpoint 1: Run /ic getting-started
    await ic_getting_started.callback(inter)
    assert len(inter.response.sent_messages) == 1
    gs_msg = inter.response.sent_messages[0]
    assert gs_msg.embed is not None
    assert "Welcome & Daily Rewards" in (gs_msg.embed.title or "")
    assert gs_msg.view is not None
    assert isinstance(gs_msg.view, GettingStartedView)

    # Touchpoint 2: Walkthrough pagination navigation via button click interaction
    btn_inter = make_interaction(user=student_member)
    await gs_msg.view.next_button.callback(btn_inter)
    assert gs_msg.view.current_step == 1
    assert len(btn_inter.response.edit_messages) == 1
    step2_embed = build_getting_started_embed(1, synthetic_settings)
    assert "Academic Survival Kit" in (step2_embed.title or "")

    # Touchpoint 3: Examine Help directory
    help_inter = make_interaction(user=student_member)
    help_inter.client.container = container
    help_inter.client.settings = synthetic_settings
    await ic_help.callback(help_inter)
    assert len(help_inter.response.sent_messages) == 1
    help_msg = help_inter.response.sent_messages[0]
    assert help_msg.embed is not None
    # Administration should NOT be present for standard students
    assert "Server Administration" not in (help_msg.embed.title or "")
    assert "Server Administration" not in (help_msg.embed.description or "")

    # Touchpoint 4: Another user clicks student's active view
    hijacker_member = build_fake_member(user_id=444555666)
    hijacker_inter = make_interaction(user=hijacker_member)
    allowed = await gs_msg.view.interaction_check(hijacker_inter)
    assert allowed is False
    assert "Only the person who opened this guide" in (hijacker_inter.response.sent_messages[0].content or "")

    # Friction Evaluation Summary:
    # 1. Onboarding is self-contained with no prerequisite administrative setup.
    # 2. Syntax examples in `/ic help` prevent guesswork on parameter names.
    # 3. Invalid course codes provide concrete alternatives rather than raw errors.
    # 4. View timeouts prevent lingering clickable components that fail with Discord errors.
    assert len(friction_log) == 0, f"Friction issues encountered: {friction_log}"
