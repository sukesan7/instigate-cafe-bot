from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
import discord

from icbot.embeds import system as system_embed


# Yahoo Finance endpoints are undocumented and can require cookie+crumb.
YF_QUOTE_ENDPOINTS = [
    "https://query2.finance.yahoo.com/v7/finance/quote",
    "https://query1.finance.yahoo.com/v7/finance/quote",
]

# Cookie+crumb helpers
YF_FC_URL = "https://fc.yahoo.com"
YF_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

# ---- Persistent session + crumb cache (for 24/7 stability) ----
_SESSION: aiohttp.ClientSession | None = None
_SESSION_LOCK = asyncio.Lock()

_CRUMB: str | None = None
_CRUMB_EXP_TS: float = 0.0
_CRUMB_LOCK = asyncio.Lock()

# Refresh crumb periodically (Yahoo can invalidate unpredictably)
CRUMB_TTL_SECONDS = 30 * 60  # 30 minutes


@dataclass(frozen=True)
class Quote:
    symbol: str
    market_state: str | None

    regular_price: float | None
    regular_change_pct: float | None
    regular_change: float | None

    pre_price: float | None
    pre_change_pct: float | None
    pre_change: float | None

    post_price: float | None
    post_change_pct: float | None
    post_change: float | None

    previous_close: float | None

    def best_session(self) -> tuple[str, float | None, float | None, float | None]:
        """
        Pick the best "current" session view for alerting:
          - PRE: use preMarket fields when available
          - POST: use postMarket fields when available (often persists while marketState is CLOSED)
          - REG: regularMarket fields otherwise

        Returns: (session_name, price, change_pct, change)
        """
        ms = (self.market_state or "").upper()

        if self.pre_change_pct is not None and ms.startswith("PRE"):
            return ("PRE", self.pre_price or self.regular_price, self.pre_change_pct, self.pre_change)

        if self.post_change_pct is not None and (ms.startswith("POST") or ms == "CLOSED" or ms == "POSTPOST"):
            return ("POST", self.post_price or self.regular_price, self.post_change_pct, self.post_change)

        if self.pre_change_pct is not None and ms == "CLOSED":
            return ("PRE", self.pre_price or self.regular_price, self.pre_change_pct, self.pre_change)

        return ("REG", self.regular_price, self.regular_change_pct, self.regular_change)


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2f}"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int((s or "").strip() or default)
    except ValueError:
        return default


def _to_bool(s: str) -> bool:
    return (s or "").strip() in ("1", "true", "True", "yes", "on")


def _yf_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
        "Origin": "https://finance.yahoo.com",
        "Connection": "keep-alive",
    }


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        return _SESSION

    async with _SESSION_LOCK:
        if _SESSION and not _SESSION.closed:
            return _SESSION

        timeout = aiohttp.ClientTimeout(total=15)
        _SESSION = aiohttp.ClientSession(timeout=timeout, headers=_yf_headers())
        return _SESSION


async def _refresh_crumb(session: aiohttp.ClientSession, debug: bool) -> str:
    """
    Refresh cookie+crumb.
    This populates session cookies by hitting fc.yahoo.com and then reads crumb.
    """
    # Step 1: hit fc.yahoo.com to populate cookies
    async with session.get(YF_FC_URL, allow_redirects=True) as _r:
        await _r.read()

    # Step 2: request crumb
    async with session.get(YF_CRUMB_URL, allow_redirects=True) as r:
        if r.status != 200:
            txt = await r.text(errors="ignore")
            raise RuntimeError(f"crumb fetch failed: status={r.status} body={txt[:200]!r}")
        crumb = (await r.text(errors="ignore")).strip()
        if not crumb or "unauthorized" in crumb.lower():
            raise RuntimeError(f"crumb invalid: {crumb[:80]!r}")

    if debug:
        print("[YF] crumb refreshed")
    return crumb


