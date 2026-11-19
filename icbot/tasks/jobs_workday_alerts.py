from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import aiohttp
import aiosqlite
import discord

from icbot.container import get_container
from icbot.db import Database
from icbot.embeds import job_alert as job_alert_embed
from icbot.errors import format_exception_safe
from icbot.services.jobs_workday_client import WorkdaySite, fetch_workday_jobs, parse_workday_sites
from icbot.tasks.job_alerts import (
    _classify,
    _has_seen,
    _looks_like_not_found,
    _mark_seen,
    _pick_bootstrap_sample_workday,
    _resolve_sendable_channel,
    _source_has_any,
    _workday_passes_filters,
)

log = logging.getLogger(__name__)


def _jobs_channel_id(settings) -> int:
    """
    Back-compat: accept either JOB_ALERTS_CHANNEL_ID (preferred) or JOBS_CHANNEL_ID.
    """
    for attr in ("job_alerts_channel_id", "jobs_channel_id", "jobs_channel", "jobs_channelid"):
        try:
            v = int(getattr(settings, attr, 0) or 0)
            if v:
                return v
        except (TypeError, ValueError) as exc:
            log.debug("Skipping invalid jobs channel attr %s: %s", attr, exc)
            continue
    return 0


_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


async def _get_cached_api_url(
    db: Database,
    careers_url: str,
    *,
    clock: Callable[[], float] | None = None,
) -> str | None:
    row = await db.fetchone(
        "SELECT api_url, last_updated_unix FROM workday_cxs_cache WHERE careers_url=?",
        (careers_url,),
    )
    if not row:
        return None

    api_url, last_updated = row
    now = int(clock()) if clock is not None else int(time.time())
    try:
        if int(last_updated) and (now - int(last_updated) < _CACHE_TTL_SECONDS):
            return str(api_url)
    except (TypeError, ValueError):
        return None
    return None


async def _set_cached_api_url(
    db: Database,
    careers_url: str,
    api_url: str,
    tenant: str | None,
    site: str | None,
    *,
    clock: Callable[[], float] | None = None,
) -> None:
    now = int(clock()) if clock is not None else int(time.time())
    await db.execute(
        """
        INSERT OR REPLACE INTO workday_cxs_cache (careers_url, api_url, tenant, site, last_updated_unix)
        VALUES (?, ?, ?, ?, ?)
        """,
        (careers_url, api_url, tenant, site, now),
    )


@asynccontextmanager
async def _scoped_workday_session(
    shared_session: aiohttp.ClientSession | None,
    *,
    timeout_seconds: int,
):
    if shared_session is not None and not shared_session.closed:
        yield shared_session
        return
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        headers={"User-Agent": "InstigateCafeBot/1.0"},
    ) as created:
        yield created


