from __future__ import annotations

import discord
from discord import app_commands

from icbot.app import casino
from icbot.embeds import game as game_embed
from icbot.services.user_service import UserStore, InsufficientBalanceError
from icbot.games.roulette_engine import (
    spin,
    evaluate,
    color_emoji,
    color_square,
    BetType,
)


def _make_user_store(interaction: discord.Interaction) -> UserStore:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    db = interaction.client.db  # type: ignore[attr-defined]
    return UserStore(
        db,
        starting_balance=int(getattr(settings, "bj_starting_bankroll", 1000) or 1000),
        daily_claim_amount=int(getattr(settings, "daily_claim_amount", 100) or 100),
    )


VALID_BET_TYPES = {
    "red", "black", "even", "odd", "high", "low",
    "dozen1", "dozen2", "dozen3",
    "col1", "col2", "col3",
    "straight",
}

BET_DESCRIPTIONS = {
    "red": "🔴 Red",
    "black": "⚫ Black",
    "even": "Even numbers",
    "odd": "Odd numbers",
    "high": "High (19-36)",
    "low": "Low (1-18)",
    "dozen1": "1st Dozen (1-12)",
    "dozen2": "2nd Dozen (13-24)",
    "dozen3": "3rd Dozen (25-36)",
    "col1": "Column 1",
    "col2": "Column 2",
    "col3": "Column 3",
    "straight": "Straight Up",
}

PAYOUT_LABELS = {
    "straight": "35:1",
    "red": "1:1", "black": "1:1",
    "even": "1:1", "odd": "1:1",
    "high": "1:1", "low": "1:1",
    "dozen1": "2:1", "dozen2": "2:1", "dozen3": "2:1",
    "col1": "2:1", "col2": "2:1", "col3": "2:1",
}


@casino.command(name="roulette", description="Spin the roulette wheel. Place your bet!")
@app_commands.describe(
    bet="Bet amount (integer)",
    bet_type="Bet type: red, black, even, odd, high, low, dozen1, dozen2, dozen3, col1, col2, col3, straight",
    number="Number to bet on (0-36 or 00). Only needed for 'straight' bets.",
)
async def roulette_cmd(
    interaction: discord.Interaction,
    bet: int,
    bet_type: str,
    number: str | None = None,
):
    settings = interaction.client.settings  # type: ignore[attr-defined]

    # Validate bet amount
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be at least $1.", ephemeral=True)
        return

    # Normalize bet type
    bt = bet_type.strip().lower()
    if bt not in VALID_BET_TYPES:
        valid = ", ".join(sorted(VALID_BET_TYPES))
        await interaction.response.send_message(
            f"❌ Invalid bet type `{bet_type}`. Valid types: {valid}",
            ephemeral=True,
        )
        return

    # Validate straight bet target
    bet_target = bt
    if bt == "straight":
        if number is None:
            await interaction.response.send_message(
                "❌ For a straight bet, specify the number (0-36 or 00).",
                ephemeral=True,
            )
            return
        num_str = number.strip()
        if num_str == "00":
            bet_target = "00"
        else:
            try:
                n = int(num_str)
                if n < 0 or n > 36:
                    raise ValueError
                bet_target = str(n)
            except ValueError:
                await interaction.response.send_message(
                    "❌ Number must be 0-36 or 00.",
                    ephemeral=True,
                )
                return

    # Deduct balance
    store = _make_user_store(interaction)
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

    # Spin and evaluate
    result = spin()
    outcome = evaluate(bt, bet_target, bet, result)

    # Credit winnings (if any)
    if outcome.payout > 0:
        await store.add_balance(interaction.user.id, outcome.payout)

    new_balance = await store.get_balance(interaction.user.id)

    # Build embed
    ce = color_emoji(result.color)
    cs = color_square(result.color)

    # Wheel visual
    wheel_display = f"{cs} **{result.display}** {ce}"

    if outcome.won:
        title = "🎰 Roulette — Winner!"
        net_text = f"💰 **+${outcome.net}**"
    else:
        title = "🎰 Roulette — No Luck"
        net_text = f"📉 **-${bet}**"

    bet_desc = BET_DESCRIPTIONS.get(bt, bt)
    if bt == "straight":
        bet_desc = f"Straight Up #{bet_target}"
    payout_label = PAYOUT_LABELS.get(bt, "?")

    fields = [
        ("🎡 Wheel Result", wheel_display, False),
        ("🎯 Your Bet", f"**${bet}** on {bet_desc} ({payout_label})", True),
        ("📊 Result", net_text, True),
        ("💳 Balance", f"**${new_balance}**", True),
    ]

    embed = game_embed(
        settings,
        title=title,
        fields=fields,
        footer_hint=f"Roulette • {interaction.user.display_name}",
    )
    await interaction.response.send_message(embed=embed)
