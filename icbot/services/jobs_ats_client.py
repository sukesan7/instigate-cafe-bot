from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp


@dataclass(frozen=True)
class JobPosting:
    """Normalized job posting across multiple ATS providers."""
    source: str           # e.g. "greenhouse:stripe" or "lever:palantir"
    external_id: str      # provider job id
    company: str          # label (board token / site)
    title: str
    location: str
    url: str
    posted_unix: int
    text_blob: str        # title + team/dept + location (for filtering)


def _now_unix() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _parse_iso8601_to_unix(value: Any) -> int:
    if not value:
        return _now_unix()
    if isinstance(value, (int, float)):
        # Some providers use ms epoch; assume anything too big is ms
        if value > 10_000_000_000:
            return int(value / 1000)
        return int(value)

    s = str(value).strip()
    if not s:
        return _now_unix()

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return _now_unix()


async def fetch_greenhouse_jobs(
    session: aiohttp.ClientSession,
    board_token: str,
    *,
    timeout_seconds: int = 12,
    limit: int = 100,
) -> list[JobPosting]:
    """Fetch Greenhouse Job Board postings for a board token."""
    board_token = (board_token or "").strip()
    if not board_token:
        return []

    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"Greenhouse HTTP {r.status}: {text[:200]}")
            data = await r.json()
    except Exception as e:
        raise RuntimeError(f"Greenhouse fetch failed ({board_token}): {e!r}")

    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    out: list[JobPosting] = []

    for j in jobs[: max(0, int(limit))]:
        try:
            job_id = str(j.get("id") or "")
            title = str(j.get("title") or "").strip() or "Untitled Role"
            loc = ""
            loc_obj = j.get("location")
            if isinstance(loc_obj, dict):
                loc = str(loc_obj.get("name") or "").strip()
            url_job = str(j.get("absolute_url") or j.get("url") or "").strip()

            depts = []
            for d in (j.get("departments") or []):
                if isinstance(d, dict) and d.get("name"):
                    depts.append(str(d["name"]).strip())
            dept_s = ", ".join([d for d in depts if d])

            content = str(j.get("content") or "")
            posted_unix = _parse_iso8601_to_unix(j.get("updated_at") or j.get("created_at"))
            blob = " ".join([title, dept_s, loc]).strip()

            if not job_id or not url_job:
                continue

            out.append(
                JobPosting(
                    source=f"greenhouse:{board_token}",
                    external_id=job_id,
                    company=board_token,
                    title=title,
                    location=loc or "(location not listed)",
                    url=url_job,
                    posted_unix=posted_unix,
                    text_blob=blob,
                )
            )
        except Exception:
            continue

    out.sort(key=lambda x: x.posted_unix, reverse=True)
    return out


async def fetch_lever_jobs(
    session: aiohttp.ClientSession,
    site: str,
    *,
    timeout_seconds: int = 12,
    limit: int = 100,
    eu_instance: bool = False,
) -> list[JobPosting]:
    """Fetch Lever public postings for a company site."""
    site = (site or "").strip()
    if not site:
        return []

    base = "https://api.eu.lever.co" if eu_instance else "https://api.lever.co"
    url = f"{base}/v0/postings/{site}?mode=json"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"Lever HTTP {r.status}: {text[:200]}")
            data = await r.json()
    except Exception as e:
        raise RuntimeError(f"Lever fetch failed ({site}): {e!r}")

    jobs = data if isinstance(data, list) else []
    out: list[JobPosting] = []

    for j in jobs[: max(0, int(limit))]:
        try:
            job_id = str(j.get("id") or "")
            title = str(j.get("text") or j.get("title") or "").strip() or "Untitled Role"
            url_job = str(j.get("hostedUrl") or j.get("applyUrl") or j.get("url") or "").strip()

            cats = j.get("categories") or {}
            loc = ""
            if isinstance(cats, dict):
                loc = str(cats.get("location") or "").strip()
                team = str(cats.get("team") or "").strip()
                dept = str(cats.get("department") or "").strip()
                commitment = str(cats.get("commitment") or "").strip()
            else:
                team = dept = commitment = ""

            content = ""
            if j.get("descriptionPlain"):
                content = str(j.get("descriptionPlain") or "")
            elif j.get("description"):
                content = str(j.get("description") or "")

            posted_unix = _parse_iso8601_to_unix(j.get("createdAt"))
            blob = " ".join([title, team, dept, commitment, loc]).strip()

            if not job_id or not url_job:
                continue

            out.append(
                JobPosting(
                    source=f"{site}",
                    external_id=job_id,
                    company=site,
                    title=title,
                    location=loc or "(location not listed)",
                    url=url_job,
                    posted_unix=posted_unix,
                    text_blob=blob,
                )
            )
        except Exception:
            continue

    out.sort(key=lambda x: x.posted_unix, reverse=True)
    return out
