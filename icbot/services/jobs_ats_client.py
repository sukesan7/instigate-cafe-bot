from __future__ import annotations

import logging
from collections.abc import Callable

import aiohttp

from icbot.services.jobs.adapters import GreenhouseAdapter, LeverAdapter
from icbot.services.jobs.models import JobPosting

log = logging.getLogger(__name__)

__all__ = [
    "JobPosting",
    "fetch_greenhouse_jobs",
    "fetch_lever_jobs",
]

_gh_adapter = GreenhouseAdapter()
_lv_adapter = LeverAdapter()


async def fetch_greenhouse_jobs(
    session: aiohttp.ClientSession,
    board_token: str,
    *,
    timeout_seconds: int = 12,
    limit: int = 100,
    clock: Callable[[], float] | None = None,
) -> list[JobPosting]:
    """Fetch Greenhouse Job Board postings for a board token."""
    return await _gh_adapter.fetch_postings(
        session,
        board_token,
        timeout_seconds=timeout_seconds,
        limit=limit,
        clock=clock,
    )


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
    return await _lv_adapter.fetch_postings(
        session,
        site,
        timeout_seconds=timeout_seconds,
        limit=limit,
        eu_instance=eu_instance,
        clock=clock,
    )
