from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

import aiohttp
import aiosqlite
import discord

from icbot.config import Settings
from icbot.embeds import job_alert as job_alert_embed
from icbot.errors import ProviderError
from icbot.services.jobs.adapters import (
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
)
from icbot.services.jobs.metrics import JobIngestionMetrics
from icbot.services.jobs.models import (
    JobFilterCriteria,
    JobPosting,
    MatchDecision,
    SourceHealth,
)
from icbot.services.jobs.rules import DeterministicRuleEngine, location_matches

log = logging.getLogger(__name__)


class _Sendable(Protocol):
    async def send(self, *args: Any, **kwargs: Any) -> Any: ...


_DEFAULT_RULE_ENGINE = DeterministicRuleEngine()


def location_passes_smart(location: str, text_blob: str, allowlist: Sequence[str] | None) -> bool:
    """Check if location matches criteria, falling back to remote keyword in text."""
    return location_matches(location, text_blob, allowlist)


def matches_filter_criteria(job: JobPosting, criteria: JobFilterCriteria) -> bool:
    """Evaluate whether a JobPosting matches the filtering rules via DeterministicRuleEngine."""
    return _DEFAULT_RULE_ENGINE.evaluate(job, criteria).matched


class JobsIngestionPipeline:
    """
    Unified ingestion pipeline across Greenhouse, Lever, and Workday ATS providers.
    Coordinates fetching, concurrency, cooldowns, deduplication, matching, and publishing.
    """

    def __init__(
        self,
        *,
        concurrency_limit: int = 5,
        reannounce_on_update: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.concurrency_limit = max(1, int(concurrency_limit))
        self.reannounce_on_update = bool(reannounce_on_update)
        self._clock = clock if clock is not None else time.time
        self._semaphore = asyncio.Semaphore(self.concurrency_limit)
        self.source_health_map: dict[str, SourceHealth] = {}
        self.greenhouse_adapter = GreenhouseAdapter()
        self.lever_adapter = LeverAdapter()
        self.workday_adapter = WorkdayAdapter()
        self.rule_engine = DeterministicRuleEngine()
        self.metrics = JobIngestionMetrics()

    def evaluate_posting(self, job: JobPosting, criteria: JobFilterCriteria) -> MatchDecision:
        """Evaluate a JobPosting against JobFilterCriteria and return an explainable MatchDecision."""
        return self.rule_engine.evaluate(job, criteria)

    def get_or_create_health(self, source_key: str, provider: str, target: str) -> SourceHealth:
        if source_key not in self.source_health_map:
            self.source_health_map[source_key] = SourceHealth(
                source_key=source_key,
                provider=cast(Any, provider),
                target=target,
            )
        return self.source_health_map[source_key]

    async def has_seen_global(self, db: Any, source_key: str, external_id: str) -> bool:
        row = await db.fetchone(
            "SELECT 1 FROM job_seen WHERE source=? AND external_id=? LIMIT 1",
            (source_key, external_id),
        )
        return row is not None

    async def get_seen_job_meta(
        self, db: Any, source_key: str, external_id: str
    ) -> tuple[int, int | None] | None:
        """Returns (first_seen_unix, posted_unix) if seen in job_seen."""
        try:
            row = await db.fetchone(
                "SELECT first_seen_unix, posted_unix FROM job_seen WHERE source=? AND external_id=? LIMIT 1",
                (source_key, external_id),
            )
            if row:
                return int(row[0]), int(row[1]) if row[1] is not None else None
        except (aiosqlite.Error, sqlite3.Error, AttributeError, KeyError, ValueError):
            row = await db.fetchone(
                "SELECT first_seen_unix FROM job_seen WHERE source=? AND external_id=? LIMIT 1",
                (source_key, external_id),
            )
            if row:
                return int(row[0]), None
        return None

    async def is_already_delivered(
        self,
        db: Any,
        guild_id: int,
        channel_id: int,
        source_key: str,
        external_id: str,
        updated_unix: int | None,
    ) -> bool:
        """Check if posting has already been delivered to this guild channel."""
        dedupe_key = f"{channel_id}:{source_key}:{external_id}"
        try:
            row = await db.fetchone(
                "SELECT delivered_at FROM alert_delivery_dedupes "
                "WHERE guild_id=? AND alert_type='job_alert' AND dedupe_key=? LIMIT 1",
                (guild_id, dedupe_key),
            )
            if not row:
                return False

            if self.reannounce_on_update and updated_unix is not None:
                last_delivered = int(row[0])
                if updated_unix > last_delivered:
                    return False  # Needs reannouncement!
            return True
        except (aiosqlite.Error, sqlite3.Error, AttributeError, KeyError, ValueError):
            # Fallback to job_seen if alert_delivery_dedupes check fails
            return await self.has_seen_global(db, source_key, external_id)

    async def record_delivery(
        self,
        db: Any,
        guild_id: int,
        channel_id: int,
        job: Any,
    ) -> None:
        now = int(self._clock())
        src_key = getattr(job, "source_key", None) or getattr(job, "source", "")
        ext_id = getattr(job, "external_id", "")
        dedupe_key = f"{channel_id}:{src_key}:{ext_id}"
        try:
            await db.execute(
                """
                INSERT OR REPLACE INTO alert_delivery_dedupes(
                    guild_id, alert_type, dedupe_key, delivered_at
                ) VALUES (?, 'job_alert', ?, ?)
                """,
                (guild_id, dedupe_key, now),
            )
        except (aiosqlite.Error, sqlite3.Error, AttributeError, KeyError, ValueError) as exc:
            log.debug("alert_delivery_dedupes insert skipped: %s", exc)

    async def checkpoint_seen(self, db: Any, jobs: Sequence[JobPosting]) -> None:
        """Record jobs into job_seen table."""
        if not jobs:
            return
        now = int(self._clock())
        try:
            stmts_ext = [
                (
                    "INSERT OR IGNORE INTO job_seen("
                    "source, external_id, first_seen_unix, posted_unix, company, location, title, url, text_blob"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        j.source_key,
                        j.external_id,
                        now,
                        int(j.posted_unix or 0),
                        j.employer,
                        j.location,
                        j.title,
                        j.url,
                        j.text_blob,
                    ),
                )
                for j in jobs
            ]
            await db.execute_many(stmts_ext)
            return
        except (aiosqlite.Error, sqlite3.Error, AttributeError, KeyError, ValueError) as exc:
            log.debug("Extended columns insert skipped, falling back to basic: %s", exc)

        # Fallback to basic columns
        stmts_basic = [
            (
                "INSERT OR IGNORE INTO job_seen(source, external_id, first_seen_unix, title, url) VALUES(?,?,?,?,?)",
                (j.source_key, j.external_id, now, j.title, j.url),
            )
            for j in jobs
        ]
        await db.execute_many(stmts_basic)

    async def fetch_source_postings(
        self,
        session: aiohttp.ClientSession,
        provider: str,
        target: str,
        *,
        employer_label: str = "",
        api_url_override: str | None = None,
        timeout_seconds: int = 12,
        bad_sources: set[str] | None = None,
    ) -> tuple[list[JobPosting], str | None]:
        """
        Fetch postings for a single source with rate-limit and cooldown handling.
        Returns: (postings, discovered_api_url_for_workday)
        """
        if provider == "greenhouse":
            source_key = f"greenhouse:{target}"
        elif provider == "lever":
            source_key = target
        elif provider == "workday":
            source_key = f"workday:{employer_label or target}"
        else:
            source_key = f"{provider}:{target}"

        health = self.get_or_create_health(source_key, provider, target)
        now = self._clock()

        if bad_sources and source_key in bad_sources:
            return [], None

        if health.status == "blacklisted":
            if bad_sources is not None:
                bad_sources.add(source_key)
            return [], None

        if health.is_cooling_down(now):
            log.info("Source %s is cooling down until %s; skipping", source_key, health.cooldown_until)
            return [], None

        async with self._semaphore:
            start_t = self._clock()
            try:
                if provider == "greenhouse":
                    jobs = await self.greenhouse_adapter.fetch_postings(
                        session, target, timeout_seconds=timeout_seconds, clock=self._clock
                    )
                    health.mark_success(now)
                    self.metrics.record_source_success(source_key, self._clock() - start_t)
                    return jobs, None
                elif provider == "lever":
                    jobs = await self.lever_adapter.fetch_postings(
                        session, target, timeout_seconds=timeout_seconds, clock=self._clock
                    )
                    health.mark_success(now)
                    self.metrics.record_source_success(source_key, self._clock() - start_t)
                    return jobs, None
                elif provider == "workday":
                    jobs, api_url, _tenant, _site_slug = await self.workday_adapter.fetch_postings(
                        session,
                        target,
                        employer_label=employer_label,
                        api_url_override=api_url_override,
                        timeout_seconds=timeout_seconds,
                        clock=self._clock,
                    )
                    health.mark_success(now)
                    self.metrics.record_source_success(source_key, self._clock() - start_t)
                    return jobs, api_url
                else:
                    raise ValueError(f"Unknown ATS provider: {provider}")
            except ProviderError as pe:
                self.metrics.record_http_failure(source_key, str(pe))
                st = pe.status
                if st == 429:
                    cooldown = pe.retry_after if pe.retry_after is not None else 30.0
                    health.mark_cooldown(cooldown, status_code=429, error=str(pe), now=now)
                elif st in (404, 410) or (st and 400 <= st < 500):
                    health.mark_blacklisted(str(pe), status_code=st)
                    if bad_sources is not None:
                        bad_sources.add(source_key)
                else:
                    health.mark_cooldown(15.0, status_code=st, error=str(pe), now=now)
                raise
            except Exception as exc:
                self.metrics.record_http_failure(source_key, repr(exc))
                health.mark_cooldown(10.0, error=repr(exc), now=now)
                raise

    async def ingest_and_enqueue_postings(
        self,
        db: Any,
        jobs: Sequence[JobPosting],
        *,
        subscriptions: Sequence[dict[str, Any]] | None = None,
        bootstrap_mode: bool = False,
    ) -> list[JobPosting]:
        """
        Ingest newly discovered postings into job_seen and queue them into job_alert_outbox
        for each matching active subscription across all guilds.
        """
        if not jobs:
            return []

        # 1. Resolve active subscriptions
        active_subs = (
            list(subscriptions) if subscriptions is not None else await db.get_all_active_job_subscriptions()
        )

        # 2. Identify new postings vs seen
        new_jobs: list[JobPosting] = []
        for j in jobs:
            seen_meta = await self.get_seen_job_meta(db, j.source_key, j.external_id)
            if seen_meta is None or (
                self.reannounce_on_update
                and j.updated_unix is not None
                and j.updated_unix > (seen_meta[1] or seen_meta[0])
            ):
                new_jobs.append(j)

        # Checkpoint seen state
        await self.checkpoint_seen(db, jobs)

        if not new_jobs or not active_subs or bootstrap_mode:
            return new_jobs

        # 3. Match against each subscription and create outbox items
        now_ts = int(self._clock())
        outbox_batch: list[dict[str, Any]] = []

        for sub in active_subs:
            gid = int(sub.get("guild_id", 0))
            cid = int(sub["channel_id"])
            preset_name = str(sub.get("preset", "tech_swe"))
            criteria = JobFilterCriteria(preset=preset_name)

            for job in new_jobs:
                # Check channel delivery dedupe
                if await self.is_already_delivered(
                    db, gid, cid, job.source_key, job.external_id, job.updated_unix
                ):
                    self.metrics.record_suppressed_duplicates(1)
                    continue

                decision = self.rule_engine.evaluate(job, criteria)
                if not decision.matched:
                    continue

                self.metrics.record_matching_jobs(1)
                outbox_batch.append(
                    {
                        "guild_id": gid,
                        "channel_id": cid,
                        "source_key": job.source_key,
                        "external_id": job.external_id,
                        "job_title": job.title,
                        "job_company": job.employer,
                        "job_url": job.url,
                        "job_location": job.location,
                        "career_level": decision.career_level,
                        "discipline_tags": list(decision.disciplines),
                        "preset": preset_name,
                        "posted_unix": job.posted_unix,
                        "enqueued_at": now_ts,
                        "reannounce": bool(job.updated_unix and seen_meta),
                    }
                )

        if outbox_batch and hasattr(db, "enqueue_job_outbox"):
            await db.enqueue_job_outbox(outbox_batch)

        return new_jobs

    async def dispatch_outbox(
        self,
        bot: discord.Client,
        db: Any,
        *,
        max_posts_per_channel: int = 12,
        settings: Any | None = None,
    ) -> int:
        """
        Process pending items from job_alert_outbox and send embeds to subscribed channels.
        Handles missing channels, permissions, rate limits (Discord 429), and delivery recording.
        """
        if not hasattr(db, "fetch_pending_job_outbox"):
            return 0

        pending_items = await db.fetch_pending_job_outbox(limit=100)
        if not pending_items:
            return 0

        # Group by channel
        by_channel: dict[int, list[dict[str, Any]]] = {}
        for item in pending_items:
            by_channel.setdefault(item["channel_id"], []).append(item)

        delivered_total = 0
        now_ts = int(self._clock())

        for channel_id, items in by_channel.items():
            # Resolve channel
            channel: Any = bot.get_channel(channel_id)
            if channel is None and hasattr(bot, "fetch_channel"):
                try:
                    channel = await bot.fetch_channel(channel_id)
                except (discord.DiscordException, LookupError, ValueError) as exc:
                    log.warning("[JOBS] Channel %d lookup failed: %s", channel_id, exc)
                    channel = None

            if channel is None or not callable(getattr(channel, "send", None)):
                # Channel deleted or inaccessible
                for itm in items:
                    await db.mark_job_outbox_failed(
                        itm["id"], itm["attempts"] + 1, "ChannelNotFound", is_terminal=True
                    )
                self.metrics.record_discord_delivery_failure("channel_not_found", len(items))
                continue

            # Permission check if channel is in a guild
            guild = getattr(channel, "guild", None)
            me = getattr(guild, "me", None) if guild else None
            if me and hasattr(channel, "permissions_for"):
                perms = channel.permissions_for(me)
                if not (getattr(perms, "send_messages", True) and getattr(perms, "embed_links", True)):
                    for itm in items:
                        await db.mark_job_outbox_failed(
                            itm["id"], itm["attempts"] + 1, "MissingPermissions", is_terminal=True
                        )
                    self.metrics.record_discord_delivery_failure("missing_permissions", len(items))
                    continue

            # Send batch capped by volume limit
            posts_sent = 0
            for item in items:
                if posts_sent >= max_posts_per_channel:
                    break

                # Mark sending
                await db.mark_job_outbox_sending([item["id"]])

                eff_settings = cast(Settings, settings or getattr(bot, "settings", None) or Settings())
                embed = job_alert_embed(
                    eff_settings,
                    title=item["job_title"],
                    company=item["job_company"],
                    location=item["job_location"],
                    url=item["job_url"],
                    career_level=item["career_level"],
                    tags=item["discipline_tags"],
                    preset=item["preset"],
                    source=item["source_key"],
                    posted_unix=item["posted_unix"],
                )

                try:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    await db.mark_job_outbox_delivered(item["id"], now_ts)
                    await self.record_delivery(db, item["guild_id"], channel_id, item)
                    self.metrics.record_discord_delivery_success(1)
                    posts_sent += 1
                    delivered_total += 1
                except discord.RateLimited as rl:
                    log.warning("[JOBS] Discord 429 rate limit hit on channel %d: %s", channel_id, rl)
                    self.metrics.record_discord_delivery_failure("rate_limited_429", 1)
                    await db.mark_job_outbox_failed(
                        item["id"], item["attempts"] + 1, "DiscordHTTP429", is_terminal=False
                    )
                    break  # Pause channel dispatch on 429
                except discord.Forbidden as fb:
                    log.warning("[JOBS] Forbidden posting to channel %d: %s", channel_id, fb)
                    self.metrics.record_discord_delivery_failure("missing_permissions", 1)
                    await db.mark_job_outbox_failed(
                        item["id"], item["attempts"] + 1, "Forbidden", is_terminal=True
                    )
                    break
                except (discord.DiscordException, OSError, RuntimeError, ValueError) as exc:
                    log.warning("[JOBS] Failed to send job alert to channel %d: %s", channel_id, exc)
                    self.metrics.record_discord_delivery_failure("transient", 1)
                    await db.mark_job_outbox_failed(
                        item["id"], item["attempts"] + 1, repr(exc), is_terminal=False
                    )

        return delivered_total

    async def recover_stale_outbox(self, db: Any, *, lease_seconds: int = 300) -> int:
        """Recover any items left in 'sending' state due to mid-cycle process restart."""
        if hasattr(db, "recover_stale_job_outbox"):
            return int(await db.recover_stale_job_outbox(lease_seconds, now=int(self._clock())))
        return 0
