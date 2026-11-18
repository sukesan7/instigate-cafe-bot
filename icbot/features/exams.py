from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

from icbot.academics.models import Exam, ExamVisibility, normalize_course_key
from icbot.app import exam
from icbot.config import Settings
from icbot.container import extract_guild_id, get_container, resolve_guild_settings
from icbot.embeds import system
from icbot.permissions import is_admin_or_allowed
from icbot.registry import guard_feature

# Backwards-compatible alias for external callers referencing ExamItem
ExamItem = Exam

_EXAMS_PER_PAGE = 10


async def _get_settings_and_db(interaction: discord.Interaction) -> tuple[Settings | None, Any | None]:
    bot = interaction.client
    base_settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if base_settings is None or db is None:
        return None, None
    settings = await resolve_guild_settings(bot, interaction)
    return settings, db


def _zone(tz_name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        try:
            return ZoneInfo("America/Toronto")
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            return None


def _fmt_range(start_ts: int, end_ts: int | None, tz_name: str) -> str:
    tz = _zone(tz_name)
    if tz is None:
        s = datetime.fromtimestamp(start_ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if end_ts:
            e = datetime.fromtimestamp(end_ts, UTC).strftime("%H:%M UTC")
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
        dt = datetime(y, m, d, hh, mm, tzinfo=UTC)
        return int(dt.timestamp())

    dt = datetime(y, m, d, hh, mm, tzinfo=tz)
    return int(dt.timestamp())


async def _fetch_exams(db: Any, guild_id: int = 0) -> list[Exam]:
    rows = await db.fetchall(
        """
        SELECT id, guild_id, course_code, course_key, course_name, start_ts, end_ts,
               organizer, location, notes, visibility, created_by, created_at
        FROM exams
        WHERE guild_id = ?
        ORDER BY start_ts ASC
        """,
        (int(guild_id),),
    )
    out: list[Exam] = []
    for r in rows:
        vis_raw = str(r[10] or "guild").strip().lower()
        vis: ExamVisibility = (
            cast(ExamVisibility, vis_raw) if vis_raw in ("guild", "public", "private") else "guild"
        )
        out.append(
            Exam(
                id=int(r[0]),
                guild_id=int(r[1] or guild_id),
                course_code=str(r[2] or "").strip(),
                course_key=str(r[3] or "").strip() or normalize_course_key(str(r[2] or "")),
                course_name=str(r[4] or "").strip(),
                start_ts=int(r[5] or 0),
                end_ts=int(r[6]) if r[6] is not None else None,
                organizer=str(r[7] or "").strip(),
                location=str(r[8] or "").strip(),
                notes=str(r[9] or "").strip(),
                visibility=vis,
                created_by=int(r[11] or 0),
                created_at=int(r[12] or 0),
            )
        )
    return out


def _exam_field(exam_item: Exam, tz_name: str) -> tuple[str, str, bool]:
    name = f"{exam_item.course_code} — {exam_item.course_name}".strip(" —")
    when = _fmt_range(exam_item.start_ts, exam_item.end_ts, tz_name)
    parts = [f"**When:** {when}"]
    if exam_item.location:
        parts.append(f"**Where:** {exam_item.location}")
    if exam_item.organizer:
        parts.append(f"**Organizer:** {exam_item.organizer}")
    if exam_item.notes:
        parts.append(f"**Notes:** {exam_item.notes}")
    parts.append(f"**ID:** {exam_item.id}")
    return (name[:256], "\n".join(parts)[:1024], False)


def build_exam_schedule_embed(
    settings: Settings,
    exams: list[Exam],
    tz_name: str,
    *,
    page: int = 0,
    total_pages: int = 1,
) -> discord.Embed:
    """Build a Discord-limit-safe exam schedule embed for the given page."""
    start = page * _EXAMS_PER_PAGE
    shown = exams[start : start + _EXAMS_PER_PAGE]
    fields = [_exam_field(x, tz_name) for x in shown]
    summary = "Configured exams (ordered by date/time)."
    if total_pages > 1:
        summary += f" Showing {start + 1}–{start + len(shown)} of {len(exams)}."

    footer_hint = f"Exams • Page {page + 1}/{total_pages}" if total_pages > 1 else "Exams"
    return system(
        settings,
        title="Exam Schedule",
        summary=summary,
        fields=fields,
        footer_hint=footer_hint,
        subtitle=f"Timezone: {tz_name}",
    )


class ExamPaginationView(discord.ui.View):
    """Pagination view for guilds with more exams than fit on a single embed page."""

    def __init__(self, *, settings: Settings, exams: list[Exam], tz_name: str) -> None:
        super().__init__(timeout=180)
        self.settings = settings
        self.exams = exams
        self.tz_name = tz_name
        self.current_page = 0
        self.total_pages = max(1, (len(exams) + _EXAMS_PER_PAGE - 1) // _EXAMS_PER_PAGE)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    def current_embed(self) -> discord.Embed:
        return build_exam_schedule_embed(
            self.settings,
            self.exams,
            self.tz_name,
            page=self.current_page,
            total_pages=self.total_pages,
        )

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = max(0, self.current_page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


@exam.command(name="list", description="Show the current exam schedule.")
async def exams_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    gid = extract_guild_id(interaction) or int(getattr(settings, "guild_id", 0) or 0)

    exams = await _fetch_exams(db, gid)
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

    total_pages = max(1, (len(exams) + _EXAMS_PER_PAGE - 1) // _EXAMS_PER_PAGE)
    if total_pages > 1:
        view = ExamPaginationView(settings=settings, exams=exams, tz_name=tz_name)
        await interaction.followup.send(embed=view.current_embed(), view=view)
        return

    e = build_exam_schedule_embed(settings, exams, tz_name, page=0, total_pages=1)
    await interaction.followup.send(embed=e)


@exam.command(name="add", description="(Admin) Add an exam to the schedule.")
@app_commands.describe(
    course_code="e.g., COE817, CS 135, HIST-2010",
    course_name="e.g., Applied Cryptography",
    date="YYYY-MM-DD",
    start_time="HH:MM (24h)",
    end_time="HH:MM (24h) (optional)",
    location="Room / building / online link",
    notes="Optional notes (allowed materials, seating, etc.)",
    organizer="Optional instructor, department, or study group organizer",
    visibility="Exam schedule visibility (guild, public, or private)",
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
    organizer: str | None = None,
    visibility: Literal["guild", "public", "private"] = "guild",
) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    gid = extract_guild_id(interaction) or int(getattr(settings, "guild_id", 0) or 0)

    try:
        start_ts = _parse_date_time(date, start_time, tz_name)
        end_ts: int | None = None
        if end_time and str(end_time).strip():
            end_ts = _parse_date_time(date, str(end_time), tz_name)
            if end_ts < start_ts:
                raise ValueError("end_time must be after start_time")
    except (ValueError, TypeError) as e:
        await interaction.followup.send(
            f"Error: invalid date/time ({e}). Use date=YYYY-MM-DD and time=HH:MM (24h).",
            ephemeral=True,
        )
        return

    clean_code = (course_code or "").strip()
    course_key = normalize_course_key(clean_code)
    clean_vis = visibility if visibility in ("guild", "public", "private") else "guild"
    now_ts = int(get_container(interaction.client).clock())

    exam_id = await db.execute_insert(
        """
        INSERT INTO exams (
            guild_id, course_code, course_key, course_name, start_ts, end_ts,
            organizer, location, notes, visibility, created_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gid,
            clean_code,
            course_key,
            (course_name or "").strip(),
            int(start_ts),
            int(end_ts) if end_ts is not None else None,
            (organizer or "").strip(),
            (location or "").strip(),
            (notes or "").strip(),
            clean_vis,
            int(interaction.user.id),
            now_ts,
        ),
    )

    when = _fmt_range(start_ts, end_ts, tz_name)
    msg = (
        f"Added exam {clean_code} ({course_name.strip()})\n"
        f"- When: {when}\n"
        f"- Where: {(location or '').strip() or '(not set)'}\n"
        f"- ID: {exam_id}"
    )
    await interaction.followup.send(msg, ephemeral=True)


@exam.command(name="remove", description="(Admin) Remove an exam by ID.")
@app_commands.describe(exam_id="The exam ID shown in /ic exam list.")
async def exams_remove_cmd(interaction: discord.Interaction, exam_id: int) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    gid = extract_guild_id(interaction) or int(getattr(settings, "guild_id", 0) or 0)
    async with db.write_transaction() as tx:
        row = await tx.fetchone("SELECT id FROM exams WHERE id = ? AND guild_id = ?", (int(exam_id), gid))
        if not row:
            await interaction.followup.send(
                "Error: no exam found with that ID in this server.", ephemeral=True
            )
            return
        await tx.execute("DELETE FROM exams WHERE id = ? AND guild_id = ?", (int(exam_id), gid))
    await interaction.followup.send(f"Removed exam ID {exam_id}.", ephemeral=True)


@exam.command(name="clear", description="(Admin) Remove ALL exams.")
async def exams_clear_cmd(interaction: discord.Interaction) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    gid = extract_guild_id(interaction) or int(getattr(settings, "guild_id", 0) or 0)
    await db.execute("DELETE FROM exams WHERE guild_id = ?", (gid,))
    await interaction.followup.send("Cleared all exams for this server.", ephemeral=True)
