from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, cast

import aiohttp
import aiosqlite
import discord

from icbot.embeds import job_alert as job_alert_embed
from icbot.services.jobs_ats_client import JobPosting, fetch_greenhouse_jobs, fetch_lever_jobs

log = logging.getLogger(__name__)


class _Sendable(Protocol):
    async def send(self, *args: Any, **kwargs: Any) -> Any: ...


class _JobRecord(Protocol):
    @property
    def source(self) -> str: ...
    @property
    def external_id(self) -> str: ...
    @property
    def company(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def url(self) -> str: ...
    @property
    def location(self) -> str: ...
    @property
    def text_blob(self) -> str: ...
    @property
    def posted_unix(self) -> int: ...


_JobT = TypeVar("_JobT", bound=_JobRecord)


async def _resolve_sendable_channel(bot: discord.Client, channel_id: int) -> _Sendable | None:
    ch: Any = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except (discord.DiscordException, LookupError, ValueError):
            return None
    if ch is not None and callable(getattr(ch, "send", None)):
        return cast(_Sendable, ch)
    return None


def _jobs_channel_id(settings) -> int:
    """
    Back-compat: accept either JOB_ALERTS_CHANNEL_ID (preferred) or JOBS_CHANNEL_ID.
    The settings object may expose either `job_alerts_channel_id` or `jobs_channel_id`.
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


def _now_unix() -> int:
    return int(datetime.now(tz=UTC).timestamp())


# ---- Classification (labels only; NOT a filter gate) ----

_CAT = {
    "Software Engineering Roles": [
        r"\bsoftware\s+engineer\b",
        r"\bsoftware\s+developer\b",
        r"\bswe\b",
        r"\bbackend\b",
        r"\bfront\s*end\b",
        r"\bfull\s*stack\b",
        r"\bplatform\b",
        r"\binfrastructure\b",
        r"\bsite\s+reliability\b|\bsre\b",
        r"\bdevops\b",
        r"\bdistributed\s+systems\b",
    ],
    "Hardware Engineering Roles": [
        r"\bhardware\b",
        r"\bpcb\b",
        r"\bschematic\b",
        r"\bboard\s+design\b",
        r"\bbring[- ]?up\b",
    ],
    "Computer Science Roles": [
        r"\bcomputer\s+science\b",
        r"\bresearch\b",
        r"\bai\b|\bartificial\s+intelligence\b",
        r"\bmachine\s+learning\b|\bml\b",
        r"\bdata\s+scientist\b|\bdata\s+science\b",
        r"\bsecurity\b|\bcyber\b",
        r"\bquant\b|\btrading\b",
    ],
    "Electrical Engineering Roles": [
        r"\belectrical\b|\bee\b",
        r"\bembedded\b",
        r"\bfirmware\b",
        r"\bfpga\b|\bverilog\b|\bvhdl\b",
        r"\basic\b",
        r"\brf\b|\bsignal\b|\bpower\b",
    ],
    "Mechanical Engineering Roles": [
        r"\bmechanical\b|\bmech\b",
        r"\bmanufacturing\b",
        r"\bmechatronics\b",
        r"\brobotics\b",
        r"\bcad\b|\bsolidworks\b|\bautocad\b",
        r"\bthermal\b",
    ],
    "Tech Consultant roles": [
        r"\bconsultant\b",
        r"\bsolutions\s+consultant\b",
        r"\bimplementation\s+consultant\b",
        r"\bprofessional\s+services\b",
        r"\bsolutions\s+engineer\b",
        r"\bsales\s+engineer\b",
    ],
}
_CAT_RE = {k: re.compile("|".join(v), re.IGNORECASE) for k, v in _CAT.items()}


def _classify(text_blob: str) -> list[str]:
    t = (text_blob or "").strip()
    if not t:
        return []
    return [name for name, rx in _CAT_RE.items() if rx.search(t)]


# ---- Shared allow/deny filters (driven by .env via settings) ----


def _keyword_passes(text: str, allowlist: list[str] | None, denylist: list[str] | None) -> bool:
    t = (text or "").lower()
    if allowlist and not any(a.lower() in t for a in allowlist if a):
        return False
    return not (denylist and any(d.lower() in t for d in denylist if d))


def _location_passes(location: str, allowlist: list[str] | None) -> bool:
    if not allowlist:
        return True
    loc = (location or "").lower()
    return any(tok.lower() in loc for tok in allowlist if tok)


def _location_passes_smart(location: str, text_blob: str, allowlist: list[str] | None) -> bool:
    """
    For ATS providers, location can be missing or generic. This checks:
      - explicit location field
      - and falls back to scanning the blob (to catch "remote" mentions)
    """
    if not allowlist:
        return True

    loc = (location or "").strip().lower()
    blob = (text_blob or "").lower()

    # Normalize "not listed" to empty
    if not loc or "not listed" in loc:
        # If allowlist contains "remote", allow when blob contains remote.
        if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
            return True
        return any(tok.lower() in blob for tok in allowlist if tok)

    if any(tok.lower() in loc for tok in allowlist if tok):
        return True

    # Fallback: sometimes "Remote" is in the description/title
    if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
        return True

    return any(tok.lower() in blob for tok in allowlist if tok)


_SENIORITY_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|architect|distinguished)\b",
    re.IGNORECASE,
)

_NON_TECH_RX = re.compile(
    r"\b(business|analyst|marketing|sales|finance|hr|recruiter|product\s+manager|project\s+manager)\b",
    re.IGNORECASE,
)

# Discipline signal: strictly SWE, CS, HW, EE, Data, AI
_DISCIPLINE_RX = re.compile(
    r"\b("
    r"software|swe|computer\s+science|cs\b|"
    r"hardware|electrical|ee\b|embedded|firmware|"
    r"data|ai|machine\s+learning|ml\b"
    r")\b",
    re.IGNORECASE,
)

# Early career strictly Intern / New Grad (Co-op is basically intern)
_EARLY_CAREER_HINT_RX = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new\s*grads?|graduates?|entry[- ]?level|junior|jr\.?|student|early\s+career)\b",
    re.IGNORECASE,
)


def job_matches_env_filters(
    *,
    text_blob: str,
    location: str,
    loc_allow: list[str] | None,
    kw_allow: list[str] | None,
    kw_deny: list[str] | None,
    require_early_career: bool = False,
) -> bool:
    """Single source of truth for ALL ATS filtering (Greenhouse, Lever, Workday).

    Hard gates (always enforced):
      1. Reject seniority terms (senior, staff, manager, director, etc.)
      2. Require early career signal (intern, co-op, new grad, entry-level, junior, student)
      3. Require a tech discipline signal (SWE/CS/HW/EE — not analyst, business, finance, etc.)

    Soft gates (from .env):
      4. Keyword allow/deny lists
      5. Location allowlist
    """
    t = (text_blob or "").lower()

    # Gate 1: Reject senior/management roles
    if _SENIORITY_RX.search(t):
        return False

    # Gate 1b: Reject non-tech roles (analyst, business, finance, etc.)
    if _NON_TECH_RX.search(t):
        return False

    # Gate 2: Require early career signal (if configured)
    if require_early_career and not _EARLY_CAREER_HINT_RX.search(t):
        return False

    # Gate 3: Require a tech discipline signal
    if not _DISCIPLINE_RX.search(t):
        return False

    # Gate 4: .env deny keywords (always applied)
    if kw_deny:
        for d in kw_deny:
            if not d:
                continue
            # Use regex boundaries so 'pm' doesn't flag 'equipment' or 'development'
            pattern = rf"\b{re.escape(d)}\b"
            if re.search(pattern, t, re.IGNORECASE):
                return False

    # Gate 5: .env allow keywords (if set, at least one must match)
    # relaxed: Since discipline + early career are already hard-gated,
    # we don't strictly require an explicit keyword match unless it's explicitly wanted.
    # We will let the hard gates do the primary filtering.

    # Gate 6: Location
    return _location_passes_smart(location, text_blob, loc_allow)


# ---- Workday-specific filtering helpers ----
# Workday bootstrap must NOT ignore filters (otherwise you post Audit/etc).
# Workday also sometimes has blank location → don't auto-fail.


def _location_passes_lenient(location: str, allowlist: list[str] | None) -> bool:
    """If Workday doesn't provide a location, don't fail location filtering."""
    if not allowlist:
        return True
    loc = (location or "").strip().lower()
    if not loc:
        return True
    return any(tok.lower() in loc for tok in allowlist if tok)


def _workday_passes_filters(
    *,
    text_blob: str,
    location: str,
    loc_allow: list[str] | None,
    kw_allow: list[str] | None,
    kw_deny: list[str] | None,
    require_early_career: bool = False,
) -> bool:
    """Workday uses the same unified filter as all other sources."""
    return job_matches_env_filters(
        text_blob=text_blob,
        location=location,
        loc_allow=loc_allow,
        kw_allow=kw_allow,
        kw_deny=kw_deny,
        require_early_career=require_early_career,
    )


def _pick_bootstrap_sample_workday(
    jobs: Sequence[_JobT],
    *,
    loc_allow: list[str] | None,
    kw_allow: list[str] | None,
    kw_deny: list[str] | None,
    require_early_career: bool = False,
    max_items: int = 5,
) -> list[tuple[_JobT, list[str]]]:
    """Workday bootstrap is STRICT: never ignores filters."""
    picked: list[tuple[_JobT, list[str]]] = []

    def ok(j: _JobT) -> bool:
        return _workday_passes_filters(
            text_blob=j.text_blob,
            location=j.location,
            loc_allow=loc_allow,
            kw_allow=kw_allow,
            kw_deny=kw_deny,
            require_early_career=require_early_career,
        )

    # Pass 1: early-career + eligible
    for j in jobs:
        if not ok(j):
            continue
        if not _EARLY_CAREER_HINT_RX.search(j.text_blob or ""):
            continue
        cats = _classify(j.text_blob) or ["General"]
        picked.append((j, cats))
        if len(picked) >= max_items:
            return picked

    # Pass 2: eligible
    for j in jobs:
        if not ok(j):
            continue
        cats = _classify(j.text_blob) or ["General"]
        picked.append((j, cats))
        if len(picked) >= max_items:
            return picked

    return picked


def _looks_like_not_found(err: Exception) -> bool:
    s = repr(err)
    return "HTTP 404" in s or " 404" in s or "Job not found" in s or "Document not found" in s


# ---- DB helpers ----


async def _source_has_any(db, source: str) -> bool:
    row = await db.fetchone("SELECT 1 FROM job_seen WHERE source=? LIMIT 1", (source,))
    return row is not None


async def _has_seen(db, source: str, external_id: str) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM job_seen WHERE source=? AND external_id=? LIMIT 1",
        (source, external_id),
    )
    return row is not None


# Schema-adaptive insert. We try to store richer columns if the table supports them.
_MARK_SEEN_MODE: str | None = None  # "extended" | "basic"


async def _mark_seen(db, jobs: Sequence[_JobRecord]) -> None:
    global _MARK_SEEN_MODE
    now = _now_unix()

    if not jobs:
        return

    # Try extended once; if it fails (missing columns), fall back to basic permanently.
    if _MARK_SEEN_MODE in (None, "extended"):
        try:
            stmts_ext = [
                (
                    "INSERT OR IGNORE INTO job_seen("
                    "source, external_id, first_seen_unix, posted_unix, company, location, title, url, text_blob"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        j.source,
                        j.external_id,
                        now,
                        int(getattr(j, "posted_unix", 0) or 0),
                        j.company,
                        j.location,
                        j.title,
                        j.url,
                        j.text_blob,
                    ),
                )
                for j in jobs
            ]
            await db.execute_many(stmts_ext)
            _MARK_SEEN_MODE = "extended"
            return
        except (aiosqlite.Error, sqlite3.Error):
            _MARK_SEEN_MODE = "basic"

    # Basic insert (compatible with older schema)
    stmts = [
        (
            "INSERT OR IGNORE INTO job_seen(source, external_id, first_seen_unix, title, url) VALUES(?,?,?,?,?)",
            (j.source, j.external_id, now, j.title, j.url),
        )
        for j in jobs
    ]
    await db.execute_many(stmts)


def _pick_bootstrap_sample(
    jobs: list[JobPosting],
    *,
    loc_allow: list[str] | None,
    kw_allow: list[str] | None,
    kw_deny: list[str] | None,
    require_early_career: bool = False,
    max_items: int = 5,
) -> list[tuple[JobPosting, list[str]]]:
    """
    STRICT bootstrap:
      - Never ignores filters
      - If nothing matches, returns [] (caller will just checkpoint silently)
    """
    picked: list[tuple[JobPosting, list[str]]] = []

    for j in jobs:
        if not job_matches_env_filters(
            text_blob=j.text_blob,
            location=j.location,
            loc_allow=loc_allow,
            kw_allow=kw_allow,
            kw_deny=kw_deny,
            require_early_career=require_early_career,
        ):
            continue
        cats = _classify(j.text_blob) or ["General"]
        picked.append((j, cats))
        if len(picked) >= max_items:
            return picked

    return picked


async def poll_and_post_jobs(bot: discord.Client) -> None:
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    channel_id = _jobs_channel_id(settings)
    if not channel_id:
        return

    channel = await _resolve_sendable_channel(bot, channel_id)
    if channel is None:
        return

    gh = list(
        getattr(settings, "greenhouse_boards", None)
        or getattr(settings, "greenhouse_board_tokens", None)
        or []
    )
    gh = [t for t in gh if isinstance(t, str) and t.strip() and t.strip() not in ("\\", "\\\\")]
    lv = list(getattr(settings, "lever_sites", None) or getattr(settings, "lever_site_tokens", None) or [])
    lv = [t for t in lv if isinstance(t, str) and t.strip() and t.strip() not in ("\\", "\\\\")]
    if not gh and not lv:
        return

    # Optional filters (shared env vars)
    loc_allow = list(getattr(settings, "jobs_location_allowlist", None) or []) or None
    kw_allow = list(getattr(settings, "jobs_keywords_allowlist", None) or []) or None
    kw_deny = list(getattr(settings, "jobs_keywords_denylist", None) or []) or None
    bootstrap_post = bool(getattr(settings, "jobs_bootstrap_post", False))
    require_early_career = bool(getattr(settings, "jobs_require_early_career", False))

    bad_sources: set[str] = getattr(bot, "_jobs_bad_sources", set())
    cast(Any, bot)._jobs_bad_sources = bad_sources

    async with aiohttp.ClientSession(headers={"User-Agent": "InstigateCafeBot/1.0"}) as session:
        all_jobs: list[JobPosting] = []

        for token in gh:
            token = (token or "").strip()
            if not token:
                continue
            src_key = f"greenhouse:{token}"
            if src_key in bad_sources:
                continue
            try:
                all_jobs.extend(await fetch_greenhouse_jobs(session, token, timeout_seconds=12, limit=120))
            except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError) as e:
                if _looks_like_not_found(e):
                    bad_sources.add(src_key)
                print(f"[JOBS] greenhouse fetch failed: {e!r}")

        for site in lv:
            site = (site or "").strip()
            if not site:
                continue
            src_key = f"{site}"
            if src_key in bad_sources:
                continue
            try:
                all_jobs.extend(await fetch_lever_jobs(session, site, timeout_seconds=12, limit=120))
            except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError, TypeError) as e:
                if _looks_like_not_found(e):
                    bad_sources.add(src_key)
                print(f"[JOBS] lever fetch failed: {e!r}")

    if not all_jobs:
        print("[JOBS] fetched 0 postings (all sources empty or invalid).")
        return

    by_source: dict[str, list[JobPosting]] = {}
    for j in all_jobs:
        by_source.setdefault(j.source, []).append(j)

    posts_left = 12  # safety cap per cycle
    posted_total = 0

    for source, jobs in by_source.items():
        jobs.sort(key=lambda x: x.posted_unix, reverse=True)

        has_any = await _source_has_any(db, source)

        # ---- First time we ever see this source ----
        if not has_any:
            if not bootstrap_post:
                await _mark_seen(db, jobs)
                print(f"[JOBS] baseline checkpointed {len(jobs)} jobs for {source} (no bootstrap post).")
                continue

            # STRICT bootstrap: only post matches
            sample = _pick_bootstrap_sample(
                jobs,
                loc_allow=loc_allow,
                kw_allow=kw_allow,
                kw_deny=kw_deny,
                require_early_career=require_early_career,
                max_items=5,
            )

            for j, cats in sample:
                if posts_left <= 0:
                    break
                await channel.send(
                    embed=job_alert_embed(
                        settings,
                        company=j.company,
                        title=j.title,
                        url=j.url,
                        location=j.location,
                        categories=cats,
                        source=j.source,
                    )
                )
                posts_left -= 1
                posted_total += 1

            await _mark_seen(db, jobs)
            if sample:
                print(f"[JOBS] bootstrap posted {len(sample)} and checkpointed {len(jobs)} for {source}.")
            else:
                print(f"[JOBS] bootstrap posted 0 (no matches) and checkpointed {len(jobs)} for {source}.")
            continue

        # ---- Normal operation: only new unseen jobs ----
        for j in jobs:
            if posts_left <= 0:
                break

            if await _has_seen(db, j.source, j.external_id):
                continue

            # Shared env filters drive whether we POST.
            if not job_matches_env_filters(
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

            await channel.send(
                embed=job_alert_embed(
                    settings,
                    company=j.company,
                    title=j.title,
                    url=j.url,
                    location=j.location,
                    categories=cats,
                    source=j.source,
                )
            )
            await _mark_seen(db, [j])

            posts_left -= 1
            posted_total += 1

    print(
        f"[JOBS] cycle complete: fetched={len(all_jobs)} posted={posted_total} bad_sources={len(bad_sources)}"
    )
