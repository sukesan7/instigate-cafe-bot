"""Extensible academic provider interfaces, built-in optional plugins, and provider registry.

Separates universal offline baseline functionality (per-guild ``ManualCatalogNotesProvider``)
from optional external campus plugins (``NotesSpaProvider``, ``RmpProfessorProvider``,
``TmuGradesZoneProvider``) behind typed provenance-carrying contracts.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

import aiohttp
import aiosqlite

from icbot.academics.cache import (
    DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
    DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
    AcademicProviderCache,
    CacheKey,
)
from icbot.academics.catalog import ManualResourceCatalogStore
from icbot.academics.models import (
    Campus,
    GradeDistribution,
    ProfessorGradeRow,
    ProfessorRecord,
    ProviderProvenance,
    ProviderResult,
    ResourceLink,
    is_tmu_campus,
    normalize_course_key,
    resolve_course_key,
)
from icbot.academics.parsers import (
    PayloadLimitError,
    SchemaDriftError,
    parse_notes_spa_bundle,
    parse_notes_spa_html,
    parse_rmp_prof_response,
    parse_rmp_school_response,
)
from icbot.academics.url_safety import validate_fetch_url

log = logging.getLogger(__name__)

_NOTES_SPA_TTL_SECONDS = 6 * 3600
_RMP_SCHOOL_TTL_SECONDS = 12 * 3600  # Conservative 12h school ID cache
_RMP_PROF_TTL_SECONDS = 6 * 3600  # Conservative 6h professor rating cache
_GRADES_MEMORY_TTL_SECONDS = 6 * 3600

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
    """Fetch a Notion-index SPA after strict HTTPS + SSRF validation using bounded parsers.

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

        parsed_html = parse_notes_spa_html(html, base_url=safe_url, allowed_hosts=allowed_hosts)
        results: list[tuple[str, str]] = list(parsed_html.links)
        seen: set[tuple[str, str]] = set(results)

        if parsed_html.bundle_url:
            async with session.get(parsed_html.bundle_url, headers=headers, timeout=timeout) as js_resp:
                if js_resp.status == 200:
                    js_text = await js_resp.text()
                    js_pairs, _trunc = parse_notes_spa_bundle(js_text)
                    for pair in js_pairs:
                        if pair not in seen:
                            seen.add(pair)
                            results.append(pair)

        return results, ""
    except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError, PayloadLimitError, ValueError) as exc:
        log.warning("[NOTES] Error fetching from %s: %s", title, exc)
        return [], str(exc)


