from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple
import random

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]


def card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


@dataclass
class Deck:
    cards: List[Card] = field(default_factory=list)

    @staticmethod
    def standard(shuffle: bool = True) -> "Deck":
        cards = [Card(r, s) for s in SUITS for r in RANKS]
        if shuffle:
            random.shuffle(cards)
        return Deck(cards=cards)

    def draw(self) -> Card:
        if not self.cards:
            self.cards = Deck.standard(shuffle=True).cards
        return self.cards.pop()


class Shoe:
    """Multi-deck shoe (standard casino: 6 decks, reshuffle at ~75% penetration)."""

    def __init__(self, num_decks: int = 6, reshuffle_pct: float = 0.75):
        self.num_decks = num_decks
        self.reshuffle_pct = reshuffle_pct
        self._total_cards = num_decks * 52
        self.cards: List[Card] = []
        self._build_and_shuffle()

    def _build_and_shuffle(self) -> None:
        self.cards = [Card(r, s) for _ in range(self.num_decks) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)

    def needs_reshuffle(self) -> bool:
        dealt = self._total_cards - len(self.cards)
        return dealt >= int(self._total_cards * self.reshuffle_pct)

    def draw(self) -> Card:
        if not self.cards:
            self._build_and_shuffle()
        return self.cards.pop()


@dataclass
class Hand:
    cards: List[Card] = field(default_factory=list)
    doubled: bool = False
    from_split: bool = False  # split-hands do not qualify for natural blackjack payout

    def add(self, c: Card) -> None:
        self.cards.append(c)

    def values(self) -> List[int]:
        totals = [0]
        for c in self.cards:
            if c.rank == "A":
                totals = [t + 1 for t in totals] + [t + 11 for t in totals]
            else:
                v = card_value(c.rank)
                totals = [t + v for t in totals]
        return sorted(set(totals))

    def best_total(self) -> int:
        vals = self.values()
        under = [v for v in vals if v <= 21]
        return max(under) if under else min(vals)

    def is_soft(self) -> bool:
        vals = self.values()
        # soft if we can subtract 10 (Ace counted as 11) and still be a reachable total
        return any(v <= 21 for v in vals) and any((v - 10) in vals for v in vals)

    def is_blackjack(self) -> bool:
        return len(self.cards) == 2 and self.best_total() == 21 and not self.from_split

    def is_bust(self) -> bool:
        return min(self.values()) > 21

    def can_split(self) -> bool:
        return len(self.cards) == 2 and self.cards[0].rank == self.cards[1].rank

    def render(self, hide_first: bool = False) -> str:
        if not self.cards:
            return "(empty)"
        if hide_first:
            return "?? " + " ".join(str(c) for c in self.cards[1:])
        return " ".join(str(c) for c in self.cards)


# Shared shoe across hands (reshuffled between rounds when penetration threshold hit)
_SHOE: Shoe | None = None


def _get_shoe() -> Shoe:
    global _SHOE
    if _SHOE is None or _SHOE.needs_reshuffle():
        _SHOE = Shoe(num_decks=6, reshuffle_pct=0.75)
    return _SHOE


