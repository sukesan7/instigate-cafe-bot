"""Exam scheduling business logic, DST validation, sandboxed CSV/ICS I/O, and opt-in reminders.

All operations enforce strict guild boundaries, atomic same-transaction SQLite ID retrieval,
timezone-aware datetime handling with explicit DST gap/ambiguity validation, and zero
implicit network calls or portal scraping.
"""

from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icbot.academics.models import Exam, ExamSource, ExamVisibility, normalize_course_key
from icbot.db import Database

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


@dataclass(frozen=True)
class ExamAddResult:
    """Result of atomically adding an exam inside a single write transaction."""

    exam: Exam
    is_duplicate: bool = False
    overlapping: tuple[Exam, ...] = ()


@dataclass(frozen=True)
class ExamImportResult:
    """Summary returned by sandboxed CSV/ICS import preserving manual exams."""

    imported: tuple[Exam, ...] = ()
    skipped_duplicates: int = 0
    errors: tuple[str, ...] = ()
    preserved_manual_count: int = 0


@dataclass(frozen=True)
class DueExamReminder:
    """Represents a single due exam deadline reminder ready for admin-configured channel delivery."""

    guild_id: int
    channel_id: int
    exam: Exam
    lead_hours: int
    dedupe_key: str


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
# Atomic Guild-Scoped ExamStore
# ---------------------------------------------------------------------------


def _clean_text(value: str | None, max_len: int = MAX_TEXT_FIELD_LEN) -> str:
    raw = _CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    return raw[:max_len]


def _row_to_exam(row: tuple[Any, ...], fallback_guild_id: int = 0) -> Exam:
    vis_raw = str(row[10] or "guild").strip().lower()
    vis: ExamVisibility = (
        cast(ExamVisibility, vis_raw) if vis_raw in ("guild", "public", "private") else "guild"
    )
    src_raw = str(row[15] or "manual").strip().lower()
    src: ExamSource = (
        cast(ExamSource, src_raw) if src_raw in ("manual", "csv_import", "ics_import") else "manual"
    )
    return Exam(
        id=int(row[0]),
        guild_id=int(row[1] if row[1] is not None else fallback_guild_id),
        course_code=str(row[2] or "").strip(),
        course_key=str(row[3] or "").strip() or normalize_course_key(str(row[2] or "")),
        course_name=str(row[4] or "").strip(),
        start_ts=int(row[5] or 0),
        end_ts=int(row[6]) if row[6] is not None else None,
        organizer=str(row[7] or "").strip(),
        location=str(row[8] or "").strip(),
        notes=str(row[9] or "").strip(),
        visibility=vis,
        created_by=int(row[11] or 0),
        created_at=int(row[12] or 0),
        term=str(row[13] or "").strip(),
        is_all_day=bool(int(row[14] or 0)),
        source=src,
        channel_id=int(row[16] or 0),
        external_uid=str(row[17] or "").strip(),
    )


_EXAM_SELECT_COLS = """
    id, guild_id, course_code, course_key, course_name, start_ts, end_ts,
    organizer, location, notes, visibility, created_by, created_at,
    term, is_all_day, source, channel_id, external_uid
"""


