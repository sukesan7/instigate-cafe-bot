from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

# American roulette: 0, 00, 1-36
NUMBERS = [*range(37), -1]  # -1 represents "00"

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

BetType = Literal[
    "red",
    "black",
    "even",
    "odd",
    "high",
    "low",
    "dozen1",
    "dozen2",
    "dozen3",
    "col1",
    "col2",
    "col3",
    "straight",
]


ROULETTE_PAYOUT_RATIOS: dict[str, int] = {
    "straight": 35,
    "red": 1,
    "black": 1,
    "even": 1,
    "odd": 1,
    "high": 1,
    "low": 1,
    "dozen1": 2,
    "dozen2": 2,
    "dozen3": 2,
    "col1": 2,
    "col2": 2,
    "col3": 2,
}


@dataclass(frozen=True)
class SpinResult:
    number: int  # 0-36, or -1 for 00
    color: str  # "red", "black", or "green"
    display: str  # "0", "00", "1", ..., "36"


@dataclass(frozen=True)
class RouletteResult:
    spin: SpinResult
    bet_type: str
    bet_target: str  # e.g., "red", "17", "dozen1"
    bet_amount: int
    payout: int  # 0 if loss, bet + winnings if win
    net: int  # payout - bet_amount
    won: bool


def _number_color(n: int) -> str:
    if n == 0 or n == -1:
        return "green"
    if n in RED_NUMBERS:
        return "red"
    return "black"


def _display_number(n: int) -> str:
    if n == -1:
        return "00"
    return str(n)


def spin(rng: random.Random | None = None, seed: int | None = None) -> SpinResult:
    """Spin the wheel and return the result with optional deterministic RNG or seed injection."""
    if rng is not None:
        n = rng.choice(NUMBERS)
    elif seed is not None:
        n = random.Random(seed).choice(NUMBERS)
    else:
        n = random.choice(NUMBERS)
    return SpinResult(number=n, color=_number_color(n), display=_display_number(n))


def _is_outside_winner(n: int) -> bool:
    """Returns True if number is NOT 0 or 00 (outside bets lose on green)."""
    return n > 0


def evaluate(bet_type: str, bet_target: str, bet_amount: int, result: SpinResult) -> RouletteResult:
    """Evaluate a bet against a spin result."""
    n = result.number
    won = False
    multiplier = 0

    if bet_type == "straight":
        target_n = -1 if bet_target == "00" else int(bet_target)
        if n == target_n:
            won = True
            multiplier = 35
    elif bet_type == "red":
        if _is_outside_winner(n) and n in RED_NUMBERS:
            won = True
            multiplier = 1
    elif bet_type == "black":
        if _is_outside_winner(n) and n in BLACK_NUMBERS:
            won = True
            multiplier = 1
    elif bet_type == "even":
        if _is_outside_winner(n) and n % 2 == 0:
            won = True
            multiplier = 1
    elif bet_type == "odd":
        if _is_outside_winner(n) and n % 2 == 1:
            won = True
            multiplier = 1
    elif bet_type == "low":
        if 1 <= n <= 18:
            won = True
            multiplier = 1
    elif bet_type == "high":
        if 19 <= n <= 36:
            won = True
            multiplier = 1
    elif bet_type == "dozen1":
        if 1 <= n <= 12:
            won = True
            multiplier = 2
    elif bet_type == "dozen2":
        if 13 <= n <= 24:
            won = True
            multiplier = 2
    elif bet_type == "dozen3":
        if 25 <= n <= 36:
            won = True
            multiplier = 2
    elif bet_type == "col1":
        if _is_outside_winner(n) and n % 3 == 1:
            won = True
            multiplier = 2
    elif bet_type == "col2":
        if _is_outside_winner(n) and n % 3 == 2:
            won = True
            multiplier = 2
    elif bet_type == "col3" and _is_outside_winner(n) and n % 3 == 0:
        won = True
        multiplier = 2

    payout = bet_amount + (bet_amount * multiplier) if won else 0
    net = payout - bet_amount

    return RouletteResult(
        spin=result,
        bet_type=bet_type,
        bet_target=bet_target,
        bet_amount=bet_amount,
        payout=payout,
        net=net,
        won=won,
    )


# Color emoji helpers
def color_emoji(color: str) -> str:
    return {"red": "🔴", "black": "⚫", "green": "🟢"}.get(color, "⚪")


def color_square(color: str) -> str:
    return {"red": "🟥", "black": "⬛", "green": "🟩"}.get(color, "⬜")
