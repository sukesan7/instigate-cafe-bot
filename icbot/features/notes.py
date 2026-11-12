from __future__ import annotations

import asyncio
import re
import time

import aiohttp
import discord
from discord import app_commands

from icbot.app import ic
from icbot.embeds import system as system_embed

# Global cache: (timestamp: float, links: dict[str, list[tuple[str, str]]])
# Format of `links` dictionary:
# Key = site_title (e.g. "Jasper Cruz Notes")
# Value = list of tuples (course_code_upper, url)
_NOTES_CACHE: tuple[float, dict[str, list[tuple[str, str]]]] = (0.0, {})
_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours TTL


async def fetch_notes_from_site(
    session: aiohttp.ClientSession, title: str, base_url: str
) -> list[tuple[str, str]]:
    """
    Fetches a standard GitHub Pages SPA (like Jasper's) and extracts notion links.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        }
        # 1. Fetch the root index.html
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(base_url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"[NOTES] Failed to fetch {base_url} (HTTP {resp.status})")
                return []
            html = await resp.text()

        raw_links = set(re.findall(r'https://[^\.]+\.notion\.site/[^\s"\'\\>}]+', html))

        # 2. Find the main index-*.js bundle if available
        # The regex accommodates different absolute/relative paths based on the base_url
        js_match = re.search(r'src="([^"]+/assets/index-[a-f0-9]+\.js)"', html)
        if js_match:
            js_path = js_match.group(1)
            if js_path.startswith("/"):
                js_url = f"{base_url.split('/')[0]}//{base_url.split('/')[2]}{js_path}"
            else:
                js_url = f"{base_url.rstrip('/')}/{js_path}"

            async with session.get(js_url, headers=headers, timeout=timeout) as js_resp:
                if js_resp.status == 200:
                    js_text = await js_resp.text()
                    js_links = re.findall(r'https://[^\.]+\.notion\.site/[^\s"\'\\>}]+', js_text)
                    raw_links.update(js_links)

        # 4. Extract notion links that contain course codes.
        results = []
        for rl in raw_links:
            # Look for 3-4 letters optionally followed by a dash or space, then 3-4 numbers.
            code_match = re.search(r"([A-Z]{3,4}[-\s]?\d{3,4})", rl, re.IGNORECASE)

            if code_match:
                course_code = code_match.group(1).upper().replace("-", "").replace(" ", "")
                results.append((course_code, rl))

        print(f"[NOTES] Successfully parsed {len(results)} links from {title}")
        return results
    except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError, ValueError) as e:
        print(f"[NOTES] Error fetching from {title} ({base_url}): {e}")
        return []


async def get_all_notes(settings) -> dict[str, list[tuple[str, str]]]:
    """Retrieve all parsed notes, utilizing the in-memory cache if fresh."""
    global _NOTES_CACHE
    now = time.time()

    # Return cache if valid
    cache_ts, cache_data = _NOTES_CACHE
    if cache_data and (now - cache_ts) < _CACHE_TTL_SECONDS:
        return cache_data

    # We need to rebuild the cache
    sites_raw = getattr(settings, "notes_sites", None)
    if not sites_raw:
        print("[NOTES] No NOTES_SITES configured in env.")
        return {}

    # Parse site configs: "Title|URL"
    sites: list[tuple[str, str]] = []
    for s in sites_raw:
        parts = s.split("|", 1)
        if len(parts) == 2:
            title = parts[0].strip()
            # Clean up the URL: User might wrap it in extra quotes inside the .env
            url = parts[1].strip().strip('"').strip("'")
            sites.append((title, url))

    if not sites:
        return {}

    new_cache: dict[str, list[tuple[str, str]]] = {}

    # Fetch concurrently
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_notes_from_site(session, title, url) for title, url in sites]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (title, _url), res in zip(sites, results, strict=True):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                print(f"[NOTES] Task error for {title}: {res}")
            elif res:
                new_cache[title] = res

    # Update cache
    _NOTES_CACHE = (now, new_cache)
    return new_cache


@ic.command(name="notes", description="Search through available course notes repositories.")
@app_commands.describe(course="The course code (e.g. ELE888, ele 888)")
async def notes_cmd(interaction: discord.Interaction, course: str):
    await interaction.response.defer(thinking=True)

    # Normalize user input (e.g. "ele 888" -> "ELE888")
    normalized_course = "".join(course.split()).upper()

    if len(normalized_course) < 3:
        await interaction.followup.send("❌ Please provide a more specific course code.", ephemeral=True)
        return

    settings = getattr(interaction.client, "settings", None)
    if settings is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings).", ephemeral=True)
        return

    all_notes = await get_all_notes(settings)

    if not all_notes:
        await interaction.followup.send(
            "❌ Internal error linking to notes sites. Check configuration.", ephemeral=True
        )
        return

    # Look for matches across all sites
    fields = []
    matches = 0

    for title, links in all_notes.items():
        # Find exact matches first, fallback to partial matches if course search is slightly off
        site_matches = [url for code, url in links if normalized_course == code]
        if not site_matches:
            # Fallback to loose matching if exact fails (e.g., if user types ELE8)
            site_matches = [url for code, url in links if normalized_course in code]

        if site_matches:
            # Create a markdown list of links if there's multiple (usually there's only 1)
            links_str = "\n".join(f"• [Link to Notion]({url})" for url in set(site_matches))
            fields.append((title, links_str, False))
            matches += len(site_matches)

    if not fields:
        await interaction.followup.send(f"❌ No notes found for `{course}` across configured repositories.")
        return

    embed = system_embed(
        settings,
        title=f"📚 Course Notes — {course.upper()}",
        summary=f"Found **{matches}** notes repository containing this course.",
        fields=fields,
        footer_hint="Engineering Notes",
    )

    await interaction.followup.send(embed=embed)
