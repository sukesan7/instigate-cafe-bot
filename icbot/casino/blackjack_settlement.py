from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from icbot.casino.blackjack_fsm import BlackjackFSMState, BlackjackRound
from icbot.casino.blackjack_store import BlackjackStore
from icbot.games.blackjack_engine import GameState

log = logging.getLogger(__name__)


def reserved_bets(state: GameState) -> int:
    """Return total active wagers currently escrowed for this hand."""
    return int(sum(state.bets))


@dataclass(frozen=True)
class BlackjackSettlementResult:
    """Immutable outcome and accounting record of a completed Blackjack round."""

    round_id: str
    settlement_id: str
    guild_id: int
    user_id: int
    reserved_bets: int
    payout_credit: int
    net_delta: int
    new_bankroll: int
    results: list[tuple[str, int]]
    counts: dict[str, int]
    hands_played: int
    fsm_state: BlackjackFSMState
    already_settled: bool = False


async def settle_blackjack_round(
    round: BlackjackRound,
    store: BlackjackStore,
    *,
    is_forfeit: bool = False,
    is_timeout: bool = False,
    clock: Any = None,
) -> BlackjackSettlementResult:
    """
    Settle a Blackjack round atomically and record its transition to a terminal FSM state.

    Ensures:
    1. Legal transition to SETTLED, FORFEITED, or TIMED_OUT.
    2. Zero double-payouts using round.settlement_id as idempotency key.
    3. Immutable result metrics.
    """
    state = round.state
    if state is None:
        raise RuntimeError("Cannot settle Blackjack round without active GameState")
    reserved = reserved_bets(state)

    if is_forfeit:
        round.transition_to(
            BlackjackFSMState.FORFEITED,
            action="forfeit",
            clock=clock,
            reserved_bets=reserved,
        )
        results = state.forfeit()
        delta_total = sum(d for _, d in results)
        payout_credit = 0  # House keeps escrowed bet on forfeit
        counts = {"loss": max(1, len(state.bets))}
        hands_played = max(1, len(state.bets))
    elif is_timeout:
        round.transition_to(
            BlackjackFSMState.TIMED_OUT,
            action="timeout",
            clock=clock,
            reserved_bets=reserved,
        )
        results = state.timeout()
        delta_total = sum(d for _, d in results)
        payout_credit = 0  # House keeps escrowed bet on inactivity timeout
        counts = {"loss": max(1, len(state.bets))}
        hands_played = max(1, len(state.bets))
    else:
        # Standard natural or played-out round settlement
        if not state.done:
            round.transition_to(BlackjackFSMState.DEALER_TURN, action="dealer_turn", clock=clock)
        results = state.settle()
        delta_total = sum(d for _, d in results)
        payout_credit = reserved + delta_total

        counts = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        hands_played = len(results)

        round.transition_to(
            BlackjackFSMState.SETTLED,
            action="settle",
            clock=clock,
            delta_total=delta_total,
            payout_credit=payout_credit,
        )

    # Atomic ledger & bankroll settlement with idempotency
    bet_tx_id = f"bj_bet:{round.round_id}"
    new_bankroll = await store.apply_delta_and_stats(
        round.user_id,
        payout_credit,
        counts,
        hands_played=hands_played,
        guild_id=round.guild_id,
        idempotency_key=round.settlement_id,
        reference_id=bet_tx_id,
    )

    return BlackjackSettlementResult(
        round_id=round.round_id,
        settlement_id=round.settlement_id,
        guild_id=round.guild_id,
        user_id=round.user_id,
        reserved_bets=reserved,
        payout_credit=payout_credit,
        net_delta=delta_total,
        new_bankroll=new_bankroll,
        results=results,
        counts=counts,
        hands_played=hands_played,
        fsm_state=round.fsm_state,
        already_settled=False,
    )
