from __future__ import annotations

import asyncio
import logging

import discord

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed

log = logging.getLogger(__name__)

# ==========
# Constants
# ==========
PAGE_SIZE = 10
VIEW_TIMEOUT_SECONDS = 5 * 60  # 5 minutes


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
    ):
        super().__init__(timeout=timeout_seconds)
        self.owner_id = owner_id
        self.settings = settings
        self.snaps = snaps
        self.state = LBState(page=page)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._sync_nav_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the command author can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _page_count(self) -> int:
        return max(1, (len(self.snaps) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _sync_nav_buttons(self) -> None:
        pages = self._page_count()
        # Ensure page is clamped
        self.state.page = max(0, min(self.state.page, pages - 1))

        self.prev_btn.disabled = self.state.page <= 0
        self.next_btn.disabled = self.state.page >= (pages - 1)

    def _build_embed(self) -> discord.Embed:
        pages = self._page_count()
        offset = self.state.page * PAGE_SIZE
        page_snaps = self.snaps[offset : offset + PAGE_SIZE]

        start_rank = offset + 1
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
        )
        v.message = self.message
        self.stop()
        return v

    async def _render(self, interaction: discord.Interaction) -> None:
        self._sync_nav_buttons()
        embed = self._build_embed()
        new_view = self._renew()
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, RuntimeError) as exc:
                log.debug("Ignoring leaderboard timeout edit failure: %s", exc)

    # ---- Buttons ----

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
    )

    embed = view._build_embed()
    await interaction.followup.send(embed=embed, view=view)
    msg = await interaction.original_response()
    view.message = msg


@ic.command(name="leaderboard", description="Interactive balance & assets leaderboard.")
async def leaderboard_root(interaction: discord.Interaction):
    await _send_interactive_leaderboard(interaction)
