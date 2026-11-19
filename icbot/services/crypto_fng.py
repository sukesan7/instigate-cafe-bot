from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from icbot.errors import ProviderError, parse_retry_after, redact_secrets
from icbot.services.http_transport import ProviderProvenance, ProviderResult

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


async def fetch_fear_greed(*, session: aiohttp.ClientSession | None = None) -> FearGreed | None:
    active_session = session if session is not None and not session.closed else await _get_session()
    async with active_session.get(FNG_URL, headers={"User-Agent": "InstigateCafeBot/0.2.3"}) as r:
        if r.status != 200:
            txt = await r.text(errors="ignore")
            safe_txt = redact_secrets(txt[:200])
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            raise ProviderError(
                f"fng fetch failed status={r.status} body={safe_txt!r}",
                status=r.status,
                retry_after=retry_after,
            )
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


async def fetch_fear_greed_provenance(
    *,
    session: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] | None = None,
) -> ProviderResult[FearGreed | None]:
    """Fetch Fear & Greed index wrapped in a ProviderResult with provenance."""
    fng = await fetch_fear_greed(session=session)
    now = clock() if clock is not None else time.time()
    source_ts: float | None = float(fng.timestamp) if fng is not None and fng.timestamp is not None else None
    prov = ProviderProvenance(
        source="crypto_fng",
        source_url=FNG_URL,
        fetched_at=now,
        source_timestamp=source_ts,
        status_code=200,
        content_length=1 if fng is not None else 0,
    )
    return ProviderResult(data=fng, provenance=prov)
