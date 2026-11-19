from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from icbot.services.jobs.adapters import WorkdayAdapter

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class WorkdaySite:
    label: str
    careers_url: str
    api_url_override: str | None = None
    locale: str = "en-US"


@dataclass(frozen=True)
class WorkdayCxsInfo:
    api_url: str
    tenant: str | None
    site: str | None
    locale: str


# Legacy JobItem kept for exact type compatibility
@dataclass(frozen=True)
class JobItem:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    posted_unix: int
    text_blob: str

    @property
    def source_key(self) -> str:
        return self.source


def parse_workday_sites(items: list[str]) -> list[WorkdaySite]:
    """
    WORKDAY_SITES (comma-separated):
      LABEL|CAREERS_URL
      LABEL|CAREERS_URL|API_URL_OVERRIDE
      LABEL|CAREERS_URL|API_URL_OVERRIDE|LOCALE
      LABEL|CAREERS_URL||LOCALE
    """
    out: list[WorkdaySite] = []
    for raw in items or []:
        s = (raw or "").strip()
        if not s or s in ("\\", "\\\\"):
            continue

        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 2:
            continue

        label = parts[0]
        careers_url = parts[1]
        api_override = parts[2] if len(parts) >= 3 and parts[2] else None
        locale = parts[3] if len(parts) >= 4 and parts[3] else "en-US"

        if careers_url and not careers_url.startswith(("http://", "https://")):
            careers_url = "https://" + careers_url
        if api_override and not api_override.startswith(("http://", "https://")):
            api_override = "https://" + api_override

        out.append(
            WorkdaySite(label=label, careers_url=careers_url, api_url_override=api_override, locale=locale)
        )
    return out


_adapter = WorkdayAdapter()


async def fetch_workday_jobs(
    session: aiohttp.ClientSession,
    site: WorkdaySite,
    *,
    api_url_override: str | None = None,
    timeout_seconds: int = 12,
    max_results: int = 240,
) -> tuple[list[JobItem], WorkdayCxsInfo]:
    """Fetch Workday jobs using normalized WorkdayAdapter while preserving JobItem interface."""
    override = api_url_override or site.api_url_override
    jobs, api_url, tenant, site_slug = await _adapter.fetch_postings(
        session,
        site.careers_url,
        employer_label=site.label,
        api_url_override=override,
        locale=site.locale,
        timeout_seconds=timeout_seconds,
        limit=max_results,
    )
    cxs = WorkdayCxsInfo(api_url=api_url, tenant=tenant, site=site_slug, locale=site.locale)
    job_items: list[JobItem] = [
        JobItem(
            source=j.source_key,
            external_id=j.external_id,
            title=j.title,
            company=j.employer,
            location=j.location,
            url=j.url,
            posted_unix=int(j.posted_unix or 0),
            text_blob=j.text_blob,
        )
        for j in jobs
    ]
    return job_items, cxs
