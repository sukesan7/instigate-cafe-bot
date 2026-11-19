from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

import aiohttp
import aiosqlite

from icbot.errors import ProviderError
from icbot.services.jobs.adapters import (
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
)
from icbot.services.jobs.models import (
    JobFilterCriteria,
    JobPosting,
    SourceHealth,
)

log = logging.getLogger(__name__)

_SENIORITY_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|architect|distinguished)\b",
    re.IGNORECASE,
)

_NON_TECH_RX = re.compile(
    r"\b(business|analyst|marketing|sales|finance|hr|recruiter|product\s+manager|project\s+manager)\b",
    re.IGNORECASE,
)

_DISCIPLINE_RX = re.compile(
    r"\b("
    r"software|swe|computer\s+science|cs\b|"
    r"hardware|electrical|ee\b|embedded|firmware|"
    r"data|ai|machine\s+learning|ml\b|"
    r"mechanical|mech\b|mechatronics|robotics|"
    r"consultant|solutions\s+engineer"
    r")\b",
    re.IGNORECASE,
)

_EARLY_CAREER_HINT_RX = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new\s*grads?|graduates?|entry[- ]?level|junior|jr\.?|student|early\s+career)\b",
    re.IGNORECASE,
)


class _Sendable(Protocol):
    async def send(self, *args: Any, **kwargs: Any) -> Any: ...


def location_passes_smart(location: str, text_blob: str, allowlist: Sequence[str] | None) -> bool:
    """Check if location matches criteria, falling back to remote keyword in text."""
    if not allowlist:
        return True

    loc = (location or "").strip().lower()
    blob = (text_blob or "").lower()

    if not loc or "not listed" in loc:
        if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
            return True
        return any(tok.lower() in blob for tok in allowlist if tok)

    if any(tok.lower() in loc for tok in allowlist if tok):
        return True

    if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
        return True

    return any(tok.lower() in blob for tok in allowlist if tok)


def matches_filter_criteria(job: JobPosting, criteria: JobFilterCriteria) -> bool:
    """
    Evaluate whether a JobPosting matches the filtering rules.

    Hard gates:
      1. Rejects seniority terms.
      2. Rejects non-tech terms.
      3. Requires tech discipline signal.
      4. Requires early-career signal if require_early_career is True.
    Soft gates:
      5. Deny keywords (regex word-boundary).
      6. Allow keywords (if configured).
      7. Location allowlist.
    """
    blob = (job.text_blob or f"{job.title} {job.location}").lower()

    # Gate 1: Reject seniority
    if _SENIORITY_RX.search(blob):
        return False

    # Gate 1b: Reject non-tech
    if _NON_TECH_RX.search(blob):
        return False

    # Gate 2: Require early career if configured
    if criteria.require_early_career and not _EARLY_CAREER_HINT_RX.search(blob):
        return False

    # Gate 3: Require a tech discipline signal
    if not _DISCIPLINE_RX.search(blob):
        return False

    # Gate 4: Deny keywords
    if criteria.keywords_denylist:
        for d in criteria.keywords_denylist:
            if not d:
                continue
            pattern = rf"\b{re.escape(d)}\b"
            if re.search(pattern, blob, re.IGNORECASE):
                return False

    # Gate 5: Allow keywords (if provided, at least one must match)
    if criteria.keywords_allowlist and not any(a.lower() in blob for a in criteria.keywords_allowlist if a):
        return False

    # Gate 6: Location
    return location_passes_smart(job.location, job.text_blob, criteria.location_allowlist)


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
            try:
                if provider == "greenhouse":
                    jobs = await self.greenhouse_adapter.fetch_postings(
                        session, target, timeout_seconds=timeout_seconds, clock=self._clock
                    )
                    health.mark_success(now)
                    return jobs, None
                elif provider == "lever":
                    jobs = await self.lever_adapter.fetch_postings(
                        session, target, timeout_seconds=timeout_seconds, clock=self._clock
                    )
                    health.mark_success(now)
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
                    return jobs, api_url
                else:
                    raise ValueError(f"Unknown ATS provider: {provider}")
            except ProviderError as pe:
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
                health.mark_cooldown(10.0, error=repr(exc), now=now)
                raise
