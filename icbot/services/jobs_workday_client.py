from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

import aiohttp


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class WorkdaySite:
    label: str
    careers_url: str
    api_url_override: Optional[str] = None
    locale: str = "en-US"


@dataclass(frozen=True)
class WorkdayCxsInfo:
    api_url: str
    tenant: Optional[str]
    site: Optional[str]
    locale: str


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

        out.append(WorkdaySite(label=label, careers_url=careers_url, api_url_override=api_override, locale=locale))
    return out


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
    except Exception:
        txt = repr(s)
    return (txt[:n] + "...") if len(txt) > n else txt


_CSRF_IN_HTML = re.compile(r"(?:x-calypso-csrf-token|csrfToken)[\"']?\s*[:=]\s*[\"']([a-zA-Z0-9\-_/\.]+)[\"']", re.I)


async def _warmup_and_get_csrf(
    session: aiohttp.ClientSession,
    careers_url: str,
    *,
    locale: str,
    timeout_seconds: int,
) -> Optional[str]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        # IMPORTANT: single locale only
        "Accept-Language": locale,
    }
    async with session.get(careers_url, headers=headers, timeout=timeout_seconds) as r:
        token = r.headers.get("x-calypso-csrf-token") or r.headers.get("X-Calypso-CSRF-Token")
        text = await r.text(errors="ignore")

    if token:
        return token

    text = _unescape_workday_text(text)
    m = _CSRF_IN_HTML.search(text)
    if m:
        return m.group(1)

    # Cookie-based fallback (best-effort)
    try:
        jar = session.cookie_jar.filter_cookies(careers_url)
        for k, morsel in jar.items():
            name = k.lower()
            if "csrf" in name or "xsrf" in name:
                if morsel.value:
                    return morsel.value
    except Exception:
        pass

    return None


def _heuristic_cxs_from_careers_url(careers_url: str, locale: str) -> WorkdayCxsInfo:
    u = urlparse(careers_url)
    base = f"{u.scheme}://{u.netloc}"

    tenant = u.netloc.split(".")[0] if u.netloc else None

    path_parts = [p for p in (u.path or "").split("/") if p]
    site = None
    if "en-US" in path_parts:
        i = path_parts.index("en-US")
        if i + 1 < len(path_parts):
            site = path_parts[i + 1]
    if not site and path_parts:
        site = path_parts[-1]

    if not tenant or not site:
        raise RuntimeError(f"Workday heuristic failed (tenant={tenant!r}, site={site!r}) from {careers_url!r}")

    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    api = _add_or_replace_query(api, locale=locale)
    return WorkdayCxsInfo(api_url=api, tenant=tenant, site=site, locale=locale)




def _careers_prefix_from_careers_url(careers_url: str, locale: str) -> str:
    """
    Derive the web path prefix from a Workday careers URL.

    Examples:
      careers_url = https://cppib.wd10.myworkdayjobs.com/en-US/cppinvestments
      -> prefix "/en-US/cppinvestments"

    Some payloads return externalPath like "/job/..." (missing the locale/site prefix).
    We use this prefix to construct a valid application URL.
    """
    u = urlparse(careers_url)
    parts = [p for p in (u.path or "").split("/") if p]
    if locale in parts:
        i = parts.index(locale)
        if i + 1 < len(parts):
            return f"/{locale}/{parts[i+1]}"
    # Fallback: if the path already ends with a site slug, keep last segment
    if parts:
        return "/" + "/".join(parts[:2]) if len(parts) >= 2 else "/" + parts[0]
    return ""
