from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FunStockDef:
    symbol: str
    name: str
    start_price: int
    category: str = "misc"
    active: bool = True
    description: str = ""


# Code-defined catalog (edit this file to add/remove server stocks)
# Missing symbols are marked inactive on sync (history/positions are preserved).
FUN_STOCK_CATALOG: tuple[FunStockDef, ...] = (
    FunStockDef("IC", "Instigate Cafe", 100, "core", True, "Server flagship stock."),
    FunStockDef("DISD", "Distance Detector", 10, "capstone", True, "Capstone team stock."),
    FunStockDef("AIBS", "AI Blind Stick", 10, "capstone", True, "Capstone team stock."),
    FunStockDef("EMVC", "Emotional Voice Recognition", 10, "capstone", True, "Capstone team stock."),
    FunStockDef("AGIS", "Aegis - Paramedic Dashboard", 10, "misc", True, "Hackathon Team Stock."),
    FunStockDef("PLPT", "Plot Point", 10, "misc", True, "Private Entity stock."),
    FunStockDef("TGP", "The Graduates Project", 10, "misc", True, "Private Entity stock."),
    FunStockDef("CONT", "Continuity AI", 10, "misc", False, "Private Entity stock."),
    FunStockDef("BNZI", "Bonzai Stress Relief", 0, "unlisted", False, "Unlisted stock."),
    FunStockDef("HVAC", "AI Hvac", 10, "capstone", True, "Capstone team stock."),
)


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if s.startswith("$"):
        s = s[1:]
    return s


def catalog_map() -> dict[str, FunStockDef]:
    out: dict[str, FunStockDef] = {}
    for item in FUN_STOCK_CATALOG:
        sym = normalize_symbol(item.symbol)
        if not sym:
            continue
        out[sym] = FunStockDef(
            symbol=sym,
            name=item.name.strip() or sym,
            start_price=max(1, int(item.start_price)),
            category=(item.category or "misc").strip().lower() or "misc",
            active=bool(item.active),
            description=(item.description or "").strip(),
        )
    return out
