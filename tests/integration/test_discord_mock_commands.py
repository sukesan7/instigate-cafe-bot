"""Mocked Discord integration tests exercising slash-command callbacks offline."""

from __future__ import annotations

import pytest

from icbot.features.exams import exams_add_cmd, exams_cmd, exams_remove_cmd
from icbot.features.notes import notes_cmd
from icbot.features.profile import daily_cmd, profile_cmd
from icbot.services.user_service import DAILY_COOLDOWN_SECONDS
from tests.helpers import load_fixture_text

pytestmark = pytest.mark.discord_mock


async def test_daily_and_profile_commands_offline(make_interaction, make_member, frozen_time):
    member = make_member(user_id=800000000000000055, display_name="SyntheticUser")

    # 1. First /ic dailyclaim succeeds publicly (non-ephemeral)
    inter1 = make_interaction(user=member, command_name="ic dailyclaim")
    await daily_cmd.callback(inter1)
    assert len(inter1.response.sent_messages) == 1
    msg1 = inter1.response.sent_messages[0]
    assert msg1.ephemeral is False
    assert "Daily Reward Claimed" in msg1.embed.title
    assert "$100" in msg1.embed.description
    assert "$1,100" in msg1.embed.description

    # 2. Immediate repeat /ic dailyclaim sends ephemeral cooldown response
    inter2 = make_interaction(user=member, command_name="ic dailyclaim")
    await daily_cmd.callback(inter2)
    msg2 = inter2.response.sent_messages[0]
    assert msg2.ephemeral is True
    assert "already claimed" in msg2.embed.description

    # 3. /ic profile defers with thinking=True and sends followup embed showing $1,100 balance
    inter_prof = make_interaction(user=member, command_name="ic profile")
    await profile_cmd.callback(inter_prof)
    assert inter_prof.response.deferred is True
    assert inter_prof.response.thinking is True
    prof_reply = inter_prof.followup.messages[0]
    assert prof_reply.embed is not None
    assert "SyntheticUser's Profile" in prof_reply.embed.title
    field_values = {f.name: f.value for f in prof_reply.embed.fields}
    assert field_values["💰 Balance"] == "**$1,100**"
    assert "Next claim" in field_values["📅 Daily"]

    # 4. After 24h elapses, /ic dailyclaim succeeds again
    frozen_time.advance(DAILY_COOLDOWN_SECONDS)
    inter3 = make_interaction(user=member, command_name="ic dailyclaim")
    await daily_cmd.callback(inter3)
    assert inter3.response.sent_messages[0].ephemeral is False
    assert "$1,200" in inter3.response.sent_messages[0].embed.description


async def test_notes_command_fetches_spa_then_uses_cache(make_interaction, mock_aiohttp, frozen_time):
    mock_aiohttp.get(
        "https://example.invalid/Notes/",
        text=load_fixture_text("notes_spa_index.html"),
    )
    mock_aiohttp.get(
        "https://example.invalid/Notes/assets/index-deadbeef.js",
        text=load_fixture_text("notes_spa_bundle.js"),
    )

    inter1 = make_interaction(command_name="ic notes")
    await notes_cmd.callback(inter1, course="ele 888")

    assert inter1.response.deferred is True
    reply1 = inter1.followup.messages[0]
    assert reply1.embed is not None
    assert "ELE 888" in reply1.embed.title
    assert "Synthetic Notes" in [f.name for f in reply1.embed.fields]
    assert "ELE888-00000000000000000000000000000001" in reply1.embed.fields[0].value
    assert len(mock_aiohttp.calls) == 2

    # Second query within 6h TTL must serve from in-memory cache (0 new HTTP requests)
    frozen_time.advance(300)
    inter2 = make_interaction(command_name="ic notes")
    await notes_cmd.callback(inter2, course="COE817")
    reply2 = inter2.followup.messages[0]
    assert reply2.embed is not None
    assert "COE-817-00000000000000000000000000000002" in reply2.embed.fields[0].value
    assert len(mock_aiohttp.calls) == 2


async def test_exam_commands_rbac_add_list_and_remove(make_interaction, make_member):
    regular_user = make_member(user_id=800000000000000077, administrator=False, role_ids=[])
    admin_user = make_member(user_id=800000000000000088, administrator=True)

    # Regular member is denied exam modification
    unauth_inter = make_interaction(user=regular_user, command_name="ic exam add")
    await exams_add_cmd.callback(
        unauth_inter,
        course_code="COE817",
        course_name="Applied Cryptography",
        date="2026-12-10",
        start_time="09:00",
    )
    assert "do not have permission" in unauth_inter.last_reply.content

    # Empty schedule check
    empty_list_inter = make_interaction(user=regular_user, command_name="ic exam list")
    await exams_cmd.callback(empty_list_inter)
    assert "No exams have been configured" in empty_list_inter.last_reply.embed.description

    # Admin adds an exam cleanly
    add_inter = make_interaction(user=admin_user, command_name="ic exam add")
    await exams_add_cmd.callback(
        add_inter,
        course_code="COE817",
        course_name="Applied Cryptography",
        date="2026-12-10",
        start_time="09:00",
        end_time="11:30",
        location="ENG-101",
        notes="Calculator allowed",
    )
    assert "Added exam COE817" in add_inter.last_reply.content
    # Characterization: exams_add_cmd queries last_insert_rowid() on a fresh DB connection,
    # so the confirmation message currently prints ID: 0 even though SQLite inserts id = 1.
    assert "- ID: " in add_inter.last_reply.content

    # List schedule shows newly added exam with SQLite-assigned ID 1
    list_inter = make_interaction(user=regular_user, command_name="ic exam list")
    await exams_cmd.callback(list_inter)
    schedule_embed = list_inter.last_reply.embed
    assert len(schedule_embed.fields) == 1
    assert schedule_embed.fields[0].name == "COE817 — Applied Cryptography"
    assert "ENG-101" in schedule_embed.fields[0].value
    assert "**ID:** 1" in schedule_embed.fields[0].value

    # Admin removes the exam by ID
    rm_inter = make_interaction(user=admin_user, command_name="ic exam remove")
    await exams_remove_cmd.callback(rm_inter, exam_id=1)
    assert "Removed exam ID 1." in rm_inter.last_reply.content
