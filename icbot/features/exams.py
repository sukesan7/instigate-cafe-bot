from __future__ import annotations

import contextlib
import io
from typing import Any, Literal

import discord
from discord import app_commands

from icbot.academics.exams_service import (
    MAX_IMPORT_BYTES,
    DstResolution,
    ExamImportError,
    ExamStore,
    ExamTimeValidationError,
    ParsedExamDraft,
    export_exams_csv,
    export_exams_ics,
    format_exam_timestamp,
    parse_exam_datetime,
    parse_exams_csv,
    parse_exams_ics,
)
from icbot.academics.models import Exam
from icbot.app import exam
from icbot.config import Settings
from icbot.container import extract_guild_id, get_container, resolve_guild_settings
from icbot.embeds import system
from icbot.permissions import is_admin_or_allowed
from icbot.registry import guard_feature, is_feature_enabled
from icbot.ui import (
    DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
    BasePaginationView,
    compute_page_slice,
    resolve_live_view_settings,
    safe_edit_view_message,
    safe_timeout_disable,
    send_ephemeral_denial,
)

# Backwards-compatible alias for external callers referencing ExamItem
ExamItem = Exam

_EXAMS_PER_PAGE = 10

# Explicit Exam Data Visibility & Output-Channel Classification:
# - "guild" / "public": Visible to all members of the owning guild_id. Allowed in
#   public channel embeds (/ic exam list), guild-scoped CSV/ICS exports (/ic exam export),
#   guild course autocomplete, and opt-in admin-configured reminder channels.
# - "private": Restricted strictly to the exam creator (created_by == viewer_id > 0)
#   and authorized guild admins within the same guild_id. Allowed ONLY in ephemeral
#   interaction responses (ephemeral=True) bound to the authorized viewer. Must never
#   appear in public embeds, default exports, public autocomplete, or reminder broadcasts.
PUBLIC_EXAM_VISIBILITIES: frozenset[str] = frozenset({"guild", "public"})
PRIVATE_EXAM_VISIBILITY = "private"


async def _get_settings_and_db(interaction: discord.Interaction) -> tuple[Settings | None, Any | None]:
    bot = interaction.client
    base_settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if base_settings is None or db is None:
        return None, None
    settings = await resolve_guild_settings(bot, interaction)
    return settings, db


def _require_active_guild_id(interaction: discord.Interaction) -> tuple[int | None, str | None]:
    """Verify that the interaction comes from an active guild member context.

    Prevents DM interactions (`interaction.guild is None`), non-member users, or
    cross-guild member objects from falling back to a global `settings.guild_id`.
    """
    gid = extract_guild_id(interaction)
    if gid is None or getattr(interaction, "guild", None) is None:
        return None, "Error: `/ic exam` commands can only be used inside a Discord server channel."
    user = getattr(interaction, "user", None)
    if not isinstance(user, discord.Member):
        return None, "Error: you must be a server member to access this server's exam schedule."
    member_guild = getattr(user, "guild", None)
    member_gid = getattr(member_guild, "id", None) if member_guild is not None else None
    if isinstance(member_gid, int) and member_gid > 0 and int(member_gid) != int(gid):
        return None, "Error: guild context mismatch."
    return int(gid), None


async def _send_ephemeral_error(interaction: discord.Interaction, message: str) -> None:
    """Reliably send an ephemeral error message whether or not the interaction was already deferred."""
    with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden, RuntimeError):
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


async def _safe_edit_interaction_message(
    interaction: discord.Interaction,
    *,
    content: Any = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> bool:
    """Edit an interaction's message without crashing on deleted messages or Discord HTTP errors."""
    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(content=content, embed=embed, view=view)
        elif hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=content, embed=embed, view=view)
        elif getattr(interaction, "message", None) is not None:
            await interaction.message.edit(content=content, embed=embed, view=view)  # type: ignore[union-attr]
        return True
    except (discord.HTTPException, discord.NotFound, discord.Forbidden, RuntimeError):
        return False


