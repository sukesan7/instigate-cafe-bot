from __future__ import annotations

import discord
from discord import app_commands

from icbot.app import casino
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import game as game_embed
from icbot.games.slots_engine import payout_table, reel_display, spin
from icbot.registry import guard_feature
from icbot.services.user_service import InsufficientBalanceError


@casino.command(name="slots", description="Spin the slot machine!")
@app_commands.describe(bet="Bet amount (integer)")
async def slots_cmd(interaction: discord.Interaction, bet: int = 10):
    if not await guard_feature(interaction, "casino"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)

    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be at least $1.", ephemeral=True)
        return

    # Deduct balance
    store = get_container(interaction.client).user_store(settings)
    await store.ensure_user(
        interaction.user.id,
        display_name=interaction.user.display_name,
        avatar_url=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else "",
    )
    try:
        await store.subtract_balance(interaction.user.id, bet)
    except InsufficientBalanceError:
        balance = await store.get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Insufficient balance. You have **${balance}**, need **${bet}**.",
            ephemeral=True,
        )
        return

    # Spin
    result = spin(bet)

    # Credit winnings (if any)
    if result.payout > 0:
        await store.add_balance(interaction.user.id, result.payout)

    new_balance = await store.get_balance(interaction.user.id)

    # Build embed
    reels_str = reel_display(result.reels)

    if result.match_type == "three":
        title = "🎰 Slots — JACKPOT!"
        summary = f"Three of a kind! **{result.multiplier}x** payout!"
    elif result.match_type == "two":
        title = "🎰 Slots — Push"
        summary = "Two matching — bet returned."
    else:
        title = "🎰 Slots — No Match"
        summary = "Better luck next time!"

    if result.net > 0:
        net_text = f"💰 **+${result.net}**"
    elif result.net == 0:
        net_text = "🔄 **$0** (push)"
    else:
        net_text = f"📉 **-${bet}**"

    fields = [
        ("🎰 Reels", reels_str, False),
        ("🎯 Bet", f"**${bet}**", True),
        ("📊 Result", net_text, True),
        ("💳 Balance", f"**${new_balance}**", True),
    ]

    embed = game_embed(
        settings,
        title=title,
        summary=summary,
        fields=fields,
        footer_hint=f"Slots • {interaction.user.display_name}",
    )
    await interaction.response.send_message(embed=embed)


@casino.command(name="payouts", description="Show the slot machine payout table.")
async def slots_payouts_cmd(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    table = payout_table()

    embed = game_embed(
        settings,
        title="🎰 Slot Machine — Payouts",
        summary=table,
        footer_hint=f"Slots • {interaction.user.display_name}",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
