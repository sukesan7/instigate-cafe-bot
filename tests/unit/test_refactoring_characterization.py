"""Characterization tests locking behavior of config parsers and exam CSV/ICS codecs before/after extraction."""

from __future__ import annotations

import pytest

from icbot.academics.exams_service import (
    MAX_IMPORT_BYTES,
    ExamImportError,
    ExamTimeValidationError,
    export_exams_csv,
    export_exams_ics,
    format_exam_timestamp,
    parse_exam_datetime,
    parse_exams_csv,
    parse_exams_ics,
    resolve_zone,
)
from icbot.academics.models import Exam
from icbot.config import (
    MAX_SNOWFLAKE,
    _is_memory_sqlite_path,
    _is_valid_hhmm,
    _is_valid_http_url,
    _is_valid_snowflake,
    _is_valid_tz,
    _to_bool,
    _to_color,
    _to_float,
    _to_int,
    _to_str_list,
    normalize_sqlite_path,
)
from icbot.errors import ConfigValidationError
from icbot.features.admin import _is_valid_tz as admin_is_valid_tz
from icbot.tasks.btc_watch import _to_int as btc_to_int
from icbot.tasks.market_watch import _to_bool as mw_to_bool
from icbot.tasks.market_watch import _to_int as mw_to_int

pytestmark = pytest.mark.unit


def test_config_parsing_and_validation_characterization() -> None:
    """Lock exact edge-case behavior of config coercion, URL/time/tz/snowflake, and SQLite path helpers."""
    assert _to_int(" 42 ", default=7) == 42
    assert _to_int("", default=7) == 7
    assert _to_int(None, default=7) == 7
    assert _to_int("bad", default=7) == 7
    assert _to_int(str(MAX_SNOWFLAKE + 1), default=0, clamp_snowflake=True) == 0
    assert _to_int("-1", default=5, clamp_snowflake=True) == 5
    assert _to_int("123456", default=0, clamp_snowflake=True) == 123456

    assert _to_float(" 3.25 ", default=1.0) == 3.25
    assert _to_float("", default=1.0) == 1.0
    assert _to_float("nope", default=1.0) == 1.0

    assert _to_bool("YES", default=False) is True
    assert _to_bool("off", default=True) is False
    assert _to_bool("maybe", default=True) is True
    assert _to_bool(None, default=True) is True

    assert _to_color("#a64dff") == 0xA64DFF
    assert _to_color("0x00FF10") == 0x00FF10
    assert _to_color("12345") == 12345
    assert _to_color(True, default=0x112233) == 0x112233
    assert _to_color("not-a-color", default=0x112233) == 0x112233

    assert _to_str_list("Alpha, beta, ALPHA, \\, , gamma") == ["Alpha", "beta", "gamma"]
    assert _to_str_list(["A", "a", "\\\\"]) == ["A"]
    assert _to_str_list(None) == []

    assert _is_valid_http_url("https://example.invalid/path") is True
    assert _is_valid_http_url("ftp://example.invalid") is False
    assert _is_valid_http_url("https://bad host/path") is False

    assert _is_valid_hhmm("00:00") is True
    assert _is_valid_hhmm("23:59") is True
    assert _is_valid_hhmm("24:00") is False
    assert _is_valid_hhmm("9:00") is False

    assert _is_valid_tz("America/Toronto") is True
    assert _is_valid_tz("Mars/Olympus") is False
    assert _is_valid_tz("") is False

    assert _is_valid_snowflake(0, allow_zero=True) is True
    assert _is_valid_snowflake(0, allow_zero=False) is False
    assert _is_valid_snowflake(True) is False
    assert _is_valid_snowflake(MAX_SNOWFLAKE + 1) is False

    assert _is_memory_sqlite_path(":memory:") is True
    assert _is_memory_sqlite_path("file:memdb?mode=memory") is True
    assert _is_memory_sqlite_path("/data/campus.db") is False

    assert normalize_sqlite_path(":memory:") == ":memory:"
    assert normalize_sqlite_path("", allow_empty=True) == ""
    with pytest.raises(ConfigValidationError):
        normalize_sqlite_path("")
    with pytest.raises(ConfigValidationError):
        normalize_sqlite_path("bad\x00.db")
    with pytest.raises(ConfigValidationError):
        normalize_sqlite_path("/data/")

    # Cross-module duplicate helpers behave consistently
    assert admin_is_valid_tz("America/Toronto") is True
    assert admin_is_valid_tz("Invalid/Zone") is False
    assert mw_to_int(" 15 ", 0) == 15
    assert mw_to_int("", 9) == 9
    assert mw_to_bool("true") is True
    assert mw_to_bool("0") is False
    assert btc_to_int("bad", 3) == 3


