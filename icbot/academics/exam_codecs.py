"""Pure exam datetime/DST validation and sandboxed CSV/ICS import/export codecs.

Extracted from ``icbot.academics.exams_service`` so wire-format parsing,
spreadsheet formula-injection neutralization, RFC 5545 escaping, and IANA DST
validation stay decoupled from async SQLite persistence and Discord views.
All public symbols are re-exported by ``icbot.academics.exams_service``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icbot.academics.models import Exam, ExamSource, ExamVisibility

DstResolution = Literal["reject", "earlier", "later"]

# Sandboxed import caps (anti-DoS / anti-bomb)
MAX_IMPORT_BYTES = 128 * 1024  # 128 KiB
MAX_IMPORT_LINES = 500
MAX_IMPORT_EVENTS = 100
MAX_TEXT_FIELD_LEN = 200
MAX_NOTES_FIELD_LEN = 500

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_ICS_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_ICS_DATETIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CSV_EXPORT_HEADERS: tuple[str, ...] = (
    "course_code",
    "course_name",
    "date",
    "start_time",
    "end_time",
    "location",
    "notes",
    "term",
    "organizer",
    "all_day",
    "visibility",
    "uid",
)


class ExamTimeValidationError(ValueError):
    """Raised on invalid calendar dates, nonexistent DST times, or unresolved DST ambiguity."""


class ExamImportError(ValueError):
    """Raised when an imported CSV/ICS payload violates sandbox limits or structural rules."""


@dataclass(frozen=True)
class ParsedExamDraft:
    """Validated exam candidate ready for atomic persistence into SQLite."""

    course_code: str
    course_name: str
    start_ts: int
    end_ts: int | None
    is_all_day: bool = False
    location: str = ""
    notes: str = ""
    term: str = ""
    organizer: str = ""
    visibility: ExamVisibility = "guild"
    source: ExamSource = "manual"
    external_uid: str = ""


def _clean_text(value: str | None, max_len: int = MAX_TEXT_FIELD_LEN) -> str:
    raw = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    return raw[:max_len]


# ---------------------------------------------------------------------------
# Timezone & DST-Aware Datetime Validation and Formatting
# ---------------------------------------------------------------------------


def resolve_zone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA timezone name strictly, raising ``ExamTimeValidationError`` if invalid."""
    clean = str(tz_name or "").strip()
    if not clean:
        raise ExamTimeValidationError("Timezone name must not be empty.")
    try:
        return ZoneInfo(clean)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ExamTimeValidationError(f"Unknown IANA timezone: {clean!r}") from exc