async def poll_and_post_workday_jobs(bot: discord.Client, *, raise_on_error: bool = False) -> None:
    """Poll Workday sites and post new matching jobs into the jobs channel.

    - Uses the SAME `job_seen` table as Greenhouse/Lever so dedupe is unified.
    - Caches the discovered Workday CXS API URL per careers URL in `workday_cxs_cache`.
    - Workday filtering is stricter than GH/Lever bootstrap: it never ignores filters.
    """
    settings = getattr(bot, "settings", None)
    db: Database = getattr(bot, "db", None)  # type: ignore[assignment]
    if settings is None or db is None:
        return
    container = get_container(bot)
    secrets = settings.secret_values() if hasattr(settings, "secret_values") else ()

    channel_id = _jobs_channel_id(settings)
    if not channel_id:
        return

    raw_sites = list(getattr(settings, "workday_sites", None) or [])
    raw_sites = [s for s in raw_sites if isinstance(s, str) and s.strip() and s.strip() not in ("\\", "\\\\")]
    if not raw_sites:
        return

    channel = await _resolve_sendable_channel(bot, channel_id)
    if channel is None:
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
    succeeded_sites = 0
    last_transient_err: BaseException | None = None
    bad_sources = container.workday_bad_sources
    existing_bad = getattr(bot, "_workday_bad_sources", None)
    if isinstance(existing_bad, set) and existing_bad is not bad_sources:
        bad_sources.update(existing_bad)
    cast(Any, bot)._workday_bad_sources = bad_sources

    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    async with _scoped_workday_session(open_session, timeout_seconds=timeout_seconds) as session:
        for site in workday_sites:
            src = f"workday:{site.label}"
            if src in bad_sources:
                continue

            cached_api_url = await _get_cached_api_url(db, site.careers_url, clock=container.clock)
            try:
                jobs, cxs_used = await fetch_workday_jobs(
                    session,
                    site,
                    api_url_override=cached_api_url,
                    timeout_seconds=timeout_seconds,
                    max_results=240,
                )
            except (
                aiohttp.ClientError,
                TimeoutError,
                RuntimeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
            ) as e:
                # If cached API URL failed with 404/401/403, invalidate cache and retry once
                fetch_err: Exception | None = e
                if cached_api_url and (
                    _looks_like_not_found(e) or getattr(e, "status", None) in (401, 403, 404)
                ):
                    log.info(
                        "Cached Workday CXS URL failed for %s; invalidating cache and retrying", site.label
                    )
                    with contextlib.suppress(Exception):
                        await db.execute(
                            "DELETE FROM workday_cxs_cache WHERE careers_url=?", (site.careers_url,)
                        )
                    try:
                        jobs, cxs_used = await fetch_workday_jobs(
                            session,
                            site,
                            api_url_override=None,
                            timeout_seconds=timeout_seconds,
                            max_results=240,
                        )
                        fetch_err = None
                    except (
                        aiohttp.ClientError,
                        TimeoutError,
                        RuntimeError,
                        json.JSONDecodeError,
                        ValueError,
                        TypeError,
                    ) as retry_err:
                        fetch_err = retry_err

                if fetch_err is not None:
                    # Only blacklist permanent misconfiguration / 404 sources, never transient timeouts or 429/5xx.
                    is_permanent = (
                        _looks_like_not_found(fetch_err)
                        or "heuristic failed" in str(fetch_err).lower()
                        or (
                            isinstance(fetch_err, ValueError)
                            and not isinstance(fetch_err, json.JSONDecodeError)
                        )
                    )
                    if is_permanent:
                        bad_sources.add(src)
                    else:
                        last_transient_err = fetch_err
                    print(
                        f"[JOBS] workday fetch failed ({site.label}): {format_exception_safe(fetch_err, secrets)}"
                    )
                    continue

            succeeded_sites += 1

            # Refresh cache if the discovered/used URL works
            try:
                if cxs_used and getattr(cxs_used, "api_url", None):
                    await _set_cached_api_url(
                        db,
                        site.careers_url,
                        str(cxs_used.api_url),
                        getattr(cxs_used, "tenant", None),
                        getattr(cxs_used, "site", None),
                        clock=container.clock,
                    )
            except (aiosqlite.Error, sqlite3.Error, TypeError, ValueError) as exc:
                log.debug("Workday CXS cache refresh skipped for %s: %s", site.label, exc)

            # Baseline behavior: avoid spamming old jobs on first-ever run per source
            if not await _source_has_any(db, src):
                if not bootstrap_post:
                    await _mark_seen(db, jobs, clock=container.clock, container=container)
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
                    guild_id = int(getattr(settings, "guild_id", 0) or 0)
                    await container.jobs_pipeline.record_delivery(db, guild_id, channel_id, j)

                await _mark_seen(db, jobs, clock=container.clock, container=container)

                if sample:
                    print(
                        f"[JOBS] bootstrap posted {len(sample)} jobs for {src}; checkpointed {len(jobs)} jobs."
                    )
                else:
                    print(
                        f"[JOBS] bootstrap posted 0 (no matching SWE/CS jobs) for {src}; checkpointed {len(jobs)} jobs."
                    )
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
                    await _mark_seen(db, [j], clock=container.clock, container=container)
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
                await _mark_seen(db, [j], clock=container.clock, container=container)
                guild_id = int(getattr(settings, "guild_id", 0) or 0)
                await container.jobs_pipeline.record_delivery(db, guild_id, channel_id, j)

    if raise_on_error and succeeded_sites == 0 and last_transient_err is not None:
        raise last_transient_err

    if posted:
        print(f"[JOBS] workday cycle complete: posted={posted}")