def _parse_date_time(
    date_str: str,
    time_str: str | None,
    tz_name: str,
    *,
    all_day: bool = False,
    dst_resolution: DstResolution = "reject",
) -> int:
    """Backwards-compatible wrapper around strict DST-aware ``parse_exam_datetime``."""
    return parse_exam_datetime(
        date_str,
        time_str,
        tz_name,
        all_day=all_day,
        dst_resolution=dst_resolution,
    )


def _fmt_range(
    start_ts: int,
    end_ts: int | None,
    tz_name: str,
    *,
    is_all_day: bool = False,
    include_discord_markup: bool = True,
) -> str:
    return format_exam_timestamp(
        start_ts,
        end_ts,
        tz_name,
        is_all_day=is_all_day,
        include_discord_markup=include_discord_markup,
    )


async def _fetch_exams(db: Any, guild_id: int = 0) -> list[Exam]:
    """Backwards-compatible helper returning all guild exams ordered by start_ts."""
    store = ExamStore(db)
    return await store.list_exams(int(guild_id), is_admin=True)


def _exam_field(exam_item: Exam, tz_name: str) -> tuple[str, str, bool]:
    name = f"{exam_item.course_code} — {exam_item.course_name}".strip(" —")
    if exam_item.visibility == "private":
        name = f"🔒 {name}"
    when = format_exam_timestamp(
        exam_item.start_ts,
        exam_item.end_ts,
        tz_name,
        is_all_day=exam_item.is_all_day,
        include_discord_markup=True,
    )
    parts = [f"**When:** {when}"]
    if exam_item.term:
        parts.append(f"**Term:** {exam_item.term}")
    if exam_item.location:
        parts.append(f"**Where:** {exam_item.location}")
    if exam_item.organizer:
        parts.append(f"**Organizer:** {exam_item.organizer}")
    if exam_item.notes:
        parts.append(f"**Notes:** {exam_item.notes}")
    meta_bits: list[str] = [f"**ID:** {exam_item.id}"]
    if exam_item.source != "manual":
        meta_bits.append(f"Source: `{exam_item.source}`")
    if exam_item.created_by:
        meta_bits.append(f"Added by: <@{exam_item.created_by}>")
    parts.append(" • ".join(meta_bits))
    return (name[:256], "\n".join(parts)[:1024], False)


def build_exam_schedule_embed(
    settings: Settings,
    exams: list[Exam],
    tz_name: str,
    *,
    page: int = 0,
    total_pages: int = 1,
    filter_summary: str = "",
    allow_private: bool = False,
) -> discord.Embed:
    """Build a Discord-limit-safe exam schedule embed for the given page.

    Defense-in-depth invariant: when `allow_private=False` (any public response),
    exams with `visibility == 'private'` are stripped before rendering so private
    exam fields can never be serialized into a public embed.
    """
    safe_exams = [ex for ex in exams if allow_private or ex.visibility != PRIVATE_EXAM_VISIBILITY]
    slice_info = compute_page_slice(safe_exams, page, _EXAMS_PER_PAGE)
    shown = list(slice_info.items)
    eff_total_pages = max(1, total_pages or slice_info.total_pages)
    fields = [_exam_field(x, tz_name) for x in shown]
    summary = "Configured exams (upcoming first)."
    if filter_summary:
        summary += f" {filter_summary}"
    if eff_total_pages > 1:
        summary += (
            f" Showing {slice_info.start_index + 1}–{slice_info.start_index + len(shown)} "
            f"of {len(safe_exams)}."
        )

    footer_hint = f"Exams • Page {slice_info.page + 1}/{eff_total_pages}" if eff_total_pages > 1 else "Exams"
    return system(
        settings,
        title="Exam Schedule",
        summary=summary,
        fields=fields,
        footer_hint=footer_hint,
        subtitle=f"Timezone: {tz_name}",
    )