def _extract_jobs_from_payload(payload: dict[str, Any], site: WorkdaySite) -> list[JobItem]:
    jobs: list[JobItem] = []
    postings = payload.get("jobPostings") or payload.get("jobs") or []
    if not isinstance(postings, list):
        return jobs

    for p in postings:
        if not isinstance(p, dict):
            continue

        title = p.get("title") or p.get("postingTitle") or (p.get("jobPostingInfo") or {}).get("title") or "Job"
        external_path = p.get("externalPath") or p.get("externalUrl") or (p.get("jobPostingInfo") or {}).get("externalPath")

        if isinstance(external_path, str) and external_path.startswith("http"):
            url = external_path
        else:
            u = urlparse(site.careers_url)
            host = f"{u.scheme}://{u.netloc}"
            prefix = _careers_prefix_from_careers_url(site.careers_url, site.locale)

            if not external_path:
                url = site.careers_url
            else:
                ep = str(external_path).strip()

                # Ensure leading slash for relative paths
                if not ep.startswith("/"):
                    ep = "/" + ep

                # Common case: some tenants return "/job/..." (missing "/<locale>/<site>/")
                # Example (broken): https://cppib.wd10.myworkdayjobs.com/job/...
                # Fixed:          https://cppib.wd10.myworkdayjobs.com/en-US/cppinvestments/job/...
                if ep.startswith("/job/") or ep == "/job" or ep.startswith("/job?"):
                    if prefix and not prefix.endswith("/"):
                        url = host + prefix + ep
                    else:
                        url = host + prefix + ep
                else:
                    # If the path already includes locale/site ("/en-US/<site>/..."), keep it.
                    url = host + ep

        location = p.get("locationsText") or p.get("location") or ""
        if isinstance(location, list):
            location = ", ".join([str(x) for x in location])

        ext_id = str(p.get("jobPostingId") or p.get("id") or p.get("externalPath") or url)
        blob = f"{site.label}\n{title}\n{location}\n{url}".lower()

        jobs.append(
            JobItem(
                source=f"workday:{site.label}",
                external_id=ext_id,
                title=str(title),
                company=site.label,
                location=str(location),
                url=str(url),
                posted_unix=0,
                text_blob=blob,
            )
        )

    return jobs


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    json_body: Optional[dict[str, Any]] = None,
) -> Tuple[int, dict[str, Any]]:
    kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout_seconds}
    if json_body is not None:
        kwargs["json"] = json_body

    async with session.request(method, url, **kwargs) as r:
        raw = await r.text(errors="ignore")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {"_raw": raw}


async def fetch_workday_jobs(
    session: aiohttp.ClientSession,
    site: WorkdaySite,
    *,
    api_url_override: Optional[str] = None,
    timeout_seconds: int = 12,
    max_results: int = 240,
) -> Tuple[list[JobItem], WorkdayCxsInfo]:
    """
    Key change: Workday CXS often rejects large 'limit' values.
    We cap page size to 20, which matches many production boards.
    """
    api_url = api_url_override or site.api_url_override
    if not api_url:
        cxs = _heuristic_cxs_from_careers_url(site.careers_url, site.locale)
        api_url = cxs.api_url
    else:
        api_url = _add_or_replace_query(api_url, locale=site.locale)
        cxs = WorkdayCxsInfo(api_url=api_url, tenant=None, site=None, locale=site.locale)

    # Warmup cookies + CSRF if required
    csrf = await _warmup_and_get_csrf(session, site.careers_url, locale=site.locale, timeout_seconds=timeout_seconds)

    origin = _origin_from_url(site.careers_url)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": site.locale,  # single locale only
        "Origin": origin,
        "Referer": site.careers_url,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json; charset=utf-8",
    }
    if csrf:
        headers["X-Calypso-CSRF-Token"] = csrf
        headers["x-calypso-csrf-token"] = csrf

    # ✅ Workday-safe page size
    page_size = 20
    api_url = _add_or_replace_query(api_url, locale=site.locale)

    jobs: list[JobItem] = []
    offset = 0

    while len(jobs) < max_results:
        limit = min(page_size, max_results - len(jobs))
        body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}

        # Try POST JSON first
        st_post, payload_post = await _request_json(
            session, "POST", api_url, headers=headers, timeout_seconds=timeout_seconds, json_body=body
        )
        if st_post == 200 and isinstance(payload_post, dict):
            batch = _extract_jobs_from_payload(payload_post, site)
            jobs.extend(batch)
        else:
            # Fallback GET with searchText present + Workday-safe limit
            get_url = _add_or_replace_query(api_url, offset=str(offset), limit=str(limit), searchText="")
            st_get, payload_get = await _request_json(
                session, "GET", get_url, headers={k: v for k, v in headers.items() if k.lower() != "content-type"}, timeout_seconds=timeout_seconds
            )
            if st_get != 200:
                raise RuntimeError(
                    f"Workday HTTP {st_get} via GET {get_url} :: "
                    f"POST_JSON:{st_post}:{_snippet(payload_post)} | GET:{st_get}:{_snippet(payload_get)}"
                )
            batch = _extract_jobs_from_payload(payload_get, site)
            jobs.extend(batch)

        # Pagination stop conditions
        total = 0
        try:
            total = int((payload_post.get("total") if isinstance(payload_post, dict) else 0) or 0)
        except Exception:
            total = 0

        if total and offset + page_size >= total:
            break

        # If we got fewer than limit, assume done
        if len(jobs) < offset + limit:
            break

        offset += page_size

    return jobs[:max_results], cxs