class ExamStore:
    """Atomic, guild-scoped SQLite repository for exams with same-transaction ID retrieval."""

    def __init__(self, db: Database, *, clock: Callable[[], float] | None = None) -> None:
        self.db = db
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    async def add_exam(
        self,
        *,
        guild_id: int,
        draft: ParsedExamDraft,
        created_by: int,
        channel_id: int = 0,
        reject_duplicate: bool = True,
        is_admin: bool = False,
    ) -> ExamAddResult:
        """Atomically insert an exam and return its exact same-transaction ID plus overlap info."""
        gid = int(guild_id)
        clean_code = _clean_text(draft.course_code, 64)
        clean_name = _clean_text(draft.course_name, MAX_TEXT_FIELD_LEN)
        if not clean_code or not clean_name:
            raise ValueError("Both course_code and course_name are required.")
        course_key = normalize_course_key(clean_code)
        now_ts = self._now()

        async with self.db.write_transaction() as tx:
            # 1. Duplicate check inside the same IMMEDIATE write transaction
            if reject_duplicate:
                dup_row = None
                if draft.external_uid:
                    dup_row = await tx.fetchone(
                        f"SELECT {_EXAM_SELECT_COLS} FROM exams WHERE guild_id = ? AND external_uid = ?",
                        (gid, draft.external_uid),
                    )
                if dup_row is None:
                    dup_row = await tx.fetchone(
                        f"""
                        SELECT {_EXAM_SELECT_COLS}
                        FROM exams
                        WHERE guild_id = ? AND course_key = ? AND start_ts = ? AND is_all_day = ?
                        """,
                        (gid, course_key, int(draft.start_ts), 1 if draft.is_all_day else 0),
                    )
                if dup_row is not None:
                    return ExamAddResult(exam=_row_to_exam(dup_row, gid), is_duplicate=True)

            # 2. Same-transaction insert returning exact rowid
            exam_id = await tx.execute_insert(
                """
                INSERT INTO exams (
                    guild_id, course_code, course_key, course_name, start_ts, end_ts,
                    organizer, location, notes, visibility, created_by, created_at,
                    term, is_all_day, source, channel_id, external_uid
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    clean_code,
                    course_key,
                    clean_name,
                    int(draft.start_ts),
                    int(draft.end_ts) if draft.end_ts is not None else None,
                    _clean_text(draft.organizer, 120),
                    _clean_text(draft.location, 160),
                    _clean_text(draft.notes, MAX_NOTES_FIELD_LEN),
                    draft.visibility,
                    int(created_by),
                    now_ts,
                    _clean_text(draft.term, 60),
                    1 if draft.is_all_day else 0,
                    draft.source,
                    int(channel_id),
                    _clean_text(draft.external_uid, 160),
                ),
            )

            created_exam = Exam(
                id=exam_id,
                guild_id=gid,
                course_code=clean_code,
                course_key=course_key,
                course_name=clean_name,
                start_ts=int(draft.start_ts),
                end_ts=int(draft.end_ts) if draft.end_ts is not None else None,
                organizer=_clean_text(draft.organizer, 120),
                location=_clean_text(draft.location, 160),
                notes=_clean_text(draft.notes, MAX_NOTES_FIELD_LEN),
                visibility=draft.visibility,
                created_by=int(created_by),
                created_at=now_ts,
                term=_clean_text(draft.term, 60),
                is_all_day=bool(draft.is_all_day),
                source=draft.source,
                channel_id=int(channel_id),
                external_uid=_clean_text(draft.external_uid, 160),
            )

            # 3. Detect overlapping timed exams in the same guild (non-blocking)
            overlap_rows = await tx.fetchall(
                f"SELECT {_EXAM_SELECT_COLS} FROM exams WHERE guild_id = ? AND id != ?",
                (gid, exam_id),
            )
            overlaps: list[Exam] = []
            cand_start = created_exam.start_ts
            cand_end = created_exam.end_ts if created_exam.end_ts is not None else (cand_start + 3600)
            for r in overlap_rows:
                other = _row_to_exam(r, gid)
                if created_exam.is_all_day or other.is_all_day:
                    continue
                if (
                    other.visibility == "private"
                    and not is_admin
                    and not (int(created_by) > 0 and other.created_by == int(created_by))
                ):
                    continue
                o_start = other.start_ts
                o_end = other.end_ts if other.end_ts is not None else (o_start + 3600)
                if cand_start < o_end and o_start < cand_end:
                    overlaps.append(other)

        return ExamAddResult(exam=created_exam, is_duplicate=False, overlapping=tuple(overlaps))

    async def get_exam(self, guild_id: int, exam_id: int) -> Exam | None:
        row = await self.db.fetchone(
            f"SELECT {_EXAM_SELECT_COLS} FROM exams WHERE id = ? AND guild_id = ?",
            (int(exam_id), int(guild_id)),
        )
        return _row_to_exam(row, int(guild_id)) if row is not None else None

    async def list_exams(
        self,
        guild_id: int,
        *,
        course_filter: str | None = None,
        term_filter: str | None = None,
        upcoming_only: bool = False,
        viewer_id: int = 0,
        is_admin: bool = False,
        include_private: bool = True,
        now_ts: int | None = None,
    ) -> list[Exam]:
        """List guild exams sorted upcoming-first with course, term, and visibility filtering."""
        gid = int(guild_id)
        rows = await self.db.fetchall(
            f"SELECT {_EXAM_SELECT_COLS} FROM exams WHERE guild_id = ? ORDER BY start_ts ASC, id ASC",
            (gid,),
        )
        all_exams = [_row_to_exam(r, gid) for r in rows]

        norm_course = normalize_course_key(course_filter) if course_filter else ""
        course_sub = str(course_filter or "").strip().lower()
        term_sub = str(term_filter or "").strip().lower()
        ref_now = int(now_ts if now_ts is not None else self._now())
        viewer_int = int(viewer_id)

        filtered: list[Exam] = []
        for ex in all_exams:
            # Visibility guard
            if ex.visibility == "private":
                if not include_private:
                    continue
                is_owner = viewer_int > 0 and ex.created_by > 0 and ex.created_by == viewer_int
                if not (is_admin or is_owner):
                    continue
            # Course filter
            if norm_course or course_sub:
                key_match = bool(norm_course) and (
                    ex.course_key == norm_course or norm_course in ex.course_key
                )
                text_match = bool(course_sub) and (
                    course_sub in ex.course_code.lower() or course_sub in ex.course_name.lower()
                )
                if not (key_match or text_match):
                    continue
            # Term filter
            if term_sub and term_sub not in ex.term.lower():
                continue
            # Upcoming filter (treat an all-day event within the last 24h as still active/upcoming)
            cutoff = (
                ex.end_ts
                if ex.end_ts is not None
                else (ex.start_ts + 86400 if ex.is_all_day else ex.start_ts)
            )
            if upcoming_only and cutoff < ref_now:
                continue
            filtered.append(ex)

        # Upcoming-first deterministic sort: upcoming ascending, then past descending (most recent past first)
        def _sort_key(ex: Exam) -> tuple[int, int, int]:
            cutoff = (
                ex.end_ts
                if ex.end_ts is not None
                else (ex.start_ts + 86400 if ex.is_all_day else ex.start_ts)
            )
            if cutoff >= ref_now:
                return (0, ex.start_ts, ex.id)
            return (1, -ex.start_ts, ex.id)

        filtered.sort(key=_sort_key)
        return filtered

    async def remove_exam(self, guild_id: int, exam_id: int) -> Exam | None:
        """Atomically delete a guild-scoped exam by ID, returning the removed record or None."""
        gid = int(guild_id)
        async with self.db.write_transaction() as tx:
            row = await tx.fetchone(
                f"SELECT {_EXAM_SELECT_COLS} FROM exams WHERE id = ? AND guild_id = ?",
                (int(exam_id), gid),
            )
            if row is None:
                return None
            removed = _row_to_exam(row, gid)
            await tx.execute("DELETE FROM exams WHERE id = ? AND guild_id = ?", (int(exam_id), gid))
            return removed

    async def clear_exams(
        self,
        guild_id: int,
        *,
        preserve_manual: bool = False,
        term_filter: str | None = None,
    ) -> int:
        """Atomically delete exams for ``guild_id`` and return the count deleted."""
        gid = int(guild_id)
        clauses = ["guild_id = ?"]
        params: list[Any] = [gid]
        if preserve_manual:
            clauses.append("source != 'manual'")
        if term_filter and term_filter.strip():
            clauses.append("LOWER(term) = ?")
            params.append(term_filter.strip().lower())
        where_sql = " AND ".join(clauses)

        async with self.db.write_transaction() as tx:
            cnt_row = await tx.fetchone(f"SELECT COUNT(*) FROM exams WHERE {where_sql}", params)
            count = int(cnt_row[0]) if cnt_row else 0
            if count > 0:
                await tx.execute(f"DELETE FROM exams WHERE {where_sql}", params)
            return count

    async def list_course_suggestions(
        self,
        guild_id: int,
        partial: str = "",
        *,
        viewer_id: int = 0,
        is_admin: bool = False,
        include_private: bool = False,
    ) -> list[tuple[str, str]]:
        """Return up to 25 ``(course_code, label)`` autocomplete pairs from guild exams & catalog.

        By default (`include_private=False`), private exams are excluded so course autocomplete
        never leaks sensitive accommodation or private assessment titles to unauthorized users.
        """
        gid = int(guild_id)
        q = str(partial or "").strip().lower()
        q_norm = normalize_course_key(partial)
        seen: dict[str, str] = {}

        if include_private and is_admin:
            exam_rows = await self.db.fetchall(
                "SELECT DISTINCT course_code, course_name FROM exams WHERE guild_id = ? ORDER BY course_code ASC",
                (gid,),
            )
        elif include_private and int(viewer_id) > 0:
            exam_rows = await self.db.fetchall(
                """
                SELECT DISTINCT course_code, course_name
                FROM exams
                WHERE guild_id = ? AND (visibility != 'private' OR created_by = ?)
                ORDER BY course_code ASC
                """,
                (gid, int(viewer_id)),
            )
        else:
            exam_rows = await self.db.fetchall(
                """
                SELECT DISTINCT course_code, course_name
                FROM exams
                WHERE guild_id = ? AND visibility != 'private'
                ORDER BY course_code ASC
                """,
                (gid,),
            )
        for code, name in exam_rows:
            c = str(code or "").strip()
            n = str(name or "").strip()
            if c and c not in seen:
                seen[c] = f"{c} — {n}" if n else c

        cat_rows = await self.db.fetchall(
            "SELECT DISTINCT display_code, title FROM academic_courses WHERE guild_id = ? ORDER BY display_code ASC",
            (gid,),
        )
        for code, title in cat_rows:
            c = str(code or "").strip()
            t = str(title or "").strip()
            if c and c not in seen:
                seen[c] = f"{c} — {t}" if t else c

        out: list[tuple[str, str]] = []
        for code, label in seen.items():
            if not q or q in label.lower() or (q_norm and q_norm in normalize_course_key(code)):
                out.append((code, label[:100]))
            if len(out) >= 25:
                break
        return out

    async def import_drafts(
        self,
        *,
        guild_id: int,
        drafts: Sequence[ParsedExamDraft],
        parse_errors: Sequence[str] = (),
        created_by: int,
        channel_id: int = 0,
    ) -> ExamImportResult:
        """Atomically import validated drafts while preserving existing manual exams & skipping duplicates."""
        gid = int(guild_id)
        now_ts = self._now()
        imported: list[Exam] = []
        skipped_dups = 0
        errors = list(parse_errors)

        async with self.db.write_transaction() as tx:
            manual_row = await tx.fetchone(
                "SELECT COUNT(*) FROM exams WHERE guild_id = ? AND source = 'manual'",
                (gid,),
            )
            preserved_manual = int(manual_row[0]) if manual_row else 0

            for idx, draft in enumerate(drafts, start=1):
                clean_code = _clean_text(draft.course_code, 64)
                clean_name = _clean_text(draft.course_name, MAX_TEXT_FIELD_LEN)
                if not clean_code or not clean_name:
                    errors.append(f"Item {idx}: missing course_code or course_name.")
                    continue
                course_key = normalize_course_key(clean_code)

                dup_row = None
                if draft.external_uid:
                    dup_row = await tx.fetchone(
                        "SELECT id FROM exams WHERE guild_id = ? AND external_uid = ?",
                        (gid, draft.external_uid),
                    )
                if dup_row is None:
                    dup_row = await tx.fetchone(
                        """
                        SELECT id FROM exams
                        WHERE guild_id = ? AND course_key = ? AND start_ts = ? AND is_all_day = ?
                        """,
                        (gid, course_key, int(draft.start_ts), 1 if draft.is_all_day else 0),
                    )
                if dup_row is not None:
                    skipped_dups += 1
                    continue

                exam_id = await tx.execute_insert(
                    """
                    INSERT INTO exams (
                        guild_id, course_code, course_key, course_name, start_ts, end_ts,
                        organizer, location, notes, visibility, created_by, created_at,
                        term, is_all_day, source, channel_id, external_uid
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid,
                        clean_code,
                        course_key,
                        clean_name,
                        int(draft.start_ts),
                        int(draft.end_ts) if draft.end_ts is not None else None,
                        _clean_text(draft.organizer, 120),
                        _clean_text(draft.location, 160),
                        _clean_text(draft.notes, MAX_NOTES_FIELD_LEN),
                        draft.visibility,
                        int(created_by),
                        now_ts,
                        _clean_text(draft.term, 60),
                        1 if draft.is_all_day else 0,
                        draft.source,
                        int(channel_id),
                        _clean_text(draft.external_uid, 160),
                    ),
                )
                imported.append(
                    Exam(
                        id=exam_id,
                        guild_id=gid,
                        course_code=clean_code,
                        course_key=course_key,
                        course_name=clean_name,
                        start_ts=int(draft.start_ts),
                        end_ts=int(draft.end_ts) if draft.end_ts is not None else None,
                        organizer=_clean_text(draft.organizer, 120),
                        location=_clean_text(draft.location, 160),
                        notes=_clean_text(draft.notes, MAX_NOTES_FIELD_LEN),
                        visibility=draft.visibility,
                        created_by=int(created_by),
                        created_at=now_ts,
                        term=_clean_text(draft.term, 60),
                        is_all_day=bool(draft.is_all_day),
                        source=draft.source,
                        channel_id=int(channel_id),
                        external_uid=_clean_text(draft.external_uid, 160),
                    )
                )

        return ExamImportResult(
            imported=tuple(imported),
            skipped_duplicates=skipped_dups,
            errors=tuple(errors),
            preserved_manual_count=preserved_manual,
        )


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


