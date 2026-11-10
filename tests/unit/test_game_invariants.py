"""Pure domain invariant tests for casino game arithmetic (no DB, no Discord, no RNG math mocks)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from icbot.games import blackjack_engine as bj
from icbot.games import roulette_engine as rl
from icbot.games import slots_engine as sl

pytestmark = pytest.mark.unit


class _FixedShoe:
    """Deterministic card stack popping from index 0."""

    def __init__(self, cards: list[bj.Card]):
        self._cards = list(cards)

    def draw(self) -> bj.Card:
        if not self._cards:
            raise AssertionError("FixedShoe exhausted unexpectedly")
        return self._cards.pop(0)


# ---------------------------------------------------------------------------
# Blackjack domain invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ranks", "expected_values", "expected_best", "is_soft", "is_bj", "is_bust"),
    [
        (["A", "6"], [7, 17], 17, True, False, False),
        (["A", "A"], [2, 12, 22], 12, True, False, False),
        (["A", "K"], [11, 21], 21, True, True, False),
        (["A", "9", "A"], [11, 21, 31], 21, True, False, False),
        (["10", "7"], [17], 17, False, False, False),
        (["K", "Q", "5"], [25], 25, False, False, True),
    ],
)
def test_blackjack_hand_totals_and_classifications(
    ranks: list[str],
    expected_values: list[int],
    expected_best: int,
    is_soft: bool,
    is_bj: bool,
    is_bust: bool,
):
    hand = bj.Hand(cards=[bj.Card(r, "♠") for r in ranks])
    assert hand.values() == expected_values
    assert hand.best_total() == expected_best
    assert hand.is_soft() is is_soft
    assert hand.is_blackjack() is is_bj
    assert hand.is_bust() is is_bust


@pytest.mark.parametrize(
    ("ranks", "expected_total"),
    [
        (["A", "6", "10"], 17),       # Hard 17 (values=[17, 27]) -> dealer must stand even under H17
        (["A", "6", "10", "4"], 21),  # Hard 21 (values=[21, 31])
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-11: Hand.is_soft checks any(v<=21) separately from (v-10) in vals, misclassifying hard Ace totals ([17, 27]) as soft so H17 dealer hits hard 17. Planned fix: Phase 4.",
)
def test_blackjack_hard_total_with_forced_low_ace_is_not_soft(ranks: list[str], expected_total: int):
    hand = bj.Hand(cards=[bj.Card(r, "♠") for r in ranks])
    assert hand.best_total() == expected_total
    assert hand.is_soft() is False
    if expected_total == 17:
        state = bj.GameState(
            shoe=_FixedShoe([]),
            dealer=hand,
            hands=[bj.Hand(cards=[bj.Card("10", "♥"), bj.Card("8", "♦")])],
            bets=[100],
            dealer_hits_soft_17=True,
        )
        assert state._dealer_should_hit() is False


@pytest.mark.parametrize(
    ("player_ranks", "dealer_ranks", "bet", "from_split", "expected_outcome", "expected_delta"),
    [
        (["A", "K"], ["10", "9"], 100, False, "blackjack", 150),
        (["A", "Q"], ["10", "8"], 15, False, "blackjack", 22),  # int(1.5 * 15) truncates
        (["A", "K"], ["A", "Q"], 100, False, "push", 0),
        (["10", "9"], ["A", "K"], 100, False, "loss", -100),
        (["A", "K"], ["10", "9"], 100, True, "win", 100),  # split 21 pays 1:1, never 3:2
        (["10", "8"], ["K", "7"], 50, False, "win", 50),
        (["10", "8"], ["K", "9"], 50, False, "loss", -50),
        (["10", "8"], ["K", "8"], 50, False, "push", 0),
        (["10", "7", "8"], ["10", "7"], 75, False, "bust", -75),
        (["10", "6"], ["10", "6", "9"], 80, False, "win", 80),  # dealer bust
    ],
)
def test_blackjack_settlement_payout_table(
    player_ranks: list[str],
    dealer_ranks: list[str],
    bet: int,
    from_split: bool,
    expected_outcome: str,
    expected_delta: int,
):
    player = bj.Hand(cards=[bj.Card(r, "♥") for r in player_ranks], from_split=from_split)
    dealer = bj.Hand(cards=[bj.Card(r, "♦") for r in dealer_ranks])
    state = bj.GameState(shoe=_FixedShoe([]), dealer=dealer, hands=[player], bets=[bet], done=True)
    assert state.settle() == [(expected_outcome, expected_delta)]


def test_blackjack_multi_hand_split_and_double_lifecycle():
    # Split 8+8 against dealer 10+7; hand 1 stands on 18 (+bet), hand 2 doubles to 20 (+2*bet)
    shoe = _FixedShoe(
        [
            bj.Card("K", "♣"),  # split hand 1 receives K -> 18
            bj.Card("2", "♦"),  # split hand 2 receives 2 -> 10
            bj.Card("Q", "♠"),  # hand 2 doubles receives Q -> 20
        ]
    )
    state = bj.GameState(
        shoe=shoe,
        dealer=bj.Hand(cards=[bj.Card("10", "♠"), bj.Card("7", "♣")]),
        hands=[bj.Hand(cards=[bj.Card("8", "♥"), bj.Card("8", "♠")])],
        bets=[100],
        max_splits=1,
    )
    assert state.can_split() is True
    assert state.split() is True
    assert state.can_split() is False  # max_splits=1 reached even if another pair arrived
    assert state.active_index == 0
    assert state.done is False

    state.stand()
    assert state.active_index == 1
    assert state.done is False

    assert state.can_double() is True
    state.double()
    assert state.done is True
    assert state.bets == [100, 200]
    assert state.settle() == [("win", 100), ("win", 200)]


def test_blackjack_bust_skips_dealer_draw():
    sentinel_shoe = _FixedShoe([bj.Card("K", "♠")])
    state = bj.GameState(
        shoe=sentinel_shoe,
        dealer=bj.Hand(cards=[bj.Card("5", "♣"), bj.Card("6", "♦")]),  # would otherwise have to draw
        hands=[bj.Hand(cards=[bj.Card("10", "♥"), bj.Card("6", "♣")])],
        bets=[60],
    )
    state.hit()
    assert state.done is True
    assert len(state.dealer.cards) == 2
    assert state.settle() == [("bust", -60)]


@pytest.mark.parametrize("bad_bet", [0, -1, -100])
@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-03: GameState.start does not validate bet >= 1 at engine boundary. Planned fix: Phase 4.",
)
def test_blackjack_engine_rejects_non_positive_bet(bad_bet: int):
    with pytest.raises(ValueError):
        bj.GameState.start(bet=bad_bet)


# ---------------------------------------------------------------------------
# Roulette domain invariants (all 13 bet types + green 0 / 00)
# ---------------------------------------------------------------------------


def test_american_roulette_wheel_partition_invariant():
    assert len(rl.NUMBERS) == 38
    assert len(rl.RED_NUMBERS) == 18
    assert len(rl.BLACK_NUMBERS) == 18
    assert rl.RED_NUMBERS.isdisjoint(rl.BLACK_NUMBERS)
    assert rl.RED_NUMBERS | rl.BLACK_NUMBERS == set(range(1, 37))
    assert set(rl.NUMBERS) == (rl.RED_NUMBERS | rl.BLACK_NUMBERS | {0, -1})


@pytest.mark.parametrize(
    ("bet_type", "target", "win_num", "lose_num", "bet", "expected_payout"),
    [
        ("straight", "0", 0, -1, 10, 360),
        ("straight", "00", -1, 0, 10, 360),
        ("straight", "17", 17, 18, 25, 900),
        ("red", "red", 1, 2, 50, 100),
        ("black", "black", 2, 1, 50, 100),
        ("even", "even", 24, 23, 50, 100),
        ("odd", "odd", 23, 24, 50, 100),
        ("low", "low", 18, 19, 50, 100),
        ("high", "high", 19, 18, 50, 100),
        ("dozen1", "dozen1", 12, 13, 40, 120),
        ("dozen2", "dozen2", 13, 12, 40, 120),
        ("dozen3", "dozen3", 36, 24, 40, 120),
        ("col1", "col1", 34, 35, 30, 90),  # 34 % 3 == 1
        ("col2", "col2", 35, 36, 30, 90),  # 35 % 3 == 2
        ("col3", "col3", 36, 34, 30, 90),  # 36 % 3 == 0
    ],
)
def test_roulette_payout_arithmetic_all_bet_types(
    bet_type: str,
    target: str,
    win_num: int,
    lose_num: int,
    bet: int,
    expected_payout: int,
):
    win_spin = rl.SpinResult(win_num, rl._number_color(win_num), rl._display_number(win_num))
    lose_spin = rl.SpinResult(lose_num, rl._number_color(lose_num), rl._display_number(lose_num))

    win_res = rl.evaluate(bet_type, target, bet, win_spin)
    assert win_res.won is True
    assert win_res.payout == expected_payout
    assert win_res.net == expected_payout - bet

    lose_res = rl.evaluate(bet_type, target, bet, lose_spin)
    assert lose_res.won is False
    assert lose_res.payout == 0
    assert lose_res.net == -bet


@pytest.mark.parametrize(
    "outside_type",
    ["red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3", "col1", "col2", "col3"],
)
@pytest.mark.parametrize("green_num", [0, -1])
def test_roulette_outside_bets_always_lose_on_green_zero_and_double_zero(outside_type: str, green_num: int):
    green_spin = rl.SpinResult(green_num, "green", rl._display_number(green_num))
    res = rl.evaluate(outside_type, outside_type, 100, green_spin)
    assert res.won is False
    assert res.payout == 0
    assert res.net == -100


# ---------------------------------------------------------------------------
# Slots domain invariants (all 6 multipliers + two-match semantics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("emoji", "_weight", "mult"), sl.SYMBOLS)
def test_slots_all_three_match_multipliers(emoji: str, _weight: int, mult: int):
    sym = (emoji, mult)
    bet = 25
    with patch("icbot.games.slots_engine.random.choice", side_effect=[sym, sym, sym]):
        result = sl.spin(bet)

    assert result.match_type == "three"
    assert result.multiplier == mult
    assert result.payout == bet * mult
    assert result.net == bet * mult - bet


def test_slots_two_match_push_and_loss_characterization():
    star = ("⭐", 2)
    lemon = ("🍋", 3)
    diamond = ("💎", 50)

    # First two matching triggers push (1x return, net=0)
    with patch("icbot.games.slots_engine.random.choice", side_effect=[star, star, lemon]):
        push = sl.spin(40)
    assert push.match_type == "two"
    assert push.multiplier == 1
    assert push.payout == 40
    assert push.net == 0

    # Characterization note: spin() currently checks only `reel1 == reel2`, even though
    # payout_table() text says "Any two match — 1x (push)".
    with patch("icbot.games.slots_engine.random.choice", side_effect=[lemon, star, star]):
        back_two = sl.spin(40)
    assert back_two.match_type == "none"
    assert back_two.payout == 0
    assert back_two.net == -40

    with patch("icbot.games.slots_engine.random.choice", side_effect=[diamond, star, lemon]):
        loss = sl.spin(40)
    assert loss.match_type == "none"
    assert loss.payout == 0
    assert loss.net == -40
