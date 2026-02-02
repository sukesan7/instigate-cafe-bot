from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp
import discord

from icbot.embeds import ttc_alert as ttc_alert_embed
from icbot.services.ttc_client import TTCFetchError, TTCItem, fetch_account_status_items, resolve_account_id


_MAJOR_PATTERNS = [
    # "Hard" major-disruption signals
    r"\bno service\b",
    r"\bservice suspended\b",
    r"\bsuspended\b",
    r"\bshutdown\b",
    r"\bclosed\b",
    r"\bclosure\b",
    r"\bnot running\b",
    r"\bno trains\b",
    r"\btracks? blocked\b",
    r"\btrack level\b",
    r"\bderail\b",
    r"\bcollision\b",
    r"\bemergency\b",
    r"\bfire\b",
    r"\bpolice\b",
    r"\bsecurity\b",
    r"\binvestigation\b",
    r"\bfatal\b",
    r"\bdeath\b",
    r"\binjury\b",
    r"\bmedical\b",
    r"\btrespass\b",
    r"\bpower\b",
    r"\bsignal\s+problem\b",
    r"\bshuttle\b",
    r"\bbypass\b",

    # "Soft" major-disruption signals (these show up a lot in the live feed)
    r"\bdelay\b",
    r"\bdelays\b",
    r"\bstoppage\b",
    r"\bstopped\b",
    r"\bholding\b",
    r"\bheld\b",
    r"\bdivert(?:ed|ing)\b",
    r"\bdetour\b",
    r"\bturning back\b",
    r"\bshort turn\b",
]
_MAJOR_RE = re.compile("|".join(_MAJOR_PATTERNS), re.IGNORECASE)


def _is_major(item: TTCItem) -> bool:
    text = f"{item.title} {item.summary}".strip()
    return bool(_MAJOR_RE.search(text))


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


async def poll_and_post_ttc(bot: discord.Client) -> None:
    """
    Poll Mastodon API for TTC notices and auto-post into TTC_ALERTS_CHANNEL_ID.

    Stores in DB meta:
      - ttc_account_id: resolved numeric account id
      - ttc_last_status_id: last seen status id (checkpoint)
    """
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    channel_id = int(getattr(settings, "ttc_alerts_channel_id", 0) or 0)
    if not channel_id:
        return

    api_base = str(getattr(settings, "ttc_mastodon_base", "") or "").strip()
    acct = str(getattr(settings, "ttc_mastodon_acct", "") or "").strip()
    if not api_base or not acct:
        print("[TTC] missing TTC_MASTODON_BASE or TTC_MASTODON_ACCT")
        return

    poll_level = (getattr(settings, "ttc_alert_level", "major") or "major").strip().lower()
    if poll_level not in ("major", "all"):
        poll_level = "major"

    bootstrap_post_latest = bool(getattr(settings, "ttc_bootstrap_post_latest", False))

    # Include reply statuses by default (TTC often posts live updates as replies).
    include_replies = bool(getattr(settings, "ttc_include_replies", True))
    include_reblogs = bool(getattr(settings, "ttc_include_reblogs", False))
    debug = bool(getattr(settings, "ttc_debug", False))

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        print("[TTC] cannot resolve TTC_ALERTS_CHANNEL_ID")
        return

    # Resolve account id once (cached in DB)
    async with aiohttp.ClientSession() as session:
        account_id = await _get_meta(db, "ttc_account_id")
        if not account_id:
            try:
                account_id = await resolve_account_id(session, api_base, acct)
                await _set_meta(db, "ttc_account_id", account_id)
            except Exception as e:
                print(f"[TTC] resolve account id failed: {e!r}")
                return

        last_id = await _get_meta(db, "ttc_last_status_id")
        last_id = last_id.strip() or ""

        # Fetch with small retry on transient issues
        items: list[TTCItem] = []
        last_err: Optional[BaseException] = None
        for attempt in range(3):
            try:
                items = await fetch_account_status_items(
                    session,
                    api_base,
                    account_id,
                    since_id=last_id or None,
                    limit=20,
                    timeout_seconds=14,
                    include_replies=include_replies,
                    include_reblogs=include_reblogs,
                )
                last_err = None
                break
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
            except TTCFetchError as e:
                # These are usually config/rate-limit issues; no point retrying rapidly
                print(f"[TTC] fetch failed: {e!r}")
                return

        if last_err is not None:
            print(f"[TTC] fetch failed: {last_err!r}")
            return

        if debug:
            major_ct = sum(1 for it in items if _is_major(it))
            since_preview = (last_id[-8:] if last_id else 'none')
            print(
                f"[TTC] poll: fetched={len(items)} major={major_ct} "
                f"level={poll_level} include_replies={include_replies} since_id={since_preview}"
            )
            if len(items) > 0 and poll_level == 'major' and major_ct == 0:
                print("[TTC] NOTE: items fetched but none matched MAJOR patterns; set TTC_ALERT_LEVEL=all to see them.")

        if not items:
            return

        # Mastodon returns newest-first. We'll process oldest-first for clean chronological posting.
        items_oldest_first = list(reversed(items))

        # If first run (no checkpoint) -> optionally post ONE latest matching status, then checkpoint
        if not last_id:
            if bootstrap_post_latest:
                # Post the newest item that matches filter (prefer newest-first)
                posted = False
                for it in items:
                    major = _is_major(it)
                    if poll_level == "major" and not major:
                        continue
                    level = "MAJOR" if major else "INFO"
                    embed = ttc_alert_embed(
                        settings,
                        headline=it.title or "TTC Notice",
                        details=it.summary or None,
                        url=it.link or None,
                        level=level,
                    )
                    try:
                        await channel.send(embed=embed)  # type: ignore[call-arg]
                        posted = True
                        break
                    except Exception as e:
                        print(f"[TTC] post failed: {e!r}")
                        break

                # even if nothing matched, still checkpoint to prevent spam on restart
                _ = posted

            # Checkpoint to the newest status id we saw
            await _set_meta(db, "ttc_last_status_id", items[0].guid)
            if debug:
                print(
                    f"[TTC] bootstrap: checkpoint={items[0].guid[-8:]} posted_latest={bootstrap_post_latest} level={poll_level}"
                )
            return

        # Normal operation: post each new status (oldest -> newest)
        for it in items_oldest_first:
            major = _is_major(it)

            # IMPORTANT: advance checkpoint even if you don't post (prevents getting stuck)
            await _set_meta(db, "ttc_last_status_id", it.guid)

            if poll_level == "major" and not major:
                continue

            level = "MAJOR" if major else "INFO"
            embed = ttc_alert_embed(
                settings,
                headline=it.title or "TTC Notice",
                details=it.summary or None,
                url=it.link or None,
                level=level,
            )

            try:
                await channel.send(embed=embed)  # type: ignore[call-arg]
            except Exception as e:
                print(f"[TTC] post failed: {e!r}")
                return
