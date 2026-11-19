from __future__ import annotations

import logging

import discord
from discord import app_commands

from icbot.app import casino
from icbot.casino.blackjack_fsm import (
    BlackjackFSMState,
    BlackjackRound,
    FSMTransitionEvent,
    IllegalStateTransitionError,
)
from icbot.casino.blackjack_session import (
    BJSession,
    SessionManager,
)
from icbot.casino.blackjack_settlement import (
    BlackjackSettlementResult,
    settle_blackjack_round,
)
from icbot.casino.blackjack_settlement import (
    reserved_bets as _reserved_bets,
)
from icbot.casino.blackjack_store import (
    BlackjackStore,
    _InsufficientWagerError,
)
from icbot.casino.blackjack_views import (
    INACTIVITY_SECONDS,
    TERMINAL_VIEW_TIMEOUT_SECONDS,
    BlackjackTerminalView,
    BlackjackView,
    _begin_hand,
    _BJStartError,
    _build_done_embed,
    _build_live_embed,
    _get_settings,
    _get_store,
    _sessions,
    render_state,
)
from icbot.registry import guard_feature

log = logging.getLogger(__name__)

__all__ = [
    "INACTIVITY_SECONDS",
    "TERMINAL_VIEW_TIMEOUT_SECONDS",
    "BJSession",
    "BlackjackFSMState",
    "BlackjackRound",
    "BlackjackSettlementResult",
    "BlackjackStore",
    "BlackjackTerminalView",
    "BlackjackView",
    "FSMTransitionEvent",
    "IllegalStateTransitionError",
    "SessionManager",
    "_BJStartError",
    "_InsufficientWagerError",
    "_begin_hand",
    "_build_done_embed",
    "_build_live_embed",
    "_get_settings",
    "_get_store",
    "_reserved_bets",
    "_sessions",
    "blackjack",
    "render_state",
    "settle_blackjack_round",
]


@casino.command(name="blackjack", description="Start an interactive blackjack hand (buttons).")
@app_commands.describe(bet="Bet amount (integer). Default 10.")
async def blackjack(interaction: discord.Interaction, bet: int = 10):
    if not await guard_feature(interaction, "casino"):
        return
    try:
        embed, view, _is_live = await _begin_hand(interaction, bet=int(bet))
    except _BJStartError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    await interaction.response.send_message(embed=embed, view=view)

    # Bind message for timeout-forfeit edits
    msg = await interaction.original_response()
    if isinstance(view, (BlackjackView, BlackjackTerminalView)):
        view.message = msg
