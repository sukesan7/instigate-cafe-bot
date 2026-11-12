from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

"""Yahoo Finance quote fetcher.

Yahoo Finance quote endpoints are undocumented and can intermittently require a
cookie+crumb to work reliably.

We keep a single persistent aiohttp session and refresh the crumb periodically.
This module is used by market volatility alerts as well as the daily market report.
"""

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

# Yahoo gets upset when you request huge symbol lists.
MAX_SYMBOLS_PER_REQUEST = 60


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
        """Pick the best "current" session view for alerting."""
        ms = (self.market_state or "").upper()

        if self.pre_change_pct is not None and ms.startswith("PRE"):
            return ("PRE", self.pre_price or self.regular_price, self.pre_change_pct, self.pre_change)

        if self.post_change_pct is not None and (ms.startswith("POST") or ms == "CLOSED" or ms == "POSTPOST"):
            return ("POST", self.post_price or self.regular_price, self.post_change_pct, self.post_change)

        if self.pre_change_pct is not None and ms == "CLOSED":
            return ("PRE", self.pre_price or self.regular_price, self.pre_change_pct, self.pre_change)

        return ("REG", self.regular_price, self.regular_change_pct, self.regular_change)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError, OverflowError):
        return None


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
    """Refresh cookie+crumb."""
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
    """Return a cached crumb if still fresh; otherwise refresh."""
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
    results = ((data or {}).get("quoteResponse") or {}).get("result") or []
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
        if post_pct is None and post_price is not None and reg_price not in (None, 0.0):
            post_pct = (post_price - reg_price) / reg_price * 100.0
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


async def _fetch_quotes_chunk(symbols: list[str], *, debug: bool) -> dict[str, Quote]:
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


async def fetch_quotes(symbols: list[str], *, debug: bool = False) -> dict[str, Quote]:
    """Fetch quotes for 1..N symbols.

    If the list is large, it is split into multiple requests and the results merged.
    """
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not symbols:
        return {}

    # De-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in symbols:
        k = s.upper()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)

    if len(uniq) <= MAX_SYMBOLS_PER_REQUEST:
        return await _fetch_quotes_chunk(uniq, debug=debug)

    out: dict[str, Quote] = {}
    for i in range(0, len(uniq), MAX_SYMBOLS_PER_REQUEST):
        chunk = uniq[i : i + MAX_SYMBOLS_PER_REQUEST]
        try:
            part = await _fetch_quotes_chunk(chunk, debug=debug)
            out.update(part)
        except (
            aiohttp.ClientError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as e:
            # Best-effort: keep the rest of the chunks; caller can decide if partial is ok.
            if debug:
                print(f"[YF] chunk fetch failed: {e!r} chunk={chunk[:5]}...(+{max(0, len(chunk) - 5)})")
            continue
    return out
