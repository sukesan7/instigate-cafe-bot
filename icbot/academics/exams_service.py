"""Exam scheduling SQLite persistence and opt-in deadline reminders.

Pure timezone/DST validation and sandboxed CSV/ICS codecs live in
``icbot.academics.exam_codecs`` and are re-exported here for full backwards
compatibility. All storage operations enforce strict guild boundaries and
atomic same-transaction SQLite ID retrieval.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from icbot.academics.exam_codecs import (
    CSV_EXPORT_HEADERS as CSV_EXPORT_HEADERS,
)
from icbot.academics.exam_codecs import (
    MAX_IMPORT_BYTES as MAX_IMPORT_BYTES,
)
from icbot.academics.exam_codecs import (
    MAX_IMPORT_EVENTS as MAX_IMPORT_EVENTS,
)
from icbot.academics.exam_codecs import (
    MAX_IMPORT_LINES as MAX_IMPORT_LINES,
)
from icbot.academics.exam_codecs import (
    MAX_NOTES_FIELD_LEN as MAX_NOTES_FIELD_LEN,
)
from icbot.academics.exam_codecs import (
    MAX_TEXT_FIELD_LEN as MAX_TEXT_FIELD_LEN,
)
from icbot.academics.exam_codecs import (
    DstResolution as DstResolution,
)
from icbot.academics.exam_codecs import (
    ExamImportError as ExamImportError,
)
from icbot.academics.exam_codecs import (
    ExamTimeValidationError as ExamTimeValidationError,
)
from icbot.academics.exam_codecs import (
    ParsedExamDraft as ParsedExamDraft,
)
from icbot.academics.exam_codecs import (
    _clean_text as _clean_text,
)
from icbot.academics.exam_codecs import (
    _csv_safe_cell as _csv_safe_cell,
)
from icbot.academics.exam_codecs import (
    _decode_and_check_sandbox as _decode_and_check_sandbox,
)
from icbot.academics.exam_codecs import (
    _escape_ics_text as _escape_ics_text,
)
from icbot.academics.exam_codecs import (
    _parse_ics_dt as _parse_ics_dt,
)
from icbot.academics.exam_codecs import (
    _split_ics_summary as _split_ics_summary,
)
from icbot.academics.exam_codecs import (
    _unescape_ics_text as _unescape_ics_text,
)
from icbot.academics.exam_codecs import (
    _unfold_ics_lines as _unfold_ics_lines,
)
from icbot.academics.exam_codecs import (
    export_exams_csv as export_exams_csv,
)
from icbot.academics.exam_codecs import (
    export_exams_ics as export_exams_ics,
)
from icbot.academics.exam_codecs import (
    format_exam_timestamp as format_exam_timestamp,
)
from icbot.academics.exam_codecs import (
    parse_exam_datetime as parse_exam_datetime,
)
from icbot.academics.exam_codecs import (
    parse_exams_csv as parse_exams_csv,
)
from icbot.academics.exam_codecs import (
    parse_exams_ics as parse_exams_ics,
)
from icbot.academics.exam_codecs import (
    resolve_zone as resolve_zone,
)
from icbot.academics.models import Exam, ExamSource, ExamVisibility, normalize_course_key
from icbot.db import Database


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
# Atomic Guild-Scoped ExamStore
# ---------------------------------------------------------------------------


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
