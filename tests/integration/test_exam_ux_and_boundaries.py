"""Integration tests for exam scheduling UX, concurrency, DST/leap days, CSV/ICS, boundaries, and reminders."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from icbot.academics import (
    Course,
    compute_due_exam_reminders,
    mark_exam_reminder_delivered,
    parse_exam_datetime,
)
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.exams import (
    ExamClearConfirmView,
    _exam_course_autocomplete,
    exams_add_cmd,
    exams_clear_cmd,
    exams_cmd,
    exams_export_cmd,
    exams_import_cmd,
    exams_remove_cmd,
)

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
        emb_a = list_student_a.followup.messages[0].embed
        assert emb_a is not None
        # Private exam is hidden from student_a; upcoming ELE888 appears BEFORE past ELE532
        field_names_a = [f.name for f in emb_a.fields]
        assert len(field_names_a) == 2
        assert field_names_a[0].startswith("ELE888")
        assert field_names_a[1].startswith("ELE532")
        assert "<t:" in emb_a.fields[0].value  # Discord timestamp markup present
        assert "Added by:" in emb_a.fields[0].value  # Creator attribution present

        # Admin in Guild A sees the private exam too
        list_admin_a = make_interaction(
            user=admin_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(list_admin_a)
        assert len(list_admin_a.followup.messages[0].embed.fields) == 3

        # Course & upcoming_only filter in Guild A
        list_filtered = make_interaction(
            user=student_a, guild=guild_a, command_name="ic exam list", client=client
        )
        await exams_cmd.callback(list_filtered, course="ELE888", term="Fall 2026", upcoming_only=True)
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
