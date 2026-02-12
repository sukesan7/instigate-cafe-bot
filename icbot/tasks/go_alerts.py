from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Optional

import aiohttp
import discord

from icbot.embeds import go_alert as go_alert_embed
from icbot.services.go_client import GOFetchError, GOMessage, fetch_service_alerts


_MAJOR_PATTERNS = [
    r"\bderail(ed|ment)?\b",
    r"\bcollision\b",
    r"\bfire\b",
    r"\bhaz(?:mat|ardous)\b",
    r"\bpolice\b",
    r"\bmedical\b",
    r"\bemergency\b",
    r"\bfatal\b",
    r"\btrespass\b",
    r"\bswitch\b",
    r"\bsignal\b",
    r"\btrack\b",
    r"\bpower\b",
    r"\boutage\b",
    r"\bno service\b",
    r"\bsuspend(ed|ed)?\b",
    r"\bcancell?ed\b",
    r"\bshuttle\b",
]
_MAJOR_RE = re.compile("|".join(_MAJOR_PATTERNS), re.IGNORECASE)

# GO rail corridor codes are usually short alpha codes (e.g., LW, LE, MI).
_TRAIN_LINE_RE = re.compile(r"^[A-Z]{2,4}$")


def _text_blob(m: GOMessage) -> str:
    parts = [
        m.subject_en or "",
        m.body_en or "",
        m.category or "",
        m.subcategory or "",
        " ".join(m.lines or []),
        " ".join(m.stops or []),
    ]
    return " ".join(p for p in parts if p).strip()


def _is_major(m: GOMessage) -> bool:
    blob = _text_blob(m).lower()
    if "disruption" in (m.category or "").lower():
        return True
    if "suspend" in (m.subcategory or "").lower():
        return True
    return bool(_MAJOR_RE.search(blob))


def _is_train_related(m: GOMessage) -> bool:
    for code in (m.lines or []):
        c = str(code).strip().upper()
        if _TRAIN_LINE_RE.match(c):
            return True
    return False


def _parse_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    # Examples in docs: "2026-01-09 15:56:04"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


async def _get_meta(db, key: str) -> str:
    row = await db.fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return str(row[0]) if row else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


async def _resolve_channel(bot: discord.Client, channel_id: int):
    try:
        ch = bot.get_channel(channel_id)
        if ch:
            return ch
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


