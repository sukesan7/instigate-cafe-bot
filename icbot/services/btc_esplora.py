from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from icbot.errors import ProviderError, parse_retry_after, redact_secrets
from icbot.services.http_transport import ProviderProvenance, ProviderResult

"""Lightweight Bitcoin address watcher using an Esplora-compatible API.

We default to Blockstream's public Esplora instance:
  https://blockstream.info/api

Mempool.space and other Esplora deployments share a similar API surface.
"""

DEFAULT_BASE_URL = "https://blockstream.info/api"

_SESSION: aiohttp.ClientSession | None = None
_LOCK = asyncio.Lock()


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "InstigateCafeBot/1.0.0 (+https://github.com/)",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "keep-alive",
    }


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        return _SESSION

    async with _LOCK:
        if _SESSION and not _SESSION.closed:
            return _SESSION
        timeout = aiohttp.ClientTimeout(total=15)
        _SESSION = aiohttp.ClientSession(timeout=timeout, headers=_headers())
        return _SESSION


async def init_session() -> aiohttp.ClientSession:
    """Explicitly initialize the shared Esplora HTTP session during startup."""
    return await _get_session()


async def close_session() -> None:
    """Close and clear the shared Esplora HTTP session during shutdown/rollback."""
    global _SESSION
    session = _SESSION
    _SESSION = None
    if session is not None and not session.closed:
        await session.close()


@dataclass(frozen=True)
class AddressTx:
    txid: str
    confirmed: bool
    block_time: int | None
    value_sats: int | None


def _safe_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _tx_value_sats(tx: Any, address: str) -> int | None:
    """Best-effort net value change for the address (sats).

    Esplora tx format includes vout with scriptpubkey_address and value.
    We compute outputs to the address minus inputs from the address.
    """
    try:
        vouts = tx.get("vout") or []
        vins = tx.get("vin") or []

        out_sats = 0
        for o in vouts:
            if str(o.get("scriptpubkey_address") or "") == address:
                out_sats += int(o.get("value") or 0)

        in_sats = 0
        for i in vins:
            prev = i.get("prevout") or {}
            if str(prev.get("scriptpubkey_address") or "") == address:
                in_sats += int(prev.get("value") or 0)

        return out_sats - in_sats
    except (AttributeError, TypeError, ValueError):
        return None


async def fetch_address_txs(
    address: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = 10,
    session: aiohttp.ClientSession | None = None,
) -> list[AddressTx]:
    """Return newest txs for an address."""
    address = str(address).strip()
    if not address:
        return []

    active_session = session if session is not None and not session.closed else await _get_session()
    url = f"{base_url.rstrip('/')}/address/{address}/txs"
    async with active_session.get(url, headers=_headers()) as r:
        if r.status != 200:
            txt = await r.text(errors="ignore")
            safe_txt = redact_secrets(txt[:200])
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
            raise ProviderError(
                f"esplora address txs failed: status={r.status} body={safe_txt!r}",
                status=r.status,
                retry_after=retry_after,
            )
        data = await r.json()

    out: list[AddressTx] = []
    for tx in (data or [])[: max(1, min(limit, 50))]:
        txid = str(tx.get("txid") or "").strip()
        if not txid:
            continue
        status = tx.get("status") or {}
        confirmed = bool(status.get("confirmed"))
        block_time = _safe_int(status.get("block_time"))
        out.append(
            AddressTx(
                txid=txid,
                confirmed=confirmed,
                block_time=block_time,
                value_sats=_tx_value_sats(tx, address),
            )
        )
    return out


async def fetch_address_txs_provenance(
    address: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = 10,
    session: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] | None = None,
) -> ProviderResult[list[AddressTx]]:
    """Return newest txs for an address wrapped in a ProviderResult with provenance."""
    txs = await fetch_address_txs(
        address,
        base_url=base_url,
        limit=limit,
        session=session,
    )
    now = clock() if clock is not None else time.time()
    source_ts: float | None = None
    if txs:
        for t in txs:
            if t.block_time is not None:
                source_ts = float(t.block_time)
                break

    clean_base = base_url.rstrip("/")
    prov = ProviderProvenance(
        source="esplora_btc",
        source_url=f"{clean_base}/address/{address}/txs",
        fetched_at=now,
        source_timestamp=source_ts,
        status_code=200,
        content_length=len(txs),
    )
    return ProviderResult(data=txs, provenance=prov)
