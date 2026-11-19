from __future__ import annotations

import random

import pytest

from icbot.db import Database
from icbot.errors import InsufficientBalanceError
from icbot.games import blackjack_engine as bj
from icbot.games import roulette_engine as rl
from icbot.games import slots_engine as sl
from icbot.services.casino_service import (
    MAX_WAGER,
    CasinoService,
    ConcurrentWagerError,
    InvalidWagerError,
)

pytestmark = pytest.mark.unit


class _FixedShoe:
    """Deterministic card stack popping from index 0 for predictable blackjack testing."""

    def __init__(self, cards: list[bj.Card]):
        self._cards = list(cards)

    def draw(self) -> bj.Card:
        if not self._cards:
            raise AssertionError("FixedShoe exhausted unexpectedly")
        return self._cards.pop(0)


# ==============================================================================
# 1. Deterministic Seed Injection & Engine Separation Tests
# ==============================================================================


def test_roulette_deterministic_seed_injection():
    """Identical seeds produce identical roulette outcomes; different seeds vary."""
    spin_a = rl.spin(seed=42)
    spin_b = rl.spin(seed=42)
    assert spin_a.number == spin_b.number
    assert spin_a.color == spin_b.color
    assert spin_a.display == spin_b.display

    # RNG instance injection
    rng = random.Random(999)
    spin_c = rl.spin(rng=rng)
    rng_check = random.Random(999)
    spin_d = rl.spin(rng=rng_check)
    assert spin_c == spin_d


def test_slots_deterministic_seed_injection():
    """Identical seeds produce identical slot reels and payouts."""
    res_a = sl.spin(bet=20, seed=12345)
    res_b = sl.spin(bet=20, seed=12345)
    assert res_a.reels == res_b.reels
    assert res_a.multiplier == res_b.multiplier
    assert res_a.payout == res_b.payout
    assert res_a.net == res_b.net


def test_blackjack_deterministic_seed_injection():
    """Identical seeds produce identical shoe card sequences and initial deals."""
    game_a = bj.GameState.start(bet=50, seed=777)
    game_b = bj.GameState.start(bet=50, seed=777)
    assert [str(c) for c in game_a.hands[0].cards] == [str(c) for c in game_b.hands[0].cards]
    assert [str(c) for c in game_a.dealer.cards] == [str(c) for c in game_b.dealer.cards]

    # Deterministic next draw on hit
    game_a.hit()
    game_b.hit()
    assert [str(c) for c in game_a.hands[0].cards] == [str(c) for c in game_b.hands[0].cards]


# ==============================================================================
# 2. High-Volume Probabilistic Simulations (Seedable & Bounded)
# ==============================================================================


def test_roulette_high_volume_distribution_and_bounds():
    """
    10,000-spin simulation with fixed seed verifies:
    1. All 38 outcomes occur within statistical tolerances.
    2. Outside bets strictly lose on green 0 and 00.
    3. Payouts are non-negative and strictly bounded by 36x stake.
    """
    rng = random.Random(20261119)
    counts: dict[int, int] = {n: 0 for n in rl.NUMBERS}
    colors = {"red": 0, "black": 0, "green": 0}

    total_spins = 10_000
    for _ in range(total_spins):
        spin = rl.spin(rng=rng)
        counts[spin.number] += 1
        colors[spin.color] += 1

        # Assert outside bet loss on green
        if spin.color == "green":
            for ob in ("red", "black", "even", "odd", "high", "low", "dozen1", "col1"):
                res = rl.evaluate(ob, ob, 10, spin)
                assert res.won is False
                assert res.payout == 0
                assert res.net == -10

        # Assert straight bet bounds
        res_straight = rl.evaluate("straight", "17", 10, spin)
        assert res_straight.payout in (0, 360)

    # All 38 slots appeared
    assert all(c > 0 for c in counts.values())

    # Proportions ~ 18/38 (0.4737) and 2/38 (0.0526)
    red_rate = colors["red"] / total_spins
    black_rate = colors["black"] / total_spins
    green_rate = colors["green"] / total_spins

    assert 0.44 <= red_rate <= 0.51
    assert 0.44 <= black_rate <= 0.51
    assert 0.04 <= green_rate <= 0.07


def test_slots_high_volume_payout_bounds_and_multipliers():
    """
    10,000-spin simulation verifies:
    1. Multipliers strictly belong to {0, 1, 2, 3, 5, 10, 20, 50}.
    2. Payouts are integers bounded between 0 and 50 * bet.
    3. Return-to-player (RTP) is healthy and bounded.
    """
    rng = random.Random(88888)
    bet = 10
    total_spins = 10_000
    total_wagered = bet * total_spins
    total_payout = 0
    multipliers_seen: set[int] = set()

    for _ in range(total_spins):
        res = sl.spin(bet, rng=rng)
        multipliers_seen.add(res.multiplier)
        total_payout += res.payout

        # Invariants
        assert res.payout in (0, bet, bet * 2, bet * 3, bet * 5, bet * 10, bet * 20, bet * 50)
        assert res.net == res.payout - bet

    # Jackpots and intermediate multipliers were observed
    assert {0, 1, 2, 3, 5}.issubset(multipliers_seen)

    rtp = total_payout / total_wagered
    # Theoretical RTP of the 3-reel configuration is ~34.1% (17.98% 3-match + 16.12% 2-match push)
    assert 0.30 <= rtp <= 0.40


