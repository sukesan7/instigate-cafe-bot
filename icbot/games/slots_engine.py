from __future__ import annotations

import random
from dataclasses import dataclass

# Symbol definitions with weights and 3-match multipliers
SYMBOLS = [
    ("💎", 2, 50),  # Diamond — rarest, 50x
    ("7️⃣", 4, 20),  # Seven — 20x
    ("🍒", 8, 10),  # Cherry — 10x
    ("🔔", 10, 5),  # Bell — 5x
    ("🍋", 12, 3),  # Lemon — 3x
    ("⭐", 14, 2),  # Star — most common, 2x
]

# Build weighted pool
_POOL: list[tuple[str, int]] = []  # (emoji, multiplier)
for emoji, weight, multi in SYMBOLS:
    _POOL.extend([(emoji, multi)] * weight)


@dataclass(frozen=True)
class SlotResult:
    reels: tuple[str, str, str]
    multiplier: int  # 0 if loss
    bet: int
    payout: int  # 0 if loss, bet * multiplier if win
    net: int  # payout - bet
    match_type: str  # "three", "two", "none"


def spin(bet: int) -> SlotResult:
    """Spin the 3-reel slot machine."""
    r1 = random.choice(_POOL)
    r2 = random.choice(_POOL)
    r3 = random.choice(_POOL)

    reel1, _ = r1
    reel2, _ = r2
    reel3, _ = r3

    reels = (reel1, reel2, reel3)

    # Three of a kind
    if reel1 == reel2 == reel3:
        # Use the multiplier from the symbol table
        multi = r1[1]
        payout = bet * multi
        return SlotResult(
            reels=reels,
            multiplier=multi,
            bet=bet,
            payout=payout,
            net=payout - bet,
            match_type="three",
        )

    # Two of a kind (first two match)
    if reel1 == reel2:
        payout = bet  # 1x bet back (net = 0, push)
        return SlotResult(
            reels=reels,
            multiplier=1,
            bet=bet,
            payout=payout,
            net=0,
            match_type="two",
        )

    # No match
    return SlotResult(
        reels=reels,
        multiplier=0,
        bet=bet,
        payout=0,
        net=-bet,
        match_type="none",
    )


def reel_display(reels: tuple[str, str, str]) -> str:
    """Format reels for display."""
    return f"⟦ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ⟧"


def payout_table() -> str:
    """Generate a formatted payout table string."""
    lines = []
    for emoji, _weight, multi in SYMBOLS:
        lines.append(f"{emoji}{emoji}{emoji} — **{multi}x**")
    lines.append("Any two match — **1x** (push)")
    return "\n".join(lines)