def parse_exam_datetime(
    date_str: str,
    time_str: str | None,
    tz_name: str,
    *,
    all_day: bool = False,
    dst_resolution: DstResolution = "reject",
) -> int:
    """Parse local ``(YYYY-MM-DD, HH:MM)`` in ``tz_name`` with strict calendar and DST checks.

    - Validates leap years and real calendar dates (e.g. ``2028-02-29`` valid, ``2026-02-29`` rejected).
    - Rejects nonexistent wall times during spring-forward DST gaps.
    - Rejects ambiguous wall times during fall-back DST overlaps unless ``dst_resolution``
      explicitly specifies ``'earlier'`` (fold=0) or ``'later'`` (fold=1).
    - For ``all_day=True`` (or ``time_str`` in ``('all-day', 'allday')``), anchors to local midnight.
    """
    d_clean = str(date_str or "").strip()
    m_date = _DATE_RE.fullmatch(d_clean)
    if not m_date:
        raise ExamTimeValidationError("date must be YYYY-MM-DD")
    y, mo, d = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))

    t_clean = str(time_str or "").strip()
    is_all_day = bool(all_day) or t_clean.lower() in ("all-day", "allday")
    if is_all_day:
        hh, mm = 0, 0
    else:
        m_time = _TIME_RE.fullmatch(t_clean)
        if not m_time:
            raise ExamTimeValidationError("time must be HH:MM (24h)")
        hh, mm = int(m_time.group(1)), int(m_time.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ExamTimeValidationError("time must be a valid 24-hour HH:MM value")

    tz = resolve_zone(tz_name)

    try:
        dt0 = datetime(y, mo, d, hh, mm, tzinfo=tz, fold=0)
        dt1 = datetime(y, mo, d, hh, mm, tzinfo=tz, fold=1)
    except ValueError as exc:
        raise ExamTimeValidationError(f"invalid calendar date: {exc}") from exc

    # 1. Detect nonexistent DST "spring forward" wall time via UTC round-trip
    rt0 = dt0.astimezone(UTC).astimezone(tz)
    if (rt0.year, rt0.month, rt0.day, rt0.hour, rt0.minute) != (y, mo, d, hh, mm):
        raise ExamTimeValidationError(
            f"{d_clean} {hh:02d}:{mm:02d} does not exist in {tz_name} (DST spring-forward gap)."
        )

    # 2. Detect ambiguous DST "fall back" wall time via fold=0 vs fold=1 UTC offset comparison
    off0 = dt0.utcoffset()
    off1 = dt1.utcoffset()
    if off0 != off1:
        if dst_resolution == "earlier":
            return int(dt0.timestamp())
        if dst_resolution == "later":
            return int(dt1.timestamp())
        tz0 = dt0.tzname() or "earlier offset"
        tz1 = dt1.tzname() or "later offset"
        raise ExamTimeValidationError(
            f"{d_clean} {hh:02d}:{mm:02d} is ambiguous in {tz_name} due to DST fall-back "
            f"({tz0} vs {tz1}). Specify dst_resolution='earlier' or 'later'."
        )

    return int(dt0.timestamp())


def format_exam_timestamp(
    start_ts: int,
    end_ts: int | None,
    tz_name: str,
    *,
    is_all_day: bool = False,
    include_discord_markup: bool = True,
) -> str:
    """Format an exam schedule time with Discord ``<t:UNIX:...>`` markup and plain-text fallback."""
    try:
        tz: ZoneInfo | Any = resolve_zone(tz_name)
    except ExamTimeValidationError:
        tz = UTC

    s_dt = datetime.fromtimestamp(int(start_ts), tz)
    tz_abbr = s_dt.tzname() or tz_name

    if is_all_day:
        plain = f"All-Day • {s_dt.strftime('%a %b %d, %Y')} ({tz_abbr})"
        if include_discord_markup:
            return f"All-Day • <t:{int(start_ts)}:D> (<t:{int(start_ts)}:R>)\n*{plain}*"
        return plain

    if end_ts is not None:
        e_dt = datetime.fromtimestamp(int(end_ts), tz)
        if s_dt.date() == e_dt.date():
            plain = f"{s_dt.strftime('%a %b %d, %Y • %I:%M %p')} – {e_dt.strftime('%I:%M %p')} ({tz_abbr})"
            if include_discord_markup:
                return f"<t:{int(start_ts)}:F> – <t:{int(end_ts)}:t> (<t:{int(start_ts)}:R>)\n*{plain}*"
            return plain
        plain = (
            f"{s_dt.strftime('%a %b %d, %Y • %I:%M %p')} – "
            f"{e_dt.strftime('%a %b %d, %Y • %I:%M %p')} ({tz_abbr})"
        )
        if include_discord_markup:
            return f"<t:{int(start_ts)}:F> – <t:{int(end_ts)}:F> (<t:{int(start_ts)}:R>)\n*{plain}*"
        return plain

    plain = f"{s_dt.strftime('%a %b %d, %Y • %I:%M %p')} ({tz_abbr})"
    if include_discord_markup:
        return f"<t:{int(start_ts)}:F> (<t:{int(start_ts)}:R>)\n*{plain}*"
    return plain


# ---------------------------------------------------------------------------
# Sandboxed CSV / ICS Parsing and Export (Zero Network Downloads)
# ---------------------------------------------------------------------------


def _decode_and_check_sandbox(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        if len(raw) > MAX_IMPORT_BYTES:
            raise ExamImportError(
                f"Import payload exceeds maximum allowed size ({MAX_IMPORT_BYTES // 1024} KiB)."
            )
        if b"\x00" in raw:
            raise ExamImportError("Import payload contains disallowed NUL bytes.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExamImportError("Import payload must be valid UTF-8 text.") from exc
    else:
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_IMPORT_BYTES:
            raise ExamImportError(
                f"Import payload exceeds maximum allowed size ({MAX_IMPORT_BYTES // 1024} KiB)."
            )
        if "\x00" in raw:
            raise ExamImportError("Import payload contains disallowed NUL bytes.")
        text = raw.lstrip("\ufeff")

    lines = text.splitlines()
    if len(lines) > MAX_IMPORT_LINES:
        raise ExamImportError(
            f"Import payload exceeds maximum allowed line count ({MAX_IMPORT_LINES} lines)."
        )
    return text


def parse_exams_csv(
    raw: bytes | str,
    *,
    tz_name: str = "America/Toronto",
    dst_resolution: DstResolution = "reject",
) -> tuple[list[ParsedExamDraft], list[str]]:
    """Parse a sandboxed CSV subset into validated ``ParsedExamDraft`` items and row errors."""
    text = _decode_and_check_sandbox(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ExamImportError("CSV is missing a header row.")

    normalized_headers = {str(h or "").strip().lower() for h in reader.fieldnames}
    required = {"course_code", "course_name", "date"}
    missing = required - normalized_headers
    if missing:
        raise ExamImportError(f"CSV is missing required columns: {', '.join(sorted(missing))}.")

    drafts: list[ParsedExamDraft] = []
    errors: list[str] = []

    for row_num, raw_row in enumerate(reader, start=2):
        if len(drafts) >= MAX_IMPORT_EVENTS:
            errors.append(f"Row {row_num}: stopped after reaching max {MAX_IMPORT_EVENTS} events.")
            break

        row = {
            str(k or "").strip().lower(): str(v or "").strip() for k, v in raw_row.items() if k is not None
        }
        if not any(row.values()):
            continue

        code = _clean_text(row.get("course_code", ""), 64)
        name = _clean_text(row.get("course_name", ""), MAX_TEXT_FIELD_LEN)
        date_s = row.get("date", "")
        start_s = row.get("start_time", "")
        end_s = row.get("end_time", "")
        all_day_raw = row.get("all_day", "").lower()
        is_all_day = all_day_raw in ("1", "true", "yes", "y") or start_s.lower() in ("all-day", "allday")

        if not code or not name or not date_s:
            errors.append(f"Row {row_num}: course_code, course_name, and date are required.")
            continue

        try:
            start_ts = parse_exam_datetime(
                date_s,
                None if is_all_day else start_s,
                tz_name,
                all_day=is_all_day,
                dst_resolution=dst_resolution,
            )
            end_ts: int | None = None
            if not is_all_day and end_s:
                end_ts = parse_exam_datetime(
                    date_s,
                    end_s,
                    tz_name,
                    all_day=False,
                    dst_resolution=dst_resolution,
                )
                if end_ts < start_ts:
                    raise ExamTimeValidationError("end_time must be after start_time")
        except ExamTimeValidationError as exc:
            errors.append(f"Row {row_num} ({code}): {exc}")
            continue

        vis_raw = row.get("visibility", "guild").lower() or "guild"
        vis: ExamVisibility = (
            cast(ExamVisibility, vis_raw) if vis_raw in ("guild", "public", "private") else "guild"
        )
        drafts.append(
            ParsedExamDraft(
                course_code=code,
                course_name=name,
                start_ts=start_ts,
                end_ts=end_ts,
                is_all_day=is_all_day,
                location=_clean_text(row.get("location", ""), 160),
                notes=_clean_text(row.get("notes", ""), MAX_NOTES_FIELD_LEN),
                term=_clean_text(row.get("term", ""), 60),
                organizer=_clean_text(row.get("organizer", ""), 120),
                visibility=vis,
                source="csv_import",
                external_uid=_clean_text(row.get("uid", ""), 160),
            )
        )

    return drafts, errors


def _unfold_ics_lines(text: str) -> list[str]:
    """Unfold RFC 5545 folded lines (CRLF/LF followed by a single space or tab)."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_ics_text(val: str) -> str:
    return (
        val.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _escape_ics_text(val: str) -> str:
    return (
        str(val or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _parse_ics_dt(
    params: dict[str, str],
    value: str,
    default_tz: str,
    dst_resolution: DstResolution,
) -> tuple[int, bool]:
    """Parse an ICS DTSTART/DTEND property into ``(unix_ts, is_all_day)``."""
    val = value.strip()
    is_date_only = params.get("VALUE", "").upper() == "DATE" or bool(_ICS_DATE_RE.fullmatch(val))
    if is_date_only:
        m = _ICS_DATE_RE.fullmatch(val)
        if not m:
            raise ExamTimeValidationError(f"Invalid ICS DATE value: {val!r}")
        date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        ts = parse_exam_datetime(date_iso, None, default_tz, all_day=True, dst_resolution=dst_resolution)
        return ts, True

    m_dt = _ICS_DATETIME_RE.fullmatch(val)
    if not m_dt:
        raise ExamTimeValidationError(f"Invalid ICS DATETIME value: {val!r}")

    y, mo, d = int(m_dt.group(1)), int(m_dt.group(2)), int(m_dt.group(3))
    hh, mm, ss = int(m_dt.group(4)), int(m_dt.group(5)), int(m_dt.group(6))
    is_utc = m_dt.group(7) == "Z"
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ExamTimeValidationError(f"Invalid ICS time component: {val!r}")

    if is_utc:
        try:
            return int(datetime(y, mo, d, hh, mm, ss, tzinfo=UTC).timestamp()), False
        except ValueError as exc:
            raise ExamTimeValidationError(f"Invalid UTC calendar date: {exc}") from exc

    tz_id = params.get("TZID", "").strip() or default_tz
    ts_min = parse_exam_datetime(
        f"{y:04d}-{mo:02d}-{d:02d}",
        f"{hh:02d}:{mm:02d}",
        tz_id,
        all_day=False,
        dst_resolution=dst_resolution,
    )
    return ts_min + ss, False


def _split_ics_summary(summary: str) -> tuple[str, str]:
    clean = _clean_text(summary, MAX_TEXT_FIELD_LEN)
    for sep in (" — ", " - ", ": "):
        if sep in clean:
            left, right = clean.split(sep, 1)
            if left.strip() and right.strip():
                return left.strip()[:64], right.strip()[:MAX_TEXT_FIELD_LEN]
    parts = clean.split(None, 1)
    if len(parts) == 2 and any(ch.isdigit() for ch in parts[0]):
        return parts[0][:64], parts[1][:MAX_TEXT_FIELD_LEN]
    return clean[:64], clean[:MAX_TEXT_FIELD_LEN]


def parse_exams_ics(
    raw: bytes | str,
    *,
    tz_name: str = "America/Toronto",
    dst_resolution: DstResolution = "reject",
) -> tuple[list[ParsedExamDraft], list[str]]:
    """Parse a sandboxed RFC 5545 VCALENDAR/VEVENT subset without external fetches."""
    text = _decode_and_check_sandbox(raw)
    lines = _unfold_ics_lines(text)

    if not any(line.strip().upper() == "BEGIN:VCALENDAR" for line in lines):
        raise ExamImportError("ICS payload must contain BEGIN:VCALENDAR.")
    if not any(line.strip().upper() == "END:VCALENDAR" for line in lines):
        raise ExamImportError("ICS payload is missing END:VCALENDAR.")

    drafts: list[ParsedExamDraft] = []
    errors: list[str] = []
    in_event = False
    event_index = 0
    props: dict[str, tuple[dict[str, str], str]] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "BEGIN:VEVENT":
            if in_event:
                raise ExamImportError("Malformed ICS: nested BEGIN:VEVENT is not permitted.")
            in_event = True
            event_index += 1
            if event_index > MAX_IMPORT_EVENTS:
                errors.append(f"Stopped after reaching max {MAX_IMPORT_EVENTS} VEVENT entries.")
                in_event = False
                break
            props = {}
            continue

        if upper == "END:VEVENT":
            if not in_event:
                errors.append("Malformed ICS: unexpected END:VEVENT.")
                continue
            in_event = False
            if "SUMMARY" not in props or "DTSTART" not in props:
                errors.append(f"Event {event_index}: missing required SUMMARY or DTSTART.")
                continue

            summary_val = _unescape_ics_text(props["SUMMARY"][1])
            code, name = _split_ics_summary(summary_val)
            if not code or not name:
                errors.append(f"Event {event_index}: empty SUMMARY.")
                continue

            try:
                start_ts, is_all_day = _parse_ics_dt(
                    props["DTSTART"][0], props["DTSTART"][1], tz_name, dst_resolution
                )
                end_ts: int | None = None
                if "DTEND" in props and not is_all_day:
                    end_ts, _ = _parse_ics_dt(props["DTEND"][0], props["DTEND"][1], tz_name, dst_resolution)
                    if end_ts < start_ts:
                        raise ExamTimeValidationError("DTEND must be after DTSTART")
            except ExamTimeValidationError as exc:
                errors.append(f"Event {event_index} ({code}): {exc}")
                continue

            organizer_raw = props["ORGANIZER"][1] if "ORGANIZER" in props else ""
            organizer_cn = props["ORGANIZER"][0].get("CN", "") if "ORGANIZER" in props else ""
            organizer = _clean_text(organizer_cn or organizer_raw.removeprefix("mailto:"), 120)

            drafts.append(
                ParsedExamDraft(
                    course_code=code,
                    course_name=name,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    is_all_day=is_all_day,
                    location=_clean_text(_unescape_ics_text(props["LOCATION"][1]), 160)
                    if "LOCATION" in props
                    else "",
                    notes=_clean_text(_unescape_ics_text(props["DESCRIPTION"][1]), MAX_NOTES_FIELD_LEN)
                    if "DESCRIPTION" in props
                    else "",
                    term=_clean_text(_unescape_ics_text(props["CATEGORIES"][1]), 60)
                    if "CATEGORIES" in props
                    else "",
                    organizer=organizer,
                    visibility="guild",
                    source="ics_import",
                    external_uid=_clean_text(props["UID"][1], 160) if "UID" in props else "",
                )
            )
            continue

        if in_event and ":" in stripped:
            head, val = stripped.split(":", 1)
            head_parts = head.split(";")
            prop_name = head_parts[0].strip().upper()
            param_map: dict[str, str] = {}
            for p in head_parts[1:]:
                if "=" in p:
                    pk, pv = p.split("=", 1)
                    param_map[pk.strip().upper()] = pv.strip().strip('"')
            props[prop_name] = (param_map, val)

    if in_event:
        raise ExamImportError("Malformed ICS: unclosed BEGIN:VEVENT block.")

    return drafts, errors


def _csv_safe_cell(val: str) -> str:
    """Neutralize spreadsheet formula-injection prefixes on CSV export."""
    s = str(val or "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def export_exams_csv(
    exams: Sequence[Exam],
    tz_name: str = "America/Toronto",
    *,
    include_private: bool = False,
) -> str:
    """Export exams to the documented CSV schema using ``tz_name``.

    By default (`include_private=False`), any exam with `visibility == 'private'`
    is stripped so exported files are safe to share publicly without leaking private sittings.
    """
    try:
        tz: ZoneInfo | Any = resolve_zone(tz_name)
    except ExamTimeValidationError:
        tz = UTC

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_EXPORT_HEADERS)

    for ex in exams:
        if ex.visibility == "private" and not include_private:
            continue
        s_dt = datetime.fromtimestamp(ex.start_ts, tz)
        date_s = s_dt.strftime("%Y-%m-%d")
        start_s = "" if ex.is_all_day else s_dt.strftime("%H:%M")
        end_s = ""
        if not ex.is_all_day and ex.end_ts is not None:
            end_s = datetime.fromtimestamp(ex.end_ts, tz).strftime("%H:%M")
        writer.writerow(
            [
                _csv_safe_cell(ex.course_code),
                _csv_safe_cell(ex.course_name),
                date_s,
                start_s,
                end_s,
                _csv_safe_cell(ex.location),
                _csv_safe_cell(ex.notes),
                _csv_safe_cell(ex.term),
                _csv_safe_cell(ex.organizer),
                "true" if ex.is_all_day else "false",
                ex.visibility,
                ex.external_uid or f"icbot-exam-{ex.guild_id}-{ex.id}",
            ]
        )
    return buf.getvalue()


def export_exams_ics(
    exams: Sequence[Exam],
    tz_name: str = "America/Toronto",
    *,
    include_private: bool = False,
) -> str:
    """Export exams to a clean RFC 5545 VCALENDAR/VEVENT subset.

    By default (`include_private=False`), any exam with `visibility == 'private'`
    is stripped so exported calendar files cannot accidentally leak private sittings.
    """
    try:
        tz: ZoneInfo | Any = resolve_zone(tz_name)
    except ExamTimeValidationError:
        tz = UTC

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Campus Bot//Exam Schedule//EN",
        "CALSCALE:GREGORIAN",
    ]
    for ex in exams:
        if ex.visibility == "private" and not include_private:
            continue
        uid = ex.external_uid or f"icbot-exam-{ex.guild_id}-{ex.id}@campus-bot.invalid"
        summary = f"{ex.course_code} - {ex.course_name}".strip(" -")
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_escape_ics_text(uid)}")
        lines.append(f"SUMMARY:{_escape_ics_text(summary)}")
        if ex.is_all_day:
            d_local = datetime.fromtimestamp(ex.start_ts, tz).strftime("%Y%m%d")
            lines.append(f"DTSTART;VALUE=DATE:{d_local}")
        else:
            s_utc = datetime.fromtimestamp(ex.start_ts, UTC).strftime("%Y%m%dT%H%M%SZ")
            lines.append(f"DTSTART:{s_utc}")
            if ex.end_ts is not None:
                e_utc = datetime.fromtimestamp(ex.end_ts, UTC).strftime("%Y%m%dT%H%M%SZ")
                lines.append(f"DTEND:{e_utc}")
        if ex.location:
            lines.append(f"LOCATION:{_escape_ics_text(ex.location)}")
        if ex.notes:
            lines.append(f"DESCRIPTION:{_escape_ics_text(ex.notes)}")
        if ex.term:
            lines.append(f"CATEGORIES:{_escape_ics_text(ex.term)}")
        if ex.organizer:
            lines.append(f"ORGANIZER;CN={_escape_ics_text(ex.organizer)}:mailto:noreply@campus-bot.invalid")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = [
    "CSV_EXPORT_HEADERS",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_EVENTS",
    "MAX_IMPORT_LINES",
    "MAX_NOTES_FIELD_LEN",
    "MAX_TEXT_FIELD_LEN",
    "DstResolution",
    "ExamImportError",
    "ExamTimeValidationError",
    "ParsedExamDraft",
    "_clean_text",
    "_csv_safe_cell",
    "_decode_and_check_sandbox",
    "_escape_ics_text",
    "_parse_ics_dt",
    "_split_ics_summary",
    "_unescape_ics_text",
    "_unfold_ics_lines",
    "export_exams_csv",
    "export_exams_ics",
    "format_exam_timestamp",
    "parse_exam_datetime",
    "parse_exams_csv",
    "parse_exams_ics",
    "resolve_zone",
]
