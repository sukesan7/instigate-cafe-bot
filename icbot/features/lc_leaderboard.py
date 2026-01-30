from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands
from zoneinfo import ZoneInfo

from icbot.app import ic
from icbot.permissions import is_admin_or_allowed
from icbot.embeds import system as system_embed

# ==========
# Constants
# ==========
TZ = ZoneInfo("America/Toronto")
PAGE_SIZE = 10
VIEW_TIMEOUT_SECONDS = 5 * 60  # 5 minutes

MODE_MONTH = "month"
MODE_ALL = "all"


def _year_month_key(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(TZ)
    return f"{now.year:04d}-{now.month:02d}"


# ==========
# Tracker (DB-backed)
# ==========

class ActivityTracker:
    """
    Tracks message counts:
      - All-time: activity_counts(guild_id, user_id, count)
      - Monthly:  activity_counts_monthly(guild_id, user_id, year_month, count)

    NOTE:
      This only counts messages the bot can SEE (channel perms apply).
    """

    def __init__(self, db):
        self.db = db
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return

            # All-time
            await self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_counts (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )

            # Monthly (current month key YYYY-MM)
            await self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_counts_monthly (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    year_month TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, year_month)
                )
                """
            )

            self._schema_ready = True

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        await self.ensure_schema()

        gid = message.guild.id
        uid = message.author.id
        ym = _year_month_key()

        # ---- All-time ----
        row = await self.db.fetchone(
            "SELECT count FROM activity_counts WHERE guild_id=? AND user_id=?",
            (gid, uid),
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO activity_counts (guild_id, user_id, count) VALUES (?, ?, ?)",
                (gid, uid, 1),
            )
        else:
            await self.db.execute(
                "UPDATE activity_counts SET count=? WHERE guild_id=? AND user_id=?",
                (int(row[0]) + 1, gid, uid),
            )

        # ---- Monthly ----
        rowm = await self.db.fetchone(
            "SELECT count FROM activity_counts_monthly WHERE guild_id=? AND user_id=? AND year_month=?",
            (gid, uid, ym),
        )
        if rowm is None:
            await self.db.execute(
                "INSERT INTO activity_counts_monthly (guild_id, user_id, year_month, count) VALUES (?, ?, ?, ?)",
                (gid, uid, ym, 1),
            )
        else:
            await self.db.execute(
                "UPDATE activity_counts_monthly SET count=? WHERE guild_id=? AND user_id=? AND year_month=?",
                (int(rowm[0]) + 1, gid, uid, ym),
            )

    async def _count_rows_all(self, guild_id: int) -> int:
        await self.ensure_schema()
        row = await self.db.fetchone(
            "SELECT COUNT(*) FROM activity_counts WHERE guild_id=?",
            (guild_id,),
        )
        return int(row[0]) if row else 0

    async def _count_rows_month(self, guild_id: int, year_month: str) -> int:
        await self.ensure_schema()
        row = await self.db.fetchone(
            "SELECT COUNT(*) FROM activity_counts_monthly WHERE guild_id=? AND year_month=?",
            (guild_id, year_month),
        )
        return int(row[0]) if row else 0

    async def top_all_time(self, guild_id: int, *, limit: int, offset: int) -> list[tuple[int, int]]:
        await self.ensure_schema()
        rows = await self.db.fetchall(
            """
            SELECT user_id, count
            FROM activity_counts
            WHERE guild_id=?
            ORDER BY count DESC
            LIMIT ? OFFSET ?
            """,
            (guild_id, limit, offset),
        )
        return [(int(uid), int(c)) for uid, c in rows]

    async def top_month(self, guild_id: int, year_month: str, *, limit: int, offset: int) -> list[tuple[int, int]]:
        await self.ensure_schema()
        rows = await self.db.fetchall(
            """
            SELECT user_id, count
            FROM activity_counts_monthly
            WHERE guild_id=? AND year_month=?
            ORDER BY count DESC
            LIMIT ? OFFSET ?
            """,
            (guild_id, year_month, limit, offset),
        )
        return [(int(uid), int(c)) for uid, c in rows]

    async def reset_guild(self, guild_id: int) -> None:
        await self.ensure_schema()
        await self.db.execute("DELETE FROM activity_counts WHERE guild_id=?", (guild_id,))
        await self.db.execute("DELETE FROM activity_counts_monthly WHERE guild_id=?", (guild_id,))


# ==========
# UI (Buttons)
# ==========

@dataclass
class LBState:
    mode: str
    page: int


class LeaderboardView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        tracker: ActivityTracker,
        settings,
        timeout_seconds: int = VIEW_TIMEOUT_SECONDS,
        mode: str = MODE_MONTH,
        page: int = 0,
    ):
        super().__init__(timeout=timeout_seconds)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.tracker = tracker
        self.settings = settings
        self.state = LBState(mode=mode, page=page)
        self.year_month = _year_month_key()
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

        # initial button states will be set on first render
        self._apply_button_styles()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the command author can use these buttons.", ephemeral=True)
            return False
        return True

    def _apply_button_styles(self) -> None:
        # Highlight active mode by button style; disable active mode button (optional)
        if self.state.mode == MODE_MONTH:
            self.month_btn.style = discord.ButtonStyle.primary
            self.all_btn.style = discord.ButtonStyle.secondary
        else:
            self.month_btn.style = discord.ButtonStyle.secondary
            self.all_btn.style = discord.ButtonStyle.primary

    async def _page_count(self) -> int:
        if self.state.mode == MODE_MONTH:
            total = await self.tracker._count_rows_month(self.guild_id, self.year_month)
        else:
            total = await self.tracker._count_rows_all(self.guild_id)
        return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    async def _fetch_rows(self) -> list[tuple[int, int]]:
        offset = self.state.page * PAGE_SIZE
        if self.state.mode == MODE_MONTH:
            return await self.tracker.top_month(self.guild_id, self.year_month, limit=PAGE_SIZE, offset=offset)
        return await self.tracker.top_all_time(self.guild_id, limit=PAGE_SIZE, offset=offset)

    async def _build_embed(self) -> discord.Embed:
        pages = await self._page_count()
        # Clamp page if data changed
        self.state.page = max(0, min(self.state.page, pages - 1))

        rows = await self._fetch_rows()
        if self.state.mode == MODE_MONTH:
            title = "📊 Leaderboard — This Month"
            subtitle = f"{self.year_month} • Messages sent"
        else:
            title = "🏁 Leaderboard — All Time"
            subtitle = "Messages sent (all-time)"

        start_rank = self.state.page * PAGE_SIZE + 1
        if rows:
            lines = [f"**{start_rank + i}.** <@{uid}> — **{c}** msgs" for i, (uid, c) in enumerate(rows)]
        else:
            lines = ["No activity tracked yet."]

        summary = "Most active users by message count."
        embed = system_embed(
            self.settings,
            title=title,
            summary=summary,
            subtitle=subtitle,
            fields=(("Leaderboard", "\n".join(lines)), ("Page", f"**{self.state.page + 1} / {pages}**")),
            footer_hint="Use buttons to browse",
        )
        return embed

    async def _sync_nav_buttons(self) -> None:
        pages = await self._page_count()
        self.prev_btn.disabled = self.state.page <= 0
        self.next_btn.disabled = self.state.page >= (pages - 1)

        self._apply_button_styles()

    def _renew(self) -> "LeaderboardView":
        """
        Reset timer on every click by creating a fresh View.
        (discord.py timeouts do not auto-reset on interaction.)
        """
        v = LeaderboardView(
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            tracker=self.tracker,
            settings=self.settings,
            timeout_seconds=VIEW_TIMEOUT_SECONDS,
            mode=self.state.mode,
            page=self.state.page,
        )
        v.message = self.message
        self.stop()
        return v

    async def _render(self, interaction: discord.Interaction) -> None:
        await self._sync_nav_buttons()
        embed = await self._build_embed()
        new_view = self._renew()
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def on_timeout(self) -> None:
        # Disable buttons after timeout (view-only)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    # ---- Buttons ----

    @discord.ui.button(label="This Month", style=discord.ButtonStyle.primary)
    async def month_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            self.state.mode = MODE_MONTH
            self.state.page = 0
            await self._render(interaction)

    @discord.ui.button(label="All Time", style=discord.ButtonStyle.secondary)
    async def all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            self.state.mode = MODE_ALL
            self.state.page = 0
            await self._render(interaction)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            self.state.page = max(0, self.state.page - 1)
            await self._render(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            self.state.page = self.state.page + 1
            await self._render(interaction)


# ==========
# Commands
# ==========

async def _send_interactive_leaderboard(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    tracker = interaction.client.activity_tracker  # type: ignore[attr-defined]
    settings = interaction.client.settings  # type: ignore[attr-defined]

    # Ensure schema exists before first render
    await tracker.ensure_schema()

    view = LeaderboardView(
        owner_id=interaction.user.id,
        guild_id=interaction.guild.id,
        tracker=tracker,
        settings=settings,
        timeout_seconds=VIEW_TIMEOUT_SECONDS,
        mode=MODE_MONTH,
        page=0,
    )

    # First render
    await view._sync_nav_buttons()
    embed = await view._build_embed()

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    view.message = msg


@ic.command(name="leaderboard", description="Interactive activity leaderboard (monthly + all-time).")
async def leaderboard_root(interaction: discord.Interaction):
    await _send_interactive_leaderboard(interaction)


# ==========
# Backwards-compatible /ic lc ... commands (optional)
# ==========

@ic.command(name="reset", description="Reset activity counts for this server (admin/allowed roles only).")
async def reset(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to reset activity.", ephemeral=True)
        return

    tracker = interaction.client.activity_tracker  # type: ignore[attr-defined]
    await tracker.reset_guild(interaction.guild.id)

    embed = system_embed(
        settings,
        title="✅ Activity Reset",
        summary="Monthly + all-time activity counts reset for this server.",
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)
