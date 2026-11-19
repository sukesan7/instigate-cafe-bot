from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from icbot.errors import parse_retry_after
from icbot.services.http_transport import ProviderProvenance, ProviderResult, TransportError

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = _TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass(frozen=True)
class TTCItem:
    guid: str  # Mastodon status id
    title: str
    link: str
    summary: str
    published_iso: str | None


class TTCFetchError(TransportError):
    pass


def _norm_base(api_base: str) -> str:
    return (api_base or "").strip().rstrip("/")


async def resolve_account_id(
    session: aiohttp.ClientSession,
    api_base: str,
    acct: str,
    *,
    timeout_seconds: int = 12,
) -> str:
    """
    Resolve a Mastodon account's numeric ID via the public search endpoint.
    Works for remote accounts too *if the instance already knows about them*
    (which is true if you can view the profile on that instance).
    """
    api_base = _norm_base(api_base)
    acct_norm = (acct or "").strip().lstrip("@").lower()
    if not api_base or not acct_norm:
        raise TTCFetchError("Missing TTC_MASTODON_BASE or TTC_MASTODON_ACCT")

    url = f"{api_base}/api/v2/search"
    params = {"q": acct_norm, "type": "accounts", "limit": "5"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    headers = {
        "User-Agent": "InstigateCafeBot/0.1 (+discord bot; personal project)",
        "Accept": "application/json",
    }

    async with session.get(url, params=params, timeout=timeout, headers=headers) as resp:
        if resp.status == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            raise TTCFetchError(
                "Rate limited resolving account id (429). Increase TTC_POLL_SECONDS.",
                status=resp.status,
                retry_after=retry_after,
            )
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    accounts = data.get("accounts") or []
    if not accounts:
        raise TTCFetchError(f"No accounts returned for search q={acct_norm!r} on {api_base!r}")

    # Try exact match first: remote acct strings usually look like "user@domain"
    for a in accounts:
        a_acct = str(a.get("acct", "")).strip().lower()
        if a_acct == acct_norm:
            aid = str(a.get("id", "")).strip()
            if aid:
                return aid

    # Otherwise take the first reasonable result
    aid = str(accounts[0].get("id", "")).strip()
    if not aid:
        raise TTCFetchError("Search returned account without id.")
    return aid


async def fetch_account_status_items(
    session: aiohttp.ClientSession,
    api_base: str,
    account_id: str,
    *,
    since_id: str | None = None,
    limit: int = 20,
    timeout_seconds: int = 14,
    include_replies: bool = True,
    include_reblogs: bool = False,
) -> list[TTCItem]:
    """
    Fetch new statuses for an account. Returns newest-first (Mastodon default).
    Use since_id to only retrieve newer items.
    """
    api_base = _norm_base(api_base)
    if not api_base or not account_id:
        raise TTCFetchError("Missing api_base or account_id")

    url = f"{api_base}/api/v1/accounts/{account_id}/statuses"
    params: dict[str, str] = {
        "limit": str(max(1, min(int(limit), 40))),
    }
    # Many TTC updates are posted as replies to earlier advisories.
    # If you exclude replies, you can miss most of the live feed.
    if not include_replies:
        params["exclude_replies"] = "true"
    if not include_reblogs:
        params["exclude_reblogs"] = "true"
    if since_id:
        params["since_id"] = str(since_id)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "User-Agent": "InstigateCafeBot/0.1 (+discord bot; personal project)",
        "Accept": "application/json",
    }

    async with session.get(url, params=params, timeout=timeout, headers=headers) as resp:
        if resp.status == 401 or resp.status == 403:
            raise TTCFetchError(
                f"Instance forbids statuses API ({resp.status}). Try another base instance.",
                status=resp.status,
            )
        if resp.status == 404:
            raise TTCFetchError(
                "Account statuses not found (404). Account id may be wrong.",
                status=resp.status,
            )
        if resp.status == 429:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            raise TTCFetchError(
                "Rate limited fetching statuses (429). Increase TTC_POLL_SECONDS.",
                status=resp.status,
                retry_after=retry_after,
            )
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    if not isinstance(data, list):
        return []

    out: list[TTCItem] = []
    for st in data:
        if not isinstance(st, dict):
            continue

        sid = str(st.get("id") or "").strip()
        created_at = st.get("created_at")
        link = str(st.get("url") or st.get("uri") or "").strip()

        content_html = str(st.get("content") or "")
        spoiler_html = str(st.get("spoiler_text") or "")

        summary = _strip_html(content_html)
        spoiler = _strip_html(spoiler_html)

        # Title = spoiler_text if present, otherwise first bit of content
        headline = spoiler or summary or "TTC Notice"
        headline = headline.strip()
        if len(headline) > 140:
            headline = headline[:137].rstrip() + "..."

        if sid:
            out.append(
                TTCItem(
                    guid=sid,
                    title=headline,
                    link=link,
                    summary=summary,
                    published_iso=str(created_at) if created_at else None,
                )
            )

    return out


def _parse_published_timestamp(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        raw = iso_str.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


async def fetch_account_status_items_provenance(
    session: aiohttp.ClientSession,
    api_base: str,
    account_id: str,
    *,
    since_id: str | None = None,
    limit: int = 20,
    timeout_seconds: int = 14,
    include_replies: bool = True,
    include_reblogs: bool = False,
    clock: Callable[[], float] | None = None,
) -> ProviderResult[list[TTCItem]]:
    """Fetch status items and return them wrapped in a ProviderResult with provenance."""
    api_base_norm = _norm_base(api_base)
    url = f"{api_base_norm}/api/v1/accounts/{account_id}/statuses"
    items = await fetch_account_status_items(
        session,
        api_base_norm,
        account_id,
        since_id=since_id,
        limit=limit,
        timeout_seconds=timeout_seconds,
        include_replies=include_replies,
        include_reblogs=include_reblogs,
    )
    now = clock() if clock is not None else time.time()
    source_ts: float | None = None
    if items:
        for it in items:
            ts = _parse_published_timestamp(it.published_iso)
            if ts is not None:
                source_ts = ts
                break

    prov = ProviderProvenance(
        source="ttc_mastodon",
        source_url=url,
        fetched_at=now,
        source_timestamp=source_ts,
        status_code=200,
        content_length=len(items),
    )
    return ProviderResult(data=items, provenance=prov)
