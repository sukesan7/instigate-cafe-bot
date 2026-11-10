"""Pure unit tests for casino game engines (blackjack, roulette, slots)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    import pytest

    pytestmark = pytest.mark.unit
except ImportError:  # pragma: no cover - allows direct `python3 -m unittest` runs
    pass

from icbot.games import blackjack_engine as bj
from icbot.games import roulette_engine as rl
from icbot.games import slots_engine as sl


class _FixedShoe:
    """Deterministic card stack popping from the front for controlled Blackjack tests."""

    def __init__(self, cards: list[bj.Card]):
        self._cards = list(cards)

    def draw(self) -> bj.Card:
        if not self._cards:
            raise AssertionError("FixedShoe exhausted unexpectedly")
        return self._cards.pop(0)


class TestBlackjackEngine(unittest.TestCase):
    def test_hand_soft_ace_and_blackjack_detection(self):
        soft = bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("6", "♥")])
        self.assertEqual(soft.values(), [7, 17])
        self.assertTrue(soft.is_soft())
        self.assertEqual(soft.best_total(), 17)
        self.assertFalse(soft.is_blackjack())

        natural = bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("K", "♦")])
        self.assertTrue(natural.is_blackjack())
        natural.from_split = True
        self.assertFalse(natural.is_blackjack(), "Split 21 does not pay 3:2 natural blackjack")

        bust = bj.Hand(cards=[bj.Card("K", "♠"), bj.Card("9", "♦"), bj.Card("5", "♣")])
        self.assertTrue(bust.is_bust())
        self.assertEqual(bust.best_total(), 24)

    def test_shoe_penetration_triggers_reshuffle(self):
        shoe = bj.Shoe(num_decks=1, reshuffle_pct=0.75)
        self.assertEqual(len(shoe.cards), 52)
        for _ in range(38):
            shoe.draw()
        self.assertFalse(shoe.needs_reshuffle())
        shoe.draw()
        self.assertTrue(shoe.needs_reshuffle())

    def test_settle_natural_blackjack_win_push_and_bust(self):
        # Natural blackjack beats non-blackjack 21 with 3:2 payout
        state = bj.GameState(
            shoe=_FixedShoe([]),
            dealer=bj.Hand(cards=[bj.Card("10", "♠"), bj.Card("9", "♥")]),
            hands=[bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("Q", "♣")])],
            bets=[100],
            done=True,
        )
        self.assertEqual(state.settle(), [("blackjack", 150)])

        # Hit -> Bust loses immediately without dealer drawing
        bust_state = bj.GameState(
            shoe=_FixedShoe([bj.Card("10", "♦")]),
            dealer=bj.Hand(cards=[bj.Card("6", "♠"), bj.Card("10", "♥")]),
            hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("5", "♦")])],
            bets=[50],
        )
        bust_state.hit()
        self.assertTrue(bust_state.done)
        self.assertEqual(bust_state.settle(), [("bust", -50)])

        # Both 20 -> push
        push_state = bj.GameState(
            shoe=_FixedShoe([]),
            dealer=bj.Hand(cards=[bj.Card("K", "♠"), bj.Card("Q", "♥")]),
            hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("J", "♦")])],
            bets=[75],
            done=True,
        )
        self.assertEqual(push_state.settle(), [("push", 0)])

    def test_dealer_hits_soft_17_when_enabled(self):
        # Dealer has A+6 (soft 17) and next card is 3 -> finishes at 20 beats player's 19
        state_h17 = bj.GameState(
            shoe=_FixedShoe([bj.Card("3", "♣")]),
            dealer=bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("6", "♥")]),
            hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("9", "♦")])],
            bets=[100],
            dealer_hits_soft_17=True,
        )
        state_h17.stand()
        self.assertEqual(state_h17.dealer.best_total(), 20)
        self.assertEqual(state_h17.settle(), [("loss", -100)])

        # With S17 (dealer_hits_soft_17=False), dealer stands on A+6 (17) and loses to 19
        state_s17 = bj.GameState(
            shoe=_FixedShoe([]),
            dealer=bj.Hand(cards=[bj.Card("A", "♠"), bj.Card("6", "♥")]),
            hands=[bj.Hand(cards=[bj.Card("10", "♣"), bj.Card("9", "♦")])],
            bets=[100],
            dealer_hits_soft_17=False,
        )
        state_s17.stand()
        self.assertEqual(state_s17.dealer.best_total(), 17)
        self.assertEqual(state_s17.settle(), [("win", 100)])

    def test_double_and_split_aces(self):
        # Double doubles bet and stands
        dbl = bj.GameState(
            shoe=_FixedShoe([bj.Card("9", "♠"), bj.Card("10", "♥")]),
            dealer=bj.Hand(cards=[bj.Card("10", "♦"), bj.Card("7", "♣")]),
            hands=[bj.Hand(cards=[bj.Card("6", "♣"), bj.Card("5", "♦")])],
            bets=[50],
        )
        dbl.double()
        self.assertTrue(dbl.done)
        self.assertEqual(dbl.bets, [100])
        self.assertTrue(dbl.hands[0].doubled)
        self.assertEqual(dbl.settle(), [("win", 100)])

        # Splitting Aces deals 1 card each and auto-stands both hands
        split_aces = bj.GameState(
            shoe=_FixedShoe([bj.Card("K", "♠"), bj.Card("9", "♥")]),
            dealer=bj.Hand(cards=[bj.Card("10", "♦"), bj.Card("8", "♣")]),
            hands=[bj.Hand(cards=[bj.Card("A", "♣"), bj.Card("A", "♦")])],
            bets=[100],
            max_splits=1,
        )
        self.assertTrue(split_aces.split())
        self.assertTrue(split_aces.done)
        self.assertEqual(len(split_aces.hands), 2)
        self.assertEqual(split_aces.settle(), [("win", 100), ("win", 100)])


class TestRouletteEngine(unittest.TestCase):
    def test_wheel_and_outside_green_zero_loses(self):
        self.assertEqual(len(rl.NUMBERS), 38)
        self.assertEqual(rl._display_number(-1), "00")
        self.assertEqual(rl._number_color(0), "green")
        self.assertEqual(rl._number_color(-1), "green")

        for green in (rl.SpinResult(0, "green", "0"), rl.SpinResult(-1, "green", "00")):
            for btype in ("red", "black", "even", "odd", "low", "high", "dozen1", "col1"):
                res = rl.evaluate(btype, btype, 100, green)
                self.assertFalse(res.won)
                self.assertEqual(res.payout, 0)
                self.assertEqual(res.net, -100)

    def test_straight_and_multiplier_payouts(self):
        res_00 = rl.evaluate("straight", "00", 25, rl.SpinResult(-1, "green", "00"))
        self.assertTrue(res_00.won)
        self.assertEqual(res_00.payout, 25 * 36)
        self.assertEqual(res_00.net, 25 * 35)

        res_dozen = rl.evaluate("dozen2", "dozen2", 50, rl.SpinResult(14, "red", "14"))
        self.assertTrue(res_dozen.won)
        self.assertEqual(res_dozen.payout, 150)
        self.assertEqual(res_dozen.net, 100)


class TestSlotsEngine(unittest.TestCase):
    def test_three_match_two_match_and_loss(self):
        diamond = ("💎", 50)
        star = ("⭐", 2)
        lemon = ("🍋", 3)

        with patch("icbot.games.slots_engine.random.choice", side_effect=[diamond, diamond, diamond]):
            jackpot = sl.spin(10)
        self.assertEqual(jackpot.match_type, "three")
        self.assertEqual(jackpot.multiplier, 50)
        self.assertEqual(jackpot.payout, 500)
        self.assertEqual(jackpot.net, 490)

        with patch("icbot.games.slots_engine.random.choice", side_effect=[star, star, lemon]):
            push = sl.spin(20)
        self.assertEqual(push.match_type, "two")
        self.assertEqual(push.payout, 20)
        self.assertEqual(push.net, 0)

        with patch("icbot.games.slots_engine.random.choice", side_effect=[diamond, star, lemon]):
            loss = sl.spin(20)
        self.assertEqual(loss.match_type, "none")
        self.assertEqual(loss.payout, 0)
        self.assertEqual(loss.net, -20)

        self.assertIn("💎", sl.reel_display(("💎", "⭐", "🍋")))
        self.assertIn("50x", sl.payout_table())


if __name__ == "__main__":
    unittest.main()

