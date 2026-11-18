"""Unit tests for exam datetime/DST validation, Discord timestamp formatting, and sandboxed CSV/ICS I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from icbot.academics import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_EVENTS,
    MAX_IMPORT_LINES,
    Exam,
    ExamImportError,
    ExamTimeValidationError,
    export_exams_csv,
    export_exams_ics,
    format_exam_timestamp,
    parse_exam_datetime,
    parse_exams_csv,
    parse_exams_ics,
)

pytestmark = pytest.mark.unit


def test_leap_day_validation_accepts_2028_and_rejects_2026():
    ts = parse_exam_datetime("2028-02-29", "09:30", "America/Toronto")
    dt_utc = datetime.fromtimestamp(ts, UTC)
    assert (dt_utc.year, dt_utc.month, dt_utc.day, dt_utc.hour, dt_utc.minute) == (2028, 2, 29, 14, 30)

    with pytest.raises(ExamTimeValidationError, match="invalid calendar date"):
        parse_exam_datetime("2026-02-29", "09:30", "America/Toronto")


def test_dst_spring_forward_nonexistent_time_rejected():
    # In America/Toronto, clocks jump from 01:59 to 03:00 on 2026-03-08
    with pytest.raises(ExamTimeValidationError, match="spring-forward"):
        parse_exam_datetime("2026-03-08", "02:30", "America/Toronto")

    # Times just before and after the gap succeed
    before_ts = parse_exam_datetime("2026-03-08", "01:30", "America/Toronto")
    after_ts = parse_exam_datetime("2026-03-08", "03:30", "America/Toronto")
    assert after_ts - before_ts == 3600  # 1 real hour apart across the 2h wall jump


def test_dst_fall_back_ambiguous_time_rejected_by_default_and_resolvable():
    # In America/Toronto, 01:30 occurs twice on 2026-11-01 (EDT UTC-4, then EST UTC-5)
    with pytest.raises(ExamTimeValidationError, match="ambiguous"):
        parse_exam_datetime("2026-11-01", "01:30", "America/Toronto")

    earlier = parse_exam_datetime("2026-11-01", "01:30", "America/Toronto", dst_resolution="earlier")
    later = parse_exam_datetime("2026-11-01", "01:30", "America/Toronto", dst_resolution="later")
    assert later - earlier == 3600


def test_all_day_and_discord_timestamp_markup_with_plain_fallback():
    ts_start = parse_exam_datetime("2026-12-10", "09:00", "America/Toronto")
    ts_end = parse_exam_datetime("2026-12-10", "11:30", "America/Toronto")

    timed = format_exam_timestamp(ts_start, ts_end, "America/Toronto", include_discord_markup=True)
    assert f"<t:{ts_start}:F>" in timed
    assert f"<t:{ts_end}:t>" in timed
    assert f"<t:{ts_start}:R>" in timed
    assert "Dec 10, 2026" in timed
    assert "EST" in timed

    plain_only = format_exam_timestamp(ts_start, ts_end, "America/Toronto", include_discord_markup=False)
    assert "<t:" not in plain_only
    assert "09:00 AM" in plain_only

    all_day_ts = parse_exam_datetime("2026-12-12", "all-day", "America/Toronto")
    all_day_fmt = format_exam_timestamp(all_day_ts, None, "America/Toronto", is_all_day=True)
    assert f"<t:{all_day_ts}:D>" in all_day_fmt
    assert "All-Day" in all_day_fmt


def test_csv_export_and_import_round_trip_and_formula_safety():
    ts_start = parse_exam_datetime("2026-12-14", "14:00", "America/Toronto")
    ts_end = parse_exam_datetime("2026-12-14", "16:30", "America/Toronto")
    all_day_ts = parse_exam_datetime("2026-12-18", None, "America/Toronto", all_day=True)

    exams = [
        Exam(
            id=1,
            guild_id=100,
            course_code="COE817",
            course_name="Applied Cryptography",
            start_ts=ts_start,
            end_ts=ts_end,
            location="ENG-101",
            notes="=SUM(A1:A10) crib sheet",
            term="Fall 2026",
            organizer="ECE Dept",
            visibility="guild",
            external_uid="uid-coe817",
        ),
        Exam(
            id=2,
            guild_id=100,
            course_code="HIST-2010",
            course_name="Take-Home Essay Deadline",
            start_ts=all_day_ts,
            end_ts=None,
            is_all_day=True,
            term="Fall 2026",
            external_uid="uid-hist2010",
        ),
    ]

    csv_text = export_exams_csv(exams, tz_name="America/Toronto")
    # Formula prefix must be neutralized on export
    assert "'=SUM(A1:A10) crib sheet" in csv_text

    drafts, errors = parse_exams_csv(csv_text, tz_name="America/Toronto")
    assert errors == []
    assert len(drafts) == 2
    assert drafts[0].course_code == "COE817"
    assert drafts[0].start_ts == ts_start
    assert drafts[0].end_ts == ts_end
    assert drafts[0].term == "Fall 2026"
    assert drafts[0].source == "csv_import"
    assert drafts[1].course_code == "HIST-2010"
    assert drafts[1].is_all_day is True
    assert drafts[1].start_ts == all_day_ts


def test_ics_export_and_import_round_trip_with_folded_lines():
    ts_start = parse_exam_datetime("2026-12-15", "09:00", "America/Toronto")
    ts_end = parse_exam_datetime("2026-12-15", "11:00", "America/Toronto")
    all_day_ts = parse_exam_datetime("2028-02-29", None, "America/Toronto", all_day=True)

    exams = [
        Exam(
            id=5,
            guild_id=200,
            course_code="CS 135",
            course_name="Designing Functional Programs",
            start_ts=ts_start,
            end_ts=ts_end,
            location="PAC Gym, Section B",
            notes="No calculators; bring ID",
            term="Fall 2026",
            organizer="School of CS",
            external_uid="cs135-fall2026@example.invalid",
        ),
        Exam(
            id=6,
            guild_id=200,
            course_code="BIO120H1",
            course_name="Leap Day Lab Practical",
            start_ts=all_day_ts,
            is_all_day=True,
            external_uid="bio120-leap@example.invalid",
        ),
    ]

    ics_text = export_exams_ics(exams, tz_name="America/Toronto")
    assert "BEGIN:VCALENDAR" in ics_text
    assert "DTSTART;VALUE=DATE:20280229" in ics_text

    drafts, errors = parse_exams_ics(ics_text, tz_name="America/Toronto")
    assert errors == []
    assert len(drafts) == 2
    assert drafts[0].course_code == "CS 135"
    assert drafts[0].course_name == "Designing Functional Programs"
    assert drafts[0].start_ts == ts_start
    assert drafts[0].end_ts == ts_end
    assert drafts[0].location == "PAC Gym, Section B"
    assert drafts[0].notes == "No calculators; bring ID"
    assert drafts[0].organizer == "School of CS"
    assert drafts[1].course_code == "BIO120H1"
    assert drafts[1].is_all_day is True
    assert drafts[1].start_ts == all_day_ts


def test_sandboxed_parsers_reject_malicious_oversized_and_corrupt_inputs():
    # 1. Oversized byte payload
    with pytest.raises(ExamImportError, match="maximum allowed size"):
        parse_exams_csv(b"a" * (MAX_IMPORT_BYTES + 1))

    # 2. NUL byte injection
    with pytest.raises(ExamImportError, match="NUL"):
        parse_exams_ics(b"BEGIN:VCALENDAR\x00\nEND:VCALENDAR")

    # 3. Line count bomb
    too_many_lines = "course_code,course_name,date,start_time\n" + (
        "CS101,Intro,2026-12-10,09:00\n" * (MAX_IMPORT_LINES + 5)
    )
    with pytest.raises(ExamImportError, match="line count"):
        parse_exams_csv(too_many_lines)

    # 4. Nested VEVENT bomb
    nested_ics = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "BEGIN:VEVENT\n"
        "SUMMARY:CS101 - Bad\n"
        "DTSTART:20261210T090000Z\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    with pytest.raises(ExamImportError, match="nested BEGIN:VEVENT"):
        parse_exams_ics(nested_ics)

    # 5. Unclosed VEVENT
    unclosed_ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:CS101 - Unclosed\nEND:VCALENDAR\n"
    with pytest.raises(ExamImportError, match="unclosed BEGIN:VEVENT"):
        parse_exams_ics(unclosed_ics)

    # 6. Event count cap + per-row invalid dates / DST errors don't crash valid rows
    mixed_csv = (
        "course_code,course_name,date,start_time,end_time\n"
        "ELE888,Good Exam,2026-12-10,09:00,11:00\n"
        "BAD101,Leap Error,2026-02-29,09:00,11:00\n"
        "BAD102,DST Gap,2026-03-08,02:30,04:00\n"
        "BAD103,End Before Start,2026-12-10,14:00,12:00\n"
    )
    drafts, errors = parse_exams_csv(mixed_csv, tz_name="America/Toronto")
    assert len(drafts) == 1
    assert drafts[0].course_code == "ELE888"
    assert len(errors) == 3

    # 7. Event count > MAX_IMPORT_EVENTS stops cleanly at cap
    many_rows = ["course_code,course_name,date,start_time"] + [
        f"CRS{i},Subject {i},2026-12-10,{9 + (i % 10):02d}:{i % 60:02d}"
        for i in range(MAX_IMPORT_EVENTS + 10)
    ]
    capped_drafts, cap_errors = parse_exams_csv("\n".join(many_rows), tz_name="America/Toronto")
    assert len(capped_drafts) == MAX_IMPORT_EVENTS
    assert any("max" in e.lower() for e in cap_errors)
