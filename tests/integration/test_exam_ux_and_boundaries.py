"""Integration tests for exam scheduling UX, concurrency, DST/leap days, CSV/ICS, boundaries, and reminders."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from icbot.academics import (
    Course,
    ExamStore,
    ParsedExamDraft,
    compute_due_exam_reminders,
    mark_exam_reminder_delivered,
    parse_exam_datetime,
)
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.exams import (
    ExamClearConfirmView,
    ExamPaginationView,
    _exam_course_autocomplete,
    build_exam_schedule_embed,
    exams_add_cmd,
    exams_clear_cmd,
    exams_cmd,
    exams_export_cmd,
    exams_import_cmd,
    exams_remove_cmd,
)
from tests.helpers import FakeClient, FrozenClock

pytestmark = [pytest.mark.storage, pytest.mark.discord_mock]


async def test_two_guild_two_major_boundaries_filters_visibility_and_autocomplete(
    make_interaction,
    make_member,
    tmp_path: Path,
):
    """Two-guild, two-major test: guild isolation, cross-guild admin boundary, visibility, filters, autocomplete."""
    db = Database(str(tmp_path / "two_guilds_exams.db"))
    await db.init()

    base_settings = Settings(db_path=str(tmp_path / "two_guilds_exams.db"), guild_id=0)
    container = ServiceContainer.from_settings(base_settings, db=db, clock=lambda: 1796000000.0)

    guild_a = SimpleNamespace(id=910000000000000001, name="TMU Engineering Guild")
    guild_b = SimpleNamespace(id=910000000000000002, name="MLU Humanities & CS Guild")

    admin_a = make_member(user_id=810000000000000001, administrator=True)
    admin_b = make_member(user_id=810000000000000002, administrator=True)
    student_a = make_member(user_id=810000000000000003, administrator=False, role_ids=[])

    # Set distinct campus timezone override on Guild B
    await container.guild_settings.update_overrides(
        guild_b.id,
        {"exams_tz": "America/Vancouver", "rmp_university": "Maple Leaf University"},
        actor_id=admin_b.id,
    )

    try:
        seed_inter = make_interaction(user=admin_a, guild=guild_a, command_name="ic exam add")
        client = seed_inter.client
        client.settings = base_settings
        client.db = db
        client.services = container

        # Seed catalog course in Guild A for autocomplete discovery
        await container.academic_catalog.upsert_course(
            guild_a.id, Course.from_code("COE817", title="Applied Cryptography")
        )

        # Guild A (Engineering): add public guild exam + private admin-only exam + past exam
        inter_a1 = make_interaction(user=admin_a, guild=guild_a, command_name="ic exam add", client=client)
        await exams_add_cmd.callback(
            inter_a1,
            course_code="ELE888",
            course_name="Intelligent Systems",
            date="2026-12-12",
            start_time="09:00",
            end_time="11:30",
            location="ENG-101",
            term="Fall 2026",
            visibility="guild",
        )
        m_a1 = re.search(r"- ID:\s*(\d+)", inter_a1.last_reply.content or "")
        assert m_a1 is not None
        exam_a1_id = int(m_a1.group(1))

        # Private exam in Guild A
        inter_a_priv = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam add", client=client
        )
        await exams_add_cmd.callback(
            inter_a_priv,
            course_code="ELE888",
            course_name="Accommodation Sitting",
            date="2026-12-13",
            start_time="10:00",
            term="Fall 2026",
            visibility="private",
        )

        # Past exam in Guild A (to test upcoming-first sorting!)
        inter_a_past = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam add", client=client
        )
        await exams_add_cmd.callback(
            inter_a_past,
            course_code="ELE532",
            course_name="Signals Midterm",
            date="2026-10-15",
            start_time="09:00",
            term="Fall 2026",
        )

        # Guild B (Humanities & CS): add HIST-2010 and CS 135 (all-day)
        inter_b1 = make_interaction(user=admin_b, guild=guild_b, command_name="ic exam add", client=client)
        await exams_add_cmd.callback(
            inter_b1,
            course_code="HIST-2010",
            course_name="Modern Canadian History",
            date="2026-12-14",
            start_time="14:00",
            end_time="16:00",
            term="Winter 2027",
        )
        m_b1 = re.search(r"- ID:\s*(\d+)", inter_b1.last_reply.content or "")
        assert m_b1 is not None
        exam_b1_id = int(m_b1.group(1))

        inter_b2 = make_interaction(user=admin_b, guild=guild_b, command_name="ic exam add", client=client)
        await exams_add_cmd.callback(
            inter_b2,
            course_code="CS 135",
            course_name="Take-Home Portfolio",
            date="2028-02-29",  # Leap day!
            all_day=True,
            term="Winter 2027",
        )
        assert "Added exam CS 135" in (inter_b2.last_reply.content or "")

        # 1. Upcoming-first sorting & visibility enforcement for regular student in Guild A
        list_student_a = make_interaction(
            user=student_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(list_student_a)
        assert list_student_a.response.deferred is True
        assert list_student_a.response.ephemeral is False
        assert list_student_a.followup.messages[0].ephemeral is False
        emb_a = list_student_a.followup.messages[0].embed
        assert emb_a is not None
        # Private exam is hidden from student_a; upcoming ELE888 appears BEFORE past ELE532
        field_names_a = [f.name for f in emb_a.fields]
        assert len(field_names_a) == 2
        assert field_names_a[0].startswith("ELE888")
        assert field_names_a[1].startswith("ELE532")
        assert "<t:" in emb_a.fields[0].value  # Discord timestamp markup present
        assert "Added by:" in emb_a.fields[0].value  # Creator attribution present
        assert "Accommodation Sitting" not in str(emb_a.to_dict())

        # Admin in Guild A sees the private exam too — and the response MUST be ephemeral (decided before defer)
        list_admin_a = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(list_admin_a)
        assert list_admin_a.response.deferred is True
        assert list_admin_a.response.ephemeral is True
        assert list_admin_a.followup.messages[0].ephemeral is True
        assert len(list_admin_a.followup.messages[0].embed.fields) == 3

        # Course & upcoming_only filter in Guild A
        list_filtered = make_interaction(
            user=student_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(list_filtered, course="ELE888", term="Fall 2026", upcoming_only=True)
        assert list_filtered.response.ephemeral is False
        assert list_filtered.followup.messages[0].ephemeral is False
        assert len(list_filtered.followup.messages[0].embed.fields) == 1

        # 2. Cross-guild admin boundary: Admin A cannot remove Guild B's exam ID!
        cross_rm = make_interaction(user=admin_a, guild=guild_a, command_name="ic exam remove", client=client)
        await exams_remove_cmd.callback(cross_rm, exam_id=exam_b1_id)
        assert "no exam found" in (cross_rm.last_reply.content or "")

        # Nonexistent ID also rejected cleanly
        missing_rm = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam remove", client=client
        )
        await exams_remove_cmd.callback(missing_rm, exam_id=999999)
        assert "no exam found" in (missing_rm.last_reply.content or "")

        # Guild B still has both exams intact (and in America/Vancouver timezone)
        list_b = make_interaction(user=admin_b, guild=guild_b, command_name="ic exam list", client=client)
        await exams_cmd.callback(list_b)
        emb_b = list_b.followup.messages[0].embed
        assert emb_b is not None
        assert "America/Vancouver" in (emb_b.description or "")
        assert len(emb_b.fields) == 2

        # 3. Autocomplete is strictly guild-scoped (includes both exams and academic_courses catalog)
        ac_inter_a = make_interaction(user=student_a, guild=guild_a, client=client)
        choices_a = await _exam_course_autocomplete(ac_inter_a, "8")
        vals_a = {c.value for c in choices_a}
        assert "ELE888" in vals_a
        assert "COE817" in vals_a
        assert "HIST-2010" not in vals_a

        # Confirm Guild A's exam still exists
        assert exam_a1_id >= 1
    finally:
        await container.close()
        await db.close()


async def test_simultaneous_admin_edits_and_overlapping_exams_same_transaction_ids(
    make_interaction,
    make_member,
):
    """Concurrent admin adds yield unique same-transaction IDs and detect overlapping exams cleanly."""
    admin1 = make_member(user_id=820000000000000001, administrator=True)
    admin2 = make_member(user_id=820000000000000002, administrator=True)
    seed = make_interaction(user=admin1, command_name="ic exam add")
    client = seed.client

    async def _add(admin, code: str, title: str, start: str, end: str) -> tuple[int, str]:
        inter = make_interaction(user=admin, command_name="ic exam add", client=client)
        await exams_add_cmd.callback(
            inter,
            course_code=code,
            course_name=title,
            date="2026-12-16",
            start_time=start,
            end_time=end,
        )
        reply = inter.last_reply.content or ""
        m = re.search(r"- ID:\s*(\d+)", reply)
        assert m is not None, f"Missing ID in reply: {reply}"
        return int(m.group(1)), reply

    # Fire 6 concurrent admin adds (including overlapping times across different courses)
    results = await asyncio.gather(
        _add(admin1, "COE817", "Crypto", "09:00", "11:00"),
        _add(admin2, "ELE888", "AI Systems", "10:00", "12:00"),
        _add(admin1, "CPS530", "Web Systems", "10:30", "12:30"),
        _add(admin2, "MTH410", "Statistics", "14:00", "16:00"),
        _add(admin1, "PHY101", "Physics", "14:30", "16:30"),
        _add(admin2, "CHY102", "Chemistry", "18:00", "20:00"),
    )

    ids = [r[0] for r in results]
    assert len(set(ids)) == 6  # Every concurrent insert received its own unique same-transaction ID!
    # At least one of the overlapping pairs must emit the non-blocking overlap warning
    assert any("overlaps with existing exam" in r[1] for r in results)

    # Attempting to add an exact duplicate (same course + start_ts) is rejected cleanly
    dup_inter = make_interaction(user=admin1, command_name="ic exam add", client=client)
    await exams_add_cmd.callback(
        dup_inter,
        course_code="COE817",
        course_name="Crypto Duplicate",
        date="2026-12-16",
        start_time="09:00",
        end_time="11:00",
    )
    assert "Duplicate exam already exists" in (dup_inter.last_reply.content or "")


async def test_dst_validation_in_exam_add_command(make_interaction, make_member):
    """Verify /ic exam add rejects DST spring-forward gaps & unresolved fall-back ambiguity, then accepts explicit fold."""
    admin = make_member(user_id=830000000000000001, administrator=True)
    seed = make_interaction(user=admin, command_name="ic exam add")
    client = seed.client

    # 1. Spring-forward nonexistent time (America/Toronto 2026-03-08 02:30)
    inter_gap = make_interaction(user=admin, command_name="ic exam add", client=client)
    await exams_add_cmd.callback(
        inter_gap,
        course_code="ELE888",
        course_name="DST Gap Test",
        date="2026-03-08",
        start_time="02:30",
    )
    assert "spring-forward" in (inter_gap.last_reply.content or "")

    # 2. Fall-back ambiguous time rejected by default (2026-11-01 01:30)
    inter_amb = make_interaction(user=admin, command_name="ic exam add", client=client)
    await exams_add_cmd.callback(
        inter_amb,
        course_code="ELE888",
        course_name="DST Ambiguous Test",
        date="2026-11-01",
        start_time="01:30",
    )
    assert "ambiguous" in (inter_amb.last_reply.content or "")

    # 3. Explicit dst_resolution="later" succeeds cleanly
    inter_ok = make_interaction(user=admin, command_name="ic exam add", client=client)
    await exams_add_cmd.callback(
        inter_ok,
        course_code="ELE888",
        course_name="DST Resolved Test",
        date="2026-11-01",
        start_time="01:30",
        dst_resolution="later",
    )
    assert "Added exam ELE888" in (inter_ok.last_reply.content or "")


async def test_csv_ics_import_export_preserves_manual_exams_and_deduplicates(
    make_interaction,
    make_member,
):
    """Import/export preserves manual exams, deduplicates repeat imports, and enforces RBAC + clear confirmation."""
    admin1 = make_member(user_id=840000000000000001, administrator=True)
    admin2 = make_member(user_id=840000000000000002, administrator=True)
    regular = make_member(user_id=840000000000000003, administrator=False, role_ids=[])

    # 1. Create a manual exam first
    seed = make_interaction(user=admin1, command_name="ic exam add")
    client = seed.client
    await exams_add_cmd.callback(
        seed,
        course_code="MAN101",
        course_name="Manually Entered Exam",
        date="2026-12-10",
        start_time="09:00",
        end_time="11:00",
    )
    assert "Added exam MAN101" in (seed.last_reply.content or "")

    # 2. Regular member is blocked from importing
    unauth_imp = make_interaction(user=regular, command_name="ic exam import", client=client)
    await exams_import_cmd.callback(
        unauth_imp,
        fmt="csv",
        data="course_code,course_name,date,start_time\nCS101,Intro,2026-12-11,10:00\n",
    )
    assert "do not have permission" in (unauth_imp.last_reply.content or "")

    # 3. Admin imports CSV with 2 valid rows + 1 invalid leap-day row
    csv_payload = (
        "course_code,course_name,date,start_time,end_time,location,term,uid\n"
        "CSV201,Algorithms,2026-12-11,10:00,12:00,Hall A,Fall 2026,uid-csv-201\n"
        "CSV202,Databases,2026-12-12,13:00,15:00,Hall B,Fall 2026,uid-csv-202\n"
        "BAD999,Bad Leap,2026-02-29,09:00,11:00,Hall C,Fall 2026,uid-bad\n"
    )
    imp1 = make_interaction(user=admin1, command_name="ic exam import", client=client)
    await exams_import_cmd.callback(imp1, fmt="csv", data=csv_payload)
    emb1 = imp1.followup.messages[0].embed
    assert emb1 is not None
    assert "Imported **2** exam(s)" in (emb1.description or "")
    assert "Existing manual exams preserved: **1**" in (emb1.description or "")
    assert "Validation errors (1)" in (emb1.description or "")

    # 4. Duplicate re-import skips both cleanly and keeps manual exam intact
    imp2 = make_interaction(user=admin1, command_name="ic exam import", client=client)
    await exams_import_cmd.callback(imp2, fmt="csv", data=csv_payload)
    emb2 = imp2.followup.messages[0].embed
    assert emb2 is not None
    assert "Imported **0** exam(s)" in (emb2.description or "")
    assert "Skipped duplicates: **2**" in (emb2.description or "")

    # 5. Export as ICS and verify file attachment contains all 3 exams (manual + 2 imported)
    exp_inter = make_interaction(user=regular, command_name="ic exam export", client=client)
    await exams_export_cmd.callback(exp_inter, fmt="ics")
    exp_msg = exp_inter.followup.messages[0]
    assert exp_msg.file is not None
    ics_bytes = exp_msg.file.fp.read().decode("utf-8")
    assert "MAN101 - Manually Entered Exam" in ics_bytes
    assert "CSV201 - Algorithms" in ics_bytes
    assert "CSV202 - Databases" in ics_bytes

    # 6. Clear with preserve_manual=True shows ExamClearConfirmView; other admin cannot click confirm
    clear_inter = make_interaction(user=admin1, command_name="ic exam clear", client=client)
    await exams_clear_cmd.callback(clear_inter, preserve_manual=True)
    view = clear_inter.followup.messages[0].view
    assert isinstance(view, ExamClearConfirmView)

    # Admin2 is rejected from confirming Admin1's destructive action
    wrong_actor_inter = make_interaction(user=admin2, client=client)
    await view.confirm_button.callback(wrong_actor_inter)
    assert "only the administrator who initiated" in (wrong_actor_inter.last_reply.content or "")

    # Admin1 confirms: removes the 2 imported exams while preserving MAN101
    confirm_inter = make_interaction(user=admin1, client=client)
    await view.confirm_button.callback(confirm_inter)
    assert "manual exams preserved" in (confirm_inter.last_reply.content or "")
    assert "2 removed" in (confirm_inter.last_reply.content or "")

    list_after = make_interaction(user=regular, command_name="ic exam list", client=client)
    await exams_cmd.callback(list_after)
    fields_after = list_after.followup.messages[0].embed.fields
    assert len(fields_after) == 1
    assert fields_after[0].name.startswith("MAN101")


async def test_deadline_reminders_opt_in_only_and_deduplicated(tmp_path: Path):
    """Deadline reminders fire ONLY when admin-enabled with a channel, exclude private exams, and deduplicate."""
    db = Database(str(tmp_path / "reminders.db"))
    await db.init()
    gid = 950000000000000001
    exam_start = parse_exam_datetime("2026-12-10", "14:00", "America/Toronto")
    now_ts = exam_start - (10 * 3600)  # 10 hours before exam -> inside 24h lead, outside 2h lead

    try:
        from icbot.academics import ExamStore, ParsedExamDraft

        store = ExamStore(db, clock=lambda: float(now_ts))
        await store.add_exam(
            guild_id=gid,
            draft=ParsedExamDraft(
                course_code="COE817",
                course_name="Public Guild Exam",
                start_ts=exam_start,
                end_ts=exam_start + 7200,
                visibility="guild",
            ),
            created_by=100,
        )
        await store.add_exam(
            guild_id=gid,
            draft=ParsedExamDraft(
                course_code="PRIV999",
                course_name="Private Exam",
                start_ts=exam_start,
                end_ts=exam_start + 7200,
                visibility="private",
            ),
            created_by=100,
        )

        # 1. Default settings (disabled) -> ZERO reminders
        s_off = Settings()
        assert await compute_due_exam_reminders(db, guild_id=gid, settings=s_off, now_ts=now_ts) == []

        # 2. Enabled without channel ID -> ZERO reminders
        s_no_chan = Settings(exam_reminders_enabled=True, exam_reminders_channel_id=0)
        assert await compute_due_exam_reminders(db, guild_id=gid, settings=s_no_chan, now_ts=now_ts) == []

        # 3. Admin-configured opt-in with channel -> fires 24h reminder for COE817 only (never PRIV999)
        s_on = Settings(
            exam_reminders_enabled=True,
            exam_reminders_channel_id=777000000000000001,
            exam_reminder_lead_hours=[24, 2],
        )
        due = await compute_due_exam_reminders(db, guild_id=gid, settings=s_on, now_ts=now_ts)
        assert len(due) == 1
        assert due[0].exam.course_code == "COE817"
        assert due[0].lead_hours == 24
        assert due[0].channel_id == 777000000000000001

        # 4. Mark delivered -> subsequent check is idempotently deduplicated
        await mark_exam_reminder_delivered(db, due[0], delivered_at=now_ts)
        assert await compute_due_exam_reminders(db, guild_id=gid, settings=s_on, now_ts=now_ts) == []

        # 5. Advance to 1 hour before exam -> 2h reminder now fires once
        later_ts = exam_start - 3600
        due_2h = await compute_due_exam_reminders(db, guild_id=gid, settings=s_on, now_ts=later_ts)
        assert len(due_2h) == 1
        assert due_2h[0].lead_hours == 2
    finally:
        await db.close()


async def test_exam_list_audience_classification_owner_member_admin_dm_and_second_guild(
    tmp_path: Path,
    make_interaction,
    make_member,
):
    """Verify ephemeral audience is decided before defer across owner, member, admin, DM/nonmember, and second guild."""
    db = Database(str(tmp_path / "exams_audience.db"))
    await db.init()

    guild_a = SimpleNamespace(id=911000000000000001, name="Guild A")
    guild_b = SimpleNamespace(id=911000000000000002, name="Guild B")

    settings = Settings(
        db_path=str(tmp_path / "exams_audience.db"),
        guild_id=guild_a.id,
        exams_tz="America/Toronto",
    )
    clock = FrozenClock(float(parse_exam_datetime("2026-11-15", "12:00", "America/Toronto")))
    container = ServiceContainer.from_settings(settings, db=db, clock=clock)
    client = FakeClient(settings=settings, db=db, services=container)

    admin_a = make_member(user_id=851000000000000001, display_name="AdminA", administrator=True)
    owner_a = make_member(user_id=851000000000000002, display_name="OwnerA", administrator=False)
    other_owner_a = make_member(user_id=851000000000000003, display_name="OtherOwnerA", administrator=False)
    member_a = make_member(user_id=851000000000000004, display_name="MemberA", administrator=False)
    admin_b = make_member(user_id=851000000000000005, display_name="AdminB", administrator=True)
    member_b = make_member(user_id=851000000000000006, display_name="MemberB", administrator=False)

    try:
        store = ExamStore(db, clock=clock)
        base_ts = parse_exam_datetime("2026-12-10", "09:00", "America/Toronto")

        # 1. Schedule with ONLY private exams in Guild B (created by admin_b)
        await store.add_exam(
            guild_id=guild_b.id,
            draft=ParsedExamDraft(
                course_code="PRIVB01",
                course_name="Guild B Confidential Sitting",
                start_ts=base_ts,
                end_ts=base_ts + 7200,
                location="Room B-Secret",
                notes="Accommodated Room B",
                visibility="private",
            ),
            created_by=admin_b.id,
            is_admin=True,
        )

        # Member in Guild B running /ic exam list on a private-only schedule sees empty public response
        inter_member_b = make_interaction(
            user=member_b, guild=guild_b, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_member_b)
        assert inter_member_b.response.deferred is True
        assert inter_member_b.response.ephemeral is False
        assert inter_member_b.followup.messages[0].ephemeral is False
        serialized_member_b = json.dumps(inter_member_b.followup.messages[0].embed.to_dict())
        assert "PRIVB01" not in serialized_member_b
        assert "Room B-Secret" not in serialized_member_b

        # Admin in Guild B running /ic exam list on a private-only schedule gets an EPHEMERAL response
        inter_admin_b = make_interaction(
            user=admin_b, guild=guild_b, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_admin_b)
        assert inter_admin_b.response.deferred is True
        assert inter_admin_b.response.ephemeral is True
        assert inter_admin_b.followup.messages[0].ephemeral is True
        serialized_admin_b = json.dumps(inter_admin_b.followup.messages[0].embed.to_dict())
        assert "PRIVB01" in serialized_admin_b

        # 2. Mixed schedule in Guild A: 1 guild exam, 1 public exam, 1 private by owner_a, 1 private by other_owner_a
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="CPS109",
                course_name="Computer Science I",
                start_ts=base_ts,
                end_ts=base_ts + 7200,
                location="KHN 201",
                visibility="guild",
            ),
            created_by=admin_a.id,
            is_admin=True,
        )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="MTH110",
                course_name="Discrete Math",
                start_ts=base_ts + 10800,
                end_ts=base_ts + 18000,
                location="ENG 103",
                visibility="public",
            ),
            created_by=admin_a.id,
            is_admin=True,
        )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="ACCOM101",
                course_name="OwnerA Private Accommodation",
                start_ts=base_ts + 21600,
                end_ts=base_ts + 28800,
                location="Access Center Pod 4",
                notes="Extra time 1.5x for OwnerA",
                visibility="private",
            ),
            created_by=owner_a.id,
        )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="ACCOM202",
                course_name="OtherOwnerA Private Sitting",
                start_ts=base_ts + 32400,
                end_ts=base_ts + 39600,
                location="Access Center Pod 9",
                notes="Private medical sitting",
                visibility="private",
            ),
            created_by=other_owner_a.id,
        )

        # 2a. Regular member in Guild A gets public response (ephemeral=False) with ONLY guild + public exams
        inter_member_a = make_interaction(
            user=member_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_member_a)
        assert inter_member_a.response.deferred is True
        assert inter_member_a.response.ephemeral is False
        assert inter_member_a.followup.messages[0].ephemeral is False
        emb_member_a_dict = inter_member_a.followup.messages[0].embed.to_dict()
        emb_member_a_json = json.dumps(emb_member_a_dict)
        assert len(emb_member_a_dict["fields"]) == 2
        assert "CPS109" in emb_member_a_json
        assert "MTH110" in emb_member_a_json
        assert "ACCOM101" not in emb_member_a_json
        assert "ACCOM202" not in emb_member_a_json
        assert "Access Center" not in emb_member_a_json
        assert "PRIVB01" not in emb_member_a_json

        # 2b. OwnerA gets EPHEMERAL response (ephemeral=True) containing guild + public + ACCOM101 (NOT ACCOM202)
        inter_owner_a = make_interaction(
            user=owner_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_owner_a)
        assert inter_owner_a.response.deferred is True
        assert inter_owner_a.response.ephemeral is True
        assert inter_owner_a.followup.messages[0].ephemeral is True
        emb_owner_a_dict = inter_owner_a.followup.messages[0].embed.to_dict()
        emb_owner_a_json = json.dumps(emb_owner_a_dict)
        assert len(emb_owner_a_dict["fields"]) == 3
        assert "CPS109" in emb_owner_a_json
        assert "MTH110" in emb_owner_a_json
        assert "ACCOM101" in emb_owner_a_json
        assert "ACCOM202" not in emb_owner_a_json
        assert "Pod 9" not in emb_owner_a_json

        # 2c. AdminA gets EPHEMERAL response (ephemeral=True) containing all 4 Guild A exams (never Guild B)
        inter_admin_a = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_admin_a)
        assert inter_admin_a.response.deferred is True
        assert inter_admin_a.response.ephemeral is True
        assert inter_admin_a.followup.messages[0].ephemeral is True
        emb_admin_a_dict = inter_admin_a.followup.messages[0].embed.to_dict()
        emb_admin_a_json = json.dumps(emb_admin_a_dict)
        assert len(emb_admin_a_dict["fields"]) == 4
        assert "ACCOM101" in emb_admin_a_json
        assert "ACCOM202" in emb_admin_a_json
        assert "PRIVB01" not in emb_admin_a_json

        # 2d. AdminA passing include_private=False gets a PUBLIC response (ephemeral=False) safe for channel sharing
        inter_admin_public = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_admin_public, include_private=False)
        assert inter_admin_public.response.deferred is True
        assert inter_admin_public.response.ephemeral is False
        assert inter_admin_public.followup.messages[0].ephemeral is False
        emb_admin_pub_dict = inter_admin_public.followup.messages[0].embed.to_dict()
        emb_admin_pub_json = json.dumps(emb_admin_pub_dict)
        assert len(emb_admin_pub_dict["fields"]) == 2
        assert "ACCOM101" not in emb_admin_pub_json
        assert "ACCOM202" not in emb_admin_pub_json

        # 2e. Defense-in-depth: build_exam_schedule_embed(..., allow_private=False) strips private exams
        all_a_exams = await store.list_exams(guild_a.id, viewer_id=admin_a.id, is_admin=True)
        assert any(x.visibility == "private" for x in all_a_exams)
        safe_embed = build_exam_schedule_embed(
            settings,
            all_a_exams,
            "America/Toronto",
            allow_private=False,
        )
        safe_embed_json = json.dumps(safe_embed.to_dict())
        assert "ACCOM101" not in safe_embed_json
        assert "ACCOM202" not in safe_embed_json

        # 2f. Autocomplete never leaks private course codes/names to regular members or admins
        ac_choices = await _exam_course_autocomplete(
            make_interaction(user=member_a, guild=guild_a, client=client),
            "ACCOM",
        )
        assert ac_choices == []

        # 2g. Non-member / DM interaction (interaction.guild = None) is rejected ephemerally without falling back to settings.guild_id
        dm_inter = make_interaction(user=admin_a, guild=guild_a, command_name="ic exam list", client=client)
        dm_inter.guild = None
        await exams_cmd.callback(dm_inter)
        assert dm_inter.response.deferred is True
        assert dm_inter.response.ephemeral is True
        assert dm_inter.followup.messages[0].ephemeral is True
        assert "only be used inside a Discord server" in (dm_inter.followup.messages[0].content or "")
        assert dm_inter.followup.messages[0].embed is None

        # Non-Member user object (discord.User instead of discord.Member) in a guild channel is also rejected ephemerally
        non_member_user = SimpleNamespace(id=851999999999999999, display_name="NonMember")
        assert not isinstance(non_member_user, discord.Member)
        non_member_inter = make_interaction(
            user=non_member_user, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(non_member_inter)
        assert non_member_inter.response.ephemeral is True
        assert non_member_inter.followup.messages[0].ephemeral is True
        assert "must be a server member" in (non_member_inter.followup.messages[0].content or "")
    finally:
        await container.close()
        await db.close()


async def test_exam_pagination_view_actor_guild_authorization_timeout_and_http_failure_resilience(
    tmp_path: Path,
    make_interaction,
    make_member,
):
    """Verify multi-page ExamPaginationView and ExamClearConfirmView enforce actor/guild checks, timeout, and HTTP resilience."""
    db = Database(str(tmp_path / "exams_pagination.db"))
    await db.init()

    guild_a = SimpleNamespace(id=912000000000000001, name="Guild A")
    guild_b = SimpleNamespace(id=912000000000000002, name="Guild B")
    settings = Settings(
        db_path=str(tmp_path / "exams_pagination.db"),
        guild_id=guild_a.id,
        exams_tz="America/Toronto",
    )
    clock = FrozenClock(float(parse_exam_datetime("2026-11-15", "12:00", "America/Toronto")))
    container = ServiceContainer.from_settings(settings, db=db, clock=clock)
    client = FakeClient(settings=settings, db=db, services=container)

    owner_a = make_member(user_id=852000000000000001, display_name="OwnerA", administrator=False)
    other_member_a = make_member(user_id=852000000000000002, display_name="OtherMemberA", administrator=False)
    admin_a = make_member(user_id=852000000000000003, display_name="AdminA", administrator=True)
    member_b = make_member(user_id=852000000000000004, display_name="MemberB", administrator=False)

    try:
        store = ExamStore(db, clock=clock)
        base_ts = parse_exam_datetime("2026-12-01", "09:00", "America/Toronto")

        # Seed 11 public/guild exams + 1 private exam created by owner_a (12 total for owner_a/admin_a, 11 for other_member_a)
        for i in range(1, 12):
            await store.add_exam(
                guild_id=guild_a.id,
                draft=ParsedExamDraft(
                    course_code=f"PUB{i:03d}",
                    course_name=f"Public Course {i}",
                    start_ts=base_ts + i * 3600,
                    end_ts=base_ts + i * 3600 + 1800,
                    visibility="guild",
                ),
                created_by=admin_a.id,
                is_admin=True,
            )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="PRIV777",
                course_name="OwnerA Paginated Private Exam",
                start_ts=base_ts + 20 * 3600,
                end_ts=base_ts + 21 * 3600,
                location="Confidential Suite 777",
                visibility="private",
            ),
            created_by=owner_a.id,
        )

        # 1. Multi-page PUBLIC schedule for other_member_a (11 public exams -> 2 pages, ephemeral=False)
        inter_pub_list = make_interaction(
            user=other_member_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_pub_list)
        assert inter_pub_list.response.ephemeral is False
        pub_msg = inter_pub_list.followup.messages[0]
        assert pub_msg.ephemeral is False
        pub_view = pub_msg.view
        assert isinstance(pub_view, ExamPaginationView)
        assert pub_view.total_pages == 2
        assert pub_view.allow_private is False
        assert "PRIV777" not in json.dumps(pub_msg.embed.to_dict())

        # 2. Multi-page MIXED schedule for owner_a (12 exams including PRIV777 on page 2 -> ephemeral=True)
        inter_priv_list = make_interaction(
            user=owner_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(inter_priv_list)
        assert inter_priv_list.response.deferred is True
        assert inter_priv_list.response.ephemeral is True
        priv_msg = inter_priv_list.followup.messages[0]
        assert priv_msg.ephemeral is True
        priv_view = priv_msg.view
        assert isinstance(priv_view, ExamPaginationView)
        assert priv_view.total_pages == 2
        assert priv_view.allow_private is True
        assert priv_view.owner_id == owner_a.id
        assert priv_view.guild_id == guild_a.id

        # 2a. Another guild member cannot advance owner_a's private pagination view
        unauth_click = make_interaction(user=other_member_a, guild=guild_a, client=client)
        assert await priv_view.interaction_check(unauth_click) is False
        assert unauth_click.response.ephemeral is True
        assert "only the user who requested" in (unauth_click.last_reply.content or "")
        assert priv_view.current_page == 0

        # Direct callback call also blocks unauthorized actor even if interaction was pre-deferred!
        pre_deferred_unauth = make_interaction(user=other_member_a, guild=guild_a, client=client)
        await pre_deferred_unauth.response.defer(thinking=False, ephemeral=False)
        await priv_view.next_button.callback(pre_deferred_unauth)
        assert pre_deferred_unauth.followup.messages[0].ephemeral is True
        assert "only the user who requested" in (pre_deferred_unauth.followup.messages[0].content or "")
        assert priv_view.current_page == 0

        # 2b. Second guild member (Guild B) and DM context cannot advance pagination
        cross_guild_click = make_interaction(user=member_b, guild=guild_b, client=client)
        await priv_view.next_button.callback(cross_guild_click)
        assert cross_guild_click.response.ephemeral is True
        assert "guild context mismatch" in (cross_guild_click.last_reply.content or "")
        assert priv_view.current_page == 0

        dm_click = make_interaction(user=owner_a, guild=guild_a, client=client)
        dm_click.guild = None
        await priv_view.next_button.callback(dm_click)
        assert dm_click.response.ephemeral is True
        assert "only be used inside a Discord server" in (dm_click.last_reply.content or "")
        assert priv_view.current_page == 0

        # 2c. Authorized owner_a advances to page 2 and sees PRIV777
        owner_next = make_interaction(user=owner_a, guild=guild_a, client=client)
        assert await priv_view.interaction_check(owner_next) is True
        await priv_view.next_button.callback(owner_next)
        assert priv_view.current_page == 1
        page2_embed_dict = owner_next.response.edit_messages[0].embed.to_dict()
        assert "PRIV777" in json.dumps(page2_embed_dict)

        # 2d. Deleted message / Discord HTTP failure during button edit does not crash
        failing_edit_inter = make_interaction(user=owner_a, guild=guild_a, client=client)
        failing_edit_inter.response.edit_message = AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        )
        await priv_view.prev_button.callback(failing_edit_inter)
        assert priv_view.current_page == 0

        # 2e. Timeout disables buttons, safely handles deleted message on_timeout, and rejects later clicks ephemerally
        assert priv_view.message is not None
        priv_view.message.edit = AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        )
        await priv_view.on_timeout()
        assert priv_view.prev_button.disabled is True
        assert priv_view.next_button.disabled is True

        expired_click = make_interaction(user=owner_a, guild=guild_a, client=client)
        await priv_view.next_button.callback(expired_click)
        assert expired_click.response.ephemeral is True
        assert "expired" in (expired_click.last_reply.content or "")

        # 3. ExamClearConfirmView timeout & pre-deferred unauthorized click resilience
        clear_inter = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam clear", client=client
        )
        await exams_clear_cmd.callback(clear_inter)
        clear_view = clear_inter.followup.messages[0].view
        assert isinstance(clear_view, ExamClearConfirmView)

        pre_def_clear_unauth = make_interaction(user=other_member_a, guild=guild_a, client=client)
        await pre_def_clear_unauth.response.defer(thinking=False, ephemeral=False)
        await clear_view.confirm_button.callback(pre_def_clear_unauth)
        assert pre_def_clear_unauth.followup.messages[0].ephemeral is True
        assert "only the administrator" in (pre_def_clear_unauth.followup.messages[0].content or "")

        clear_view.message.edit = AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        )
        await clear_view.on_timeout()
        assert clear_view.confirm_button.disabled is True
        assert clear_view.cancel_button.disabled is True

        expired_clear_click = make_interaction(user=admin_a, guild=guild_a, client=client)
        await clear_view.confirm_button.callback(expired_clear_click)
        assert expired_clear_click.response.ephemeral is True
        assert "expired" in (expired_clear_click.last_reply.content or "")
    finally:
        await container.close()
        await db.close()


async def test_exam_export_private_isolation_and_ephemeral_contract(
    tmp_path: Path,
    make_interaction,
    make_member,
):
    """Verify /ic exam export is always ephemeral and excludes private exams by default unless authorized."""
    db = Database(str(tmp_path / "exams_export_privacy.db"))
    await db.init()

    guild_a = SimpleNamespace(id=913000000000000001, name="Guild A")
    guild_b = SimpleNamespace(id=913000000000000002, name="Guild B")
    settings = Settings(
        db_path=str(tmp_path / "exams_export_privacy.db"),
        guild_id=guild_a.id,
        exams_tz="America/Toronto",
    )
    clock = FrozenClock(float(parse_exam_datetime("2026-11-15", "12:00", "America/Toronto")))
    container = ServiceContainer.from_settings(settings, db=db, clock=clock)
    client = FakeClient(settings=settings, db=db, services=container)

    admin_a = make_member(user_id=853000000000000001, display_name="AdminA", administrator=True)
    owner_a = make_member(user_id=853000000000000002, display_name="OwnerA", administrator=False)
    member_a = make_member(user_id=853000000000000003, display_name="MemberA", administrator=False)
    admin_b = make_member(user_id=853000000000000004, display_name="AdminB", administrator=True)

    try:
        store = ExamStore(db, clock=clock)
        base_ts = parse_exam_datetime("2026-12-12", "10:00", "America/Toronto")
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="PUB100",
                course_name="Public Final",
                start_ts=base_ts,
                end_ts=base_ts + 7200,
                visibility="guild",
            ),
            created_by=admin_a.id,
            is_admin=True,
        )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="PRIV100",
                course_name="OwnerA Private Final",
                start_ts=base_ts + 10800,
                end_ts=base_ts + 18000,
                notes="Secret Accommodation Note",
                visibility="private",
            ),
            created_by=owner_a.id,
        )
        await store.add_exam(
            guild_id=guild_a.id,
            draft=ParsedExamDraft(
                course_code="PRIV900",
                course_name="AdminA Private Final",
                start_ts=base_ts + 21600,
                end_ts=base_ts + 28800,
                visibility="private",
            ),
            created_by=admin_a.id,
            is_admin=True,
        )

        # 1. Default export by AdminA (include_private=False) excludes private exams so shared files never leak
        exp_admin_default = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam export", client=client
        )
        await exams_export_cmd.callback(exp_admin_default, fmt="csv")
        assert exp_admin_default.response.ephemeral is True
        assert exp_admin_default.followup.messages[0].ephemeral is True
        csv_default = exp_admin_default.followup.messages[0].file.fp.read().decode("utf-8")
        assert "PUB100" in csv_default
        assert "PRIV100" not in csv_default
        assert "PRIV900" not in csv_default

        # 2. Regular member passing include_private=True still receives ZERO private exams
        exp_member_priv = make_interaction(
            user=member_a, guild=guild_a, command_name="ic exam export", client=client
        )
        await exams_export_cmd.callback(exp_member_priv, fmt="ics", include_private=True)
        assert exp_member_priv.response.ephemeral is True
        assert exp_member_priv.followup.messages[0].ephemeral is True
        ics_member = exp_member_priv.followup.messages[0].file.fp.read().decode("utf-8")
        assert "PUB100" in ics_member
        assert "PRIV100" not in ics_member
        assert "PRIV900" not in ics_member

        # 3. OwnerA passing include_private=True receives PUB100 + PRIV100 (ephemerally), but NEVER PRIV900
        exp_owner_priv = make_interaction(
            user=owner_a, guild=guild_a, command_name="ic exam export", client=client
        )
        await exams_export_cmd.callback(exp_owner_priv, fmt="csv", include_private=True)
        assert exp_owner_priv.response.ephemeral is True
        assert exp_owner_priv.followup.messages[0].ephemeral is True
        csv_owner = exp_owner_priv.followup.messages[0].file.fp.read().decode("utf-8")
        assert "PUB100" in csv_owner
        assert "PRIV100" in csv_owner
        assert "PRIV900" not in csv_owner

        # 4. AdminA passing include_private=True receives PUB100 + PRIV100 + PRIV900 ephemerally
        exp_admin_priv = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam export", client=client
        )
        await exams_export_cmd.callback(exp_admin_priv, fmt="ics", include_private=True)
        assert exp_admin_priv.response.ephemeral is True
        assert exp_admin_priv.followup.messages[0].ephemeral is True
        ics_admin = exp_admin_priv.followup.messages[0].file.fp.read().decode("utf-8")
        assert "PUB100" in ics_admin
        assert "PRIV100" in ics_admin
        assert "PRIV900" in ics_admin

        # 5. AdminB in Guild B sees no Guild A exams on export
        exp_admin_b = make_interaction(
            user=admin_b, guild=guild_b, command_name="ic exam export", client=client
        )
        await exams_export_cmd.callback(exp_admin_b, fmt="ics", include_private=True)
        assert exp_admin_b.response.ephemeral is True
        assert "No exams available to export" in (exp_admin_b.followup.messages[0].content or "")
    finally:
        await container.close()
        await db.close()