# ==============================================================================
# 3. Blackjack State Transitions, Dealer Actions, and Forfeiture
# ==============================================================================


def test_blackjack_state_transition_matrix():
    """Verify state transitions: can_hit, can_stand, can_double, can_split, history logging."""
    shoe = _FixedShoe(
        [
            bj.Card("8", "♠"),  # P1
            bj.Card("10", "♦"),  # D1
            bj.Card("8", "♥"),  # P2
            bj.Card("7", "♣"),  # D2
            bj.Card("3", "♦"),  # Hit 1 -> 19
        ]
    )
    state = bj.GameState.start(100, shoe=shoe)
    assert state.history == ["deal"]
    assert state.can_hit() is True
    assert state.can_stand() is True
    assert state.can_double() is True
    assert state.can_split() is True

    # Hit moves hand from 16 to 19
    state.hit()
    assert state.history == ["deal", "hit:hand_0"]
    assert state.active_hand().best_total() == 19
    assert state.can_double() is False  # Cannot double after hitting
    assert state.can_split() is False

    # Stand advances and concludes round against dealer 17
    state.stand()
    assert state.history == ["deal", "hit:hand_0", "stand:hand_0"]
    assert state.done is True
    assert state.settle() == [("win", 100)]

    # Illegal actions on completed round are no-ops
    state.hit()
    state.stand()
    state.double()
    assert state.history == ["deal", "hit:hand_0", "stand:hand_0"]


def test_blackjack_forfeit_and_timeout_lifecycle():
    """Forfeiting or timing out concludes the session immediately with loss."""
    shoe = _FixedShoe([bj.Card("10", "♠"), bj.Card("10", "♦"), bj.Card("5", "♥"), bj.Card("7", "♣")])
    state = bj.GameState.start(150, shoe=shoe)
    assert state.done is False

    results = state.forfeit()
    assert state.done is True
    assert state.forfeited is True
    assert results == [("forfeit", -150)]
    assert state.settle() == [("forfeit", -150)]

    # Timeout
    shoe_to = _FixedShoe([bj.Card("10", "♠"), bj.Card("10", "♦"), bj.Card("5", "♥"), bj.Card("7", "♣")])
    state_timeout = bj.GameState.start(200, shoe=shoe_to)
    res_to = state_timeout.timeout()
    assert state_timeout.done is True
    assert state_timeout.timed_out is True
    assert res_to == [("timeout", -200)]
    assert state_timeout.settle() == [("timeout", -200)]


def test_blackjack_dealer_soft_17_and_hard_17_rules():
    """Dealer hits soft 17 if enabled, stands on hard 17, and stands on 18+."""
    # Dealer has soft 17 (A + 6); H17 enabled -> draws 4 -> 21
    shoe_h17 = _FixedShoe([bj.Card("4", "♣")])
    s_h17 = bj.GameState(
        shoe=shoe_h17,
        dealer=bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("6", "♥")]),
        hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("10", "♦")])],
        bets=[100],
        dealer_hits_soft_17=True,
    )
    s_h17.stand()
    assert s_h17.dealer.best_total() == 21
    assert s_h17.settle() == [("loss", -100)]

    # Dealer has hard 17 (10 + 7); H17 enabled -> stands on 17
    shoe_hard = _FixedShoe([bj.Card("5", "♣")])
    s_hard = bj.GameState(
        shoe=shoe_hard,
        dealer=bj.Hand(cards=[bj.Card("10", "♠"), bj.Card("7", "♥")]),
        hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("9", "♦")])],
        bets=[100],
        dealer_hits_soft_17=True,
    )
    s_hard.stand()
    assert s_hard.dealer.best_total() == 17
    assert len(s_hard.dealer.cards) == 2
    assert s_hard.settle() == [("win", 100)]


# ==============================================================================
# 4. Consolidated Transactional Domain Service & Idempotency Tests
# ==============================================================================


def test_casino_service_wager_validation_and_bounds():
    """Verify play-money limits, integer validation, negative and overflow rejection."""
    assert CasinoService.validate_wager(1) == 1
    assert CasinoService.validate_wager(100) == 100
    assert CasinoService.validate_wager(MAX_WAGER) == MAX_WAGER

    # Rejection of negative amounts, zero, floats, non-numeric, overflow
    for bad in (0, -1, -500, 10.5, "100", True, False, None, MAX_WAGER + 1, 10**15):
        with pytest.raises(InvalidWagerError):
            CasinoService.validate_wager(bad)


