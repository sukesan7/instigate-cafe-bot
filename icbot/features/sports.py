from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiohttp
import discord

from icbot.app import sports
from icbot.config import Settings
from icbot.container import RssCache, get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import ProviderError, format_exception_safe, safe_error_summary
from icbot.services.sports_client import OddsAPIClient, OddsAPIError

log = logging.getLogger(__name__)

# Odds API sport keys
SPORT_KEYS = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "mma": "mma_mixed_martial_arts",
    "ufc": "mma_mixed_martial_arts",
    "boxing": "boxing_boxing",
}

# Default RSS feeds (override via SPORTS_RSS_<SPORT> env vars or guild overrides)
DEFAULT_RSS = {
    "nba": ["https://www.espn.com/espn/rss/nba/news"],
    "nfl": ["https://www.espn.com/espn/rss/nfl/news"],
    "mma": ["https://www.espn.com/espn/rss/mma/news"],
    "ufc": ["https://www.espn.com/espn/rss/mma/news"],
    "boxing": ["https://www.espn.com/espn/rss/boxing/news"],
}

# Module fallback cache when _get_news is called directly without an RssCache instance
_RSS_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_RSS_TTL_SECONDS = 600


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _get_settings(interaction: discord.Interaction):
    return getattr(interaction.client, "settings", None)


def _get_odds_client(interaction: discord.Interaction, settings: Settings | None = None) -> OddsAPIClient:
    return get_container(interaction.client).build_odds_client(settings)


def _rss_urls_for(sport: str, settings: Settings | None = None) -> list[str]:
    sport_key = sport.lower().strip()
    if settings is not None:
        getter = getattr(settings, "sports_rss_urls", None)
        if callable(getter):
            configured = getter(sport_key)
            if configured:
                return list(configured)
    return list(DEFAULT_RSS.get(sport_key, []))


@asynccontextmanager
async def _scoped_rss_session(
    session: aiohttp.ClientSession | None,
    *,
    timeout: aiohttp.ClientTimeout,
    headers: dict[str, str],
):
    if session is not None and not session.closed:
        yield session
        return
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as temp_session:
        yield temp_session


async def _fetch_rss_items(
    url: str,
    *,
    limit: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> list[tuple[str, str]]:
    """
    Return list[(title, link)] from RSS/Atom feed.
    Robust enough for simple headline pulls.
    """
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "User-Agent": "InstigateCafeBot/1.0 (+discord bot; personal project)",
        "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
    }

    async with (
        _scoped_rss_session(session, timeout=timeout, headers=headers) as http_session,
        http_session.get(url, headers=headers, timeout=timeout) as resp,
    ):
        text = await resp.text()
        if resp.status >= 400:
            return []

    # Parse XML (RSS or Atom)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    items: list[tuple[str, str]] = []

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


async def _get_news(
    sport: str,
    *,
    limit: int = 5,
    settings: Settings | None = None,
    cache: RssCache | None = None,
    session: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] | None = None,
) -> list[tuple[str, str]]:
    urls = _rss_urls_for(sport, settings=settings)
    if not urls:
        return []

    now = clock() if clock is not None else datetime.now(UTC).timestamp()
    cache_key = f"{sport}:{','.join(urls)}:{limit}"
    if cache is not None:
        cached = cache.get(cache_key, now=now, ttl_seconds=_RSS_TTL_SECONDS)
        if cached is not None:
            return cached
    else:
        cached_tuple = _RSS_CACHE.get(cache_key)
        if cached_tuple and (now - cached_tuple[0]) <= _RSS_TTL_SECONDS:
            return cached_tuple[1]

    results: list[tuple[str, str]] = []
    seen = set()
    for url in urls:
        items = await _fetch_rss_items(url, limit=limit, session=session)
        for title, link in items:
            if link in seen:
                continue
            seen.add(link)
            results.append((title, link))
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    if cache is not None:
        cache.set(cache_key, now=now, items=results)
    else:
        _RSS_CACHE[cache_key] = (now, results)
    return results


def _format_upcoming(events) -> str:
    if not events:
        return "No upcoming events found."
    lines = []
    for e in events:
        ts = _unix(e.commence_time)
        lines.append(f"• **{e.away_team} @ {e.home_team}** — <t:{ts}:f> (<t:{ts}:R>)")
    return "\n".join(lines)


def _format_news(items: list[tuple[str, str]]) -> str:
    if not items:
        return "News unavailable (no feed configured or fetch failed)."
    lines = []
    for title, link in items:
        # Discord supports raw links well; markdown can be inconsistent across clients.
        lines.append(f"• {_clip(title, 160)}\n<{link}>")
    return "\n".join(lines)


async def _send_sport(interaction: discord.Interaction, sport: str, title: str):
    if _get_settings(interaction) is None:
        await interaction.response.send_message("Bot settings not initialized.", ephemeral=True)
        return

    # Acknowledge within Discord's 3-second deadline before outbound Odds API & RSS HTTP calls
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    secrets = settings.secret_values()
    sport_key = SPORT_KEYS[sport]
    odds_degraded = False

    if (getattr(settings, "odds_api_key", "") or "").strip():
        client = container.build_odds_client(settings)
        try:
            events = await client.get_upcoming(sport_key, limit=5, days_from=7)
            upcoming_text = _format_upcoming(events)
        except (ProviderError, OddsAPIError, TimeoutError, aiohttp.ClientError, ValueError) as e:
            odds_degraded = True
            upcoming_text = (
                f"Upcoming games temporarily unavailable: {_clip(safe_error_summary(e, secrets), 900)}"
            )
    else:
        upcoming_text = "Upcoming games not configured (ODDS_API_KEY is not configured)."

    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    # News is optional: if RSS fails, we still show games or status cleanly
    news_items: list[tuple[str, str]] = []
    news_degraded = False
    try:
        news_items = await _get_news(
            sport,
            limit=5,
            settings=settings,
            cache=container.rss_cache,
            session=open_session,
            clock=container.clock,
        )
    except (aiohttp.ClientError, TimeoutError, ET.ParseError, ValueError) as exc:
        news_degraded = True
        log.debug("Sports RSS fetch failed for %s: %s", sport, format_exception_safe(exc, secrets))
        news_items = []

    emb = system_embed(
        settings,
        title=title,
        summary="Upcoming games + latest headlines.",
        fields=[
            ("Upcoming (next 5)", upcoming_text),
            ("News (latest 5)", _format_news(news_items)),
        ],
        footer_hint=f"Sports • {sport.upper()}",
        status="warning" if (odds_degraded or news_degraded) else "normal",
    )
    await interaction.followup.send(embed=emb)


@sports.command(name="nba", description="NBA updates.")
async def nba(interaction: discord.Interaction):
    await _send_sport(interaction, "nba", "🏀 NBA")


@sports.command(name="nfl", description="NFL updates.")
async def nfl(interaction: discord.Interaction):
    await _send_sport(interaction, "nfl", "🏈 NFL")


@sports.command(name="mma", description="MMA updates.")
async def mma(interaction: discord.Interaction):
    await _send_sport(interaction, "mma", "🥊 MMA")


@sports.command(name="ufc", description="UFC updates.")
async def ufc(interaction: discord.Interaction):
    await _send_sport(interaction, "ufc", "🥋 UFC")


@sports.command(name="boxing", description="Boxing updates.")
async def boxing(interaction: discord.Interaction):
    await _send_sport(interaction, "boxing", "🥊 Boxing")