class ExamPaginationView(BasePaginationView):
    """Pagination view for guilds with more exams than fit on a single embed page."""

    def __init__(
        self,
        *,
        settings: Settings,
        exams: list[Exam],
        tz_name: str,
        filter_summary: str = "",
        owner_id: int = 0,
        guild_id: int = 0,
        allow_private: bool = False,
        timeout: float = DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
    ) -> None:
        self.allow_private = bool(allow_private)
        self.exams = [ex for ex in exams if self.allow_private or ex.visibility != PRIVATE_EXAM_VISIBILITY]
        self.tz_name = tz_name
        self.filter_summary = filter_summary
        slice_info = compute_page_slice(self.exams, 0, _EXAMS_PER_PAGE)
        super().__init__(
            settings=settings,
            total_pages=slice_info.total_pages,
            current_page=0,
            owner_id=owner_id,
            guild_id=guild_id,
            feature_key="academics",
            timeout=timeout,
            expired_message="Error: this exam schedule view has expired. Run `/ic exam list` again.",
            guild_mismatch_message="Error: guild context mismatch.",
            owner_mismatch_message="Error: only the user who requested this exam schedule may change pages.",
        )

    def _check_actor(self, interaction: discord.Interaction) -> str | None:
        if self._timed_out:
            return "Error: this exam schedule view has expired. Run `/ic exam list` again."
        gid, guild_err = _require_active_guild_id(interaction)
        if guild_err is not None or gid is None:
            return guild_err or "Error: guild context mismatch."
        if self.guild_id and int(gid) != int(self.guild_id):
            return "Error: guild context mismatch."
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if self.owner_id and int(user_id or 0) != int(self.owner_id):
            return "Error: only the user who requested this exam schedule may change pages."
        return None

    async def _check_access_error(self, interaction: discord.Interaction) -> str | None:
        err = self._check_actor(interaction)
        if err is not None:
            return err
        live_settings = await resolve_live_view_settings(interaction, self.settings)
        if not is_feature_enabled("academics", live_settings):
            return "Error: the academics module is currently disabled in this server."
        return None

    def build_current_embed(self) -> discord.Embed:
        return build_exam_schedule_embed(
            self.settings,
            self.exams,
            self.tz_name,
            page=self.current_page,
            total_pages=self.total_pages,
            filter_summary=self.filter_summary,
            allow_private=self.allow_private,
        )


class ExamClearConfirmView(discord.ui.View):
    """Explicit confirmation barrier before executing destructive ``/ic exam clear``."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        settings: Settings,
        db: Any,
        pending_count: int,
        preserve_manual: bool = False,
        term_filter: str | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.settings = settings
        self.db = db
        self.pending_count = int(pending_count)
        self.preserve_manual = bool(preserve_manual)
        self.term_filter = term_filter
        self.confirmed = False
        self.cancelled = False
        self.message: Any | None = None
        self._timed_out = False
        self._last_checked_interaction_id: int | None = None

    def _disable_all(self) -> None:
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True

    def _check_actor(
        self,
        interaction: discord.Interaction,
        live_settings: Settings | None = None,
    ) -> str | None:
        if self._timed_out:
            return "Error: this confirmation prompt has expired. Run `/ic exam clear` again."
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if int(user_id or 0) != self.owner_id:
            return "Error: only the administrator who initiated this clear action may confirm it."
        gid, guild_err = _require_active_guild_id(interaction)
        if guild_err is not None or gid is None:
            return guild_err or "Error: guild context mismatch."
        if int(gid) != self.guild_id:
            return "Error: guild context mismatch."
        eff_settings = live_settings or self.settings
        if not is_feature_enabled("academics", eff_settings):
            return "Error: the academics module is currently disabled in this server."
        if not is_admin_or_allowed(interaction, eff_settings):
            return "Error: you no longer have permission to modify exams."
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        live_settings = await resolve_live_view_settings(interaction, self.settings)
        err = self._check_actor(interaction, live_settings)
        if err is not None:
            self._last_checked_interaction_id = None
            await send_ephemeral_denial(interaction, err)
            return False
        self._last_checked_interaction_id = id(interaction)
        return True

    async def _ensure_access(self, interaction: discord.Interaction) -> bool:
        if self._last_checked_interaction_id == id(interaction):
            self._last_checked_interaction_id = None
            return True
        return await self.interaction_check(interaction)

    async def on_timeout(self) -> None:
        self._timed_out = True
        self._disable_all()
        await safe_timeout_disable(self, self.message)

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._ensure_access(interaction):
            return
        if self.confirmed or self.cancelled:
            await send_ephemeral_denial(interaction, "This confirmation has already been processed.")
            return

        store = ExamStore(self.db, clock=get_container(interaction.client).clock)
        deleted = await store.clear_exams(
            self.guild_id,
            preserve_manual=self.preserve_manual,
            term_filter=self.term_filter,
        )
        self.confirmed = True
        self._disable_all()
        self.stop()
        scope_note = f" matching term `{self.term_filter}`" if self.term_filter else ""
        if self.preserve_manual:
            scope_note += " (manual exams preserved)"
        await safe_edit_view_message(
            interaction,
            content=f"Cleared all exams for this server{scope_note} ({deleted} removed).",
            embed=None,
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._ensure_access(interaction):
            return
        if self.confirmed or self.cancelled:
            await send_ephemeral_denial(interaction, "This confirmation has already been processed.")
            return
        self.cancelled = True
        self._disable_all()
        self.stop()
        await safe_edit_view_message(
            interaction,
            content="Cancelled exam clear. No exams were removed.",
            embed=None,
            view=self,
        )


async def _exam_course_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Provide server-scoped autocomplete choices from existing public/guild exams and manual catalog courses."""
    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        return []
    bot = interaction.client
    db = getattr(bot, "db", None)
    if db is None:
        return []
    store = ExamStore(db)
    suggestions = await store.list_course_suggestions(
        gid,
        current,
        include_private=False,
    )
    return [app_commands.Choice(name=label[:100], value=code[:100]) for code, label in suggestions[:25]]


