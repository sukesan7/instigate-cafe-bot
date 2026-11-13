from __future__ import annotations

import logging

import aiosqlite
import discord
from discord import app_commands

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.services.fun_market import FunMarketError
from icbot.services.user_service import DailyAlreadyClaimedError

log = logging.getLogger(__name__)


# ==========
# /ic profile
# ==========


@ic.command(name="profile", description="View your (or another user's) profile.")
@app_commands.describe(user="User to view (defaults to you)")
async def profile_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    await interaction.response.defer(thinking=True)

    target = user or interaction.user
    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    store = container.user_store(settings)
    db = container.db or getattr(interaction.client, "db", None)

    # Ensure the target user exists (auto-create if missing)
    profile = await store.ensure_user(
        target.id,
        display_name=target.display_name,
        avatar_url=str(target.display_avatar.url) if target.display_avatar else "",
    )

    # ---- Balance ----
    fields: list[tuple[str, str] | tuple[str, str, bool]] = [
        ("💰 Balance", f"**${profile.balance:,}**", True),
    ]

    # ---- Stock Portfolio summary ----
    try:
        fm = container.fun_market_store(settings)
        port = await fm.portfolio(user_id=target.id)
        num_positions = len(port.positions)
        if num_positions > 0:
            fields.append(("📊 Positions", f"**{num_positions}** open", True))
            fields.append(("💼 Net Worth", f"**${port.net_worth:,}**", True))
            fields.append(("📈 Unrealized P&L", f"**{port.unrealized_pnl:+,}**", True))
        else:
            fields.append(("📊 Positions", "None", True))
            fields.append(("💼 Net Worth", f"**${profile.balance:,}**", True))
    except (FunMarketError, aiosqlite.Error, TypeError, ValueError) as exc:
        log.debug("Profile stock summary unavailable: %s", exc)
        fields.append(("📊 Stocks", "N/A", True))

    # ---- Blackjack stats ----
    try:
        gid = int(getattr(settings, "guild_id", 0) or 0)
        bj_row = (
            await db.fetchone(
                "SELECT hands_played, wins, losses, pushes, reset_count FROM blackjack_users WHERE guild_id=? AND user_id=?",
                (gid, target.id),
            )
            if db is not None
            else None
        )
        if bj_row:
            hands, wins, losses, pushes, resets = (int(x) for x in bj_row)
            bj_text = f"**{hands}** hands • {wins}W / {losses}L / {pushes}P"
            if resets:
                bj_text += f" • {resets} reset{'s' if resets != 1 else ''}"
            fields.append(("🃏 Blackjack", bj_text, False))
        else:
            fields.append(("🃏 Blackjack", "No games played", False))
    except (aiosqlite.Error, TypeError, ValueError) as exc:
        log.debug("Profile blackjack summary unavailable: %s", exc)
        fields.append(("🃏 Blackjack", "N/A", False))

    # ---- Daily claim status ----
    now = int(container.clock())
    if profile.daily_claim_ts > 0:
        next_available = profile.daily_claim_ts + 24 * 60 * 60
        if now >= next_available:
            fields.append(("📅 Daily", "✅ Available now! Use `/ic daily`", False))
        else:
            fields.append(("📅 Daily", f"⏳ Next claim <t:{next_available}:R>", False))
    else:
        fields.append(("📅 Daily", "✅ Available now! Use `/ic dailyclaim`", False))

    # ---- Account created ----
    if profile.created_at > 0:
        fields.append(("📆 Member Since", f"<t:{profile.created_at}:D>", True))

    embed = system_embed(
        settings,
        title=f"👤 {target.display_name}'s Profile",
        summary=None,
        fields=fields,
        footer_hint="Profile",
    )

    # Set the user's avatar as thumbnail
    if target.display_avatar:
        embed.set_thumbnail(url=target.display_avatar.url)

    await interaction.followup.send(embed=embed)


# ==========
# /ic daily
# ==========


@ic.command(name="dailyclaim", description="Claim your daily $100 reward.")
async def daily_cmd(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    store = container.user_store(settings)

    # Ensure user exists first
    await store.ensure_user(
        interaction.user.id,
        display_name=interaction.user.display_name,
        avatar_url=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else "",
    )

    try:
        amount, new_balance = await store.claim_daily(interaction.user.id)
    except DailyAlreadyClaimedError as e:
        embed = system_embed(
            settings,
            title="📅 Daily Reward",
            summary=f"⏳ You've already claimed your daily reward.\nNext claim available <t:{e.next_claim_ts}:R>.",
            footer_hint="Daily Claim",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = system_embed(
        settings,
        title="📅 Daily Reward Claimed!",
        summary=f"💰 You received **${amount:,}**!\n\nNew balance: **${new_balance:,}**",
        footer_hint="Daily Claim",
    )
    await interaction.response.send_message(embed=embed)