async def _get_crumb(session: aiohttp.ClientSession, debug: bool) -> str:
    """
    Return a cached crumb if still fresh; otherwise refresh.
    """
    global _CRUMB, _CRUMB_EXP_TS
    now = time.time()

    if _CRUMB and now < _CRUMB_EXP_TS:
        return _CRUMB

    async with _CRUMB_LOCK:
        now = time.time()
        if _CRUMB and now < _CRUMB_EXP_TS:
            return _CRUMB

        _CRUMB = await _refresh_crumb(session, debug)
        _CRUMB_EXP_TS = time.time() + CRUMB_TTL_SECONDS
        return _CRUMB


def _quotes_from_payload(data: Any) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    results = (((data or {}).get("quoteResponse") or {}).get("result") or [])
    for item in results:
        sym = str(item.get("symbol") or "").strip()
        if not sym:
            continue

        prev_close = _safe_float(item.get("regularMarketPreviousClose"))

        reg_price = _safe_float(item.get("regularMarketPrice"))
        reg_pct = _safe_float(item.get("regularMarketChangePercent"))
        reg_chg = _safe_float(item.get("regularMarketChange"))

        pre_price = _safe_float(item.get("preMarketPrice"))
        pre_pct = _safe_float(item.get("preMarketChangePercent"))
        pre_chg = _safe_float(item.get("preMarketChange"))

        post_price = _safe_float(item.get("postMarketPrice"))
        post_pct = _safe_float(item.get("postMarketChangePercent"))
        post_chg = _safe_float(item.get("postMarketChange"))

        if pre_pct is None and pre_price is not None and prev_close not in (None, 0.0):
            pre_pct = (pre_price - prev_close) / prev_close * 100.0
        if post_pct is None and post_price is not None and prev_close not in (None, 0.0):
            post_pct = (post_price - prev_close) / prev_close * 100.0
        if reg_pct is None and reg_price is not None and prev_close not in (None, 0.0):
            reg_pct = (reg_price - prev_close) / prev_close * 100.0

        out[sym] = Quote(
            symbol=sym,
            market_state=(str(item.get("marketState")) if item.get("marketState") is not None else None),
            regular_price=reg_price,
            regular_change_pct=reg_pct,
            regular_change=reg_chg,
            pre_price=pre_price,
            pre_change_pct=pre_pct,
            pre_change=pre_chg,
            post_price=post_price,
            post_change_pct=post_pct,
            post_change=post_chg,
            previous_close=prev_close,
        )
    return out


async def _fetch_quotes(symbols: list[str], *, debug: bool) -> dict[str, Quote]:
    session = await _get_session()
    crumb = await _get_crumb(session, debug)

    params = {"symbols": ",".join(symbols), "crumb": crumb}

    last_status = 0
    last_body = ""

    for url in YF_QUOTE_ENDPOINTS:
        async with session.get(url, params=params) as r:
            last_status = r.status
            if r.status == 200:
                data = await r.json()
                if debug:
                    print(f"[YF] quote ok via {url} (crumb)")
                return _quotes_from_payload(data)

            txt = await r.text(errors="ignore")
            last_body = txt[:200]
            if debug:
                print(f"[YF] quote status={r.status} via {url} body={last_body!r}")

            # If Yahoo suddenly rejects the crumb, refresh once and let the next poll retry cleanly
            if r.status in (401, 403):
                global _CRUMB, _CRUMB_EXP_TS
                _CRUMB = None
                _CRUMB_EXP_TS = 0.0

    raise RuntimeError(f"Yahoo quote fetch failed (last_status={last_status}, body={last_body!r})")


async def _get_meta(db, key: str) -> str:
    row = await db.fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return str(row[0]) if row and row[0] is not None else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


async def _resolve_channel(bot: discord.Client, channel_id: int) -> Optional[discord.abc.Messageable]:
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch  # type: ignore[return-value]
    try:
        return await bot.fetch_channel(channel_id)  # type: ignore[return-value]
    except Exception:
        return None


def _default_watchlist() -> list[str]:
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        "AMD", "AVGO", "NFLX", "PLTR",
        "JPM", "BAC",
        "XOM", "CVX",
        "COST",
    ]


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


