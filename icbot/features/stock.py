from __future__ import annotations
import time
import discord
from discord import app_commands

from icbot.app import ic
from icbot.utils import clamp_nonnegative, parse_signed_int
from icbot.permissions import is_admin_or_allowed
from icbot.embeds import stock as stock_embed


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
    def __init__(self, db):
        self.db = db

    async def get_value(self) -> int:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key='stock_value'")
        return int(row[0]) if row else 0

    async def set_value(self, user_id: int, new_value: int, reason: str | None = None) -> int:
        new_value = clamp_nonnegative(new_value)
        old = await self.get_value()
        delta = new_value - old
        ts = int(time.time())
        await self.db.execute_many([
            ("UPDATE meta SET value=? WHERE key='stock_value'", (str(new_value),)),
            ("INSERT INTO stock_events (ts_unix, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?)",
             (ts, user_id, delta, new_value, reason)),
        ])
        return new_value

    async def add_delta(self, user_id: int, delta: int, reason: str | None = None) -> int:
        current = await self.get_value()
        new_value = clamp_nonnegative(current + delta)
        ts = int(time.time())
        await self.db.execute_many([
            ("UPDATE meta SET value=? WHERE key='stock_value'", (str(new_value),)),
            ("INSERT INTO stock_events (ts_unix, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?)",
             (ts, user_id, delta, new_value, reason)),
        ])
        return new_value

    async def history(self, limit: int = 10) -> list[tuple]:
        limit = max(1, min(limit, 25))
        return await self.db.fetchall(
            "SELECT ts_unix, user_id, delta, new_value, reason FROM stock_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )


@ic.command(name="stock", description="Show the current Instigate Cafe stock price.")
async def stock(interaction: discord.Interaction):
    store = StockStore(interaction.client.db)  # type: ignore[attr-defined]
    val = await store.get_value()
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title="📈 Instigate Cafe Stock", headline=f"**${val}**\n*Current price*")
    await interaction.response.send_message(embed=embed)


@ic.command(name="stock_change", description="Change the IC stock by a delta (e.g., +100 or -50). Admin/allowed roles only.")
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

    store = StockStore(interaction.client.db)  # type: ignore[attr-defined]
    new_val = await store.add_delta(interaction.user.id, delta, reason=reason)

    sign = "+" if delta >= 0 else ""
    reason_line = f"\n**Reason:** {reason}" if reason else ""
    title = "📈 IC Stock Up" if delta >= 0 else "📉 IC Stock Down"
    headline = f"**{sign}{delta} → ${new_val}**"
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title=title, headline=headline, reason=reason, footer_hint=f"Moved by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, embed)


@ic.command(name="stock_set", description="Set the IC stock to an exact value. Admin/allowed roles only.")
@app_commands.describe(value="New stock price (0+)", reason="Optional: why set it?")
async def stock_set(interaction: discord.Interaction, value: int, reason: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to set the stock.", ephemeral=True)
        return

    store = StockStore(interaction.client.db)  # type: ignore[attr-defined]
    new_val = await store.set_value(interaction.user.id, value, reason=reason)

    reason_line = f"\n**Reason:** {reason}" if reason else ""
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title="🧾 IC Stock Set", headline=f"**${new_val}**", reason=reason, footer_hint=f"Set by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, embed)


@ic.command(name="stock_history", description="Show recent IC stock moves.")
@app_commands.describe(limit="How many entries (max 25)")
async def stock_history(interaction: discord.Interaction, limit: int = 10):
    store = StockStore(interaction.client.db)  # type: ignore[attr-defined]
    rows = await store.history(limit=limit)
    if not rows:
        await interaction.response.send_message("No history yet.")
        return

    lines = []
    for ts_unix, user_id, delta, new_value, reason in rows:
        t = f"<t:{ts_unix}:R>"
        sign = "+" if delta >= 0 else ""
        reason_part = f" — {reason}" if reason else ""
        lines.append(f"{t} • <@{user_id}>: **{sign}{delta}** → **${new_value}**{reason_part}")

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(settings, title="🕰️ IC Stock History", headline="Recent moves", reason="\n".join(lines[:25]))
    await interaction.response.send_message(embed=embed)
