"""Extensible academic provider interfaces, built-in optional plugins, and provider registry.

Separates universal offline baseline functionality (per-guild ``ManualCatalogNotesProvider``)
from optional external campus plugins (``NotesSpaProvider``, ``RmpProfessorProvider``,
``TmuGradesZoneProvider``) behind typed provenance-carrying contracts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

import aiohttp
import aiosqlite

from icbot.academics.catalog import ManualResourceCatalogStore
from icbot.academics.models import (
    Campus,
    GradeDistribution,
    ProfessorGradeRow,
    ProfessorRecord,
    ProfessorReview,
    ProviderProvenance,
    ProviderResult,
    ResourceLink,
    is_tmu_campus,
    normalize_course_key,
    resolve_course_key,
)
from icbot.academics.url_safety import validate_fetch_url, validate_resource_url

log = logging.getLogger(__name__)

# Multi-campus course code pattern: handles CS 135, ELE888, COMP-2404, BIO120H1, HIST-2010A, etc.
_COURSE_CODE_EXTRACT_RE = re.compile(r"([A-Z]{2,5}[-\s]?\d{3,4}[A-Z0-9]{0,2})", re.IGNORECASE)
_NOTION_LINK_RE = re.compile(r'https://[^\.]+\.notion\.site/[^\s"\'\\>}]+')
_NOTES_SPA_TTL_SECONDS = 6 * 3600

_PROVIDER_EXCEPTIONS = (
    aiohttp.ClientError,
    TimeoutError,
    aiosqlite.Error,
    sqlite3.Error,
    OSError,
    LookupError,
    TypeError,
    ValueError,
    RuntimeError,
)


@asynccontextmanager
async def _scoped_http_session(session: aiohttp.ClientSession | None):
    if session is not None and not session.closed:
        yield session
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as temp_session:
        yield temp_session


# ---------------------------------------------------------------------------
# Provider Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class NotesProvider(Protocol):
    """Interface for course notes and external study resource providers."""

    @property
    def provider_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def supports_campus(self, campus: Campus) -> bool: ...

    async def search(
        self,
        course: str,
        *,
        campus: Campus,
        guild_id: int = 0,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ResourceLink]]: ...


@runtime_checkable
class ProfessorProvider(Protocol):
    """Interface for instructor rating and review providers."""

    @property
    def provider_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def supports_campus(self, campus: Campus) -> bool: ...

    async def search(
        self,
        name: str,
        institution: str,
        *,
        campus: Campus | None = None,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ProfessorRecord]]: ...


@runtime_checkable
class GradeDistributionProvider(Protocol):
    """Interface for historical course grade distribution providers."""

    @property
    def provider_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def supports_campus(self, campus: Campus) -> bool: ...

    async def get(
        self,
        course: str,
        campus: Campus,
        *,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[GradeDistribution | None]: ...


# ---------------------------------------------------------------------------
# Built-in Universal Baseline: Per-Guild Manual Resource Catalog
# ---------------------------------------------------------------------------


class ManualCatalogNotesProvider:
    """Offline universal baseline provider backed by guild-curated external resource links."""

    provider_id: str = "manual_catalog"
    display_name: str = "Guild Resource Catalog"

    def __init__(self, catalog: ManualResourceCatalogStore | None = None) -> None:
        self._catalog = catalog

    def supports_campus(self, campus: Campus) -> bool:
        # Works universally for every campus, major, and discipline with zero scrapers
        return True

    async def search(
        self,
        course: str,
        *,
        campus: Campus,
        guild_id: int = 0,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ResourceLink]]:
        now_ts = int(clock() if clock is not None else time.time())
        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=now_ts,
            attribution=f"Manual catalog ({campus.name})",
            cached=True,
        )
        if self._catalog is None:
            return ProviderResult(
                data=[],
                status="disabled",
                provenance=provenance,
                error_code="catalog_unavailable",
                error_message="Guild resource catalog store is not configured.",
            )

        aliases = getattr(settings, "academic_course_aliases", None) if settings is not None else None
        try:
            links = await self._catalog.search_resources(int(guild_id), course, aliases=aliases)
        except _PROVIDER_EXCEPTIONS as exc:
            log.warning("[ACADEMICS] Manual catalog search failed: %s", exc)
            return ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="catalog_read_error",
                error_message="Failed to query local guild resource catalog.",
            )

        return ProviderResult(
            data=links,
            status="ok" if links else "not_found",
            provenance=provenance,
        )


# ---------------------------------------------------------------------------
# Optional Plugin 1: GitHub Pages / Notion Notes SPA Provider
# ---------------------------------------------------------------------------


async def fetch_notes_spa_links(
    session: aiohttp.ClientSession,
    title: str,
    base_url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
) -> tuple[list[tuple[str, str]], str]:
    """Fetch a Notion-index SPA after strict HTTPS + SSRF URL validation.

    Returns ``(links, error_reason)`` where ``links`` is ``[(normalized_course_key, url), ...]``.
    """
    check = validate_fetch_url(base_url, allowed_hosts=allowed_hosts)
    if not check.ok:
        log.warning("[NOTES] Refusing unsafe fetch URL for %s: %s", title, check.reason)
        return [], check.reason

    safe_url = check.normalized_url
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(safe_url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return [], f"HTTP {resp.status}"
            html = await resp.text()

        raw_links = set(_NOTION_LINK_RE.findall(html))

        js_match = re.search(r'src="([^"]+/assets/index-[a-f0-9]+\.js)"', html)
        if js_match:
            js_path = js_match.group(1)
            if js_path.startswith("/"):
                parsed = urllib.parse.urlparse(safe_url)
                js_url = f"{parsed.scheme}://{parsed.netloc}{js_path}"
            else:
                js_url = f"{safe_url.rstrip('/')}/{js_path}"

            js_check = validate_fetch_url(js_url, allowed_hosts=allowed_hosts)
            if js_check.ok:
                async with session.get(js_check.normalized_url, headers=headers, timeout=timeout) as js_resp:
                    if js_resp.status == 200:
                        js_text = await js_resp.text()
                        raw_links.update(_NOTION_LINK_RE.findall(js_text))

        results: list[tuple[str, str]] = []
        for rl in raw_links:
            link_check = validate_resource_url(rl, require_https=True)
            if not link_check.ok:
                continue
            code_match = _COURSE_CODE_EXTRACT_RE.search(rl)
            if code_match:
                course_code = normalize_course_key(code_match.group(1))
                if course_code:
                    results.append((course_code, link_check.normalized_url))

        return results, ""
    except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
        log.warning("[NOTES] Error fetching from %s: %s", title, exc)
        return [], str(exc)


class NotesSpaProvider:
    """Optional plugin adapting configured ``NOTES_SITES`` SPA pages into ``NotesProvider``."""

    provider_id: str = "notes_spa"
    display_name: str = "Configured Notes Repositories"

    def __init__(self, *, cache: Any | None = None) -> None:
        self._cache = cache

    def supports_campus(self, campus: Campus) -> bool:
        return True

    async def search(
        self,
        course: str,
        *,
        campus: Campus,
        guild_id: int = 0,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ResourceLink]]:
        now = float(clock() if clock is not None else time.time())
        now_ts = int(now)

        sites_raw = getattr(settings, "notes_sites", None) if settings is not None else None
        if not sites_raw:
            return ProviderResult(
                data=[],
                status="disabled",
                provenance=ProviderProvenance(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    fetched_at=now_ts,
                ),
                error_code="no_sites_configured",
                error_message="No external NOTES_SITES configured.",
            )

        allowed_hosts = getattr(settings, "academic_allowed_hosts", None) if settings is not None else None
        aliases = getattr(settings, "academic_course_aliases", None) if settings is not None else None

        sites_key = tuple(str(s).strip() for s in sites_raw if str(s).strip())
        cached_hit = False
        all_notes: dict[str, list[tuple[str, str]]] | None = None
        if self._cache is not None:
            all_notes = self._cache.get(sites_key, now=now, ttl_seconds=_NOTES_SPA_TTL_SECONDS)
            if all_notes is not None:
                cached_hit = True

        unsafe_errors: list[str] = []
        fetch_errors: list[str] = []

        if all_notes is None:
            parsed_sites: list[tuple[str, str]] = []
            for entry in sites_raw:
                parts = str(entry).split("|", 1)
                if len(parts) == 2:
                    t = parts[0].strip()
                    u = parts[1].strip().strip('"').strip("'")
                    if t and u:
                        parsed_sites.append((t, u))

            all_notes = {}
            if parsed_sites:
                async with _scoped_http_session(session) as http_session:
                    tasks = [
                        fetch_notes_spa_links(http_session, t, u, allowed_hosts=allowed_hosts)
                        for t, u in parsed_sites
                    ]
                    gathered = await asyncio.gather(*tasks, return_exceptions=True)

                    for (title, _url), res in zip(parsed_sites, gathered, strict=True):
                        if isinstance(res, BaseException):
                            if isinstance(res, asyncio.CancelledError):
                                raise res
                            fetch_errors.append(f"{title}: {res}")
                            continue
                        links_list, err = res
                        if err:
                            if (
                                "allowlist" in err.lower()
                                or "scheme" in err.lower()
                                or "private" in err.lower()
                            ):
                                unsafe_errors.append(f"{title}: {err}")
                            else:
                                fetch_errors.append(f"{title}: {err}")
                        elif links_list:
                            all_notes[title] = links_list

            if self._cache is not None and all_notes:
                self._cache.set(sites_key, now=now, data=all_notes)

        raw_norm = normalize_course_key(course)
        resolved = resolve_course_key(course, aliases)
        target_keys = {k for k in (raw_norm, resolved) if k}

        matched_links: list[ResourceLink] = []
        for site_title, pairs in (all_notes or {}).items():
            exact_urls = [url for code, url in pairs if code in target_keys]
            chosen_urls = (
                exact_urls
                if exact_urls
                else [url for code, url in pairs if any(tk in code for tk in target_keys if len(tk) >= 3)]
            )
            for idx, url in enumerate(dict.fromkeys(chosen_urls)):
                matched_links.append(
                    ResourceLink(
                        resource_id=f"notes_spa:{site_title}:{idx}",
                        guild_id=int(guild_id),
                        course_key=resolved or raw_norm,
                        display_code=course.strip().upper(),
                        title=site_title,
                        url=url,
                        resource_type="notes",
                        source_attribution=site_title,
                        license_note="External Notion notes link — rights remain with original author.",
                        created_at=now_ts,
                    )
                )

        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=now_ts,
            attribution=", ".join(sorted((all_notes or {}).keys())) or self.display_name,
            cached=cached_hit,
        )

        if matched_links:
            return ProviderResult(data=matched_links, status="ok", provenance=provenance)
        if unsafe_errors and not all_notes:
            return ProviderResult(
                data=[],
                status="unsafe_url",
                provenance=provenance,
                error_code="unsafe_source_url",
                error_message="; ".join(unsafe_errors),
            )
        if fetch_errors and not all_notes:
            return ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="fetch_failed",
                error_message="; ".join(fetch_errors),
            )
        return ProviderResult(data=[], status="not_found", provenance=provenance)


# ---------------------------------------------------------------------------
# Optional Plugin 2: RateMyProfessors GraphQL Provider
# ---------------------------------------------------------------------------


class RmpProfessorProvider:
    """Optional professor rating plugin backed by RateMyProfessors GraphQL."""

    provider_id: str = "rmp_graphql"
    display_name: str = "RateMyProfessors"

    def supports_campus(self, campus: Campus) -> bool:
        return bool(campus.name)

    async def search(
        self,
        name: str,
        institution: str,
        *,
        campus: Campus | None = None,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ProfessorRecord]]:
        from icbot.features.prof import RMP_GRAPHQL_URL, get_school_id, search_prof

        now_ts = int(clock() if clock is not None else time.time())
        inst = str(institution or (campus.name if campus is not None else "")).strip()
        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=now_ts,
            source_url=RMP_GRAPHQL_URL,
            attribution=f"RateMyProfessors ({inst})",
            cached=False,
        )

        url_check = validate_fetch_url(RMP_GRAPHQL_URL)
        if not url_check.ok:
            return ProviderResult(
                data=[],
                status="unsafe_url",
                provenance=provenance,
                error_code="unsafe_endpoint",
                error_message=url_check.reason,
            )

        if not inst or not str(name or "").strip():
            return ProviderResult(
                data=[],
                status="not_found",
                provenance=provenance,
                error_code="invalid_query",
                error_message="Professor name and institution are required.",
            )

        try:
            async with _scoped_http_session(session) as http_session:
                school_id = await get_school_id(http_session, inst)
                if not school_id:
                    return ProviderResult(
                        data=[],
                        status="not_found",
                        provenance=provenance,
                        error_code="institution_not_found",
                        error_message=f"Could not locate institution ID for '{inst}'.",
                    )
                raw_profs = await search_prof(http_session, name, school_id)
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            log.warning("[RMP] Provider search failed: %s", exc)
            return ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="rmp_fetch_error",
                error_message=str(exc),
            )

        if not raw_profs:
            return ProviderResult(
                data=[],
                status="not_found",
                provenance=provenance,
                error_code="professor_not_found",
                error_message=f"No professor matching '{name}' at {inst}.",
            )

        records: list[ProfessorRecord] = []
        for raw in raw_profs:
            full_name = f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip()
            tags_raw = raw.get("teacherRatingTags") or []
            tags_sorted = sorted(tags_raw, key=lambda x: x.get("tagCount", 0), reverse=True)
            top_tags = tuple(str(t.get("tagName", "")).strip() for t in tags_sorted[:5] if t.get("tagName"))

            reviews: list[ProfessorReview] = []
            for edge in (raw.get("ratings") or {}).get("edges", []):
                node = edge.get("node") or {}
                reviews.append(
                    ProfessorReview(
                        course_code=str(node.get("class", "Unknown") or "Unknown"),
                        helpful_rating=str(node.get("helpfulRating", "?")),
                        difficulty_rating=str(node.get("difficultyRating", "?")),
                        comment=str(node.get("comment", "") or ""),
                    )
                )

            prof_id = str(raw.get("id", "") or "")
            profile_url = f"https://www.ratemyprofessors.com/professor/{prof_id}" if prof_id else ""

            records.append(
                ProfessorRecord(
                    provider_prof_id=prof_id,
                    full_name=full_name,
                    institution=inst,
                    department=str(raw.get("department", "Unknown Department") or "Unknown Department"),
                    avg_rating=float(raw.get("avgRating", 0.0) or 0.0),
                    num_ratings=int(raw.get("numRatings", 0) or 0),
                    avg_difficulty=float(raw.get("avgDifficulty", 0.0) or 0.0),
                    would_take_again_pct=float(raw.get("wouldTakeAgainPercent", -1) or -1),
                    top_tags=top_tags,
                    recent_reviews=tuple(reviews),
                    profile_url=profile_url,
                    raw_payload=dict(raw),
                )
            )

        return ProviderResult(data=records, status="ok", provenance=provenance)


# ---------------------------------------------------------------------------
# Optional Plugin 3: TMU Grades Zone Provider (strictly campus-scoped to TMU)
# ---------------------------------------------------------------------------


class TmuGradesZoneProvider:
    """Optional grade distribution plugin strictly scoped to Toronto Metropolitan University."""

    provider_id: str = "tmu_grades_zone"
    display_name: str = "TMU Grades Zone"
    _BASE_HOST: str = "tmu.grades.zone"

    def __init__(self, *, store: Any | None = None, db_path: str | None = None) -> None:
        self._store = store
        self._db_path = db_path

    def supports_campus(self, campus: Campus) -> bool:
        # Never claim coverage for non-TMU institutions
        return is_tmu_campus(campus)

    async def get(
        self,
        course: str,
        campus: Campus,
        *,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[GradeDistribution | None]:
        from icbot.features import grades as grades_mod

        now_ts = int(clock() if clock is not None else time.time())
        aliases = getattr(settings, "academic_course_aliases", None) if settings is not None else None
        resolved_key = resolve_course_key(course, aliases)
        source_url = (
            f"https://{self._BASE_HOST}/course/{urllib.parse.quote(resolved_key.lower())}"
            if resolved_key
            else ""
        )
        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=now_ts,
            source_url=source_url,
            attribution="TMU Grades Zone (tmu.grades.zone)",
            cached=False,
        )

        if not self.supports_campus(campus):
            return ProviderResult(
                data=None,
                status="unsupported_campus",
                provenance=provenance,
                error_code="unsupported_campus",
                error_message=(
                    f"TMU Grades Zone only provides historical distributions for Toronto Metropolitan "
                    f"University (current campus: {campus.name})."
                ),
            )

        if not resolved_key:
            return ProviderResult(
                data=None,
                status="not_found",
                provenance=provenance,
                error_code="invalid_course",
                error_message="Course code must contain alphanumeric characters.",
            )

        url_check = validate_fetch_url(source_url, allowed_hosts=(self._BASE_HOST,))
        if not url_check.ok:
            return ProviderResult(
                data=None,
                status="unsafe_url",
                provenance=provenance,
                error_code="unsafe_url",
                error_message=url_check.reason,
            )

        eff_db_path = self._db_path
        if eff_db_path is None and self._store is None:
            if grades_mod.DB_PATH != "tmu_grades.db":
                eff_db_path = grades_mod.DB_PATH
            elif settings is not None:
                eff_db_path = getattr(settings, "grades_db_path", grades_mod.DB_PATH)

        try:
            raw = await grades_mod.fetch_course_data(
                resolved_key.lower(),
                db_path=eff_db_path if self._store is None else None,
                session=session,
                store=self._store,
            )
        except _PROVIDER_EXCEPTIONS as exc:
            log.warning("[GRADES] TMU Grades Zone fetch failed for %s: %s", resolved_key, exc)
            return ProviderResult(
                data=None,
                status="error",
                provenance=provenance,
                error_code="grades_fetch_error",
                error_message=str(exc),
            )

        if raw is None:
            return ProviderResult(
                data=None,
                status="not_found",
                provenance=provenance,
                error_code="course_not_found",
                error_message=f"Course '{course}' was not found on TMU Grades Zone.",
            )

        profs = tuple(
            ProfessorGradeRow(
                name=str(p.get("name", "")),
                term=str(p.get("term", "")),
                avg=str(p.get("avg", "N/A")),
                common=str(p.get("common", "N/A")),
                students=str(p.get("students", "N/A")),
                distribution=tuple(p.get("dist") or ()),
            )
            for p in (raw.get("professors") or [])
        )

        dist = GradeDistribution(
            course_key=resolved_key,
            course_title=str(raw.get("title", resolved_key)),
            campus_id=campus.campus_id,
            professors=profs,
            source_url=source_url,
        )
        return ProviderResult(data=dist, status="ok", provenance=provenance)


# ---------------------------------------------------------------------------
# Academic Provider Registry
# ---------------------------------------------------------------------------


class AcademicProviderRegistry:
    """Central registry orchestrating offline baseline catalog and optional campus plugins.

    Guarantees that failures in optional remote providers are isolated and never
    prevent offline catalog searches or exam schedule commands from functioning.
    """

    def __init__(self) -> None:
        self._notes_providers: dict[str, NotesProvider] = {}
        self._professor_providers: dict[str, ProfessorProvider] = {}
        self._grade_providers: dict[str, GradeDistributionProvider] = {}

    def register_notes_provider(self, provider: NotesProvider) -> None:
        self._notes_providers[provider.provider_id] = provider

    def register_professor_provider(self, provider: ProfessorProvider) -> None:
        self._professor_providers[provider.provider_id] = provider

    def register_grade_provider(self, provider: GradeDistributionProvider) -> None:
        self._grade_providers[provider.provider_id] = provider

    @property
    def notes_provider_ids(self) -> tuple[str, ...]:
        return tuple(self._notes_providers.keys())

    @property
    def professor_provider_ids(self) -> tuple[str, ...]:
        return tuple(self._professor_providers.keys())

    @property
    def grade_provider_ids(self) -> tuple[str, ...]:
        return tuple(self._grade_providers.keys())

    @staticmethod
    def _is_provider_enabled(provider_id: str, settings: Any | None) -> bool:
        if settings is None:
            return True
        enabled_list = getattr(settings, "academic_enabled_providers", None)
        if not enabled_list:
            return True
        normalized = {str(p).strip().lower() for p in enabled_list if str(p).strip()}
        if not normalized:
            return True
        # Manual catalog is always kept enabled unless explicitly excluded via empty custom registry
        return provider_id.lower() in normalized or provider_id == "manual_catalog"

    async def search_notes(
        self,
        course: str,
        *,
        campus: Campus,
        guild_id: int = 0,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> list[ProviderResult[list[ResourceLink]]]:
        """Run all compatible enabled notes providers with strict failure isolation."""
        results: list[ProviderResult[list[ResourceLink]]] = []
        now_ts = int(clock() if clock is not None else time.time())

        for pid, provider in self._notes_providers.items():
            if not self._is_provider_enabled(pid, settings):
                continue
            if not provider.supports_campus(campus):
                continue
            try:
                res = await provider.search(
                    course,
                    campus=campus,
                    guild_id=guild_id,
                    settings=settings,
                    session=session,
                    clock=clock,
                )
            except _PROVIDER_EXCEPTIONS as exc:
                log.warning("[ACADEMICS] Isolated notes provider error (%s): %s", pid, exc)
                res = ProviderResult(
                    data=[],
                    status="error",
                    provenance=ProviderProvenance(
                        provider_id=pid,
                        provider_name=provider.display_name,
                        fetched_at=now_ts,
                    ),
                    error_code="provider_exception",
                    error_message=str(exc),
                )
            results.append(res)
        return results

    async def search_professors(
        self,
        name: str,
        *,
        campus: Campus,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[list[ProfessorRecord]]:
        """Query the first compatible professor provider for ``campus``."""
        now_ts = int(clock() if clock is not None else time.time())
        last_result: ProviderResult[list[ProfessorRecord]] | None = None

        for pid, provider in self._professor_providers.items():
            if not self._is_provider_enabled(pid, settings):
                continue
            if not provider.supports_campus(campus):
                continue
            try:
                res = await provider.search(
                    name,
                    campus.name,
                    campus=campus,
                    settings=settings,
                    session=session,
                    clock=clock,
                )
            except _PROVIDER_EXCEPTIONS as exc:
                log.warning("[ACADEMICS] Isolated professor provider error (%s): %s", pid, exc)
                res = ProviderResult(
                    data=[],
                    status="error",
                    provenance=ProviderProvenance(
                        provider_id=pid,
                        provider_name=provider.display_name,
                        fetched_at=now_ts,
                    ),
                    error_code="provider_exception",
                    error_message=str(exc),
                )
            if res.ok:
                return res
            last_result = res

        if last_result is not None:
            return last_result

        return ProviderResult(
            data=[],
            status="unsupported_campus",
            provenance=ProviderProvenance(
                provider_id="none",
                provider_name="Academic Registry",
                fetched_at=now_ts,
            ),
            error_code="no_professor_provider",
            error_message=f"No professor rating provider is available for {campus.name}.",
        )

    async def get_grade_distribution(
        self,
        course: str,
        *,
        campus: Campus,
        settings: Any | None = None,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ) -> ProviderResult[GradeDistribution | None]:
        """Query the first compatible grade distribution provider for ``campus``."""
        now_ts = int(clock() if clock is not None else time.time())
        last_result: ProviderResult[GradeDistribution | None] | None = None

        for pid, provider in self._grade_providers.items():
            if not self._is_provider_enabled(pid, settings):
                continue
            if not provider.supports_campus(campus):
                continue
            try:
                res = await provider.get(
                    course,
                    campus,
                    settings=settings,
                    session=session,
                    clock=clock,
                )
            except _PROVIDER_EXCEPTIONS as exc:
                log.warning("[ACADEMICS] Isolated grade provider error (%s): %s", pid, exc)
                res = ProviderResult(
                    data=None,
                    status="error",
                    provenance=ProviderProvenance(
                        provider_id=pid,
                        provider_name=provider.display_name,
                        fetched_at=now_ts,
                    ),
                    error_code="provider_exception",
                    error_message=str(exc),
                )
            if res.ok:
                return res
            last_result = res

        if last_result is not None:
            return last_result

        return ProviderResult(
            data=None,
            status="unsupported_campus",
            provenance=ProviderProvenance(
                provider_id="none",
                provider_name="Academic Registry",
                fetched_at=now_ts,
            ),
            error_code="unsupported_campus",
            error_message=(
                f"No grade distribution provider is configured for {campus.name}. "
                "Historical grade distributions are only available for campuses with an enabled data plugin."
            ),
        )


def build_default_academic_registry(
    *,
    catalog: ManualResourceCatalogStore | None = None,
    notes_cache: Any | None = None,
    grades_store: Any | None = None,
    grades_db_path: str | None = None,
) -> AcademicProviderRegistry:
    """Assemble the default academic provider registry with manual catalog + optional plugins."""
    reg = AcademicProviderRegistry()
    # 1. Universal offline baseline first
    reg.register_notes_provider(ManualCatalogNotesProvider(catalog=catalog))
    # 2. Optional remote plugins
    reg.register_notes_provider(NotesSpaProvider(cache=notes_cache))
    reg.register_professor_provider(RmpProfessorProvider())
    reg.register_grade_provider(TmuGradesZoneProvider(store=grades_store, db_path=grades_db_path))
    return reg
