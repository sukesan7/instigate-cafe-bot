from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp

"""Crypto Fear & Greed Index (alternative.me).

Public endpoint (no key): https://api.alternative.me/fng/
"""

FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"

_SESSION: aiohttp.ClientSession | None = None
_LOCK = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        return _SESSION
    async with _LOCK:
        if _SESSION and not _SESSION.closed:
            return _SESSION
        timeout = aiohttp.ClientTimeout(total=12)
        _SESSION = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "InstigateCafeBot/0.2.3"})
        return _SESSION


async def init_session() -> aiohttp.ClientSession:
    """Explicitly initialize the shared Fear & Greed HTTP session during startup."""
    return await _get_session()


async def close_session() -> None:
    """Close and clear the shared Fear & Greed HTTP session during shutdown/rollback."""
    global _SESSION
    session = _SESSION
    _SESSION = None
    if session is not None and not session.closed:
        await session.close()


@dataclass(frozen=True)
class FearGreed:
    value: int
    value_classification: str
    timestamp: int | None


def _to_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


async def fetch_fear_greed() -> FearGreed | None:
    session = await _get_session()
    async with session.get(FNG_URL) as r:
        if r.status != 200:
            txt = await r.text(errors="ignore")
            raise RuntimeError(f"fng fetch failed status={r.status} body={txt[:200]!r}")
        data = await r.json()

    items = (data or {}).get("data") or []
    if not items:
        return None
    it = items[0] or {}
    val = _to_int(it.get("value"))
    cls = str(it.get("value_classification") or "").strip() or "unknown"
    ts = _to_int(it.get("timestamp"))
    if val is None:
        return None
    return FearGreed(value=val, value_classification=cls, timestamp=ts)
