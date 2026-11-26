from __future__ import annotations

import io
import json
import logging
import sqlite3
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import aiohttp
import aiosqlite
import discord
from discord import app_commands

from icbot.academics.cache import format_freshness_line
from icbot.academics.charts import (
    ChartRenderError,
    build_accessible_grade_summary,
    render_grade_distribution_chart,
    render_grade_distribution_chart_async,
)
from icbot.academics.models import campus_from_settings, normalize_course_key
from icbot.academics.parsers import MAX_GRADES_HTML_BYTES, parse_tmu_grades_html
from icbot.academics.url_safety import (
    create_ssrf_safe_connector,
    read_response_text_limited,
    request_with_safe_redirects,
    validate_fetch_url,
)
from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.db import Database
from icbot.embeds import system as system_embed
from icbot.errors import format_exception_safe, redact_secrets

log = logging.getLogger(__name__)

# Legacy sentinel kept only for backwards-compatible test monkeypatching;
# production calls inject ServiceContainer.grades_cache / settings.grades_db_path.
DEFAULT_GRADES_DB_PATH = "tmu_grades.db"
DB_PATH = DEFAULT_GRADES_DB_PATH
_DEFAULT_SQLITE_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days persistent cache TTL
_TMU_GRADES_HOST = "tmu.grades.zone"


