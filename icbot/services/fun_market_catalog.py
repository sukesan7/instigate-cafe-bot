from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FunStockDef:
    """Definition of a simulated fun-market stock."""

    symbol: str
    name: str
    start_price: int
    category: str = "misc"
    active: bool = True
    description: str = ""


# Canonical synthetic sample securities (strictly simulated game points):
# Fictional campus community exchange — zero real financial entities or student private lore.
FUN_STOCK_CATALOG: tuple[FunStockDef, ...] = (
    FunStockDef("IC", "Instigate Cafe", 100, "core", True, "Simulated flagship community points."),
    FunStockDef(
        "CAFE", "Campus Coffee & Cafe Co.", 10, "campus", True, "Fictional campus snack & coffee exchange."
    ),
    FunStockDef(
        "BOOK", "Academic Bookstore Co.", 10, "academics", True, "Fictional textbook and supply store."
    ),
    FunStockDef("BYTE", "Computer Science Club", 10, "tech", True, "Fictional coding student collective."),
    FunStockDef("ROBO", "Campus Robotics Initiative", 10, "tech", True, "Fictional robotics lab project."),
    FunStockDef(
        "LABS", "Open Science Collective", 10, "academics", True, "Fictional student research project."
    ),
    FunStockDef(
        "DORM", "Campus Housing Guild", 10, "campus", True, "Fictional residential hall association."
    ),
    FunStockDef("QUIZ", "Trivia & Game Society", 10, "social", True, "Fictional student social games group."),
    FunStockDef("MEME", "Campus Meme Treasury", 10, "social", True, "Fictional community humor archive."),
    FunStockDef(
        "VOID", "Retired Inactive Venture", 10, "unlisted", False, "Fictional decommissioned project."
    ),
)

# Backward-compatibility alias map for legacy private-community symbols
LEGACY_SYMBOL_MAP: dict[str, str] = {
    "DISD": "CAFE",
    "AIBS": "BYTE",
    "EMVC": "LABS",
    "AGIS": "ROBO",
    "PLPT": "DORM",
    "TGP": "QUIZ",
    "CONT": "VOID",
    "BNZI": "VOID",
    "HVAC": "BOOK",
}


def normalize_symbol(symbol: str) -> str:
    """Strip whitespace, remove leading $, and uppercase."""
    s = str(symbol or "").strip().upper()
    if s.startswith("$"):
        s = s[1:]
    return s


def resolve_symbol(symbol: str) -> str:
    """Normalize symbol and resolve through legacy alias map if applicable."""
    norm = normalize_symbol(symbol)
    return LEGACY_SYMBOL_MAP.get(norm, norm)


def catalog_map(
    settings: Any | None = None,
    custom_catalog: Sequence[FunStockDef] | None = None,
) -> dict[str, FunStockDef]:
    """Return catalog map, adapting flagship stock name and symbol to settings when configured."""
    out: dict[str, FunStockDef] = {}
    primary_sym = str(getattr(settings, "primary_stock_symbol", "IC") or "IC").strip().upper()
    primary_name = str(getattr(settings, "primary_stock_name", "Instigate Cafe") or "Instigate Cafe").strip()

    source = custom_catalog if custom_catalog is not None else FUN_STOCK_CATALOG

    for item in source:
        sym = normalize_symbol(item.symbol)
        if not sym:
            continue
        if sym == "IC" and primary_sym != "IC":
            out[primary_sym] = FunStockDef(
                symbol=primary_sym,
                name=primary_name or primary_sym,
                start_price=max(1, int(item.start_price)),
                category="core",
                active=bool(item.active),
                description=f"Server flagship stock ({primary_name}).",
            )
            continue
        out[sym] = FunStockDef(
            symbol=sym,
            name=primary_name if sym == "IC" else (item.name.strip() or sym),
            start_price=max(1, int(item.start_price)),
            category=(item.category or "misc").strip().lower() or "misc",
            active=bool(item.active),
            description=(item.description or "").strip(),
        )
    return out
