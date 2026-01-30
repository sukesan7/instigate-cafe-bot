from __future__ import annotations

import re
from typing import List

import aiohttp
import discord

from icbot.embeds import ttc_alert as ttc_alert_embed
from icbot.services.ttc_client import TTCItem, fetch_rss_items


_MAJOR_PATTERNS = [
    r"\bmajor delay\b",
    r"\bmajor delays\b",
    r"\bno service\b",
    r"\bservice suspended\b",
    r"\bsuspended\b",
    r"\bnot running\b",
    r"\bno trains\b",
    r"\btrack level\b",
    r"\btracks? blocked\b",
    r"\bpower\b",
    r"\bfire\b",
    r"\bfatal\b",
    r"\bdeath\b",
    r"\binjury\b",
    r"\bpolice\b",
    r"\bsecurity\b",
    r"\binvestigation\b",
    r"\bshuttle\b",
    r"\bbypass\b",
    r"\bemergency\b",
    r"\bderail\b",
    r"\bcollision\b",
    r"\bsignal\s+problem\b",
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


def _diff_new_items(items: List[TTCItem], last_guid: str) -> List[TTCItem]:
    """Return new items since last_guid (oldest->newest).

    If last_guid isn't found in the current feed window, return [] to avoid spam.
    """
    if not last_guid:
        return []
    if not any(it.guid == last_guid for it in items):
        return []

    new: List[TTCItem] = []
    for it in items:
        if it.guid == last_guid:
            break
        new.append(it)

    new.reverse()
    return new


async def poll_and_post_ttc(bot: discord.Client) -> None:
    """Poll TTC RSS feed and post *major* alerts into TTC_ALERTS_CHANNEL_ID.

    Behavior:
      - First run: bootstrap last guid without posting
      - Later: post only new items since last guid (major-only if configured)
      - Stores last guid in DB meta (key=ttc_last_guid)
    """
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    if not getattr(settings, "ttc_alerts_channel_id", 0):
        return

    # Resolve channel
    channel = bot.get_channel(settings.ttc_alerts_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(settings.ttc_alerts_channel_id)
        except Exception:
            return

    feed_url = getattr(settings, "ttc_feed_url", "") or "https://mstdn.ca/@ttcnotices.rss"
    poll_level = (getattr(settings, "ttc_alert_level", "major") or "major").strip().lower()

    try:
        async with aiohttp.ClientSession() as session:
            items = await fetch_rss_items(session, feed_url, limit=20, timeout_seconds=12)
    except Exception as e:
        print(f"[TTC] fetch failed: {e!r}")
        return

    if not items:
        return

    last_guid = await _get_meta(db, "ttc_last_guid")

    # Bootstrap: on first run, set checkpoint and exit
    if not last_guid:
        await _set_meta(db, "ttc_last_guid", items[0].guid)
        return

    new_items = _diff_new_items(items, last_guid)
    if not new_items:
        return

    for it in new_items:
        major = _is_major(it)
        if poll_level == "major" and not major:
            await _set_meta(db, "ttc_last_guid", it.guid)
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
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TTC] post failed: {e!r}")
            return

        await _set_meta(db, "ttc_last_guid", it.guid)
