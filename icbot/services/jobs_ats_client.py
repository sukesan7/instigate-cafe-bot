from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from icbot.errors import ProviderError, redact_secrets

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobPosting:
    """Normalized job posting across multiple ATS providers."""

    source: str  # e.g. "greenhouse:stripe" or "lever:palantir"
    external_id: str  # provider job id
    company: str  # label (board token / site)
    title: str
    location: str
    url: str
    posted_unix: int
    text_blob: str  # title + team/dept + location (for filtering)


def _now_unix(clock: Callable[[], float] | None = None) -> int:
    return int(clock() if clock is not None else time.time())


def _parse_iso8601_to_unix(value: Any, *, clock: Callable[[], float] | None = None) -> int:
    if not value:
        return _now_unix(clock)
    if isinstance(value, (int, float)):
        # Some providers use ms epoch; assume anything too big is ms
        if value > 10_000_000_000:
            return int(value / 1000)
        return int(value)

    s = str(value).strip()
    if not s:
        return _now_unix(clock)

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return _now_unix(clock)


async def fetch_greenhouse_jobs(
    session: aiohttp.ClientSession,
    board_token: str,
    *,
    timeout_seconds: int = 12,
    limit: int = 100,
    clock: Callable[[], float] | None = None,
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
                raise ProviderError(f"Greenhouse HTTP {r.status}: {redact_secrets(text[:200])}")
            data = await r.json()
    except ProviderError:
        raise
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, ValueError, TypeError) as e:
        raise ProviderError(f"Greenhouse fetch failed ({board_token}): {redact_secrets(repr(e))}") from e

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
            for d in j.get("departments") or []:
                if isinstance(d, dict) and d.get("name"):
                    depts.append(str(d["name"]).strip())
            dept_s = ", ".join([d for d in depts if d])

            posted_unix = _parse_iso8601_to_unix(j.get("updated_at") or j.get("created_at"), clock=clock)
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
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed Greenhouse job (%s): %s", board_token, exc)
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
    clock: Callable[[], float] | None = None,
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
                raise ProviderError(f"Lever HTTP {r.status}: {redact_secrets(text[:200])}")
            data = await r.json()
    except ProviderError:
        raise
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, ValueError, TypeError) as e:
        raise ProviderError(f"Lever fetch failed ({site}): {redact_secrets(repr(e))}") from e

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

            posted_unix = _parse_iso8601_to_unix(j.get("createdAt"), clock=clock)
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
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed Lever job (%s): %s", site, exc)
            continue

    out.sort(key=lambda x: x.posted_unix, reverse=True)
    return out