@exam.command(name="list", description="Show the current exam schedule.")
@app_commands.describe(
    course="Optional course code or title filter",
    term="Optional academic term filter (e.g., Fall 2026)",
    upcoming_only="Show only upcoming exams (hide past exams)",
    include_private="Include your private exams or admin-visible private exams (makes response ephemeral)",
)
@app_commands.autocomplete(course=_exam_course_autocomplete)
async def exams_cmd(
    interaction: discord.Interaction,
    course: str | None = None,
    term: str | None = None,
    upcoming_only: bool = False,
    include_private: bool = True,
) -> None:
    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(
            guild_err or "Error: `/ic exam list` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    container = get_container(interaction.client)
    store = ExamStore(db, clock=container.clock)
    viewer_is_admin = is_admin_or_allowed(interaction, settings)

    exams = await store.list_exams(
        gid,
        course_filter=course,
        term_filter=term,
        upcoming_only=bool(upcoming_only),
        viewer_id=interaction.user.id,
        is_admin=viewer_is_admin,
        include_private=bool(include_private),
        now_ts=int(container.clock()),
    )

    # Decide ephemeral status BEFORE interaction.response.defer because Discord
    # does not permit changing response audience safely after deferring.
    has_private_exams = any(ex.visibility == PRIVATE_EXAM_VISIBILITY for ex in exams)
    response_ephemeral = bool(has_private_exams)
    await interaction.response.defer(thinking=True, ephemeral=response_ephemeral)

    filter_parts: list[str] = []
    if course and course.strip():
        filter_parts.append(f"Course: `{course.strip()}`")
    if term and term.strip():
        filter_parts.append(f"Term: `{term.strip()}`")
    if upcoming_only:
        filter_parts.append("Upcoming only")
    filter_summary = f"Filters: {', '.join(filter_parts)}." if filter_parts else ""

    if not exams:
        empty_summary = (
            f"No exams match your filters ({', '.join(filter_parts)})."
            if filter_parts
            else "No exams have been configured yet."
        )
        e = system(
            settings,
            title="Exam Schedule",
            summary=empty_summary,
            footer_hint="Exams",
            subtitle=f"Timezone: {tz_name}",
        )
        await interaction.followup.send(embed=e, ephemeral=response_ephemeral)
        return

    total_pages = max(1, (len(exams) + _EXAMS_PER_PAGE - 1) // _EXAMS_PER_PAGE)
    if total_pages > 1:
        view = ExamPaginationView(
            settings=settings,
            exams=exams,
            tz_name=tz_name,
            filter_summary=filter_summary,
            owner_id=interaction.user.id,
            guild_id=gid,
            allow_private=response_ephemeral,
        )
        sent_msg = await interaction.followup.send(
            embed=view.current_embed(),
            view=view,
            ephemeral=response_ephemeral,
            wait=True,
        )
        view.message = sent_msg
        return

    e = build_exam_schedule_embed(
        settings,
        exams,
        tz_name,
        page=0,
        total_pages=1,
        filter_summary=filter_summary,
        allow_private=response_ephemeral,
    )
    await interaction.followup.send(embed=e, ephemeral=response_ephemeral)


@exam.command(name="add", description="(Admin) Add an exam to the schedule.")
@app_commands.describe(
    course_code="e.g., COE817, CS 135, HIST-2010",
    course_name="e.g., Applied Cryptography",
    date="YYYY-MM-DD",
    start_time="HH:MM (24h) — omit or pass 'all-day' when all_day=True",
    end_time="HH:MM (24h) (optional)",
    location="Room / building / online link",
    notes="Optional notes (allowed materials, seating, etc.)",
    organizer="Optional instructor, department, or study group organizer",
    visibility="Exam schedule visibility (guild, public, or private)",
    term="Optional academic term (e.g., Fall 2026)",
    all_day="Mark as an all-day event (no specific start/end clock time)",
    dst_resolution="How to resolve ambiguous DST fall-back times (reject, earlier, or later)",
)
@app_commands.autocomplete(course_code=_exam_course_autocomplete)
async def exams_add_cmd(
    interaction: discord.Interaction,
    course_code: str,
    course_name: str,
    date: str,
    start_time: str | None = None,
    end_time: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    organizer: str | None = None,
    visibility: Literal["guild", "public", "private"] = "guild",
    term: str | None = None,
    all_day: bool = False,
    dst_resolution: DstResolution = "reject",
) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.followup.send(
            guild_err or "Error: `/ic exam add` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    is_all_day = bool(all_day) or str(start_time or "").strip().lower() in ("all-day", "allday")

    try:
        start_ts = parse_exam_datetime(
            date,
            None if is_all_day else start_time,
            tz_name,
            all_day=is_all_day,
            dst_resolution=dst_resolution,
        )
        end_ts: int | None = None
        if not is_all_day and end_time and str(end_time).strip():
            end_ts = parse_exam_datetime(
                date,
                str(end_time),
                tz_name,
                all_day=False,
                dst_resolution=dst_resolution,
            )
            if end_ts < start_ts:
                raise ExamTimeValidationError("end_time must be after start_time")
    except (ExamTimeValidationError, ValueError, TypeError) as e:
        await interaction.followup.send(
            f"Error: invalid date/time ({e}). Use date=YYYY-MM-DD and time=HH:MM (24h).",
            ephemeral=True,
        )
        return

    clean_vis: Literal["guild", "public", "private"] = (
        visibility if visibility in ("guild", "public", "private") else "guild"
    )
    container = get_container(interaction.client)
    store = ExamStore(db, clock=container.clock)
    channel_id = int(getattr(interaction, "channel_id", 0) or 0)

    draft = ParsedExamDraft(
        course_code=(course_code or "").strip(),
        course_name=(course_name or "").strip(),
        start_ts=start_ts,
        end_ts=end_ts,
        is_all_day=is_all_day,
        location=(location or "").strip(),
        notes=(notes or "").strip(),
        term=(term or "").strip(),
        organizer=(organizer or "").strip(),
        visibility=clean_vis,
        source="manual",
    )

    try:
        add_res = await store.add_exam(
            guild_id=gid,
            draft=draft,
            created_by=interaction.user.id,
            channel_id=channel_id,
            reject_duplicate=True,
            is_admin=True,
        )
    except ValueError as exc:
        await interaction.followup.send(f"Error: {exc}", ephemeral=True)
        return

    if add_res.is_duplicate:
        await interaction.followup.send(
            f"Duplicate exam already exists for {add_res.exam.course_code} at that time (ID: {add_res.exam.id}).",
            ephemeral=True,
        )
        return

    ex = add_res.exam
    when = format_exam_timestamp(
        ex.start_ts,
        ex.end_ts,
        tz_name,
        is_all_day=ex.is_all_day,
        include_discord_markup=True,
    )
    lines = [
        f"Added exam {ex.course_code} ({ex.course_name})",
        f"- When: {when}",
        f"- Where: {ex.location or '(not set)'}",
    ]
    if ex.term:
        lines.append(f"- Term: {ex.term}")
    if ex.visibility != "guild":
        lines.append(f"- Visibility: {ex.visibility}")
    lines.append(f"- ID: {ex.id}")
    if add_res.overlapping:
        ov_str = ", ".join(f"{o.course_code} (ID {o.id})" for o in add_res.overlapping[:3])
        lines.append(f"⚠️ Note: overlaps with existing exam(s): {ov_str}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@exam.command(name="remove", description="(Admin) Remove an exam by ID.")
@app_commands.describe(exam_id="The exam ID shown in /ic exam list.")
async def exams_remove_cmd(interaction: discord.Interaction, exam_id: int) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.followup.send(
            guild_err or "Error: `/ic exam remove` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    store = ExamStore(db, clock=get_container(interaction.client).clock)
    removed = await store.remove_exam(gid, int(exam_id))
    if removed is None:
        await interaction.followup.send("Error: no exam found with that ID in this server.", ephemeral=True)
        return
    await interaction.followup.send(f"Removed exam ID {removed.id}.", ephemeral=True)


@exam.command(name="clear", description="(Admin) Remove exams for this server (requires confirmation).")
@app_commands.describe(
    term="Optional term filter — only clear exams in this term",
    preserve_manual="Keep manually created exams and remove only imported exams",
)
async def exams_clear_cmd(
    interaction: discord.Interaction,
    term: str | None = None,
    preserve_manual: bool = False,
) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.followup.send(
            guild_err or "Error: `/ic exam clear` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    store = ExamStore(db, clock=get_container(interaction.client).clock)
    matching = await store.list_exams(
        gid,
        term_filter=term,
        viewer_id=interaction.user.id,
        is_admin=True,
        include_private=True,
    )
    if preserve_manual:
        matching = [e for e in matching if e.source != "manual"]

    if not matching:
        await interaction.followup.send("No matching exams found to clear for this server.", ephemeral=True)
        return

    view = ExamClearConfirmView(
        owner_id=interaction.user.id,
        guild_id=gid,
        settings=settings,
        db=db,
        pending_count=len(matching),
        preserve_manual=bool(preserve_manual),
        term_filter=term.strip() if term and term.strip() else None,
    )
    scope_desc = f" in term `{term.strip()}`" if term and term.strip() else ""
    if preserve_manual:
        scope_desc += " (imported only; manual exams preserved)"
    embed = system(
        settings,
        title="⚠️ Confirm Destructive Exam Clear",
        summary=(
            f"You are about to permanently remove **{len(matching)}** exam(s){scope_desc} "
            f"from this server.\n\nClick **Confirm Clear** below to proceed, or **Cancel** to abort."
        ),
        footer_hint="Destructive Action Confirmation",
    )
    sent_msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
    view.message = sent_msg


@exam.command(name="import", description="(Admin) Import exams from a CSV or ICS file/snippet (offline).")
@app_commands.describe(
    fmt="Input format: csv or ics",
    file="Optional uploaded CSV/ICS file (max 128 KiB, no external downloads)",
    data="Optional inline CSV/ICS text snippet",
    dst_resolution="How to resolve ambiguous DST fall-back times (reject, earlier, or later)",
)
async def exams_import_cmd(
    interaction: discord.Interaction,
    fmt: Literal["csv", "ics"],
    file: discord.Attachment | None = None,
    data: str | None = None,
    dst_resolution: DstResolution = "reject",
) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.followup.send(
            guild_err or "Error: `/ic exam import` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    if not is_admin_or_allowed(interaction, settings):
        await interaction.followup.send("Error: you do not have permission to modify exams.", ephemeral=True)
        return

    raw_payload: bytes | str | None = None
    if file is not None:
        if getattr(file, "size", 0) > MAX_IMPORT_BYTES:
            await interaction.followup.send(
                f"Error: attachment exceeds maximum allowed import size ({MAX_IMPORT_BYTES // 1024} KiB).",
                ephemeral=True,
            )
            return
        raw_payload = await file.read()
    elif data is not None and data.strip():
        raw_payload = data
    else:
        await interaction.followup.send(
            "Error: provide either a CSV/ICS file attachment or inline data snippet.",
            ephemeral=True,
        )
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    channel_id = int(getattr(interaction, "channel_id", 0) or 0)

    try:
        if fmt == "csv":
            drafts, parse_errors = parse_exams_csv(
                raw_payload, tz_name=tz_name, dst_resolution=dst_resolution
            )
        else:
            drafts, parse_errors = parse_exams_ics(
                raw_payload, tz_name=tz_name, dst_resolution=dst_resolution
            )
    except ExamImportError as exc:
        await interaction.followup.send(f"Error importing {fmt.upper()}: {exc}", ephemeral=True)
        return

    store = ExamStore(db, clock=get_container(interaction.client).clock)
    result = await store.import_drafts(
        guild_id=gid,
        drafts=drafts,
        parse_errors=parse_errors,
        created_by=interaction.user.id,
        channel_id=channel_id,
    )

    summary_lines = [
        f"Imported **{len(result.imported)}** exam(s) from `{fmt.upper()}`.",
        f"- Skipped duplicates: **{result.skipped_duplicates}**",
        f"- Existing manual exams preserved: **{result.preserved_manual_count}**",
    ]
    if result.errors:
        preview = "\n".join(f"• {e}" for e in result.errors[:5])
        more = f"\n… and {len(result.errors) - 5} more." if len(result.errors) > 5 else ""
        summary_lines.append(f"- Validation errors ({len(result.errors)}):\n{preview}{more}")

    embed = system(
        settings,
        title="📥 Exam Import Summary",
        summary="\n".join(summary_lines),
        footer_hint="Offline Exam Import",
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@exam.command(name="export", description="Export this server's exam schedule as CSV or ICS.")
@app_commands.describe(
    fmt="Output format: ics or csv",
    course="Optional course filter",
    term="Optional academic term filter",
    upcoming_only="Export only upcoming exams",
    include_private="Include your private exams or admin-visible private exams (default: False)",
)
@app_commands.autocomplete(course=_exam_course_autocomplete)
async def exams_export_cmd(
    interaction: discord.Interaction,
    fmt: Literal["ics", "csv"] = "ics",
    course: str | None = None,
    term: str | None = None,
    upcoming_only: bool = False,
    include_private: bool = False,
) -> None:
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    gid, guild_err = _require_active_guild_id(interaction)
    if guild_err is not None or gid is None:
        await interaction.followup.send(
            guild_err or "Error: `/ic exam export` can only be used inside a server.",
            ephemeral=True,
        )
        return

    settings, db = await _get_settings_and_db(interaction)
    if settings is None or db is None:
        await interaction.followup.send("Error: bot not initialized (missing settings/db).", ephemeral=True)
        return

    tz_name = str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto")
    container = get_container(interaction.client)
    store = ExamStore(db, clock=container.clock)
    allow_private_export = bool(include_private)

    exams = await store.list_exams(
        gid,
        course_filter=course,
        term_filter=term,
        upcoming_only=bool(upcoming_only),
        viewer_id=interaction.user.id,
        is_admin=is_admin_or_allowed(interaction, settings),
        include_private=allow_private_export,
        now_ts=int(container.clock()),
    )
    if not exams:
        await interaction.followup.send("No exams available to export.", ephemeral=True)
        return

    if fmt == "csv":
        content = export_exams_csv(exams, tz_name=tz_name, include_private=allow_private_export)
        filename = f"exams_{gid}.csv"
    else:
        content = export_exams_ics(exams, tz_name=tz_name, include_private=allow_private_export)
        filename = f"exams_{gid}.ics"

    buf = io.BytesIO(content.encode("utf-8"))
    attachment = discord.File(buf, filename=filename)
    await interaction.followup.send(
        content=f"Exported **{len(exams)}** exam(s) (`{fmt.upper()}`, timezone `{tz_name}`).",
        file=attachment,
        ephemeral=True,
    )
