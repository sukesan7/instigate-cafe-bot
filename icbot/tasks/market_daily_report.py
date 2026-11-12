from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import aiosqlite
import discord

from icbot.embeds import system as system_embed
from icbot.services.crypto_fng import fetch_fear_greed
from icbot.services.fun_market import FunMarketError, FunMarketStore
from icbot.services.yahoo_quotes import Quote, fetch_quotes


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2f}"


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int((s or "").strip() or default)
    except ValueError:
        return default


async def _get_meta(db, key: str) -> str:
    row = await db.fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return str(row[0]) if row and row[0] is not None else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


async def _fun_market_lines(settings, db) -> list[str]:
    """All fun stocks for the daily report, including IC."""
    try:
        store = FunMarketStore(
            db,
            starting_bankroll=int(getattr(settings, "bj_starting_bankroll", 1000) or 1000),
            tz_name=str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto"),
        )
        rows = await store.list_stocks(active_only=True)
    except (FunMarketError, aiosqlite.Error, sqlite3.Error, TypeError, ValueError):
        return []

    lines: list[str] = []
    # Show $IC first (flagship), then others
    ic_line = None
    other_lines: list[str] = []
    for s in rows:
        arrow = "📈" if s.weekly_change_abs >= 0 else "📉"
        line = f"{arrow} **${s.symbol}** — ${s.price} ({s.weekly_change_abs:+d}, {_fmt_pct(s.weekly_change_pct)})"
        if str(s.symbol).upper() == "IC":
            ic_line = f"🏢 **$IC** — ${s.price} ({s.weekly_change_abs:+d}, {_fmt_pct(s.weekly_change_pct)})"
        else:
            other_lines.append(line)
    if ic_line:
        lines.append(ic_line)
    lines.extend(other_lines[:10])
    return lines


async def _apply_sp500_move(settings, db, *, bot_id: int = 0, debug: bool = False) -> None:
    """Apply the S&P 500 daily % move to all active fun-market stocks.

    Called right after the daily report is posted.  Deduped per local date
    via meta key 'fun_auto_move_last_date'.
    """
    tz_name = str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto")
    today = _local_date_str(tz_name)

    # Dedupe: only run once per day
    last = await _get_meta(db, "fun_auto_move_last_date")
    if last == today:
        if debug:
            print(f"[DAILY] fun-market auto-move already applied for {today}")
        return

    # Fetch SPY quote (likely cached from the report build moments ago)
    try:
        quotes = await fetch_quotes(["SPY"], debug=debug)
        spy = quotes.get("SPY")
    except (aiohttp.ClientError, TimeoutError, RuntimeError, json.JSONDecodeError, ValueError) as e:
        if debug:
            print(f"[DAILY] SPY fetch failed for auto-move: {e!r}")
        return

    if spy is None or spy.regular_change_pct is None:
        if debug:
            print("[DAILY] no SPY data available, skipping auto-move")
        return

    pct = spy.regular_change_pct  # e.g. +1.25 or -0.80

    store = FunMarketStore(
        db,
        starting_bankroll=int(getattr(settings, "bj_starting_bankroll", 1000) or 1000),
        tz_name=tz_name,
    )

    try:
        stocks = await store.list_stocks(active_only=True)
    except (FunMarketError, aiosqlite.Error, sqlite3.Error, TypeError, ValueError) as e:
        if debug:
            print(f"[DAILY] list_stocks failed for auto-move: {e!r}")
        return

    moved = 0
    for s in stocks:
        price = int(s.price)
        if price <= 0:
            continue

        # Calculate delta from percentage; no minimum limit
        raw_delta = price * pct / 100.0
        delta = round(raw_delta)

        if delta == 0:
            continue  # 0% move or too small to move even $1, skip

        try:
            await store.admin_move(
                symbol=s.symbol,
                delta=delta,
                user_id=bot_id,
                reason=f"S&P 500 daily: {pct:+.2f}%",
            )
            moved += 1
        except (FunMarketError, aiosqlite.Error, sqlite3.Error, TypeError, ValueError) as e:
            if debug:
                print(f"[DAILY] auto-move {s.symbol}: {e!r}")

    await _set_meta(db, "fun_auto_move_last_date", today)
    if debug:
        print(f"[DAILY] auto-move applied: SPY {pct:+.2f}% → {moved} stocks moved")


async def _resolve_channel(bot: discord.Client, channel_id: int) -> discord.abc.Messageable | None:
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch  # type: ignore[return-value]
    try:
        return await bot.fetch_channel(channel_id)  # type: ignore[return-value]
    except (discord.DiscordException, LookupError, ValueError):
        return None