def test_exam_datetime_and_csv_ics_codecs_characterization() -> None:
    """Lock DST checks, sandboxed CSV/ICS parsing, formula neutralization, and private filtering."""
    assert resolve_zone("America/Toronto").key == "America/Toronto"
    with pytest.raises(ExamTimeValidationError):
        resolve_zone("Not/ATimezone")

    # Leap year check
    assert parse_exam_datetime("2028-02-29", "09:30", "America/Toronto") > 0
    with pytest.raises(ExamTimeValidationError):
        parse_exam_datetime("2026-02-29", "09:30", "America/Toronto")

    # DST spring-forward gap rejected
    with pytest.raises(ExamTimeValidationError):
        parse_exam_datetime("2026-03-08", "02:30", "America/Toronto")

    # DST fall-back ambiguity
    with pytest.raises(ExamTimeValidationError):
        parse_exam_datetime("2026-11-01", "01:30", "America/Toronto", dst_resolution="reject")
    ts_early = parse_exam_datetime("2026-11-01", "01:30", "America/Toronto", dst_resolution="earlier")
    ts_late = parse_exam_datetime("2026-11-01", "01:30", "America/Toronto", dst_resolution="later")
    assert ts_late - ts_early == 3600

    formatted = format_exam_timestamp(ts_early, ts_late, "America/Toronto", include_discord_markup=False)
    assert "2026" in formatted

    # Sandboxed CSV import
    csv_text = (
        "course_code,course_name,date,start_time,end_time,location,visibility\n"
        "CPS109,Computer Science I,2026-12-10,09:00,11:00,Hall A,guild\n"
        "MTH140,Calculus I,2026-12-12,all-day,,Main Gym,private\n"
    )
    drafts, errors = parse_exams_csv(csv_text, tz_name="America/Toronto")
    assert errors == []
    assert len(drafts) == 2
    assert drafts[0].course_code == "CPS109"
    assert drafts[0].source == "csv_import"
    assert drafts[1].is_all_day is True
    assert drafts[1].visibility == "private"

    # Sandbox caps (size & NUL byte)
    with pytest.raises(ExamImportError):
        parse_exams_csv(b"a" * (MAX_IMPORT_BYTES + 1))
    with pytest.raises(ExamImportError):
        parse_exams_csv(b"course_code,course_name,date\nCS1,\x00,2026-12-10\n")

    # Sandboxed ICS import (with folded line and escaped comma)
    ics_text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:exam-1@example.invalid\r\n"
        "SUMMARY:CPS109 - Computer Science\\, Part\r\n"
        "  I\r\n"
        "DTSTART;TZID=America/Toronto:20261210T090000\r\n"
        "DTEND;TZID=America/Toronto:20261210T110000\r\n"
        "LOCATION:Hall A\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    ics_drafts, ics_errors = parse_exams_ics(ics_text, tz_name="America/Toronto")
    assert ics_errors == []
    assert len(ics_drafts) == 1
    assert ics_drafts[0].course_code == "CPS109"
    assert ics_drafts[0].course_name == "Computer Science, Part I"
    assert ics_drafts[0].source == "ics_import"

    # Export CSV & ICS: formula neutralization + private stripping by default
    sample_exams = [
        Exam(
            id=1,
            guild_id=9001,
            course_code="=CMD()",
            course_name="Algorithms",
            start_ts=ts_early,
            end_ts=ts_late,
            location="Room 1",
            notes="+1+1",
            visibility="guild",
        ),
        Exam(
            id=2,
            guild_id=9001,
            course_code="PRIV101",
            course_name="Private Sitting",
            start_ts=ts_early,
            end_ts=ts_late,
            visibility="private",
        ),
    ]
    exported_csv = export_exams_csv(sample_exams, include_private=False)
    assert "'=CMD()" in exported_csv
    assert "'+1+1" in exported_csv
    assert "PRIV101" not in exported_csv

    exported_ics = export_exams_ics(sample_exams, include_private=False)
    assert "BEGIN:VCALENDAR" in exported_ics
    assert "PRIV101" not in exported_ics
    assert "PRIV101" in export_exams_ics(sample_exams, include_private=True)


def test_extracted_modules_preserve_identity_and_avoid_heavy_or_cyclic_imports() -> None:
    """Verify compatibility re-exports preserve function identity and pure modules stay acyclic."""
    import ast
    import subprocess
    import sys
    from pathlib import Path

    from icbot import config_parsers
    from icbot.academics import exam_codecs, exams_service

    # Identity checks: zero wrapper overhead and full backwards compatibility
    assert _to_int is config_parsers.parse_int
    assert _to_bool is config_parsers.parse_bool
    assert _is_valid_tz is config_parsers.is_valid_tz
    assert admin_is_valid_tz is config_parsers.is_valid_tz
    assert mw_to_int is config_parsers.parse_int
    assert mw_to_bool is config_parsers.parse_bool
    assert btc_to_int is config_parsers.parse_int
    assert exams_service.parse_exams_csv is exam_codecs.parse_exams_csv
    assert exams_service.parse_exams_ics is exam_codecs.parse_exams_ics
    assert exams_service.export_exams_csv is exam_codecs.export_exams_csv
    assert exams_service.export_exams_ics is exam_codecs.export_exams_ics

    # Fresh interpreter check: icbot.config_parsers must never pull in discord or aiosqlite
    probe = (
        "import sys; "
        "import icbot.config_parsers; "
        "assert 'discord' not in sys.modules, 'discord leaked into icbot.config_parsers'; "
        "assert 'aiosqlite' not in sys.modules, 'aiosqlite leaked into icbot.config_parsers'; "
        "assert 'icbot.db' not in sys.modules, 'icbot.db leaked into icbot.config_parsers'"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Pure module import isolation failed: {proc.stderr}"

    # AST import check: exam_codecs.py itself must not import discord, aiosqlite, icbot.db, or exams_service
    codecs_path = Path(exam_codecs.__file__)
    tree = ast.parse(codecs_path.read_text(encoding="utf-8"), filename=str(codecs_path))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    for forbidden in ("discord", "aiosqlite", "icbot.db", "icbot.academics.exams_service"):
        assert not any(m == forbidden or m.startswith(f"{forbidden}.") for m in imported_modules), (
            f"exam_codecs.py must stay decoupled from '{forbidden}', found: {sorted(imported_modules)}"
        )
