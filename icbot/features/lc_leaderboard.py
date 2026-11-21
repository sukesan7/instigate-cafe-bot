from __future__ import annotations

import asyncio
import logging

import discord

from icbot.app import ic
from icbot.container import extract_guild_id, get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.ui import (
    DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
    compute_page_slice,
    safe_edit_view_message,
    safe_timeout_disable,
    send_ephemeral_denial,
    verify_component_access,
)

log = logging.getLogger(__name__)

# ==========
# Constants
# ==========
PAGE_SIZE = 10
VIEW_TIMEOUT_SECONDS = int(DEFAULT_PAGINATOR_TIMEOUT_SECONDS)  # 180 seconds


# ==========
# UI (Buttons)
# ==========


class LBState:
    def __init__(self, page: int = 0):
        self.page = page


class LeaderboardView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        settings,
        snaps: list,
        timeout_seconds: int = VIEW_TIMEOUT_SECONDS,
        page: int = 0,
        guild_id: int = 0,
    ):
        super().__init__(timeout=timeout_seconds)
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id or 0)
        self.settings = settings
        self.snaps = snaps
        self.state = LBState(page=page)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._timed_out = False
        self._last_checked_interaction_id: int | None = None
        self._sync_nav_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        err = await verify_component_access(
            interaction,
            timed_out=self._timed_out,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            feature_key="economy",
            fallback_settings=self.settings,
            expired_message="⏳ This leaderboard view has expired. Run `/ic leaderboard` again.",
            guild_mismatch_message="❌ Command context mismatch.",
            owner_mismatch_message="❌ Only the command author can use these buttons.",
        )
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

    def _page_count(self) -> int:
        return compute_page_slice(self.snaps, 0, PAGE_SIZE).total_pages

    def _sync_nav_buttons(self) -> None:
        slice_info = compute_page_slice(self.snaps, self.state.page, PAGE_SIZE)
        self.state.page = slice_info.page
        if self._timed_out:
            self.prev_btn.disabled = True
            self.next_btn.disabled = True
            return
        self.prev_btn.disabled = not slice_info.has_prev
        self.next_btn.disabled = not slice_info.has_next

    def _build_embed(self) -> discord.Embed:
        slice_info = compute_page_slice(self.snaps, self.state.page, PAGE_SIZE)
        pages = slice_info.total_pages
        page_snaps = list(slice_info.items)
        start_rank = slice_info.start_index + 1

        if page_snaps:
            lines = []
            for i, snap in enumerate(page_snaps):
                lines.append(
                    f"**{start_rank + i}.** <@{snap.user_id}> — **{self.settings.currency_symbol}{snap.net_worth:,}** "
                    f"(cash: {self.settings.currency_symbol}{snap.cash:,}, investments: {self.settings.currency_symbol}{snap.long_value + snap.short_equity:,})"
                )
        else:
            lines = ["No users tracked yet."]

        summary = "Most active users by total assets."
        embed = system_embed(
            self.settings,
            title=f"🏦 {self.settings.embed_footer_text} Net-Worth Leaderboard",
            summary=summary,
            subtitle="Total Assets (Cash + Investments)",
            fields=(("Leaderboard", "\n".join(lines)), ("Page", f"**{self.state.page + 1} / {pages}**")),
            footer_hint="Asset Leaderboard",
        )
        return embed

    def _renew(self) -> LeaderboardView:
        v = LeaderboardView(
            owner_id=self.owner_id,
            settings=self.settings,
            snaps=self.snaps,
            timeout_seconds=VIEW_TIMEOUT_SECONDS,
            page=self.state.page,
            guild_id=self.guild_id,
        )
        v.message = self.message
        self.stop()
        return v

    async def _render(self, interaction: discord.Interaction) -> None:
        self._sync_nav_buttons()
        embed = self._build_embed()
        new_view = self._renew()
        await safe_edit_view_message(interaction, embed=embed, view=new_view)

    async def on_timeout(self) -> None:
        self._timed_out = True
        await safe_timeout_disable(self, self.message)

    # ---- Buttons ----

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_access(interaction):
            return
        async with self._lock:
            self.state.page = max(0, self.state.page - 1)
            await self._render(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_access(interaction):
            return
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

    await interaction.response.defer()

    settings = await resolve_guild_settings(interaction.client, interaction)
    fm = get_container(interaction.client).fun_market_store(settings)
    snaps = await fm.leaderboard_by_networth(limit=100)

    view = LeaderboardView(
        owner_id=interaction.user.id,
        settings=settings,
        snaps=snaps,
        timeout_seconds=VIEW_TIMEOUT_SECONDS,
        page=0,
        guild_id=extract_guild_id(interaction) or 0,
    )

    embed = view._build_embed()
    await interaction.followup.send(embed=embed, view=view)
    msg = await interaction.original_response()
    view.message = msg


@ic.command(name="leaderboard", description="Interactive balance & assets leaderboard.")
async def leaderboard_root(interaction: discord.Interaction):
    await _send_interactive_leaderboard(interaction)