class NotesSpaProvider:
    """Optional plugin adapting configured ``NOTES_SITES`` SPA pages into ``NotesProvider``."""

    provider_id: str = "notes_spa"
    display_name: str = "Configured Notes Repositories"

    def __init__(
        self,
        *,
        cache: Any | None = None,
        provider_cache: AcademicProviderCache | None = None,
    ) -> None:
        self._cache = cache
        self._provider_cache = provider_cache

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
        site_cache_key = CacheKey.make(self.provider_id, campus.campus_id, "|".join(sites_key))
        cached_hit = False
        fetched_ts = now_ts
        all_notes: dict[str, list[tuple[str, str]]] | None = None

        # 1. Check unified AcademicProviderCache (supports positive + negative caching + fetched_at)
        if self._provider_cache is not None:
            pc_hit = self._provider_cache.get(site_cache_key, now=now)
            if pc_hit is not None:
                pc_val, pc_stored, pc_neg = pc_hit
                fetched_ts = int(pc_stored)
                cached_hit = True
                if pc_neg and isinstance(pc_val, ProviderResult):
                    cached_prov = ProviderProvenance(
                        provider_id=pc_val.provenance.provider_id,
                        provider_name=pc_val.provenance.provider_name,
                        fetched_at=fetched_ts,
                        attribution=pc_val.provenance.attribution,
                        cached=True,
                    )
                    return ProviderResult(
                        data=[],
                        status=pc_val.status,
                        provenance=cached_prov,
                        error_code=pc_val.error_code,
                        error_message=pc_val.error_message,
                    )
                if isinstance(pc_val, dict):
                    all_notes = pc_val

        # 2. Check legacy NotesCache for backwards-compatible cache partitioning
        if all_notes is None and self._cache is not None:
            legacy_hit = self._cache.get(sites_key, now=now, ttl_seconds=_NOTES_SPA_TTL_SECONDS)
            if legacy_hit is not None:
                all_notes = legacy_hit
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

            if all_notes:
                if self._cache is not None:
                    self._cache.set(sites_key, now=now, data=all_notes)
                if self._provider_cache is not None:
                    self._provider_cache.set(
                        site_cache_key,
                        all_notes,
                        ttl_seconds=_NOTES_SPA_TTL_SECONDS,
                        now=now,
                        is_negative=False,
                    )

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
                        created_at=fetched_ts,
                    )
                )

        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=fetched_ts,
            attribution=", ".join(sorted((all_notes or {}).keys())) or self.display_name,
            cached=cached_hit,
        )

        if matched_links:
            return ProviderResult(data=matched_links, status="ok", provenance=provenance)
        if unsafe_errors and not all_notes:
            res_unsafe: ProviderResult[list[ResourceLink]] = ProviderResult(
                data=[],
                status="unsafe_url",
                provenance=provenance,
                error_code="unsafe_source_url",
                error_message="; ".join(unsafe_errors),
            )
            return res_unsafe
        if fetch_errors and not all_notes:
            res_err: ProviderResult[list[ResourceLink]] = ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="fetch_failed",
                error_message="; ".join(fetch_errors),
            )
            if self._provider_cache is not None and not cached_hit:
                self._provider_cache.set(
                    site_cache_key,
                    res_err,
                    ttl_seconds=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                    now=now,
                    is_negative=True,
                )
            return res_err
        return ProviderResult(data=[], status="not_found", provenance=provenance)


# ---------------------------------------------------------------------------
# Optional Plugin 2: RateMyProfessors GraphQL Provider
# ---------------------------------------------------------------------------


