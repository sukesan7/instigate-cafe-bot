from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands

from icbot.app import exam
from icbot.config import Settings
from icbot.embeds import system
from icbot.permissions import is_admin_or_allowed


@dataclass(frozen=True)
class ExamItem:
    id: int
    course_code: str
    course_name: str
    start_ts: int
    end_ts: Optional[int]
    location: str
    notes: str


def _get_settings_and_db(interaction: discord.Interaction) -> tuple[Settings | None, Any | None]:
    bot = interaction.client
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    return settings, db


def _zone(tz_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo("America/Toronto")
        except Exception:
            return None


def _fmt_range(start_ts: int, end_ts: Optional[int], tz_name: str) -> str:
    tz = _zone(tz_name)
    if tz is None:
        s = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if end_ts:
            e = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%H:%M UTC")
            return f"{s} – {e}"
        return s

    s_dt = datetime.fromtimestamp(start_ts, tz)
    if end_ts:
        e_dt = datetime.fromtimestamp(end_ts, tz)
        if s_dt.date() == e_dt.date():
            return f"{s_dt.strftime('%a %b %d, %Y • %I:%M %p')} – {e_dt.strftime('%I:%M %p')}"
        return f"{s_dt.strftime('%a %b %d, %Y • %I:%M %p')} – {e_dt.strftime('%a %b %d, %Y • %I:%M %p')}"
    return s_dt.strftime("%a %b %d, %Y • %I:%M %p")


def _parse_date_time(date_str: str, time_str: str, tz_name: str) -> int:
    """
    Parse (YYYY-MM-DD, HH:MM) in the configured timezone and return unix seconds.
    Accepts 24-hour time.
    """
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise ValueError("date must be YYYY-MM-DD")
    if not re.fullmatch(r"\d{2}:\d{2}", time_str):
        raise ValueError("time must be HH:MM (24h)")

    y, m, d = [int(x) for x in date_str.split("-")]
    hh, mm = [int(x) for x in time_str.split(":")]
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("invalid time")

    tz = _zone(tz_name)
    if tz is None:
        dt = datetime(y, m, d, hh, mm, tzinfo=timezone.utc)
        return int(dt.timestamp())

    dt = datetime(y, m, d, hh, mm, tzinfo=tz)
    return int(dt.timestamp())


async def _fetch_exams(db: Any) -> list[ExamItem]:
    rows = await db.fetchall(
        """
        SELECT id, course_code, course_name, start_ts, end_ts, location, notes
        FROM exams
        ORDER BY start_ts ASC
        """
    )
    out: list[ExamItem] = []
    for r in rows:
        out.append(
            ExamItem(
                id=int(r[0]),
                course_code=str(r[1] or "").strip(),
                course_name=str(r[2] or "").strip(),
                start_ts=int(r[3] or 0),
                end_ts=int(r[4]) if r[4] is not None else None,
                location=str(r[5] or "").strip(),
                notes=str(r[6] or "").strip(),
            )
        )
    return out


def _exam_field(exam: ExamItem, tz_name: str) -> tuple[str, str, bool]:
    name = f"{exam.course_code} — {exam.course_name}".strip(" —")
    when = _fmt_range(exam.start_ts, exam.end_ts, tz_name)
    parts = [f"**When:** {when}"]
    if exam.location:
        parts.append(f"**Where:** {exam.location}")
    if exam.notes:
        parts.append(f"**Notes:** {exam.notes}")
    parts.append(f"**ID:** {exam.id}")
    return (name[:256], "\n".join(parts)[:1024], False)


@exam.command(name="list", description="Show the current exam schedule.")
async def exams_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    settings, db = _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")

    exams = await _fetch_exams(db)
    if not exams:
        e = system(
            settings,
            title="Exam Schedule",
            summary="No exams have been configured yet.",
            footer_hint="Exams",
            subtitle=f"Timezone: {tz_name}",
        )
        await interaction.followup.send(embed=e)
        return

    # Keep within embed field limits
    max_items = 12
    shown = exams[:max_items]
    extra = len(exams) - len(shown)

    fields = [_exam_field(x, tz_name) for x in shown]
    summary = "Configured exams (ordered by date/time)."
    if extra > 0:
        summary += f" Showing {len(shown)} of {len(exams)}."

    e = system(
        settings,
        title="Exam Schedule",
        summary=summary,
        fields=fields,
        footer_hint="Exams",
        subtitle=f"Timezone: {tz_name}",
    )
    await interaction.followup.send(embed=e)


@exam.command(name="add", description="(Admin) Add an exam to the schedule.")
@app_commands.describe(
    course_code="e.g., COE817",
    course_name="e.g., Applied Cryptography",
    date="YYYY-MM-DD",
    start_time="HH:MM (24h)",
    end_time="HH:MM (24h) (optional)",
    location="Room / building / online link",
    notes="Optional notes (allowed materials, seating, etc.)",
)
async def exams_add_cmd(
    interaction: discord.Interaction,
    course_code: str,
    course_name: str,
    date: str,
    start_time: str,
    end_time: str | None = None,
    location: str | None = None,
    notes: str | None = None,
) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")

    try:
        start_ts = _parse_date_time(date, start_time, tz_name)
        end_ts: int | None = None
        if end_time and str(end_time).strip():
            end_ts = _parse_date_time(date, str(end_time), tz_name)
            if end_ts < start_ts:
                raise ValueError("end_time must be after start_time")
    except Exception as e:
        await interaction.followup.send(
            f"Error: invalid date/time ({e}). Use date=YYYY-MM-DD and time=HH:MM (24h).",
            ephemeral=True,
        )
        return

    now_ts = int(datetime.utcnow().timestamp())
    await db.execute(
        """
        INSERT INTO exams (course_code, course_name, start_ts, end_ts, location, notes, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (course_code or "").strip(),
            (course_name or "").strip(),
            int(start_ts),
            int(end_ts) if end_ts is not None else None,
            (location or "").strip(),
            (notes or "").strip(),
            int(interaction.user.id),
            now_ts,
        ),
    )

    row = await db.fetchone("SELECT last_insert_rowid()")
    exam_id = int(row[0]) if row and row[0] is not None else 0

    when = _fmt_range(start_ts, end_ts, tz_name)
    msg = (
        f"Added exam {course_code.strip()} ({course_name.strip()})\n"
        f"- When: {when}\n"
        f"- Where: {(location or '').strip() or '(not set)'}\n"
        f"- ID: {exam_id}"
    )
    await interaction.followup.send(msg, ephemeral=True)


@exam.command(name="remove", description="(Admin) Remove an exam by ID.")
@app_commands.describe(exam_id="The exam ID shown in /ic exams.")
async def exams_remove_cmd(interaction: discord.Interaction, exam_id: int) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    row = await db.fetchone("SELECT id FROM exams WHERE id = ?", (int(exam_id),))
    if not row:
        await interaction.followup.send("Error: no exam found with that ID.", ephemeral=True)
        return

    await db.execute("DELETE FROM exams WHERE id = ?", (int(exam_id),))
    await interaction.followup.send(f"Removed exam ID {exam_id}.", ephemeral=True)


@exam.command(name="clear", description="(Admin) Remove ALL exams.")
async def exams_clear_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    await db.execute("DELETE FROM exams")
    await interaction.followup.send("Cleared all exams.", ephemeral=True)