def _parse_ticker_list(raw: Any) -> list[str]:
    def _clean(tok: Any) -> str:
        s = str(tok).strip()
        if not s:
            return ""
        s = s.strip().strip('"').strip("'").strip()
        return s.upper()

    if raw is None:
        return []
    if isinstance(raw, str):
        toks = [_clean(t) for t in raw.split(",")]
        return [t for t in toks if t]
    if isinstance(raw, list):
        toks = [_clean(x) for x in raw]
        return [t for t in toks if t]
    return []


def _default_watchlist() -> list[str]:
    # Keep aligned with market_watch defaults.
    return [
        "AAPL",
        "MSFT",
        "AMZN",
        "GOOGL",
        "META",
        "NFLX",
        "NVDA",
        "AMD",
        "AVGO",
        "TSM",
        "ASML",
        "TSLA",
        "PLTR",
        "JPM",
        "BAC",
        "XOM",
        "CVX",
        "COST",
        "VRTX",
        "REGN",
        "MRNA",
    ]


@dataclass(frozen=True)
class _Move:
    symbol: str
    price: float | None
    pct: float | None


def _regular_move(q: Quote) -> _Move:
    return _Move(symbol=q.symbol, price=q.regular_price, pct=q.regular_change_pct)


def _local_date_str(tz_name: str) -> str:
    # zoneinfo is stdlib on py3.9+.
    try:
        tz = ZoneInfo(tz_name)
        return datetime.now(tz=tz).strftime("%Y-%m-%d")
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        # Fallback: UTC date (still dedupes within a given runtime)
        return datetime.now(UTC).strftime("%Y-%m-%d")


# ------------------------------------------------------------------------------
# Shared helpers (used by scheduled daily report AND /ic report command)
# ------------------------------------------------------------------------------


def resolve_market_report_tickers(settings) -> list[str]:
    """
    Resolve the watchlist for reports (daily report + on-demand report).

    Priority:
      1) MARKET_DAILY_REPORT_TICKERS
      2) MARKET_WATCH_TICKERS
      3) internal defaults
    """
    tickers = _parse_ticker_list(getattr(settings, "market_daily_report_tickers", None))
    if not tickers:
        tickers = _parse_ticker_list(getattr(settings, "market_watch_tickers", None))
    if not tickers:
        tickers = _default_watchlist()
    return tickers


async def build_market_report_embed(
    *,
    settings,
    db,
    tz_name: str,
    tickers: list[str],
    title: str,
    subtitle: str,
    debug: bool = False,
) -> discord.Embed:
    """
    Build the report embed without sending or writing meta state.
    Designed to be reused by:
      - post_daily_market_report() (scheduled)
      - /ic report (on-demand)
    """
    indices = ["SPY", "QQQ", "DIA", "IWM"]
    fear = ["^VIX", "^VIX9D", "^VVIX"]
    symbols = list(dict.fromkeys(indices + fear + tickers))

    quotes = await fetch_quotes(symbols, debug=debug)

    # Optional crypto fear&greed
    fng_text: str | None = None
    try:
        fng = await fetch_fear_greed()
        if fng:
            fng_text = f"**{fng.value}** — {fng.value_classification}"
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError):
        fng_text = None

    # Snapshot lines
    idx_lines: list[str] = []
    for sym in indices:
        q = quotes.get(sym)
        if q:
            idx_lines.append(f"**{sym}**: {_fmt_price(q.regular_price)} ({_fmt_pct(q.regular_change_pct)})")

    fear_lines: list[str] = []
    for sym in fear:
        q = quotes.get(sym) or quotes.get(sym.replace("^", "%5E"))
        if q:
            label = sym.replace("^", "")
            fear_lines.append(
                f"**{label}**: {_fmt_price(q.regular_price)} ({_fmt_pct(q.regular_change_pct)})"
            )

    moves: list[_Move] = []
    for sym in tickers:
        q = quotes.get(sym)
        if not q:
            continue
        m = _regular_move(q)
        if m.pct is None:
            continue
        moves.append(m)

    moves.sort(key=lambda m: float(m.pct or 0.0), reverse=True)
    top_n = int(getattr(settings, "market_daily_report_top_n", 8) or 8)
    top_n = max(3, min(top_n, 15))

    gainers = moves[:top_n]
    losers = list(reversed(moves[-top_n:])) if moves else []

    def _fmt_move(m: _Move) -> str:
        return f"• **{m.symbol}** {_fmt_price(m.price)} ({_fmt_pct(m.pct)})"

    g_lines = [_fmt_move(m) for m in gainers]
    l_lines = [_fmt_move(m) for m in losers]

    fields: list[tuple[str, str, bool]] = []
    if idx_lines:
        fields.append(("📊 Indices", "\n".join(idx_lines), False))
    if g_lines:
        fields.append(("🟢 Top Gainers", "\n".join(g_lines), True))
    if l_lines:
        fields.append(("🔴 Top Losers", "\n".join(l_lines), True))
    if fear_lines:
        fields.append(("⚡ Fear / Volatility", "\n".join(fear_lines), False))
    if fng_text:
        fields.append(("🌐 Crypto F&G", fng_text, True))

    fun_lines = await _fun_market_lines(settings, db)
    if fun_lines:
        fields.append(("🏛️ Instigate Cafe Stock Exchange", "\n".join(fun_lines), False))

    footer_hint = "Daily Report"

    return system_embed(
        settings,
        title=title,
        summary=subtitle,
        fields=fields,
        footer_hint=footer_hint,
    )


