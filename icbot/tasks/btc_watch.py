from __future__ import annotations

import sqlite3
from typing import Any

import aiohttp
import aiosqlite
import discord

from icbot.embeds import system as system_embed
from icbot.services.btc_esplora import DEFAULT_BASE_URL, fetch_address_txs


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


async def _resolve_channel(bot: discord.Client, channel_id: int) -> discord.abc.Messageable | None:
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch  # type: ignore[return-value]
    try:
        return await bot.fetch_channel(channel_id)  # type: ignore[return-value]
    except (discord.DiscordException, LookupError, ValueError):
        return None


def _parse_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        toks = [t.strip() for t in raw.split(",")]
        return [t for t in toks if t]
    if isinstance(raw, list):
        toks = [str(x).strip() for x in raw]
        return [t for t in toks if t]
    return []


def _short_addr(addr: str) -> str:
    a = (addr or "").strip()
    if len(a) <= 14:
        return a
    return f"{a[:6]}…{a[-6:]}"


def _sats_to_btc(sats: int | None) -> float | None:
    if sats is None:
        return None
    return float(sats) / 100_000_000.0


def _fmt_btc(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.8f} BTC"


async def poll_and_post_btc_watch(bot: discord.Client) -> None:
    """Post alerts when watched BTC addresses see new transactions."""
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    enabled = bool(getattr(settings, "btc_watch_enabled", False))
    if not enabled:
        return

    debug = bool(getattr(settings, "btc_watch_debug", False))

    channel_id = (
        int(getattr(settings, "btc_watch_channel_id", 0) or 0)
        or int(getattr(settings, "market_watch_channel_id", 0) or 0)
        or int(getattr(settings, "stock_channel_id", 0) or 0)
    )
    if not channel_id:
        if debug:
            print("[BTC] enabled=true but no channel configured")
        return

    ch = await _resolve_channel(bot, channel_id)
    if ch is None:
        if debug:
            print(f"[BTC] could not resolve channel_id={channel_id}")
        return

    base_url = str(getattr(settings, "btc_esplora_base_url", "") or "").strip() or DEFAULT_BASE_URL
    label = str(getattr(settings, "btc_watch_label", "BTC Watch") or "BTC Watch").strip()
    addresses = _parse_list(getattr(settings, "btc_watch_addresses", None))
    if not addresses:
        if debug:
            print("[BTC] enabled but no BTC_WATCH_ADDRESSES configured")
        return

    # Optional global cooldown to prevent spam if you watch many addresses
    cooldown_min = int(getattr(settings, "btc_watch_cooldown_minutes", 30) or 30)
    cooldown_s = max(60, cooldown_min * 60)
    last_any = _to_int(await _get_meta(db, "btc_last_alert_any"), 0)
    now = int(discord.utils.utcnow().timestamp())
    if now - last_any < cooldown_s:
        return

    for addr in addresses[:50]:  # hard cap for safety
        try:
            txs = await fetch_address_txs(addr, base_url=base_url, limit=5)
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError) as e:
            if debug:
                print(f"[BTC] fetch failed addr={_short_addr(addr)} err={e!r}")
            continue

        if not txs:
            continue

        newest = txs[0]
        last_txid = (await _get_meta(db, f"btc_last_tx:{addr}")).strip()

        if newest.txid and newest.txid != last_txid:
            # Post alert
            val_btc = _sats_to_btc(newest.value_sats)
            status = "CONFIRMED" if newest.confirmed else "PENDING"
            t = f"<t:{newest.block_time}:R>" if newest.block_time else "(time n/a)"
            tx_url = f"https://blockstream.info/tx/{newest.txid}"

            fields = [
                ("Address", f"`{addr}`", False),
                ("Tx", f"{tx_url}", False),
                ("Net flow", _fmt_btc(val_btc), True),
                ("Status", f"{status} • {t}", True),
            ]

            embed = system_embed(
                settings,
                title=f"₿ {label} movement",
                summary=f"New transaction detected for **{_short_addr(addr)}**",
                fields=fields,
                footer_hint=f"cooldown {cooldown_min}m",
            )

            try:
                await ch.send(embed=embed)
                await _set_meta(db, f"btc_last_tx:{addr}", newest.txid)
                await _set_meta(db, "btc_last_alert_any", str(now))
                if debug:
                    print(f"[BTC] posted alert addr={_short_addr(addr)} txid={newest.txid[:10]}…")
                # Respect global cooldown: one alert per run
                return
            except discord.Forbidden:
                try:
                    await ch.send(f"₿ {label}: {addr} moved ({status}) {tx_url}")
                    await _set_meta(db, f"btc_last_tx:{addr}", newest.txid)
                    await _set_meta(db, "btc_last_alert_any", str(now))
                    return
                except (
                    discord.DiscordException,
                    aiosqlite.Error,
                    sqlite3.Error,
                    RuntimeError,
                    OSError,
                    ValueError,
                ) as e:
                    if debug:
                        print(f"[BTC] send failed (forbidden fallback): {e!r}")
                    return
            except (
                discord.DiscordException,
                aiosqlite.Error,
                sqlite3.Error,
                RuntimeError,
                OSError,
                ValueError,
            ) as e:
                if debug:
                    print(f"[BTC] send failed: {e!r}")
                return