class RmpProfessorProvider:
    """Optional professor rating plugin backed by RateMyProfessors' unofficial GraphQL API.

    Uses conservative positive and negative caching and defensive schema-drift parsing so
    upstream changes never crash or impact core academic features.
    """

    provider_id: str = "rmp_graphql"
    display_name: str = "RateMyProfessors (Unofficial GraphQL)"

    def __init__(self, *, provider_cache: AcademicProviderCache | None = None) -> None:
        self._provider_cache = provider_cache

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
        from icbot.features.prof import (
            PROF_SEARCH_QUERY,
            RMP_GRAPHQL_URL,
            SCHOOL_SEARCH_QUERY,
            fetch_graphql,
        )

        now = float(clock() if clock is not None else time.time())
        now_ts = int(now)
        inst = str(institution or (campus.name if campus is not None else "")).strip()
        campus_id = campus.campus_id if campus is not None else inst.lower()
        norm_query = " ".join(str(name or "").strip().lower().split())

        provenance = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=now_ts,
            source_url=RMP_GRAPHQL_URL,
            attribution=f"RateMyProfessors — Unofficial GraphQL ({inst})",
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

        if not inst or not norm_query:
            return ProviderResult(
                data=[],
                status="not_found",
                provenance=provenance,
                error_code="invalid_query",
                error_message="Professor name and institution are required.",
            )

        prof_cache_key = CacheKey.make(self.provider_id, campus_id, norm_query)
        if self._provider_cache is not None:
            cached_res = self._provider_cache.get_provider_result(prof_cache_key, now=now)
            if cached_res is not None:
                return cached_res

        school_cache_key = CacheKey.make("rmp_school", campus_id, inst.lower())

        try:
            async with _scoped_http_session(session) as http_session:
                school_id: str | None = None
                school_from_cache = False
                if self._provider_cache is not None:
                    s_hit = self._provider_cache.get(school_cache_key, now=now)
                    if s_hit is not None:
                        school_id = s_hit[0]
                        school_from_cache = True

                if not school_from_cache:
                    school_payload = await fetch_graphql(
                        http_session, SCHOOL_SEARCH_QUERY, {"query": {"text": inst}}
                    )
                    if school_payload is None:
                        err_res: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                            data=[],
                            status="error",
                            provenance=provenance,
                            error_code="rmp_http_error",
                            error_message="RateMyProfessors GraphQL request failed or timed out.",
                        )
                        if self._provider_cache is not None:
                            self._provider_cache.set_provider_result(
                                prof_cache_key,
                                err_res,
                                error_ttl=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                                now=now,
                            )
                        return err_res
                    school_id = parse_rmp_school_response(school_payload)
                    if self._provider_cache is not None:
                        self._provider_cache.set(
                            school_cache_key,
                            school_id,
                            ttl_seconds=_RMP_SCHOOL_TTL_SECONDS
                            if school_id
                            else DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
                            now=now,
                            is_negative=school_id is None,
                        )

                if not school_id:
                    nf_inst: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                        data=[],
                        status="not_found",
                        provenance=provenance,
                        error_code="institution_not_found",
                        error_message=f"Could not locate institution ID for '{inst}'.",
                    )
                    if self._provider_cache is not None:
                        self._provider_cache.set_provider_result(
                            prof_cache_key,
                            nf_inst,
                            not_found_ttl=DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
                            now=now,
                        )
                    return nf_inst

                prof_payload = await fetch_graphql(
                    http_session,
                    PROF_SEARCH_QUERY,
                    {"query": {"text": name.strip(), "schoolID": school_id}},
                )
                if prof_payload is None:
                    err_prof: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                        data=[],
                        status="error",
                        provenance=provenance,
                        error_code="rmp_http_error",
                        error_message="RateMyProfessors professor query failed or timed out.",
                    )
                    if self._provider_cache is not None:
                        self._provider_cache.set_provider_result(
                            prof_cache_key,
                            err_prof,
                            error_ttl=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                            now=now,
                        )
                    return err_prof

                records = parse_rmp_prof_response(prof_payload, institution=inst)

        except SchemaDriftError as exc:
            log.warning("[RMP] Schema drift detected: %s", exc)
            drift_res: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="schema_drift",
                error_message=f"Upstream RMP schema changed unexpectedly: {exc}",
            )
            if self._provider_cache is not None:
                self._provider_cache.set_provider_result(
                    prof_cache_key,
                    drift_res,
                    error_ttl=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                    now=now,
                )
            return drift_res
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            log.warning("[RMP] Provider search failed: %s", exc)
            fail_res: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                data=[],
                status="error",
                provenance=provenance,
                error_code="rmp_fetch_error",
                error_message=str(exc),
            )
            if self._provider_cache is not None:
                self._provider_cache.set_provider_result(
                    prof_cache_key,
                    fail_res,
                    error_ttl=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                    now=now,
                )
            return fail_res

        if not records:
            nf_prof: ProviderResult[list[ProfessorRecord]] = ProviderResult(
                data=[],
                status="not_found",
                provenance=provenance,
                error_code="professor_not_found",
                error_message=f"No professor matching '{name}' at {inst}.",
            )
            if self._provider_cache is not None:
                self._provider_cache.set_provider_result(
                    prof_cache_key,
                    nf_prof,
                    not_found_ttl=DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
                    now=now,
                )
            return nf_prof

        ok_res: ProviderResult[list[ProfessorRecord]] = ProviderResult(
            data=records, status="ok", provenance=provenance
        )
        if self._provider_cache is not None:
            self._provider_cache.set_provider_result(
                prof_cache_key,
                ok_res,
                positive_ttl=_RMP_PROF_TTL_SECONDS,
                now=now,
            )
        return ok_res


# ---------------------------------------------------------------------------
# Optional Plugin 3: TMU Grades Zone Provider (strictly campus-scoped to TMU)
# ---------------------------------------------------------------------------


