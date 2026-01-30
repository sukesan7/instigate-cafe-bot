from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import time
import aiohttp
import xml.etree.ElementTree as ET

from discord import app_commands
import discord

from icbot.app import ic
from icbot.embeds import system as system_embed
from icbot.services.sports_client import OddsAPIClient, OddsAPIError


# Odds API sport keys
SPORT_KEYS = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "mma": "mma_mixed_martial_arts",
    "ufc": "mma_mixed_martial_arts",
    "boxing": "boxing_boxing",
}

# Default RSS feeds (override via env if you want)
DEFAULT_RSS = {
    "nba": ["https://www.espn.com/espn/rss/nba/news"],
    "nfl": ["https://www.espn.com/espn/rss/nfl/news"],
    "mma": ["https://www.espn.com/espn/rss/mma/news"],
    "ufc": ["https://www.espn.com/espn/rss/mma/news"],
    "boxing": ["https://www.espn.com/espn/rss/boxing/news"],
}

# Simple in-process cache for RSS so we don't spam
_RSS_CACHE: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
_RSS_TTL_SECONDS = 600


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _get_settings(interaction: discord.Interaction):
    # Your bot stores settings on interaction.client (from main.py)
    return getattr(interaction.client, "settings", None)


def _get_odds_client(interaction: discord.Interaction) -> OddsAPIClient:
    settings = _get_settings(interaction)
    api_key = ""
    # Try settings first (if you later add it to Settings), else env
    api_key = getattr(settings, "odds_api_key", "") if settings else ""
    api_key = (api_key or os.getenv("ODDS_API_KEY", "") or "").strip()
    return OddsAPIClient(api_key=api_key, cache_ttl_seconds=600)


def _rss_urls_for(sport: str) -> List[str]:
    sport = sport.lower().strip()
    env_key = f"SPORTS_RSS_{sport.upper()}"
    override = (os.getenv(env_key, "") or "").strip()
    if override:
        return [u.strip() for u in override.split(",") if u.strip()]
    return DEFAULT_RSS.get(sport, [])


async def _fetch_rss_items(url: str, *, limit: int = 5) -> List[Tuple[str, str]]:
    """
    Return list[(title, link)] from RSS/Atom feed.
    Robust enough for simple headline pulls.
    """
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "User-Agent": "InstigateCafeBot/1.0 (+discord bot; personal project)",
        "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            text = await resp.text()
            if resp.status >= 400:
                return []

    # Parse XML (RSS or Atom)
    try:
        root = ET.fromstring(text)
    except Exception:
        return []

    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    items: List[Tuple[str, str]] = []

    # RSS: <rss><channel><item>...
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        if title and link:
            items.append((_clip(title, 120), link))
        if len(items) >= limit:
            return items

    # Atom: <feed><entry>...
    for entry in root.findall(".//{*}entry"):
        title = entry.findtext("{*}title") or ""
        link_el = entry.find("{*}link")
        link = link_el.get("href", "") if link_el is not None else ""
        if title and link:
            items.append((_clip(title, 120), link))
        if len(items) >= limit:
            return items

    return items


async def _get_news(sport: str, *, limit: int = 5) -> List[Tuple[str, str]]:
    urls = _rss_urls_for(sport)
    if not urls:
        return []

    cache_key = f"{sport}:{','.join(urls)}:{limit}"
    cached = _RSS_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) <= _RSS_TTL_SECONDS:
        return cached[1]

    results: List[Tuple[str, str]] = []
    seen = set()
    for url in urls:
        items = await _fetch_rss_items(url, limit=limit)
        for title, link in items:
            if link in seen:
                continue
            seen.add(link)
            results.append((title, link))
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    _RSS_CACHE[cache_key] = (time.time(), results)
    return results


def _format_upcoming(events) -> str:
    if not events:
        return "No upcoming events found."
    lines = []
    for e in events:
        ts = _unix(e.commence_time)
        lines.append(f"• **{e.away_team} @ {e.home_team}** — <t:{ts}:f> (<t:{ts}:R>)")
    return "\n".join(lines)


def _format_news(items: List[Tuple[str, str]]) -> str:
    if not items:
        return "News unavailable (no feed configured or fetch failed)."
    lines = []
    for title, link in items:
        # Discord supports raw links well; markdown can be inconsistent across clients.
        lines.append(f"• {_clip(title, 160)}\n<{link}>")
    return "\n".join(lines)


async def _send_sport(interaction: discord.Interaction, sport: str, title: str):
    settings = _get_settings(interaction)
    if settings is None:
        await interaction.response.send_message("Bot settings not initialized.", ephemeral=True)
        return

    client = _get_odds_client(interaction)
    sport_key = SPORT_KEYS[sport]

    try:
        events = await client.get_upcoming(sport_key, limit=5, days_from=7)
    except OddsAPIError as e:
        emb = system_embed(
            settings,
            title=f"{title}",
            summary="Could not fetch upcoming games.",
            fields=[("Error", _clip(str(e), 900))],
            footer_hint="Sports",
        )
        await interaction.response.send_message(embed=emb)
        return

    # News is optional: if RSS fails, we still show games
    news_items = []
    try:
        news_items = await _get_news(sport, limit=5)
    except Exception:
        news_items = []

    emb = system_embed(
        settings,
        title=title,
        summary="Upcoming games + latest headlines.",
        fields=[
            ("Upcoming (next 5)", _format_upcoming(events)),
            ("News (latest 5)", _format_news(news_items)),
        ],
        footer_hint=f"Sports • {sport.upper()}",
    )
    await interaction.response.send_message(embed=emb)


@ic.command(name="nba", description="Upcoming NBA games + latest headlines")
async def nba(interaction: discord.Interaction):
    await _send_sport(interaction, "nba", "🏀 NBA")


@ic.command(name="nfl", description="Upcoming NFL games + latest headlines")
async def nfl(interaction: discord.Interaction):
    await _send_sport(interaction, "nfl", "🏈 NFL")


@ic.command(name="mma", description="Upcoming MMA events + latest headlines")
async def mma(interaction: discord.Interaction):
    await _send_sport(interaction, "mma", "🥊 MMA")


@ic.command(name="ufc", description="Upcoming UFC (MMA) events + latest headlines")
async def ufc(interaction: discord.Interaction):
    await _send_sport(interaction, "ufc", "🥋 UFC")


@ic.command(name="boxing", description="Upcoming boxing bouts + latest headlines")
async def boxing(interaction: discord.Interaction):
    await _send_sport(interaction, "boxing", "🥊 Boxing")
