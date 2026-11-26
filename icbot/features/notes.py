from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import discord
from discord import app_commands

from icbot.academics.cache import format_freshness_line
from icbot.academics.models import ResourceLink, campus_from_settings, normalize_course_key
from icbot.academics.providers import fetch_notes_spa_links
from icbot.academics.url_safety import create_ssrf_safe_connector
from icbot.app import ic
from icbot.config import Settings
from icbot.container import NotesCache, extract_guild_id, get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.registry import guard_feature
from icbot.ui import DEFAULT_PAGINATOR_TIMEOUT_SECONDS, BasePaginationView, compute_page_slice

# Module-level fallback cache for direct external callers that omit a NotesCache instance.
_NOTES_CACHE: tuple[float, dict[str, list[tuple[str, str]]]] = (0.0, {})
_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours TTL
_FIELDS_PER_PAGE = 8


@asynccontextmanager
async def _scoped_http_session(session: aiohttp.ClientSession | None):
    if session is not None and not session.closed:
        yield session
        return
    async with aiohttp.ClientSession(
        connector=create_ssrf_safe_connector(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as temp_session:
        yield temp_session


async def fetch_notes_from_site(
    session: aiohttp.ClientSession,
    title: str,
    base_url: str,
    *,
    allowed_hosts: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Fetch a Notion-index SPA after strict HTTPS + SSRF URL validation."""
    links, _err = await fetch_notes_spa_links(session, title, base_url, allowed_hosts=allowed_hosts)
    return links


async def get_all_notes(
    settings: Any,
    *,
    cache: NotesCache | None = None,
    session: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Retrieve all parsed SPA notes, utilizing an injected or fallback in-memory cache if fresh."""
    global _NOTES_CACHE
    now = clock() if clock is not None else time.time()

    sites_raw = getattr(settings, "notes_sites", None)
    if not sites_raw:
        return {}

    sites_key = tuple(str(s).strip() for s in sites_raw if str(s).strip())
    if cache is not None:
        cached = cache.get(sites_key, now=now, ttl_seconds=_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
    else:
        cache_ts, cache_data = _NOTES_CACHE
        if cache_data and (now - cache_ts) < _CACHE_TTL_SECONDS:
            return cache_data

    sites: list[tuple[str, str]] = []
    for s in sites_raw:
        parts = s.split("|", 1)
        if len(parts) == 2:
            title = parts[0].strip()
            url = parts[1].strip().strip('"').strip("'")
            if title and url:
                sites.append((title, url))

    if not sites:
        return {}

    allowed_hosts = getattr(settings, "academic_allowed_hosts", None)
    new_cache: dict[str, list[tuple[str, str]]] = {}

    async with _scoped_http_session(session) as http_session:
        tasks = [
            fetch_notes_from_site(http_session, title, url, allowed_hosts=allowed_hosts)
            for title, url in sites
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (title, _url), res in zip(sites, results, strict=True):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
            elif res:
                new_cache[title] = res

    if cache is not None:
        if new_cache:
            cache.set(sites_key, now=now, data=new_cache)
    else:
        _NOTES_CACHE = (now, new_cache)
    return new_cache


def _build_notes_fields(links: list[ResourceLink]) -> list[tuple[str, str, bool]]:
    """Group external resource links by source attribution into Discord-safe embed fields."""
    grouped: dict[str, list[ResourceLink]] = {}
    for item in links:
        group_name = item.source_attribution or item.title or "Course Resources"
        grouped.setdefault(group_name, []).append(item)

    fields: list[tuple[str, str, bool]] = []
    for group_name, group_links in grouped.items():
        seen_urls: set[str] = set()
        lines: list[str] = []
        for link in group_links:
            if link.url in seen_urls:
                continue
            seen_urls.add(link.url)
            if link.resource_id.startswith("notes_spa:"):
                lines.append(f"• [Link to Notion]({link.url})")
            else:
                type_tag = f" ({link.resource_type})" if link.resource_type else ""
                lines.append(f"• [{link.title}]({link.url}){type_tag}")
        if lines:
            value = "\n".join(lines[:15])
            if len(lines) > 15:
                value += f"\n… and {len(lines) - 15} more"
            fields.append((group_name[:256], value[:1024], False))
    return fields


def build_notes_page_embed(
    settings: Settings,
    *,
    course_display: str,
    campus_name: str,
    all_fields: list[tuple[str, str, bool]],
    total_matches: int,
    page: int,
    total_pages: int,
    degraded_providers: list[str] | None = None,
    freshness_lines: list[str] | None = None,
) -> discord.Embed:
    """Build a single paginated course-notes embed respecting Discord limits."""
    slice_info = compute_page_slice(all_fields, page, _FIELDS_PER_PAGE)
    page_fields = list(slice_info.items)
    eff_total_pages = max(1, total_pages or slice_info.total_pages)
    summary_lines = [
        f"Found **{total_matches}** resource link(s) for **{course_display}** ({campus_name}).",
        "*External links only — rights remain with original authors/institutions.*",
    ]
    if freshness_lines:
        summary_lines.extend(freshness_lines[:3])
    if degraded_providers:
        summary_lines.append(f"⚠️ Some optional sources were unavailable: {', '.join(degraded_providers)}")

    footer_hint = (
        f"Course Notes • Page {slice_info.page + 1}/{eff_total_pages}"
        if eff_total_pages > 1
        else "Course Notes"
    )
    return system_embed(
        settings,
        title=f"📚 Course Notes — {course_display}",
        summary="\n".join(summary_lines),
        fields=page_fields,
        footer_hint=footer_hint,
    )


class NotesPaginationView(BasePaginationView):
    """Pagination controls when a course search returns more resource groups than fit on one page.

    Deliberate shared-interaction exception: when `owner_id == 0` and `shared_guild_navigation=True`,
    any member of the owning `guild_id` may navigate pages because all course links are public.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        course_display: str,
        campus_name: str,
        all_fields: list[tuple[str, str, bool]],
        total_matches: int,
        degraded_providers: list[str] | None = None,
        freshness_lines: list[str] | None = None,
        guild_id: int = 0,
        owner_id: int = 0,
        shared_guild_navigation: bool = True,
        timeout: float = DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
    ) -> None:
        self.course_display = course_display
        self.campus_name = campus_name
        self.all_fields = all_fields
        self.total_matches = total_matches
        self.degraded_providers = degraded_providers or []
        self.freshness_lines = freshness_lines or []
        slice_info = compute_page_slice(all_fields, 0, _FIELDS_PER_PAGE)
        super().__init__(
            settings=settings,
            total_pages=slice_info.total_pages,
            current_page=0,
            owner_id=owner_id,
            guild_id=guild_id,
            shared_guild_navigation=shared_guild_navigation,
            feature_key="academics",
            timeout=timeout,
            expired_message="⏳ This course notes view has expired. Run `/ic notes` again.",
            guild_mismatch_message="❌ Command context mismatch.",
            owner_mismatch_message="❌ Only the user who ran `/ic notes` can navigate these pages.",
        )

    def build_current_embed(self) -> discord.Embed:
        return build_notes_page_embed(
            self.settings,
            course_display=self.course_display,
            campus_name=self.campus_name,
            all_fields=self.all_fields,
            total_matches=self.total_matches,
            page=self.current_page,
            total_pages=self.total_pages,
            degraded_providers=self.degraded_providers,
            freshness_lines=self.freshness_lines,
        )


@ic.command(name="notes", description="Course notes.")
@app_commands.describe(course="Course code")
async def notes_cmd(interaction: discord.Interaction, course: str):
    if not await guard_feature(interaction, "academics"):
        return
    await interaction.response.defer(thinking=True)

    normalized_course = normalize_course_key(course)
    if len(normalized_course) < 3:
        await interaction.followup.send(
            "❌ Please provide a valid course code with at least 3 letters/digits (e.g. `CPS109`, `MTH140`, or `CS135`).",
            ephemeral=True,
        )
        return

    if getattr(interaction.client, "settings", None) is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings).", ephemeral=True)
        return

    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    campus = campus_from_settings(settings)
    guild_id = extract_guild_id(interaction) or int(getattr(settings, "guild_id", 0) or 0)
    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    provider_results = await container.academic_registry.search_notes(
        course,
        campus=campus,
        guild_id=guild_id,
        settings=settings,
        session=open_session,
        clock=container.clock,
    )

    collected_links: list[ResourceLink] = []
    degraded_providers: list[str] = []
    freshness_lines: list[str] = []
    any_configured = False

    for res in provider_results:
        if res.status != "disabled":
            any_configured = True
        if res.ok and res.data:
            collected_links.extend(res.data)
            freshness_lines.append(format_freshness_line(res.provenance, clock=container.clock))
        elif res.status in ("error", "unsafe_url"):
            degraded_providers.append(res.provenance.provider_name)

    if not collected_links:
        if not any_configured:
            await interaction.followup.send(
                "❌ No notes sources or manual guild catalog entries are configured yet.",
                ephemeral=True,
            )
            return
        if degraded_providers:
            await interaction.followup.send(
                f"❌ No notes found for `{course}` (optional source error: {', '.join(degraded_providers)}).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"❌ No notes found for `{course}` across configured repositories or this server's catalog."
        )
        return

    all_fields = _build_notes_fields(collected_links)
    total_matches = len({link.url for link in collected_links})
    course_display = course.strip().upper()
    total_pages = max(1, (len(all_fields) + _FIELDS_PER_PAGE - 1) // _FIELDS_PER_PAGE)

    if total_pages > 1:
        view = NotesPaginationView(
            settings=settings,
            course_display=course_display,
            campus_name=campus.name,
            all_fields=all_fields,
            total_matches=total_matches,
            degraded_providers=degraded_providers,
            freshness_lines=freshness_lines,
            guild_id=guild_id,
        )
        sent_msg = await interaction.followup.send(embed=view.current_embed(), view=view, wait=True)
        view.message = sent_msg
        return

    embed = build_notes_page_embed(
        settings,
        course_display=course_display,
        campus_name=campus.name,
        all_fields=all_fields,
        total_matches=total_matches,
        page=0,
        total_pages=1,
        degraded_providers=degraded_providers,
        freshness_lines=freshness_lines,
    )
    await interaction.followup.send(embed=embed)