async def poll_and_post_go_alerts(bot: discord.Client) -> None:
    """Poll GO Transit Service Alerts and post to a channel."""
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        print("[GO] missing bot.settings or bot.db")
        return

    api_base = str(getattr(settings, "go_api_base", "") or "").strip()
    access_key = str(getattr(settings, "go_api_key", "") or "").strip()

    # Channel defaults to TTC if not explicitly set.
    channel_id = int(
        getattr(settings, "go_alerts_channel_id", 0)
        or getattr(settings, "ttc_alerts_channel_id", 0)
        or 0
    )

    if not channel_id:
        print("[GO] WARNING: no channel id set (GO_ALERTS_CHANNEL_ID or TTC_ALERTS_CHANNEL_ID). ")
        return

    if not api_base or not access_key:
        print("[GO] WARNING: GO API not configured (GO_API_BASE/GO_API_KEY missing). ")
        return

    poll_level = str(getattr(settings, "go_alert_level", "major") or "major").strip().lower()
    poll_level = poll_level if poll_level in ("major", "all") else "major"

    bootstrap_post_latest = bool(getattr(settings, "go_bootstrap_post_latest", True))
    include_buses = bool(getattr(settings, "go_include_buses", False))
    debug = bool(getattr(settings, "go_debug", False))

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        print("[GO] cannot resolve channel")
        return

    # Fetch with small retry on transient issues
    msgs: list[GOMessage] = []
    last_err: Optional[BaseException] = None
    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                msgs = await fetch_service_alerts(session, api_base, access_key, timeout_seconds=18)
                last_err = None
                break
            except (asyncio.TimeoutError, aiohttp.ClientError, GOFetchError) as e:
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))

    if last_err is not None:
        print(f"[GO] fetch failed: {last_err!r}")
        return

    if not msgs:
        if debug:
            print("[GO] poll: fetched=0")
        return

    # Filter to train-related messages unless include_buses=true
    if not include_buses:
        msgs = [m for m in msgs if _is_train_related(m)]

    # Sort (oldest -> newest) by PostedDateTime where possible.
    msgs_sorted = sorted(
        msgs,
        key=lambda m: _parse_dt(m.posted_dt) or datetime.min,
    )

    newest = max(msgs_sorted, key=lambda m: _parse_dt(m.posted_dt) or datetime.min)

    last_code = (await _get_meta(db, "go_last_servicealert_code")).strip()

    if debug:
        major_ct = sum(1 for m in msgs_sorted if _is_major(m))
        print(
            f"[GO] poll: fetched={len(msgs_sorted)} major={major_ct} level={poll_level} "
            f"include_buses={include_buses} last_code={'set' if bool(last_code) else 'none'}"
        )
        if len(msgs_sorted) > 0 and poll_level == 'major' and major_ct == 0:
            print("[GO] NOTE: items fetched but none matched MAJOR patterns; set GO_ALERT_LEVEL=all to see them.")

    # Bootstrap behavior: optionally post ONE latest matching item, then checkpoint
    if not last_code:
        if bootstrap_post_latest:
            posted = False
            for m in reversed(msgs_sorted):  # newest-first
                major = _is_major(m)
                if poll_level == "major" and not major:
                    continue
                level = "MAJOR" if major else "INFO"
                embed = go_alert_embed(
                    settings,
                    headline=m.subject_en or "GO Transit Alert",
                    details=m.body_en or None,
                    level=level,
                    meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
                    posted_dt=m.posted_dt,
                    lines=m.lines,
                )
                try:
                    await channel.send(embed=embed)  # type: ignore[call-arg]
                    posted = True
                    break
                except Exception as e:
                    print(f"[GO] post failed: {e!r}")
                    break
            _ = posted

        await _set_meta(db, "go_last_servicealert_code", newest.code)
        if debug:
            print(f"[GO] bootstrap: checkpoint={newest.code} posted_latest={bootstrap_post_latest} level={poll_level}")
        return

    # If we can't find the last_code in the current feed (e.g., retention), treat it like a bootstrap.
    codes = [m.code for m in msgs_sorted]
    if last_code not in codes:
        if debug:
            print("[GO] WARNING: last checkpoint not found in feed; treating as bootstrap.")
        await _set_meta(db, "go_last_servicealert_code", newest.code)
        if bootstrap_post_latest:
            for m in reversed(msgs_sorted):
                major = _is_major(m)
                if poll_level == "major" and not major:
                    continue
                level = "MAJOR" if major else "INFO"
                embed = go_alert_embed(
                    settings,
                    headline=m.subject_en or "GO Transit Alert",
                    details=m.body_en or None,
                    level=level,
                    meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
                    posted_dt=m.posted_dt,
                    lines=m.lines,
                )
                try:
                    await channel.send(embed=embed)  # type: ignore[call-arg]
                    break
                except Exception as e:
                    print(f"[GO] post failed: {e!r}")
                    return
        return

    # Normal operation: post messages after the checkpoint (oldest -> newest)
    started = False
    for m in msgs_sorted:
        if m.code == last_code:
            started = True
            continue
        if not started:
            continue

        # IMPORTANT: advance checkpoint even if you don't post (prevents getting stuck)
        await _set_meta(db, "go_last_servicealert_code", m.code)

        major = _is_major(m)
        if poll_level == "major" and not major:
            continue

        level = "MAJOR" if major else "INFO"
        embed = go_alert_embed(
            settings,
            headline=m.subject_en or "GO Transit Alert",
            details=m.body_en or None,
            level=level,
            meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
            posted_dt=m.posted_dt,
            lines=m.lines,
        )
        try:
            await channel.send(embed=embed)  # type: ignore[call-arg]
        except Exception as e:
            print(f"[GO] post failed: {e!r}")
            return