class TmuGradesZoneProvider:
    """Optional grade distribution plugin strictly scoped to Toronto Metropolitan University."""

    provider_id: str = "tmu_grades_zone"
    display_name: str = "TMU Grades Zone"
    _BASE_HOST: str = "tmu.grades.zone"

    def __init__(
        self,
        *,
        store: Any | None = None,
        db_path: str | None = None,
        provider_cache: AcademicProviderCache | None = None,
    ) -> None:
        self._store = store
        self._db_path = db_path
        self._provider_cache = provider_cache

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

        now = float(clock() if clock is not None else time.time())
        now_ts = int(now)
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

        cache_key = CacheKey.make(self.provider_id, campus.campus_id, resolved_key)
        if self._provider_cache is not None:
            cached_res = self._provider_cache.get_provider_result(cache_key, now=now)
            if cached_res is not None:
                return cached_res

        # Respect injected store or configured settings.grades_db_path (no hardcoded tmu_grades.db!)
        eff_store = self._store
        eff_db_path = (self._db_path or "").strip() or None
        if grades_mod.DB_PATH != grades_mod.DEFAULT_GRADES_DB_PATH:
            # Explicit monkeypatch override in isolated test harnesses
            eff_store = None
            eff_db_path = grades_mod.DB_PATH
        elif eff_store is None and eff_db_path is None and settings is not None:
            eff_db_path = (
                (getattr(settings, "grades_db_path", "") or "").strip()
                or (getattr(settings, "db_path", "") or "").strip()
                or None
            )

        try:
            raw, fetched_at, from_sqlite_cache = await grades_mod.fetch_course_data_with_meta(
                resolved_key.lower(),
                db_path=eff_db_path if eff_store is None else None,
                session=session,
                store=eff_store,
                clock=clock,
            )
        except (PayloadLimitError, *_PROVIDER_EXCEPTIONS) as exc:
            log.warning("[GRADES] TMU Grades Zone fetch failed for %s: %s", resolved_key, exc)
            err_res: ProviderResult[GradeDistribution | None] = ProviderResult(
                data=None,
                status="error",
                provenance=provenance,
                error_code="grades_timeout" if isinstance(exc, TimeoutError) else "grades_fetch_error",
                error_message=str(exc),
            )
            if self._provider_cache is not None:
                self._provider_cache.set_provider_result(
                    cache_key,
                    err_res,
                    error_ttl=DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
                    now=now,
                )
            return err_res

        if raw is None:
            nf_res: ProviderResult[GradeDistribution | None] = ProviderResult(
                data=None,
                status="not_found",
                provenance=provenance,
                error_code="course_not_found",
                error_message=f"Course '{course}' was not found on TMU Grades Zone.",
            )
            if self._provider_cache is not None:
                self._provider_cache.set_provider_result(
                    cache_key,
                    nf_res,
                    not_found_ttl=DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
                    now=now,
                )
            return nf_res

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
        final_prov = ProviderProvenance(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            fetched_at=int(fetched_at or now_ts),
            source_url=source_url,
            attribution="TMU Grades Zone (tmu.grades.zone)",
            cached=bool(from_sqlite_cache),
        )
        ok_res: ProviderResult[GradeDistribution | None] = ProviderResult(
            data=dist, status="ok", provenance=final_prov
        )
        if self._provider_cache is not None:
            self._provider_cache.set_provider_result(
                cache_key,
                ok_res,
                positive_ttl=_GRADES_MEMORY_TTL_SECONDS,
                now=now,
            )
        return ok_res


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
    provider_cache: AcademicProviderCache | None = None,
    grades_store: Any | None = None,
    grades_db_path: str | None = None,
) -> AcademicProviderRegistry:
    """Assemble the default academic provider registry with manual catalog + optional plugins."""
    eff_cache = provider_cache if provider_cache is not None else AcademicProviderCache()
    reg = AcademicProviderRegistry()
    # 1. Universal offline baseline first
    reg.register_notes_provider(ManualCatalogNotesProvider(catalog=catalog))
    # 2. Optional remote plugins with unified provider+campus+query TTL & negative cache
    reg.register_notes_provider(NotesSpaProvider(cache=notes_cache, provider_cache=eff_cache))
    reg.register_professor_provider(RmpProfessorProvider(provider_cache=eff_cache))
    reg.register_grade_provider(
        TmuGradesZoneProvider(store=grades_store, db_path=grades_db_path, provider_cache=eff_cache)
    )
    return reg
