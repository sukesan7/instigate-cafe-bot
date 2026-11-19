from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
from yarl import URL

from icbot.errors import ProviderError, parse_retry_after, redact_secrets
from icbot.services.jobs.classification import (
    canonicalize_company_name,
    canonicalize_title,
    classify_career_level,
    classify_discipline_tags,
    detect_is_remote,
)
from icbot.services.jobs.models import JobPosting

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

_CSRF_IN_HTML = re.compile(
    r"(?:x-calypso-csrf-token|csrfToken)[\"']?\s*[:=]\s*[\"']([a-zA-Z0-9\-_/\.]+)[\"']", re.I
)


def _now_unix(clock: Callable[[], float] | None = None) -> int:
    return int(clock() if clock is not None else time.time())


def _parse_iso8601_to_unix(value: Any, *, clock: Callable[[], float] | None = None) -> int:
    if not value:
        return _now_unix(clock)
    if isinstance(value, (int, float)):
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


def _add_or_replace_query(url: str, **params: str) -> str:
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query))
    for k, v in params.items():
        q[k] = v
    new_query = urlencode(q)
    return urlunparse(parsed._replace(query=new_query))


def _origin_from_url(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _unescape_workday_text(html: str) -> str:
    return html.replace("\\u002F", "/").replace("\\/", "/")


def _snippet(s: Any, n: int = 350) -> str:
    try:
        txt = s if isinstance(s, str) else json.dumps(s)
    except (TypeError, ValueError):
        txt = repr(s)
    return (txt[:n] + "...") if len(txt) > n else txt


class AtsAdapter(Protocol):
    """Protocol for an ATS provider adapter."""

    @property
    def provider_name(self) -> str: ...

    async def fetch_postings(
        self,
        session: aiohttp.ClientSession,
        target: str,
        *,
        timeout_seconds: int = 12,
        limit: int = 120,
        clock: Callable[[], float] | None = None,
    ) -> list[JobPosting]: ...


class GreenhouseAdapter:
    """Adapter for Greenhouse Public Job Boards."""

    provider_name: str = "greenhouse"

    async def fetch_postings(
        self,
        session: aiohttp.ClientSession,
        target: str,
        *,
        timeout_seconds: int = 12,
        limit: int = 120,
        clock: Callable[[], float] | None = None,
    ) -> list[JobPosting]:
        board_token = (target or "").strip()
        if not board_token:
            return []

        url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
        now_unix = _now_unix(clock)

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as r:
                if r.status != 200:
                    text = await r.text()
                    retry_after = parse_retry_after(r.headers.get("Retry-After"))
                    raise ProviderError(
                        f"Greenhouse HTTP {r.status}: {redact_secrets(text[:200])}",
                        status=r.status,
                        retry_after=retry_after,
                    )
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
                raw_title = str(j.get("title") or "").strip() or "Untitled Role"
                title = canonicalize_title(raw_title)

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

                raw_updated = j.get("updated_at")
                raw_created = j.get("created_at")
                posted_unix = _parse_iso8601_to_unix(raw_created or raw_updated, clock=clock)
                updated_unix = _parse_iso8601_to_unix(raw_updated, clock=clock) if raw_updated else None

                blob = " ".join([title, dept_s, loc]).strip()
                if not job_id or not url_job:
                    continue

                career_level = classify_career_level(blob)
                discipline_tags = classify_discipline_tags(blob)
                is_remote = detect_is_remote(loc, blob)

                out.append(
                    JobPosting(
                        provider="greenhouse",
                        employer=board_token,
                        external_id=job_id,
                        title=title,
                        career_level=career_level,
                        discipline_tags=discipline_tags,
                        location=loc or "(location not listed)",
                        is_remote=is_remote,
                        url=url_job,
                        posted_unix=posted_unix,
                        updated_unix=updated_unix,
                        fetched_unix=now_unix,
                        text_blob=blob,
                        source_key=f"greenhouse:{board_token}",
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                log.debug("Skipping malformed Greenhouse job (%s): %s", board_token, exc)
                continue

        out.sort(key=lambda x: int(x.posted_unix or 0), reverse=True)
        return out


class LeverAdapter:
    """Adapter for Lever Public Postings API."""

    provider_name: str = "lever"

    async def fetch_postings(
        self,
        session: aiohttp.ClientSession,
        target: str,
        *,
        timeout_seconds: int = 12,
        limit: int = 120,
        eu_instance: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> list[JobPosting]:
        site = (target or "").strip()
        if not site:
            return []

        base = "https://api.eu.lever.co" if eu_instance else "https://api.lever.co"
        url = f"{base}/v0/postings/{site}?mode=json"
        now_unix = _now_unix(clock)

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as r:
                if r.status != 200:
                    text = await r.text()
                    retry_after = parse_retry_after(r.headers.get("Retry-After"))
                    raise ProviderError(
                        f"Lever HTTP {r.status}: {redact_secrets(text[:200])}",
                        status=r.status,
                        retry_after=retry_after,
                    )
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
                raw_title = str(j.get("text") or j.get("title") or "").strip() or "Untitled Role"
                title = canonicalize_title(raw_title)
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

                career_level = classify_career_level(blob)
                discipline_tags = classify_discipline_tags(blob)
                is_remote = detect_is_remote(loc, blob)

                out.append(
                    JobPosting(
                        provider="lever",
                        employer=site,
                        external_id=job_id,
                        title=title,
                        career_level=career_level,
                        discipline_tags=discipline_tags,
                        location=loc or "(location not listed)",
                        is_remote=is_remote,
                        url=url_job,
                        posted_unix=posted_unix,
                        updated_unix=None,
                        fetched_unix=now_unix,
                        text_blob=blob,
                        source_key=site,
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                log.debug("Skipping malformed Lever job (%s): %s", site, exc)
                continue

        out.sort(key=lambda x: int(x.posted_unix or 0), reverse=True)
        return out


class WorkdayAdapter:
    """Adapter for Workday CXS Career Sites."""

    provider_name: str = "workday"

    @staticmethod
    def derive_cxs_info(careers_url: str, locale: str = "en-US") -> tuple[str, str | None, str | None]:
        """
        Derive API endpoint, tenant, and site slug from various Workday careers URL shapes.
        Returns: (api_url, tenant, site)
        """
        u = urlparse(careers_url)
        base = f"{u.scheme}://{u.netloc}"
        netloc = u.netloc or ""
        path_parts = [p for p in (u.path or "").split("/") if p]

        # Case 1: Standard subdomain tenant (e.g. acme.wd3.myworkdayjobs.com or acme.myworkdayjobs.com)
        parts = netloc.split(".")
        tenant: str | None = None
        if len(parts) >= 2 and parts[0] not in ("myworkdayjobs", "wd1", "wd2", "wd3", "wd5", "wd10"):
            tenant = parts[0]
        elif len(path_parts) >= 2 and locale in path_parts:
            # Case 2: Path-based tenant, e.g. myworkdayjobs.com/acme/en-US/Careers
            loc_idx = path_parts.index(locale)
            if loc_idx > 0:
                tenant = path_parts[loc_idx - 1]

        site: str | None = None
        if locale in path_parts:
            i = path_parts.index(locale)
            if i + 1 < len(path_parts):
                site = path_parts[i + 1]
        if not site and path_parts:
            site = path_parts[-1]

        if not tenant or not site:
            raise ProviderError(
                f"Workday heuristic failed (tenant={tenant!r}, site={site!r}) from {redact_secrets(careers_url)!r}"
            )

        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        api = _add_or_replace_query(api, locale=locale)
        return api, tenant, site

    @staticmethod
    def careers_prefix_from_url(careers_url: str, locale: str = "en-US") -> str:
        u = urlparse(careers_url)
        parts = [p for p in (u.path or "").split("/") if p]
        if locale in parts:
            i = parts.index(locale)
            if i + 1 < len(parts):
                return f"/{locale}/{parts[i + 1]}"
        if parts:
            return "/" + "/".join(parts[:2]) if len(parts) >= 2 else "/" + parts[0]
        return ""

    async def _warmup_and_get_csrf(
        self,
        session: aiohttp.ClientSession,
        careers_url: str,
        locale: str,
        timeout_seconds: int,
    ) -> str | None:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": locale,
        }
        try:
            async with session.get(
                careers_url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
            ) as r:
                token = r.headers.get("x-calypso-csrf-token") or r.headers.get("X-Calypso-CSRF-Token")
                text = await r.text(errors="ignore")
        except (aiohttp.ClientError, TimeoutError) as e:
            log.debug("Workday warmup GET failed (%s): %s", careers_url, e)
            return None

        if token:
            return token

        text = _unescape_workday_text(text)
        m = _CSRF_IN_HTML.search(text)
        if m:
            return m.group(1)

        try:
            jar = session.cookie_jar.filter_cookies(URL(careers_url))
            for k, morsel in jar.items():
                name = k.lower()
                if ("csrf" in name or "xsrf" in name) and morsel.value:
                    return morsel.value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass

        return None

    def _extract_postings(
        self,
        payload: dict[str, Any],
        employer_label: str,
        careers_url: str,
        locale: str,
        now_unix: int,
    ) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        postings = payload.get("jobPostings") or payload.get("jobs") or []
        if not isinstance(postings, list):
            return jobs

        for p in postings:
            if not isinstance(p, dict):
                continue

            raw_title = (
                p.get("title")
                or p.get("postingTitle")
                or (p.get("jobPostingInfo") or {}).get("title")
                or "Job"
            )
            title = canonicalize_title(str(raw_title))
            external_path = (
                p.get("externalPath")
                or p.get("externalUrl")
                or (p.get("jobPostingInfo") or {}).get("externalPath")
            )

            if isinstance(external_path, str) and external_path.startswith("http"):
                url = external_path
            else:
                u = urlparse(careers_url)
                host = f"{u.scheme}://{u.netloc}"
                prefix = self.careers_prefix_from_url(careers_url, locale)

                if not external_path:
                    url = careers_url
                else:
                    ep = str(external_path).strip()
                    if not ep.startswith("/"):
                        ep = "/" + ep
                    url = (
                        host + prefix + ep if ep.startswith(("/job/", "/job?")) or ep == "/job" else host + ep
                    )

            location = p.get("locationsText") or p.get("location") or ""
            if isinstance(location, list):
                location = ", ".join([str(x) for x in location])
            location = str(location)

            ext_id = str(p.get("jobPostingId") or p.get("id") or p.get("externalPath") or url)
            blob = f"{employer_label}\n{title}\n{location}\n{url}".lower()

            raw_posted = p.get("postedOn") or p.get("startDate")
            posted_unix = _parse_iso8601_to_unix(raw_posted) if raw_posted else 0

            career_level = classify_career_level(blob)
            discipline_tags = classify_discipline_tags(blob)
            is_remote = detect_is_remote(location, blob)

            jobs.append(
                JobPosting(
                    provider="workday",
                    employer=employer_label,
                    external_id=ext_id,
                    title=title,
                    career_level=career_level,
                    discipline_tags=discipline_tags,
                    location=location,
                    is_remote=is_remote,
                    url=str(url),
                    posted_unix=posted_unix,
                    updated_unix=None,
                    fetched_unix=now_unix,
                    text_blob=blob,
                    source_key=f"workday:{employer_label}",
                )
            )

        return jobs

    async def fetch_postings(
        self,
        session: aiohttp.ClientSession,
        target: str,
        *,
        employer_label: str = "",
        api_url_override: str | None = None,
        locale: str = "en-US",
        timeout_seconds: int = 12,
        limit: int = 240,
        clock: Callable[[], float] | None = None,
    ) -> tuple[list[JobPosting], str, str | None, str | None]:
        """
        Fetch postings for a Workday site.
        target: careers_url
        Returns: (jobs, api_url_used, tenant, site)
        """
        careers_url = target.strip()
        employer = employer_label or canonicalize_company_name(careers_url)
        now_unix = _now_unix(clock)

        if api_url_override:
            api_url = _add_or_replace_query(api_url_override, locale=locale)
            tenant = None
            site_slug = None
        else:
            api_url, tenant, site_slug = self.derive_cxs_info(careers_url, locale=locale)

        csrf = await self._warmup_and_get_csrf(session, careers_url, locale, timeout_seconds)
        origin = _origin_from_url(careers_url)

        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": locale,
            "Origin": origin,
            "Referer": careers_url,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=utf-8",
        }
        if csrf:
            headers["X-Calypso-CSRF-Token"] = csrf
            headers["x-calypso-csrf-token"] = csrf

        page_size = 20
        api_url = _add_or_replace_query(api_url, locale=locale)

        jobs: list[JobPosting] = []
        offset = 0

        while len(jobs) < limit:
            batch_limit = min(page_size, limit - len(jobs))
            body = {"appliedFacets": {}, "limit": batch_limit, "offset": offset, "searchText": ""}

            # Try POST first
            st_post = 0
            payload_post: dict[str, Any] = {}
            ra_post: float | None = None
            async with session.post(
                api_url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as r_post:
                st_post = r_post.status
                ra_post = parse_retry_after(r_post.headers.get("Retry-After"))
                try:
                    payload_post = await r_post.json()
                except (json.JSONDecodeError, ValueError, TypeError):
                    raw_text = await r_post.text(errors="ignore")
                    payload_post = {"_raw": raw_text}

            if st_post == 200 and isinstance(payload_post, dict):
                batch = self._extract_postings(payload_post, employer, careers_url, locale, now_unix)
                jobs.extend(batch)
            else:
                # Fallback GET
                get_url = _add_or_replace_query(
                    api_url, offset=str(offset), limit=str(batch_limit), searchText=""
                )
                get_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                try:
                    async with session.get(
                        get_url,
                        headers=get_headers,
                        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    ) as r_get:
                        st_get = r_get.status
                        ra_get = parse_retry_after(r_get.headers.get("Retry-After"))
                        try:
                            payload_get = await r_get.json()
                        except (json.JSONDecodeError, ValueError, TypeError):
                            raw_text = await r_get.text(errors="ignore")
                            payload_get = {"_raw": raw_text}
                except (aiohttp.ClientError, TimeoutError) as e:
                    raise ProviderError(
                        f"Workday network failure ({employer}): {redact_secrets(repr(e))}"
                    ) from e

                if st_get != 200:
                    retry_after = ra_get if ra_get is not None else ra_post
                    raise ProviderError(
                        f"Workday HTTP {st_get} via GET {redact_secrets(get_url)} :: "
                        f"POST_JSON:{st_post}:{redact_secrets(_snippet(payload_post))} | "
                        f"GET:{st_get}:{redact_secrets(_snippet(payload_get))}",
                        status=st_get,
                        retry_after=retry_after,
                    )
                batch = self._extract_postings(payload_get, employer, careers_url, locale, now_unix)
                jobs.extend(batch)

            total = 0
            try:
                total = int((payload_post.get("total") if isinstance(payload_post, dict) else 0) or 0)
            except (TypeError, ValueError):
                total = 0

            if total and offset + page_size >= total:
                break
            if len(jobs) < offset + batch_limit:
                break

            offset += page_size

        return jobs[:limit], api_url, tenant, site_slug
