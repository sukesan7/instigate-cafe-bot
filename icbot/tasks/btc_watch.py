from __future__ import annotations

from typing import Any

import aiohttp
import discord

from icbot.container import get_container
from icbot.embeds import system as system_embed
from icbot.errors import format_exception_safe
from icbot.services.alert_dedupe import (
    AlertDedupeManager,
    AlertLifecycleStatus,
    compute_content_hash,
    mask_btc_address,
    safe_send_alert,
    sanitize_btc_addresses,
)
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


async def poll_and_post_btc_watch(bot: discord.Client, *, raise_on_error: bool = False) -> None:
    """Post alerts when watched BTC addresses see new transactions."""
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return
    container = get_container(bot)
    secrets = settings.secret_values() if hasattr(settings, "secret_values") else ()

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
    addresses = sanitize_btc_addresses(getattr(settings, "btc_watch_addresses", None), max_addresses=50)
    if not addresses:
        if debug:
            print("[BTC] enabled but no valid BTC_WATCH_ADDRESSES configured")
        return

    guild = getattr(ch, "guild", None)
    guild_id = int(getattr(guild, "id", 0) or 0)
    dedupe_mgr = AlertDedupeManager(clock=container.clock)
    bootstrap_post = bool(getattr(settings, "btc_bootstrap_post", True))

    # Optional global cooldown to prevent spam if you watch many addresses
    cooldown_min = int(getattr(settings, "btc_watch_cooldown_minutes", 30) or 30)
    cooldown_s = max(60, cooldown_min * 60)
    last_any = _to_int(await _get_meta(db, "btc_last_alert_any"), 0)
    now = int(container.clock())
    if now - last_any < cooldown_s:
        return

    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    any_fetch_ok = False
    last_fetch_err: BaseException | None = None
    for addr in addresses:
        try:
            if container.fetch_address_txs_fn is not None:
                txs = await container.fetch_address_txs_fn(addr, base_url=base_url, limit=5)
            else:
                txs = await fetch_address_txs(addr, base_url=base_url, limit=5, session=open_session)
            any_fetch_ok = True
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError) as e:
            last_fetch_err = e
            if debug:
                print(
                    f"[BTC] fetch failed addr={mask_btc_address(addr)} err={format_exception_safe(e, secrets)}"
                )
            continue

        if not txs:
            continue

        newest = txs[0]
        if not newest.txid:
            continue

        last_txid = (await _get_meta(db, f"btc_last_tx:{addr}")).strip()
        h = compute_content_hash(f"{newest.confirmed}:{newest.value_sats}:{newest.block_time}")
        entity_id = f"{addr}:{newest.txid}"

        # Bootstrap: checkpoint without posting unless btc_bootstrap_post=True
        if (
            not last_txid or not await dedupe_mgr.is_bootstrapped(db, guild_id, "btc_watch", channel_id)
        ) and not bootstrap_post:
            await _set_meta(db, f"btc_last_tx:{addr}", newest.txid)
            await dedupe_mgr.checkpoint_bootstrap_item(db, guild_id, "btc_watch", channel_id, entity_id, h)
            await dedupe_mgr.mark_bootstrapped(db, guild_id, "btc_watch", channel_id)
            if debug:
                print(f"[BTC] bootstrap checkpoint addr={mask_btc_address(addr)} txid={newest.txid[:8]}")
            continue

        lifecycle = await dedupe_mgr.check_alert(db, guild_id, "btc_watch", channel_id, entity_id, h)
        if lifecycle == AlertLifecycleStatus.IDENTICAL_DUPLICATE:
            continue

        # Post alert for new transaction or material confirmation update
        val_btc = _sats_to_btc(newest.value_sats)
        status = "CONFIRMED" if newest.confirmed else "PENDING"
        t = f"<t:{newest.block_time}:R>" if newest.block_time else "(time n/a)"
        tx_url = f"https://blockstream.info/tx/{newest.txid}"

        headline_prefix = (
            "[CONFIRMED]"
            if lifecycle == AlertLifecycleStatus.MATERIAL_UPDATE and newest.confirmed
            else "New transaction"
        )

        fields = [
            ("Address", f"`{mask_btc_address(addr)}`", False),
            ("Tx", f"{tx_url}", False),
            ("Net flow", _fmt_btc(val_btc), True),
            ("Status", f"{status} • {t}", True),
        ]

        embed = system_embed(
            settings,
            title=f"₿ {label} movement",
            summary=f"{headline_prefix} detected for **{mask_btc_address(addr)}**",
            fields=fields,
            footer_hint=f"cooldown {cooldown_min}m • Data source: blockstream.info",
        )

        fallback = f"₿ {label}: {mask_btc_address(addr)} moved ({status}) {tx_url}"
        ok, send_status = await safe_send_alert(bot, channel_id, embed=embed, fallback_text=fallback)
        if not ok:
            if debug:
                print(f"[BTC] send failed: {send_status}")
            if raise_on_error:
                raise RuntimeError(f"BTC send failed: {send_status}")
            return

        await dedupe_mgr.record_delivery(db, guild_id, "btc_watch", channel_id, entity_id, h)
        await dedupe_mgr.mark_bootstrapped(db, guild_id, "btc_watch", channel_id)
        await _set_meta(db, f"btc_last_tx:{addr}", newest.txid)
        await _set_meta(db, "btc_last_alert_any", str(now))
        if debug:
            print(f"[BTC] posted alert addr={mask_btc_address(addr)} txid={newest.txid[:10]}…")
        return

    if raise_on_error and not any_fetch_ok and last_fetch_err is not None:
        raise last_fetch_err