@pytest.mark.asyncio
async def test_casino_service_atomic_reservation_and_idempotent_settlement(tmp_db: Database):
    """
    Verify complete transactional lifecycle:
    1. Balance reserved up front into escrow.
    2. Replay with identical reservation key is safe and returns existing reservation.
    3. Settlement credits payout and prevents duplicate payout on duplicate settlement calls.
    """
    svc = CasinoService(tmp_db, guild_id=100, starting_bankroll=1000)
    user_id = 555001

    # Ensure user exists
    await svc.ensure_user(user_id)
    assert await svc.get_balance(user_id) == 1000

    # 1. Reserve $200 wager
    res = await svc.reserve_wager(100, user_id, 200, "roulette", idempotency_key="tx_roulette_001")
    assert res.amount == 200
    assert res.balance_after == 800
    assert await svc.get_balance(user_id) == 800

    # Replay of identical reservation key returns existing reservation without double deducting
    res_dup = await svc.reserve_wager(100, user_id, 200, "roulette", idempotency_key="tx_roulette_001")
    assert res_dup.balance_after == 800
    assert await svc.get_balance(user_id) == 800

    # 2. Settle winning wager (+400 payout -> $1200 balance)
    settle = await svc.settle_wager(
        res, payout_amount=400, net_delta=200, outcome="win", idempotency_key="settle_roulette_001"
    )
    assert settle.already_settled is False
    assert settle.balance_after == 1200
    assert await svc.get_balance(user_id) == 1200

    # Replay of identical settlement key returns already_settled=True without double crediting
    settle_dup = await svc.settle_wager(
        res, payout_amount=400, net_delta=200, outcome="win", idempotency_key="settle_roulette_001"
    )
    assert settle_dup.already_settled is True
    assert settle_dup.balance_after == 1200
    assert await svc.get_balance(user_id) == 1200


@pytest.mark.asyncio
async def test_casino_service_insufficient_balance_rejection(tmp_db: Database):
    """Attempting to wager more than current bankroll raises InsufficientBalanceError."""
    svc = CasinoService(tmp_db, guild_id=100, starting_bankroll=500)
    user_id = 555002
    await svc.ensure_user(user_id)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await svc.reserve_wager(100, user_id, 600, "slots")
    assert "Insufficient balance" in str(exc_info.value)
    assert await svc.get_balance(user_id) == 500


@pytest.mark.asyncio
async def test_casino_service_concurrent_wager_guard(tmp_db: Database):
    """Active wager in progress prevents concurrent second wager by same user until settled."""
    svc = CasinoService(tmp_db, guild_id=100, starting_bankroll=1000)
    user_id = 555003
    await svc.ensure_user(user_id)

    # First wager in-flight
    res = await svc.reserve_wager(100, user_id, 100, "blackjack")
    assert res.balance_after == 900

    # Second concurrent wager by same user in same guild rejected
    with pytest.raises(ConcurrentWagerError) as exc_info:
        await svc.reserve_wager(100, user_id, 100, "blackjack")
    assert "already have an active game" in str(exc_info.value)

    # After settlement, next wager succeeds
    await svc.settle_wager(res, payout_amount=0, net_delta=-100, outcome="loss")
    res_next = await svc.reserve_wager(100, user_id, 100, "blackjack")
    assert res_next.balance_after == 800
    await svc.settle_wager(res_next, payout_amount=100, net_delta=0, outcome="push")
    assert await svc.get_balance(user_id) == 900


@pytest.mark.asyncio
async def test_casino_service_refund_and_forfeit_lifecycle(tmp_db: Database):
    """Refund restores reserved funds; forfeit records loss with zero payout."""
    svc = CasinoService(tmp_db, guild_id=100, starting_bankroll=1000)
    user_id = 555004
    await svc.ensure_user(user_id)

    # 1. Refund scenario
    res_ref = await svc.reserve_wager(100, user_id, 300, "blackjack")
    assert await svc.get_balance(user_id) == 700
    new_bal = await svc.refund_wager(res_ref, reason="bot_restart")
    assert new_bal == 1000
    assert await svc.get_balance(user_id) == 1000

    # 2. Forfeit scenario
    res_forfeit = await svc.reserve_wager(100, user_id, 250, "blackjack")
    assert await svc.get_balance(user_id) == 750
    settle_forfeit = await svc.forfeit_wager(res_forfeit, reason="timeout")
    assert settle_forfeit.payout_amount == 0
    assert settle_forfeit.net_delta == -250
    assert settle_forfeit.balance_after == 750
    assert await svc.get_balance(user_id) == 750
