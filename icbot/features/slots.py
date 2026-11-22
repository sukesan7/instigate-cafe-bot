from __future__ import annotations

import discord
from discord import app_commands

from icbot.app import casino
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import game as game_embed
from icbot.errors import InsufficientBalanceError
from icbot.games.slots_engine import payout_table, reel_display, spin
from icbot.registry import guard_feature
from icbot.services.casino_service import InvalidWagerError


@casino.command(name="slots", description="Play slots.")
@app_commands.describe(bet="Bet amount")
async def slots_cmd(interaction: discord.Interaction, bet: int = 10):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    if not await guard_feature(interaction, "casino"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)

    # Validate and deduct balance via consolidated transactional casino domain service
    casino_svc = get_container(interaction.client).casino_service(settings)
    try:
        val_bet = casino_svc.validate_wager(bet)
    except InvalidWagerError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    try:
        reservation = await casino_svc.reserve_wager(
            interaction.guild_id or 0,
            interaction.user.id,
            val_bet,
            "slots",
            display_name=interaction.user.display_name,
            avatar_url=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else "",
        )
    except InsufficientBalanceError:
        balance = await casino_svc.get_balance(interaction.user.id, guild_id=interaction.guild_id or 0)
        await interaction.response.send_message(
            f"❌ Insufficient balance. You have **${balance}**, need **${bet}**.",
            ephemeral=True,
        )
        return

    # Spin
    result = spin(val_bet)

    # Settle transactionally
    settlement = await casino_svc.settle_wager(
        reservation,
        payout_amount=result.payout,
        net_delta=result.net,
        outcome=result.match_type,
        details={"reels": list(result.reels), "multiplier": result.multiplier},
    )
    new_balance = settlement.balance_after

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


@casino.command(name="payouts", description="Slot payouts.")
async def slots_payouts_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    if not await guard_feature(interaction, "casino"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    table = payout_table()

    embed = game_embed(
        settings,
        title="🎰 Slot Machine — Payouts",
        summary=table,
        footer_hint=f"Slots • {interaction.user.display_name}",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
