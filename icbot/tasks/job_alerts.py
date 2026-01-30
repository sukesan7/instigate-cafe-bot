from __future__ import annotations

import re
from datetime import datetime, timezone

import aiohttp
import discord

from icbot.embeds import job_alert as job_alert_embed
from icbot.services.jobs_ats_client import JobPosting, fetch_greenhouse_jobs, fetch_lever_jobs


def _now_unix() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


# ---- Classification / filtering ----

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


def _keyword_passes(text: str, allowlist: list[str] | None, denylist: list[str] | None) -> bool:
    t = (text or "").lower()
    if allowlist and not any(a.lower() in t for a in allowlist if a):
        return False
    if denylist and any(d.lower() in t for d in denylist if d):
        return False
    return True


def _location_passes(location: str, allowlist: list[str] | None) -> bool:
    if not allowlist:
        return True
    loc = (location or "").lower()
    return any(tok.lower() in loc for tok in allowlist if tok)


def _looks_like_not_found(err: Exception) -> bool:
    s = repr(err)
    # your client wraps HTTP errors like: "Greenhouse HTTP 404: ..." / "Lever HTTP 404: ..."
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


async def _mark_seen(db, jobs: list[JobPosting]) -> None:
    now = _now_unix()
    stmts = [
        (
            "INSERT OR IGNORE INTO job_seen(source, external_id, first_seen_unix, title, url) VALUES(?,?,?,?,?)",
            (j.source, j.external_id, now, j.title, j.url),
        )
        for j in jobs
    ]
    if stmts:
        await db.execute_many(stmts)


def _pick_bootstrap_sample(
    jobs: list[JobPosting],
    *,
    loc_allow: list[str] | None,
    kw_allow: list[str] | None,
    kw_deny: list[str] | None,
    max_items: int = 5,
) -> list[tuple[JobPosting, list[str]]]:
    """
    Try to pick a filtered sample first; if nothing matches, fall back to newest
    jobs and label them as "General" so you still get a proof-of-life post.
    """
    picked: list[tuple[JobPosting, list[str]]] = []

    # Pass 1: strict filtering + category classification
    for j in jobs:
        cats = _classify(j.text_blob)
        if not cats:
            continue
        if not _location_passes(j.location, loc_allow):
            continue
        if not _keyword_passes(j.text_blob, kw_allow, kw_deny):
            continue
        picked.append((j, cats))
        if len(picked) >= max_items:
            return picked

    # Pass 2: relaxed fallback (still respects location/keyword filters), allow uncategorized
    for j in jobs:
        if not _location_passes(j.location, loc_allow):
            continue
        if not _keyword_passes(j.text_blob, kw_allow, kw_deny):
            continue
        cats = _classify(j.text_blob) or ["General"]
        picked.append((j, cats))
        if len(picked) >= max_items:
            return picked

    # Pass 3: absolute fallback: newest few, ignore filters, label uncategorized
    for j in jobs[:max_items]:
        cats = _classify(j.text_blob) or ["General"]
        picked.append((j, cats))
    return picked


async def poll_and_post_jobs(bot: discord.Client) -> None:
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        return

    channel_id = int(getattr(settings, "job_alerts_channel_id", 0) or 0)
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)  # type: ignore[attr-defined]
        except Exception:
            return

    gh = list(getattr(settings, "greenhouse_board_tokens", None) or [])
    lv = list(getattr(settings, "lever_sites", None) or [])
    if not gh and not lv:
        return

    # Optional filters
    loc_allow = list(getattr(settings, "jobs_location_allowlist", None) or []) or None
    kw_allow = list(getattr(settings, "jobs_keywords_allowlist", None) or []) or None
    kw_deny = list(getattr(settings, "jobs_keywords_denylist", None) or []) or None
    bootstrap_post = bool(getattr(settings, "jobs_bootstrap_post", False))

    # Track bad ATS sources during runtime to avoid repeated 404 spam
    bad_sources: set[str] = getattr(bot, "_jobs_bad_sources", set())
    setattr(bot, "_jobs_bad_sources", bad_sources)

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
            except Exception as e:
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
            except Exception as e:
                if _looks_like_not_found(e):
                    bad_sources.add(src_key)
                print(f"[JOBS] lever fetch failed: {e!r}")

    if not all_jobs:
        print("[JOBS] fetched 0 postings (all sources empty or filtered/invalid).")
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
                # Silent baseline: mark everything as seen and do NOT post.
                await _mark_seen(db, jobs)
                print(f"[JOBS] baseline checkpointed {len(jobs)} jobs for {source} (no bootstrap post).")
                continue

            # Bootstrap post mode:
            # Pick a sample first, post it, THEN checkpoint everything as seen.
            sample = _pick_bootstrap_sample(
                jobs,
                loc_allow=loc_allow,
                kw_allow=kw_allow,
                kw_deny=kw_deny,
                max_items=5,
            )

            # Post sample
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

            # Checkpoint everything so you don't spam old postings later
            await _mark_seen(db, jobs)
            print(f"[JOBS] bootstrap posted {len(sample)} and checkpointed {len(jobs)} for {source}.")
            continue

        # ---- Normal operation: only new unseen jobs ----
        for j in jobs:
            if posts_left <= 0:
                break

            if await _has_seen(db, j.source, j.external_id):
                continue

            # Decide eligibility FIRST
            cats = _classify(j.text_blob) or []
            if cats:
                pass  # categorized
            else:
                # If it doesn't match any bucket, skip it (but still mark seen so we don't reprocess forever)
                await _mark_seen(db, [j])
                continue

            if not _location_passes(j.location, loc_allow):
                await _mark_seen(db, [j])
                continue

            if not _keyword_passes(j.text_blob, kw_allow, kw_deny):
                await _mark_seen(db, [j])
                continue

            # Post, then mark seen
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

    print(f"[JOBS] cycle complete: fetched={len(all_jobs)} posted={posted_total} bad_sources={len(bad_sources)}")
