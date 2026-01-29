from __future__ import annotations
import discord
from discord import app_commands

from icbot.app import ic
from icbot.permissions import is_admin_or_allowed
from icbot.embeds import system as system_embed


lc = app_commands.Group(name="lc", description="Lightweight leaderboards")
ic.add_command(lc)


class ActivityTracker:
    def __init__(self, db):
        self.db = db

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        row = await self.db.fetchone(
            "SELECT count FROM activity_counts WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id),
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO activity_counts (guild_id, user_id, count) VALUES (?, ?, ?)",
                (message.guild.id, message.author.id, 1),
            )
        else:
            await self.db.execute(
                "UPDATE activity_counts SET count=? WHERE guild_id=? AND user_id=?",
                (int(row[0]) + 1, message.guild.id, message.author.id),
            )

    async def top(self, guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
        limit = max(1, min(limit, 25))
        rows = await self.db.fetchall(
            "SELECT user_id, count FROM activity_counts WHERE guild_id=? ORDER BY count DESC LIMIT ?",
            (guild_id, limit),
        )
        return [(int(uid), int(c)) for uid, c in rows]

    async def reset_guild(self, guild_id: int) -> None:
        await self.db.execute("DELETE FROM activity_counts WHERE guild_id=?", (guild_id,))


@lc.command(name="leaderboard", description="Most active users (message count).")
@app_commands.describe(limit="How many entries (max 25)")
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    tracker = interaction.client.activity_tracker  # type: ignore[attr-defined]
    rows = await tracker.top(interaction.guild.id, limit=limit)
    if not rows:
        await interaction.response.send_message("No activity tracked yet.")
        return

    lines = [f"**{i}.** <@{uid}> — **{c}** msgs" for i, (uid, c) in enumerate(rows, start=1)]
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = system_embed(settings, title="📊 Activity Leaderboard", summary="Most active users", fields=(("Leaderboard", "\n".join(lines)),), footer_hint="Messages")
    await interaction.response.send_message(embed=embed)


@lc.command(name="reset", description="Reset activity counts for this server (admin/allowed roles only).")
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
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = system_embed(settings, title="✅ Activity Reset", summary="Activity leaderboard reset.", footer_hint=interaction.user.display_name)
    await interaction.response.send_message(embed=embed)
