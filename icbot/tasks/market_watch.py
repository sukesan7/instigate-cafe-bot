from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord

from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import format_exception_safe
from icbot.services.alert_dedupe import (
    FINANCIAL_DISCLAIMER,
    AlertDedupeManager,
    compute_content_hash,
    format_financial_embed_footer,
    safe_send_alert,
)
from icbot.services.yahoo_quotes import Quote, fetch_quotes

log = logging.getLogger(__name__)


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


def _to_bool(s: str) -> bool:
    return (s or "").strip() in ("1", "true", "True", "yes", "on")


async def _get_meta(db, key: str) -> str:
    row = await db.fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return str(row[0]) if row and row[0] is not None else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


async def _resolve_channel(bot: discord.Client, channel_id: int) -> discord.abc.Messageable | None:
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch  # type: ignore[return-value]
    try:
        return await bot.fetch_channel(channel_id)  # type: ignore[return-value]
    except (discord.DiscordException, LookupError, ValueError):
        return None


def _default_watchlist() -> list[str]:
    """Default watchlist.

    Keep this reasonably sized; users can extend via MARKET_WATCH_TICKERS.
    """
    return [
        # FAANG+
        "AAPL",
        "MSFT",
        "AMZN",
        "GOOGL",
        "META",
        "NFLX",
        # Semis / infra
        "NVDA",
        "AMD",
        "AVGO",
        "TSM",
        "ASML",
        # High-beta / AI / hype
        "TSLA",
        "PLTR",
        # Financials / energy / broad retail
        "JPM",
        "BAC",
        "XOM",
        "CVX",
        "COST",
        # Biotech (light)
        "VRTX",
        "REGN",
        "MRNA",
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


async def poll_and_post_market_watch(bot: discord.Client, *, raise_on_error: bool = False) -> None:
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return
    container = get_container(bot)
    secrets = settings.secret_values() if hasattr(settings, "secret_values") else ()

    enabled = bool(getattr(settings, "market_watch_enabled", False))
    if not enabled:
        return

    debug = bool(getattr(settings, "market_watch_debug", False))

    channel_id = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
        getattr(settings, "stock_channel_id", 0) or 0
    )
    if not channel_id:
        if debug:
            log.debug("[MARKET] enabled=true but no channel_id configured")
        return

    ch = await _resolve_channel(bot, channel_id)
    if ch is None:
        if debug:
            log.warning("[MARKET] could not resolve channel_id=%s", channel_id)
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

    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    try:
        if container.fetch_quotes_fn is not None:
            quotes = await container.fetch_quotes_fn(symbols, debug=debug)
        else:
            quotes = await fetch_quotes(
                symbols,
                debug=debug,
                session=open_session,
                crumb_cache=container.yahoo_crumb,
                clock=container.clock,
            )
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError) as e:
        if debug:
            log.warning("[MARKET] fetch error: %s", format_exception_safe(e, secrets))
        if raise_on_error:
            raise
        return

    q_spy = quotes.get("SPY")
    q_qqq = quotes.get("QQQ")
    q_vix = quotes.get("^VIX") or quotes.get("%5EVIX")

    market_state = (
        (q_spy.market_state if q_spy else None)
        or (q_qqq.market_state if q_qqq else None)
        or (q_vix.market_state if q_vix else None)
    )

    raw_state = str(market_state).upper()
    state_map = {
        "PREPRE": "Pre-Market",
        "PRE": "Pre-Market",
        "REGULAR": "Regular Trading Hours",
        "POST": "Post-Market",
        "POSTPOST": "Post-Market",
        "CLOSED": "Closed",
    }
    market_state = state_map.get(raw_state, raw_state)

    async def should_alert(key: str, triggered: bool) -> bool:
        """Edge-triggered alert gate with cooldown.

        Fires ONLY on a rising edge (False -> True). While the condition remains True,
        it will NOT re-post, regardless of cooldown. After the condition clears (True -> False),
        it can fire again on the next rising edge, but not more often than the cooldown window
        (to avoid flapping spam).
        """

        state_key = f"mw_state:{key}"
        last_key = f"mw_last_alert:{key}"

        prev_state = _to_bool(await _get_meta(db, state_key))
        last_alert = _to_int(await _get_meta(db, last_key), 0)
        now = int(container.clock())

        if not triggered:
            if prev_state:
                await _set_meta(db, state_key, "0")
            return False

        # Already active -> do not repeat while the condition remains true.
        if prev_state:
            return False

        # Rising edge: respect cooldown to avoid rapid flapping.
        if (now - last_alert) < cooldown_s:
            return False

        await _set_meta(db, state_key, "1")
        await _set_meta(db, last_key, str(now))
        return True

    alerts: list[str] = []

    vix_level = q_vix.regular_price if q_vix else None
    vix_high = bool(vix_level is not None and vix_level >= vix_level_thr)
    if await should_alert("vix_level", vix_high):
        alerts.append(f"**VIX elevated**: **{_fmt_price(vix_level)}** (thr ≥ {vix_level_thr:g})")

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
        log.debug(
            "[MARKET] snapshot: state=%s VIX=%s(thr>=%g) SPY(%s)=%s(thr>=%g%%) QQQ(%s)=%s(thr>=%g%%) movers=%d(thr>=%g%%) alerts=%d",
            market_state,
            _fmt_price(vix_level),
            vix_level_thr,
            spy_s,
            _fmt_pct(spy_pct),
            index_move_thr,
            qqq_s,
            _fmt_pct(qqq_pct),
            index_move_thr,
            len(movers),
            stock_move_thr,
            len(alerts),
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
    fields.append(("Notice", FINANCIAL_DISCLAIMER, False))

    now_ts = int(container.clock())
    footer_text = format_financial_embed_footer("Yahoo Finance", now_ts, extra="Market Volatility Tracker")

    guild = getattr(ch, "guild", None)
    guild_id = int(getattr(guild, "id", 0) or 0)
    eff_settings = await resolve_guild_settings(bot, guild_id) if guild_id > 0 else settings
    dedupe_mgr = AlertDedupeManager(clock=container.clock)

    embed = system_embed(
        eff_settings,
        title=title,
        summary=subtitle,
        fields=fields,
        footer_hint=footer_text,
    )

    if debug:
        log.info("[MARKET] posting alert -> channel_id=%s", channel_id)

    plain_text = f"⚠️ Market Volatility | {market_state}\n" + "\n".join(alerts) + f"\n\n{FINANCIAL_DISCLAIMER}"
    ok, send_status = await safe_send_alert(bot, channel_id, embed=embed, fallback_text=plain_text)
    if not ok:
        if debug:
            log.warning("[MARKET] send failed: %s", send_status)
        if raise_on_error:
            raise RuntimeError(f"Market watch send failed: {send_status}")
        return

    content_hash = compute_content_hash("\n".join(alerts))
    await dedupe_mgr.record_delivery(
        db, guild_id, "market_volatility", channel_id, f"mv_{now_ts}", content_hash
    )
    if debug:
        log.info("[MARKET] posted ok: status=%s", send_status)
