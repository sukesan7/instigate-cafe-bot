from __future__ import annotations

import time
from typing import Optional

import aiohttp
import discord

from icbot.db import Database
from icbot.embeds import job_alert as job_alert_embed
from icbot.services.jobs_workday_client import WorkdaySite, fetch_workday_jobs, parse_workday_sites

from icbot.tasks.job_alerts import (
    _classify,
    _has_seen,
    _mark_seen,
    _pick_bootstrap_sample_workday,
    _source_has_any,
    _workday_passes_filters,
)

def _jobs_channel_id(settings) -> int:
    """
    Back-compat: accept either JOB_ALERTS_CHANNEL_ID (preferred) or JOBS_CHANNEL_ID.
    """
    for attr in ("job_alerts_channel_id", "jobs_channel_id", "jobs_channel", "jobs_channelid"):
        try:
            v = int(getattr(settings, attr, 0) or 0)
            if v:
                return v
        except Exception:
            continue
    return 0

_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


async def _get_cached_api_url(db: Database, careers_url: str) -> Optional[str]:
    row = await db.fetchone(
        "SELECT api_url, last_updated_unix FROM workday_cxs_cache WHERE careers_url=?",
        (careers_url,),
    )
    if not row:
        return None

    api_url, last_updated = row
    try:
        if int(last_updated) and (int(time.time()) - int(last_updated) < _CACHE_TTL_SECONDS):
            return str(api_url)
    except Exception:
        return None
    return None


async def _set_cached_api_url(
    db: Database,
    careers_url: str,
    api_url: str,
    tenant: Optional[str],
    site: Optional[str],
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO workday_cxs_cache (careers_url, api_url, tenant, site, last_updated_unix)
        VALUES (?, ?, ?, ?, ?)
        """,
        (careers_url, api_url, tenant, site, int(time.time())),
    )


async def poll_and_post_workday_jobs(bot: discord.Client) -> None:
    """Poll Workday sites and post new matching jobs into the jobs channel.

    - Uses the SAME `job_seen` table as Greenhouse/Lever so dedupe is unified.
    - Caches the discovered Workday CXS API URL per careers URL in `workday_cxs_cache`.
    - Workday filtering is stricter than GH/Lever bootstrap: it never ignores filters.
    """
    settings = getattr(bot, "settings", None)
    db: Database = getattr(bot, "db", None)  # type: ignore[assignment]
    if settings is None or db is None:
        return

    channel_id = _jobs_channel_id(settings)
    if not channel_id:
        return

    raw_sites = list(getattr(settings, "workday_sites", None) or [])
    raw_sites = [
        s for s in raw_sites if isinstance(s, str) and s.strip() and s.strip() not in ("\\", "\\\\")
    ]
    if not raw_sites:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)  # type: ignore[attr-defined]
        except Exception:
            return

    # Optional filters (shared env vars)
    loc_allow = list(getattr(settings, "jobs_location_allowlist", None) or []) or None
    kw_allow = list(getattr(settings, "jobs_keywords_allowlist", None) or []) or None
    kw_deny = list(getattr(settings, "jobs_keywords_denylist", None) or []) or None
    bootstrap_post = bool(getattr(settings, "jobs_bootstrap_post", False))
    require_early_career = bool(getattr(settings, "jobs_require_early_career", False))

    workday_sites: list[WorkdaySite] = parse_workday_sites(raw_sites)
    if not workday_sites:
        return

    timeout_seconds = int(getattr(settings, "workday_timeout_seconds", 12) or 12)

    posted = 0
    bad_sources: set[str] = getattr(bot, "_workday_bad_sources", set())
    setattr(bot, "_workday_bad_sources", bad_sources)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        headers={"User-Agent": "InstigateCafeBot/1.0"},
    ) as session:
        for site in workday_sites:
            src = f"workday:{site.label}"
            if src in bad_sources:
                continue

            cached_api_url = await _get_cached_api_url(db, site.careers_url)
            try:
                jobs, cxs_used = await fetch_workday_jobs(
                    session,
                    site,
                    api_url_override=cached_api_url,
                    timeout_seconds=timeout_seconds,
                    max_results=240,
                )
            except Exception as e:
                bad_sources.add(src)
                print(f"[JOBS] workday fetch failed ({site.label}): {e!r}")
                continue

            # Refresh cache if the discovered/used URL works
            try:
                if cxs_used and getattr(cxs_used, "api_url", None):
                    await _set_cached_api_url(
                        db,
                        site.careers_url,
                        str(cxs_used.api_url),
                        getattr(cxs_used, "tenant", None),
                        getattr(cxs_used, "site", None),
                    )
            except Exception:
                pass

            # Baseline behavior: avoid spamming old jobs on first-ever run per source
            if not await _source_has_any(db, src):
                if not bootstrap_post:
                    await _mark_seen(db, jobs)
                    print(f"[JOBS] baseline checkpointed {len(jobs)} jobs for {src} (no bootstrap post).")
                    continue

                # Workday bootstrap is STRICT: never ignores filters.
                sample = _pick_bootstrap_sample_workday(
                    jobs,
                    loc_allow=loc_allow,
                    kw_allow=kw_allow,
                    kw_deny=kw_deny,
                    require_early_career=require_early_career,
                    max_items=5,
                )

                for j, cats in sample:
                    emb = job_alert_embed(
                        settings,
                        title=j.title,
                        url=j.url,
                        company=j.company,
                        location=j.location,
                        source=j.source,
                        categories=cats,
                    )
                    await channel.send(embed=emb)
                    posted += 1

                await _mark_seen(db, jobs)

                if sample:
                    print(f"[JOBS] bootstrap posted {len(sample)} jobs for {src}; checkpointed {len(jobs)} jobs.")
                else:
                    print(f"[JOBS] bootstrap posted 0 (no matching SWE/CS jobs) for {src}; checkpointed {len(jobs)} jobs.")
                continue

            # Normal cycle
            jobs_sorted = sorted(jobs, key=lambda x: int(getattr(x, "posted_unix", 0) or 0), reverse=True)
            posts_left = 12

            for j in jobs_sorted:
                if posts_left <= 0:
                    break

                if await _has_seen(db, j.source, j.external_id):
                    continue

                # Workday eligibility: SWE/CS signal + deny seniority + shared allow/deny vars
                if not _workday_passes_filters(
                    text_blob=j.text_blob,
                    location=j.location,
                    loc_allow=loc_allow,
                    kw_allow=kw_allow,
                    kw_deny=kw_deny,
                    require_early_career=require_early_career,
                ):
                    await _mark_seen(db, [j])
                    continue

                cats = _classify(j.text_blob) or ["General"]

                emb = job_alert_embed(
                    settings,
                    title=j.title,
                    url=j.url,
                    company=j.company,
                    location=j.location,
                    source=j.source,
                    categories=cats,
                )
                await channel.send(embed=emb)
                posted += 1
                posts_left -= 1
                await _mark_seen(db, [j])

    if posted:
        print(f"[JOBS] workday cycle complete: posted={posted}")