@dataclass
class GameState:
    shoe: Shoe
    dealer: Hand
    hands: List[Hand]
    bets: List[int]
    active_index: int = 0
    splits_used: int = 0
    dealer_hits_soft_17: bool = True
    max_splits: int = 1
    done: bool = False

    def active_hand(self) -> Hand:
        return self.hands[self.active_index]

    def _hand_standing(self, idx: int) -> bool:
        return getattr(self.hands[idx], "_stood", False)  # type: ignore[attr-defined]

    def _set_stand(self, idx: int) -> None:
        setattr(self.hands[idx], "_stood", True)  # type: ignore[attr-defined]

    @staticmethod
    def start(bet: int, dealer_hits_soft_17: bool = True, max_splits: int = 1) -> "GameState":
        shoe = _get_shoe()
        dealer = Hand()
        player = Hand()
        player.add(shoe.draw())
        dealer.add(shoe.draw())
        player.add(shoe.draw())
        dealer.add(shoe.draw())
        return GameState(
            shoe=shoe,
            dealer=dealer,
            hands=[player],
            bets=[bet],
            dealer_hits_soft_17=dealer_hits_soft_17,
            max_splits=max_splits,
        )

    def can_double(self) -> bool:
        h = self.active_hand()
        return len(h.cards) == 2 and not self._hand_standing(self.active_index) and not h.is_bust()

    def can_split(self) -> bool:
        h = self.active_hand()
        return h.can_split() and self.splits_used < self.max_splits

    def hit(self) -> None:
        if self.done:
            return
        h = self.active_hand()
        if self._hand_standing(self.active_index):
            return
        h.add(self.shoe.draw())
        if h.is_bust():
            self._set_stand(self.active_index)
            self._advance_if_needed()

    def stand(self) -> None:
        if self.done:
            return
        self._set_stand(self.active_index)
        self._advance_if_needed()

    def double(self) -> None:
        if self.done or not self.can_double():
            return
        self.bets[self.active_index] *= 2
        h = self.active_hand()
        h.doubled = True
        h.add(self.shoe.draw())
        self._set_stand(self.active_index)
        self._advance_if_needed()

    def split(self) -> bool:
        if self.done or not self.can_split():
            return False
        h = self.active_hand()
        c1, c2 = h.cards
        h.cards = [c1]
        h.from_split = True

        new_hand = Hand(cards=[c2], from_split=True)
        bet = self.bets[self.active_index]
        self.hands.insert(self.active_index + 1, new_hand)
        self.bets.insert(self.active_index + 1, bet)
        self.splits_used += 1

        # Deal one card to each split hand
        h.add(self.shoe.draw())
        new_hand.add(self.shoe.draw())

        # Split aces restriction: one card only, auto-stand both hands
        if c1.rank == "A":
            self._set_stand(self.active_index)
            self._set_stand(self.active_index + 1)
            self._advance_if_needed()

        return True

    def _advance_if_needed(self) -> None:
        while self.active_index < len(self.hands) and (self._hand_standing(self.active_index) or self.hands[self.active_index].is_bust()):
            self.active_index += 1
        if self.active_index >= len(self.hands):
            self._finish_round()

    def _dealer_should_hit(self) -> bool:
        total = self.dealer.best_total()
        if total < 17:
            return True
        if total > 17:
            return False
        # total == 17
        if self.dealer_hits_soft_17 and self.dealer.is_soft():
            return True
        return False

    def _finish_round(self) -> None:
        if any(not h.is_bust() for h in self.hands):
            while self._dealer_should_hit():
                self.dealer.add(self.shoe.draw())
        self.done = True

    def settle(self) -> List[Tuple[str, int]]:
        if not self.done:
            self._finish_round()

        dealer_total = self.dealer.best_total()
        dealer_bust = self.dealer.is_bust()
        dealer_bj = self.dealer.is_blackjack()

        results: List[Tuple[str, int]] = []
        for hand, bet in zip(self.hands, self.bets):
            if hand.is_bust():
                results.append(("bust", -bet))
                continue

            player_total = hand.best_total()
            player_bj = hand.is_blackjack()

            if dealer_bj and player_bj:
                results.append(("push", 0))
                continue
            if dealer_bj and not player_bj:
                results.append(("loss", -bet))
                continue
            if player_bj and not dealer_bj:
                results.append(("blackjack", int(1.5 * bet)))
                continue

            if dealer_bust:
                results.append(("win", +bet))
                continue

            if player_total > dealer_total:
                results.append(("win", +bet))
            elif player_total < dealer_total:
                results.append(("loss", -bet))
            else:
                results.append(("push", 0))

        return results