async def poll_and_post_market_watch(bot: discord.Client) -> None:
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    enabled = bool(getattr(settings, "market_watch_enabled", False))
    if not enabled:
        return

    debug = bool(getattr(settings, "market_watch_debug", False))

    channel_id = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
        getattr(settings, "stock_channel_id", 0) or 0
    )
    if not channel_id:
        if debug:
            print("[MARKET] enabled=true but no channel_id configured")
        return

    ch = await _resolve_channel(bot, channel_id)
    if ch is None:
        if debug:
            print(f"[MARKET] could not resolve channel_id={channel_id}")
        return

    vix_level_thr = float(getattr(settings, "market_watch_vix_level", 20.0) or 20.0)
    index_move_thr = float(getattr(settings, "market_watch_index_move_pct", 2.5) or 2.5)
    stock_move_thr = float(getattr(settings, "market_watch_stock_move_pct", 10.0) or 10.0)
    cooldown_min = int(getattr(settings, "market_watch_cooldown_minutes", 30) or 30)
    cooldown_s = max(60, cooldown_min * 60)

    tickers = _parse_ticker_list(getattr(settings, "market_watch_tickers", None))
    if not tickers:
        tickers = _default_watchlist()

    core = ["SPY", "QQQ", "^VIX"]
    symbols = list(dict.fromkeys(core + tickers))

    try:
        quotes = await _fetch_quotes(symbols, debug=debug)
    except Exception as e:
        if debug:
            print(f"[MARKET] fetch error: {e!r}")
        return

    q_spy = quotes.get("SPY")
    q_qqq = quotes.get("QQQ")
    q_vix = quotes.get("^VIX") or quotes.get("%5EVIX")

    market_state = (q_spy.market_state if q_spy else None) or (q_qqq.market_state if q_qqq else None) or (
        q_vix.market_state if q_vix else None
    ) or "UNKNOWN"
    market_state = str(market_state).upper()

    async def should_alert(key: str, triggered: bool) -> bool:
        state_key = f"mw_state:{key}"
        last_key = f"mw_last_alert:{key}"

        prev_state = _to_bool(await _get_meta(db, state_key))
        last_alert = _to_int(await _get_meta(db, last_key), 0)
        now = int(discord.utils.utcnow().timestamp())

        if triggered and ((not prev_state) or (now - last_alert) >= cooldown_s):
            await _set_meta(db, state_key, "1")
            await _set_meta(db, last_key, str(now))
            return True

        if not triggered and prev_state:
            await _set_meta(db, state_key, "0")
        return False

    alerts: list[str] = []

    vix_level = q_vix.regular_price if q_vix else None
    vix_high = bool(vix_level is not None and vix_level >= vix_level_thr)
    if await should_alert("vix_level", vix_high):
        alerts.append(f"😱 **VIX elevated**: **{_fmt_price(vix_level)}** (thr ≥ {vix_level_thr:g})")

    def _idx_line(q: Quote, label: str) -> str:
        sess, price, pct, _chg = q.best_session()
        return f"📉 **{label} move** ({sess}): {_fmt_price(price)} ({_fmt_pct(pct)})"

    spy_sess = q_spy.best_session() if q_spy else ("REG", None, None, None)
    qqq_sess = q_qqq.best_session() if q_qqq else ("REG", None, None, None)

    spy_move = bool(q_spy and spy_sess[2] is not None and abs(float(spy_sess[2])) >= index_move_thr)
    qqq_move = bool(q_qqq and qqq_sess[2] is not None and abs(float(qqq_sess[2])) >= index_move_thr)

    if q_spy and await should_alert("spy_move", spy_move):
        alerts.append(_idx_line(q_spy, "SPY"))
    if q_qqq and await should_alert("qqq_move", qqq_move):
        alerts.append(_idx_line(q_qqq, "QQQ"))

    movers: list[tuple[Quote, str, float | None, float | None]] = []
    for sym in tickers:
        q = quotes.get(sym)
        if not q:
            continue
        sess, price, pct, _chg = q.best_session()
        if pct is None:
            continue
        if abs(float(pct)) >= stock_move_thr:
            movers.append((q, sess, price, pct))

    movers.sort(key=lambda t: abs(float(t[3] or 0.0)), reverse=True)

    mover_alerts: list[str] = []
    for q, sess, price, pct in movers[:10]:
        key = f"mover:{q.symbol}"
        if await should_alert(key, True):
            mover_alerts.append(f"• **{q.symbol}** ({sess}) {_fmt_price(price)} ({_fmt_pct(pct)})")

    for sym in tickers:
        q = quotes.get(sym)
        triggered_now = False
        if q:
            _sess, _price, pct, _chg = q.best_session()
            triggered_now = bool(pct is not None and abs(float(pct)) >= stock_move_thr)

        if not triggered_now:
            state_key = f"mw_state:mover:{sym}"
            if _to_bool(await _get_meta(db, state_key)):
                await _set_meta(db, state_key, "0")

    if mover_alerts:
        alerts.append("🔥 **Big movers:**\n" + "\n".join(mover_alerts))

    if debug:
        spy_s, _spy_p, spy_pct, _ = spy_sess
        qqq_s, _qqq_p, qqq_pct, _ = qqq_sess
        print(
            "[MARKET] snapshot: "
            f"state={market_state} "
            f"VIX={_fmt_price(vix_level)}(thr>={vix_level_thr:g}) "
            f"SPY({spy_s})={_fmt_pct(spy_pct)}(thr>={index_move_thr:g}%) "
            f"QQQ({qqq_s})={_fmt_pct(qqq_pct)}(thr>={index_move_thr:g}%) "
            f"movers={len(movers)}(thr>={stock_move_thr:g}%) "
            f"alerts={len(alerts)}"
        )

    if not alerts:
        return

    title = "⚠️ Market Volatility"
    subtitle = f"Market state: **{market_state}**"

    fields: list[tuple[str, str, bool]] = []

    snap_lines: list[str] = []
    if q_vix:
        snap_lines.append(f"VIX: **{_fmt_price(vix_level)}** (thr ≥ {vix_level_thr:g})")
    if q_spy:
        sess, price, pct, _ = q_spy.best_session()
        snap_lines.append(f"SPY ({sess}): **{_fmt_price(price)}** ({_fmt_pct(pct)})")
    if q_qqq:
        sess, price, pct, _ = q_qqq.best_session()
        snap_lines.append(f"QQQ ({sess}): **{_fmt_price(price)}** ({_fmt_pct(pct)})")

    if snap_lines:
        fields.append(("Snapshot", "\n".join(snap_lines), False))
    fields.append(("Triggered", "\n".join(alerts), False))

    footer_hint = (
        f"thr: VIX≥{vix_level_thr:g} | idx {index_move_thr:.1f}% | "
        f"stk {stock_move_thr:.1f}% | cooldown {cooldown_min}m"
    )

    embed = system_embed(
        settings,
        title=title,
        summary=subtitle,
        fields=fields,
        footer_hint=footer_hint,
    )

    if debug:
        print(f"[MARKET] posting alert -> channel_id={channel_id}")

    try:
        msg = await ch.send(embed=embed)
        if debug:
            print(f"[MARKET] posted ok: message_id={getattr(msg, 'id', 'n/a')}")
    except discord.Forbidden:
        # Fallback to plain text if Embed Links is blocked
        text = f"⚠️ Market Volatility | {market_state}\n" + "\n".join(alerts)
        try:
            msg = await ch.send(text)
            if debug:
                print(f"[MARKET] posted plain-text ok: message_id={getattr(msg, 'id', 'n/a')}")
        except Exception as e:
            if debug:
                print(f"[MARKET] ERROR: forbidden + plain-text send failed: {e!r}")
        return
    except Exception as e:
        if debug:
            print(f"[MARKET] ERROR: send failed: {e!r}")
        return