# ------------------------------------------------------------------------------
# Scheduled daily report (runs once/day from your scheduler)
# ------------------------------------------------------------------------------


async def post_daily_market_report(bot: discord.Client) -> None:
    """Post the daily market summary (intended to run once/day)."""
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    if not bool(getattr(settings, "market_daily_report_enabled", False)):
        return

    debug = bool(getattr(settings, "market_daily_report_debug", False))

    channel_id = (
        int(getattr(settings, "market_daily_report_channel_id", 0) or 0)
        or int(getattr(settings, "market_watch_channel_id", 0) or 0)
        or int(getattr(settings, "stock_channel_id", 0) or 0)
    )

    if not channel_id:
        if debug:
            print("[DAILY] enabled=true but no channel configured")
        return

    ch = await _resolve_channel(bot, channel_id)
    if ch is None:
        if debug:
            print(f"[DAILY] could not resolve channel_id={channel_id}")
        return

    tz_name = str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto")
    today = _local_date_str(tz_name)

    last = await _get_meta(db, "daily_report_last_date")
    if last == today:
        if debug:
            print(f"[DAILY] already posted for {today}")
        return

    tickers = resolve_market_report_tickers(settings)

    try:
        embed = await build_market_report_embed(
            settings=settings,
            db=db,
            tz_name=tz_name,
            tickers=tickers,
            title="📊 Daily Market Report",
            subtitle=f"End-of-day snapshot • {today} ({tz_name})",
            debug=debug,
        )
    except (
        aiohttp.ClientError,
        TimeoutError,
        RuntimeError,
        aiosqlite.Error,
        sqlite3.Error,
        ValueError,
        TypeError,
    ) as e:
        if debug:
            print(f"[DAILY] build embed failed: {e!r}")
        return

    try:
        await ch.send(embed=embed)
        await _set_meta(db, "daily_report_last_date", today)
        if debug:
            print(f"[DAILY] posted ok for {today}")

        # Auto-move fun-market stocks by S&P 500 daily %
        try:
            bot_id = bot.user.id if bot.user else 0
            await _apply_sp500_move(settings, db, bot_id=bot_id, debug=debug)
        except (
            aiohttp.ClientError,
            TimeoutError,
            RuntimeError,
            FunMarketError,
            aiosqlite.Error,
            sqlite3.Error,
            ValueError,
            TypeError,
        ) as e:
            if debug:
                print(f"[DAILY] auto-move error: {e!r}")

        return

    except discord.Forbidden:
        # Plain-text fallback (embed perms blocked)
        lines: list[str] = []
        title = embed.title or "📊 Daily Market Report"
        lines.append(f"{title} ({today})")

        if getattr(embed, "description", None):
            lines.append(str(embed.description))

        for f in getattr(embed, "fields", []):
            name = getattr(f, "name", "").strip()
            value = getattr(f, "value", "").strip()
            if name and value:
                lines.append(f"\n{name}\n{value}")

        text = "\n".join(lines).strip()

        # Chunk to avoid Discord 2000-char limit
        chunks: list[str] = []
        while text:
            chunks.append(text[:1800])
            text = text[1800:]

        try:
            for chunk in chunks:
                await ch.send(chunk)
            await _set_meta(db, "daily_report_last_date", today)
            if debug:
                print(f"[DAILY] posted text ok for {today}")
        except (
            discord.DiscordException,
            aiosqlite.Error,
            sqlite3.Error,
            RuntimeError,
            OSError,
            ValueError,
        ) as e:
            if debug:
                print(f"[DAILY] send failed (forbidden fallback): {e!r}")

    except (
        discord.DiscordException,
        aiosqlite.Error,
        sqlite3.Error,
        RuntimeError,
        OSError,
        ValueError,
    ) as e:
        if debug:
            print(f"[DAILY] send failed: {e!r}")