# ---------------------------------------------------------------------------
# Opt-In Administrator-Configured Deadline Reminders (Idempotent & Deduplicated)
# ---------------------------------------------------------------------------


async def compute_due_exam_reminders(
    db: Database,
    *,
    guild_id: int,
    settings: Any,
    now_ts: int | None = None,
) -> list[DueExamReminder]:
    """Compute due, undelivered exam reminders ONLY when explicitly enabled by a guild admin.

    - Returns empty list if ``exam_reminders_enabled`` is False or ``exam_reminders_channel_id`` <= 0.
    - Never emits reminders for ``private`` exams into a shared channel.
    - Deduplicates via ``alert_delivery_dedupes`` (``alert_type='exam_reminder'``).
    """
    enabled = bool(getattr(settings, "exam_reminders_enabled", False))
    channel_id = int(getattr(settings, "exam_reminders_channel_id", 0) or 0)
    if not enabled or channel_id <= 0:
        return []

    raw_leads = getattr(settings, "exam_reminder_lead_hours", None)
    lead_hours_list = sorted(
        {int(h) for h in (raw_leads if raw_leads else (24, 2)) if int(h) >= 1},
        reverse=True,
    )
    if not lead_hours_list:
        return []

    ref_now = int(now_ts if now_ts is not None else time.time())
    store = ExamStore(db, clock=lambda: float(ref_now))
    upcoming = await store.list_exams(
        int(guild_id),
        upcoming_only=True,
        viewer_id=0,
        is_admin=False,
        include_private=False,  # Exclude private exams from public channel reminders
        now_ts=ref_now,
    )

    due: list[DueExamReminder] = []
    for ex in upcoming:
        if ex.visibility == "private":
            continue
        remaining_sec = ex.start_ts - ref_now
        if remaining_sec <= 0:
            continue
        for lead_h in lead_hours_list:
            if remaining_sec <= lead_h * 3600:
                dedupe_key = f"exam:{ex.id}:h{lead_h}"
                seen = await db.fetchone(
                    """
                    SELECT 1 FROM alert_delivery_dedupes
                    WHERE guild_id = ? AND alert_type = 'exam_reminder' AND dedupe_key = ?
                    """,
                    (int(guild_id), dedupe_key),
                )
                if seen is None:
                    due.append(
                        DueExamReminder(
                            guild_id=int(guild_id),
                            channel_id=channel_id,
                            exam=ex,
                            lead_hours=lead_h,
                            dedupe_key=dedupe_key,
                        )
                    )
    return due


async def mark_exam_reminder_delivered(
    db: Database,
    reminder: DueExamReminder,
    *,
    delivered_at: int | None = None,
) -> None:
    """Record reminder delivery in ``alert_delivery_dedupes`` so restarts never double-post."""
    ts = int(delivered_at if delivered_at is not None else time.time())
    await db.execute(
        """
        INSERT OR IGNORE INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at)
        VALUES (?, 'exam_reminder', ?, ?)
        """,
        (int(reminder.guild_id), reminder.dedupe_key, ts),
    )