class GradesCacheStore:
    """Configurable SQLite cache store for historical course grade distribution data."""

    def __init__(self, db: Database | str) -> None:
        if isinstance(db, Database):
            self._db = db
            self._owns_db = False
        elif isinstance(db, str):
            if not db.strip():
                raise ValueError("GradesCacheStore db path must not be empty.")
            self._db = Database(db)
            self._owns_db = True
        else:
            raise TypeError(f"Unsupported db type: {type(db)}")

    @property
    def db(self) -> Database:
        return self._db

    async def init(self) -> None:
        """Initialize the underlying database and ensure grades cache tables exist."""
        await self._db.init()
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS grades_course_cache (
                course_code TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                parsed_data TEXT NOT NULL DEFAULT '[]',
                last_updated INTEGER NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS course_cache (
                course_code TEXT PRIMARY KEY,
                title TEXT,
                parsed_data JSON,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    async def get_course_with_meta(
        self,
        course_code: str,
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> tuple[dict[str, Any], int] | None:
        """Return ``(payload, last_updated_ts)`` when cached and fresh, else ``None``."""
        code = course_code.lower().strip()
        ref_now = float(now if now is not None else time.time())

        try:
            row = await self._db.fetchone(
                "SELECT title, parsed_data, last_updated FROM grades_course_cache WHERE course_code = ?",
                (code,),
            )
            if row:
                last_updated = int(row[2] or 0)
                if ttl_seconds is not None and last_updated > 0 and (ref_now - last_updated) >= ttl_seconds:
                    return None
                return {"title": str(row[0]), "professors": json.loads(str(row[1]))}, last_updated
        except (aiosqlite.Error, sqlite3.Error, json.JSONDecodeError, OSError, ValueError) as exc:
            log.debug("Grades cache read error on namespaced table: %s", exc)

        # Fallback to legacy un-namespaced table if present
        try:
            row = await self._db.fetchone(
                "SELECT title, parsed_data FROM course_cache WHERE course_code = ?",
                (code,),
            )
            if row:
                return {"title": str(row[0]), "professors": json.loads(str(row[1]))}, int(ref_now)
        except (aiosqlite.Error, sqlite3.Error, json.JSONDecodeError, OSError, ValueError) as exc:
            log.debug("Grades cache read error on legacy table: %s", exc)

        return None

    async def get_course(self, course_code: str) -> dict[str, Any] | None:
        hit = await self.get_course_with_meta(course_code)
        return hit[0] if hit is not None else None

    async def set_course(
        self,
        course_code: str,
        title: str,
        professors: list[dict[str, Any]],
        *,
        fetched_at: int | None = None,
    ) -> int:
        code = course_code.lower().strip()
        payload = json.dumps(professors)
        now_ts = int(fetched_at if fetched_at is not None else time.time())

        await self._db.execute(
            """
            INSERT OR REPLACE INTO grades_course_cache (course_code, title, parsed_data, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (code, title, payload, now_ts),
        )
        try:
            await self._db.execute(
                """
                INSERT OR REPLACE INTO course_cache (course_code, title, parsed_data)
                VALUES (?, ?, ?)
                """,
                (code, title, payload),
            )
        except (aiosqlite.Error, sqlite3.Error, json.JSONDecodeError, OSError, ValueError) as exc:
            log.debug("Skipping legacy course_cache mirror: %s", exc)
        return now_ts

    async def close(self) -> None:
        if self._owns_db:
            await self._db.close()


_STORES: dict[str, GradesCacheStore] = {}


async def _init_db(db_path: str | None = None) -> GradesCacheStore:
    eff = db_path or DB_PATH
    if eff not in _STORES or _STORES[eff].db.is_closed:
        store = GradesCacheStore(eff)
        await store.init()
        _STORES[eff] = store
    return _STORES[eff]


async def _fetch_grades_html(url: str, headers: dict[str, str], session: aiohttp.ClientSession) -> str | None:
    check = validate_fetch_url(url, allowed_hosts=(_TMU_GRADES_HOST,))
    if not check.ok:
        raise ValueError(f"Unsafe grades fetch URL rejected: {check.reason}")
    timeout = aiohttp.ClientTimeout(total=15)
    async with request_with_safe_redirects(
        session,
        "GET",
        check.normalized_url,
        allowed_hosts=(_TMU_GRADES_HOST,),
        headers=headers,
        timeout=timeout,
    ) as resp:
        if resp.status == 404:
            return None  # Course not found
        resp.raise_for_status()
        return await read_response_text_limited(resp, MAX_GRADES_HTML_BYTES, label="TMU Grades HTML")


async def fetch_course_data_with_meta(
    course_code: str,
    *,
    db_path: str | None = None,
    session: aiohttp.ClientSession | None = None,
    store: GradesCacheStore | None = None,
    clock: Callable[[], float] | None = None,
    ttl_seconds: float | None = _DEFAULT_SQLITE_CACHE_TTL_SECONDS,
) -> tuple[dict[str, Any] | None, int, bool]:
    """Fetch TMU Grades Zone course data with configurable SQLite cache and provenance metadata.

    Returns ``(payload_or_none, fetched_at_unix, from_cache)``.
    """
    now = float(clock() if clock is not None else time.time())
    now_ts = int(now)

    if store is None:
        eff_db = (db_path or "").strip() or DB_PATH
        store = await _init_db(eff_db)
    else:
        await store.init()

    code = course_code.lower().strip()

    cached_hit = await store.get_course_with_meta(code, now=now, ttl_seconds=ttl_seconds)
    if cached_hit is not None:
        payload, cached_ts = cached_hit
        log.info("Serving %s from local SQLite grades cache.", code)
        return payload, cached_ts, True

    url = f"https://{_TMU_GRADES_HOST}/course/{urllib.parse.quote(code)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    if session is not None and not session.closed:
        html = await _fetch_grades_html(url, headers, session)
    else:
        async with aiohttp.ClientSession(
            connector=create_ssrf_safe_connector(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as tmp_session:
            html = await _fetch_grades_html(url, headers, tmp_session)

    if html is None:
        return None, now_ts, False

    parsed = parse_tmu_grades_html(html, code)
    if parsed is None:
        return None, now_ts, False

    if parsed.alignment_warning:
        log.warning("[GRADES] Alignment note for %s: %s", code, parsed.alignment_warning)

    legacy_dict = parsed.to_legacy_dict()
    stored_ts = await store.set_course(
        code,
        legacy_dict["title"],
        legacy_dict["professors"],
        fetched_at=now_ts,
    )
    return legacy_dict, stored_ts, False


async def fetch_course_data(
    course_code: str,
    *,
    db_path: str | None = None,
    session: aiohttp.ClientSession | None = None,
    store: GradesCacheStore | None = None,
) -> dict[str, Any] | None:
    """Backwards-compatible wrapper returning the course grade payload dict or ``None``."""
    data, _ts, _cached = await fetch_course_data_with_meta(
        course_code,
        db_path=db_path,
        session=session,
        store=store,
    )
    return data


def generate_chart(course_title: str, professors: list[dict[str, Any]]) -> io.BytesIO:
    """Render a thread-safe PNG chart using Matplotlib's OO API (no global pyplot state)."""
    return render_grade_distribution_chart(course_title, professors).to_bytes_io()


@ic.command(name="grades", description="Course grades.")
@app_commands.describe(course="Course code")
async def grades_command(interaction: discord.Interaction, course: str):
    await interaction.response.defer(thinking=True)

    settings = await resolve_guild_settings(interaction.client, interaction)

    clean_key = normalize_course_key(course)
    if len(clean_key) < 2:
        await interaction.followup.send(
            embed=system_embed(
                settings,
                title="Invalid Course Code",
                summary="❌ Please provide a valid course code (e.g. `ELE888` or `CPS109`).",
                footer_hint="Academics",
            ),
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    campus = campus_from_settings(settings)
    shared_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    try:
        result = await container.academic_registry.get_grade_distribution(
            course,
            campus=campus,
            settings=settings,
            session=shared_session,
            clock=container.clock,
        )

        if result.status == "unsupported_campus":
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="Grade Provider Not Available",
                    summary=result.error_message
                    or f"No grade distribution provider is configured for **{campus.name}**.",
                    footer_hint=campus.name,
                )
            )
            return

        if result.status in ("error", "unsafe_url"):
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="Grades Error",
                    summary="An error occurred while querying the grade distribution provider.",
                    footer_hint=result.provenance.provider_name,
                )
            )
            return

        dist = result.data
        if not result.ok or dist is None:
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="Course Not Found",
                    summary=(
                        f"Could not locate grade distributions for `{course}` via **{result.provenance.provider_name}**.\n"
                        f"{format_freshness_line(result.provenance, clock=container.clock)}"
                    ),
                    footer_hint=result.provenance.provider_name,
                )
            )
            return

        if not dist.professors:
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="No Professor Data",
                    summary=f"`{dist.course_title}` exists, but has no discrete professor distributions available.",
                    footer_hint=result.provenance.provider_name,
                )
            )
            return

        legacy_profs = [p.to_legacy_dict() for p in dist.professors]
        chart_file: discord.File | None = None
        alt_text = build_accessible_grade_summary(dist.course_title, legacy_profs)
        chart_note = ""

        # Offload CPU-bound Matplotlib chart rendering off the Discord asyncio event loop
        try:
            chart_res = await render_grade_distribution_chart_async(dist.course_title, legacy_profs)
            chart_file = discord.File(
                chart_res.to_bytes_io(),
                filename="distribution.png",
                description=chart_res.accessible_alt_text,
            )
            alt_text = chart_res.accessible_alt_text
            if chart_res.truncated_count > 0:
                chart_note = f"*Chart plots top {chart_res.plotted_count} of {len(dist.professors)} instructors for readability.*"
        except ChartRenderError as exc:
            log.warning("Chart render skipped for %s, serving accessible text fallback: %s", course, exc)
            chart_note = "*Chart image omitted; full accessible text breakdown shown below.*"

        summary_lines: list[str] = []
        if dist.source_url:
            summary_lines.append(f"[View on {result.provenance.provider_name}]({dist.source_url})")
        summary_lines.append(format_freshness_line(result.provenance, clock=container.clock))
        if chart_note:
            summary_lines.append(chart_note)
        if len(dist.professors) > 19:
            summary_lines.append(f"*Showing 19 of {len(dist.professors)} instructor records.*")

        fields: list[tuple[str, str, bool]] = [
            (
                prof.name,
                f"**{prof.students} students** | {prof.avg} | {prof.common}\n*{prof.term}*",
                False,
            )
            for prof in dist.professors[:19]
        ]
        fields.append(("Accessible Summary", alt_text, False))

        embed = system_embed(
            settings,
            title=dist.course_title,
            summary="\n".join(summary_lines),
            fields=fields,
            footer_hint=result.provenance.provider_name,
        )
        if chart_file is not None:
            embed.set_image(url="attachment://distribution.png")
            await interaction.followup.send(embed=embed, file=chart_file)
        else:
            await interaction.followup.send(embed=embed)

    except (
        aiohttp.ClientError,
        TimeoutError,
        aiosqlite.Error,
        discord.DiscordException,
        OSError,
        LookupError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        secrets = settings.secret_values()
        log.warning(
            "Error fetching grades for %s: %s",
            redact_secrets(course, secrets),
            format_exception_safe(exc, secrets),
        )
        await interaction.followup.send(
            embed=system_embed(
                settings,
                title="Grades Error",
                summary="An unexpected internal error occurred while fetching course grades.",
            )
        )
