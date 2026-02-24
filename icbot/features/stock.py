from __future__ import annotations

import discord
from discord import app_commands

from icbot.app import ic
from icbot.utils import parse_signed_int
from icbot.permissions import is_admin_or_allowed
from icbot.embeds import stock as stock_embed
from icbot.services.fun_market import FunMarketStore, FunMarketError


async def _post_optional_announcement(interaction: discord.Interaction, embed: discord.Embed) -> None:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if settings.stock_channel_id and interaction.guild:
        ch = interaction.guild.get_channel(settings.stock_channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass


class StockStore:
    """Legacy $IC wrapper kept for backwards-compatible commands.

    The source of truth now lives in the Fun Market table (`fun_stocks` symbol IC).
    We still mirror updates into legacy `meta.stock_value` + `stock_events` so existing
    features/reports/history remain intact.
    """

    def __init__(self, db, *, starting_bankroll: int = 1000, tz_name: str = "America/Toronto"):
        self.fun = FunMarketStore(db, starting_bankroll=starting_bankroll, tz_name=tz_name)

    async def get_value(self) -> int:
        s = await self.fun.get_stock("IC")
        return int(s.price)

    async def set_value(self, user_id: int, new_value: int, reason: str | None = None) -> int:
        s = await self.fun.admin_set(symbol="IC", new_value=max(1, int(new_value)), user_id=user_id, reason=reason)
        return int(s.price)

    async def add_delta(self, user_id: int, delta: int, reason: str | None = None) -> int:
        s = await self.fun.admin_move(symbol="IC", delta=int(delta), user_id=user_id, reason=reason)
        return int(s.price)

    async def history(self, limit: int = 10) -> list[tuple]:
        # Keep using the legacy history table for compatibility with old entries.
        limit = max(1, min(limit, 25))
        return await self.fun.db.fetchall(  # type: ignore[attr-defined]
            "SELECT ts_unix, user_id, delta, new_value, reason FROM stock_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )


def _make_store(interaction: discord.Interaction) -> StockStore:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    return StockStore(
        interaction.client.db,  # type: ignore[attr-defined]
        starting_bankroll=int(getattr(settings, "bj_starting_bankroll", 1000) or 1000),
        tz_name=str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto"),
    )


@ic.command(name="stock", description="Show the current Instigate Cafe stock price.")
async def stock(interaction: discord.Interaction):
    store = _make_store(interaction)
    try:
        val = await store.get_value()
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title="📈 Instigate Cafe Stock", headline=f"**${val}**\n*Current price*")
    await interaction.response.send_message(embed=embed)


@ic.command(name="stock_change", description="Change the $IC stock by a delta (e.g., +100 or -50). Admin/allowed roles only.")
@app_commands.describe(change="Examples: +100, -50, 25", reason="Optional: why did it move?")
async def stock_change(interaction: discord.Interaction, change: str, reason: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to move the stock.", ephemeral=True)
        return

    delta = parse_signed_int(change)
    if delta is None:
        await interaction.response.send_message("❌ Invalid change. Use `+100`, `-50`, or `25`.", ephemeral=True)
        return

    store = _make_store(interaction)
    try:
        new_val = await store.add_delta(interaction.user.id, delta, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    title = "📈 $IC Stock Up" if delta >= 0 else "📉 $IC Stock Down"
    headline = f"**{delta:+d} → ${new_val}**"
    embed = stock_embed(settings, title=title, headline=headline, reason=reason, footer_hint=f"Moved by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, embed)


@ic.command(name="stock_set", description="Set the $IC stock to an exact value. Admin/allowed roles only.")
@app_commands.describe(value="New stock price (1+)", reason="Optional: why set it?")
async def stock_set(interaction: discord.Interaction, value: int, reason: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to set the stock.", ephemeral=True)
        return

    store = _make_store(interaction)
    try:
        new_val = await store.set_value(interaction.user.id, value, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    embed = stock_embed(settings, title="🧾 $IC Stock Set", headline=f"**${new_val}**", reason=reason, footer_hint=f"Set by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, embed)


@ic.command(name="stock_history", description="Show recent $IC stock moves.")
@app_commands.describe(limit="How many entries (max 25)")
async def stock_history(interaction: discord.Interaction, limit: int = 10):
    store = _make_store(interaction)
    rows = await store.history(limit=limit)
    if not rows:
        await interaction.response.send_message("No history yet.")
        return

    lines = []
    for ts_unix, user_id, delta, new_value, reason in rows:
        t = f"<t:{ts_unix}:R>"
        reason_part = f" — {reason}" if reason else ""
        lines.append(f"{t} • <@{user_id}>: **{int(delta):+d}** → **${int(new_value)}**{reason_part}")

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title="🕰️ $IC Stock History", headline="Recent moves", reason="\n".join(lines[:25]))
    await interaction.response.send_message(embed=embed)
