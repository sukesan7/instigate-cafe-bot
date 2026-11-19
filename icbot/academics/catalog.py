"""SQLite-backed per-guild manual course & external resource catalog (offline universal baseline).

Works for any campus, department, or program with zero external network calls or scrapers.
Stores external resource links and licensing/attribution metadata only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any

from icbot.academics.models import Course, ResourceLink, normalize_course_key, resolve_course_key
from icbot.academics.url_safety import validate_resource_url
from icbot.db import Database


class CatalogValidationError(ValueError):
    """Raised when a manual course or resource entry fails validation or URL safety."""


class ManualResourceCatalogStore:
    """Guild-scoped SQLite store for user-curated courses, aliases, and external resource links."""

    def __init__(self, db: Database, *, clock: Callable[[], float] | None = None) -> None:
        self.db = db
        self._clock = clock
        self._cache: dict[tuple[int, str], Course | None] = {}

    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    async def upsert_course(
        self,
        guild_id: int,
        course: Course,
    ) -> Course:
        """Create or update a guild-scoped course definition and its lookup aliases."""
        norm_key = normalize_course_key(course.normalized_key or course.display_code)
        if not norm_key:
            raise CatalogValidationError("Course code must contain alphanumeric characters.")
        display_code = course.display_code.strip() or norm_key
        aliases_list = list(
            dict.fromkeys(normalize_course_key(a) for a in course.aliases if normalize_course_key(a))
        )
        now_ts = self._now()

        await self.db.execute(
            """
            INSERT INTO academic_courses (
                guild_id, normalized_key, display_code, title, campus_id,
                department_code, aliases_json, term, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, normalized_key) DO UPDATE SET
                display_code = excluded.display_code,
                title = excluded.title,
                campus_id = excluded.campus_id,
                department_code = excluded.department_code,
                aliases_json = excluded.aliases_json,
                term = excluded.term
            """,
            (
                int(guild_id),
                norm_key,
                display_code,
                course.title.strip(),
                course.campus_id.strip(),
                course.department_code.strip().upper(),
                json.dumps(aliases_list),
                course.term.strip(),
                now_ts,
            ),
        )
        # Invalidate guild course cache on write
        to_del = [k for k in self._cache if k[0] == int(guild_id)]
        for k in to_del:
            self._cache.pop(k, None)

        return Course(
            display_code=display_code,
            normalized_key=norm_key,
            title=course.title.strip(),
            campus_id=course.campus_id.strip(),
            department_code=course.department_code.strip().upper(),
            aliases=tuple(aliases_list),
            term=course.term.strip(),
        )

    async def get_course(
        self,
        guild_id: int,
        course_code: str,
        *,
        aliases: Sequence[str] | None = None,
    ) -> Course | None:
        """Look up a course by normalized key or stored alias within ``guild_id``."""
        key = resolve_course_key(course_code, aliases)
        if not key:
            return None

        cache_key = (int(guild_id), key)
        if cache_key in self._cache:
            return self._cache[cache_key]

        row = await self.db.fetchone(
            """
            SELECT display_code, normalized_key, title, campus_id, department_code, aliases_json, term
            FROM academic_courses
            WHERE guild_id = ? AND normalized_key = ?
            """,
            (int(guild_id), key),
        )
        res: Course | None = None
        if row:
            res = self._row_to_course(row)
        else:
            # Fallback: scan guild courses for stored alias match
            rows = await self.db.fetchall(
                """
                SELECT display_code, normalized_key, title, campus_id, department_code, aliases_json, term
                FROM academic_courses
                WHERE guild_id = ?
                """,
                (int(guild_id),),
            )
            for candidate_row in rows:
                c = self._row_to_course(candidate_row)
                if key in c.aliases:
                    res = c
                    break

        if len(self._cache) >= 1024:
            self._cache.clear()
        self._cache[cache_key] = res
        return res

    async def list_courses(self, guild_id: int) -> list[Course]:
        rows = await self.db.fetchall(
            """
            SELECT display_code, normalized_key, title, campus_id, department_code, aliases_json, term
            FROM academic_courses
            WHERE guild_id = ?
            ORDER BY normalized_key ASC
            """,
            (int(guild_id),),
        )
        return [self._row_to_course(r) for r in rows]

    async def add_resource(
        self,
        *,
        guild_id: int,
        course_code: str,
        title: str,
        url: str,
        resource_type: str = "notes",
        source_attribution: str = "Guild Resource Catalog",
        license_note: str = "External resource — rights remain with original author/institution.",
        added_by: int = 0,
        require_https: bool = True,
    ) -> ResourceLink:
        """Validate and store an external course resource link (links/metadata only)."""
        norm_key = normalize_course_key(course_code)
        if not norm_key:
            raise CatalogValidationError("Course code must contain alphanumeric characters.")

        clean_title = str(title or "").strip()
        if not clean_title:
            raise CatalogValidationError("Resource title must not be empty.")

        url_check = validate_resource_url(url, require_https=require_https)
        if not url_check.ok:
            raise CatalogValidationError(f"Unsafe or invalid resource URL: {url_check.reason}")

        clean_type = str(resource_type or "notes").strip().lower() or "notes"
        clean_attr = str(source_attribution or "Guild Resource Catalog").strip()
        clean_license = str(license_note or "").strip()
        display_code = str(course_code or "").strip() or norm_key
        now_ts = self._now()

        row_id = await self.db.execute_insert(
            """
            INSERT INTO academic_resources (
                guild_id, course_key, display_code, title, url,
                resource_type, source_attribution, license_note, added_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id),
                norm_key,
                display_code,
                clean_title[:200],
                url_check.normalized_url,
                clean_type[:40],
                clean_attr[:160],
                clean_license[:240],
                int(added_by),
                now_ts,
            ),
        )
        return ResourceLink(
            resource_id=f"manual:{row_id}",
            guild_id=int(guild_id),
            course_key=norm_key,
            display_code=display_code,
            title=clean_title[:200],
            url=url_check.normalized_url,
            resource_type=clean_type[:40],
            source_attribution=clean_attr[:160],
            license_note=clean_license[:240],
            added_by=int(added_by),
            created_at=now_ts,
        )

    async def remove_resource(self, guild_id: int, resource_id: int) -> bool:
        """Delete a guild-scoped resource link by numeric ID, returning True if removed."""
        async with self.db.write_transaction() as tx:
            row = await tx.fetchone(
                "SELECT id FROM academic_resources WHERE id = ? AND guild_id = ?",
                (int(resource_id), int(guild_id)),
            )
            if row is None:
                return False
            await tx.execute(
                "DELETE FROM academic_resources WHERE id = ? AND guild_id = ?",
                (int(resource_id), int(guild_id)),
            )
            return True

    async def search_resources(
        self,
        guild_id: int,
        course_query: str,
        *,
        aliases: Sequence[str] | None = None,
    ) -> list[ResourceLink]:
        """Search guild-scoped manual resources by exact key, course alias, or prefix/substring."""
        raw_norm = normalize_course_key(course_query)
        resolved = resolve_course_key(course_query, aliases)
        if not raw_norm and not resolved:
            return []

        search_keys: set[str] = {k for k in (raw_norm, resolved) if k}

        # Expand via guild-defined course aliases if a matching course exists
        course_rec = await self.get_course(guild_id, course_query, aliases=aliases)
        if course_rec is not None:
            search_keys.add(course_rec.normalized_key)
            search_keys.update(course_rec.aliases)

        rows = await self.db.fetchall(
            """
            SELECT id, guild_id, course_key, display_code, title, url,
                   resource_type, source_attribution, license_note, added_by, created_at
            FROM academic_resources
            WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(guild_id),),
        )

        exact: list[ResourceLink] = []
        partial: list[ResourceLink] = []
        for r in rows:
            link = self._row_to_resource(r)
            if link.course_key in search_keys:
                exact.append(link)
            elif any(sk in link.course_key for sk in search_keys if len(sk) >= 3):
                partial.append(link)

        return exact if exact else partial

    async def list_resources(self, guild_id: int, *, limit: int = 100) -> list[ResourceLink]:
        rows = await self.db.fetchall(
            """
            SELECT id, guild_id, course_key, display_code, title, url,
                   resource_type, source_attribution, license_note, added_by, created_at
            FROM academic_resources
            WHERE guild_id = ?
            ORDER BY course_key ASC, created_at DESC
            LIMIT ?
            """,
            (int(guild_id), max(1, int(limit))),
        )
        return [self._row_to_resource(r) for r in rows]

    @staticmethod
    def _row_to_course(row: tuple[Any, ...]) -> Course:
        try:
            raw_aliases = json.loads(str(row[5] or "[]"))
            aliases_tuple = tuple(str(a) for a in raw_aliases if str(a).strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            aliases_tuple = ()
        return Course(
            display_code=str(row[0] or "").strip(),
            normalized_key=str(row[1] or "").strip(),
            title=str(row[2] or "").strip(),
            campus_id=str(row[3] or "").strip(),
            department_code=str(row[4] or "").strip(),
            aliases=aliases_tuple,
            term=str(row[6] or "").strip(),
        )

    @staticmethod
    def _row_to_resource(row: tuple[Any, ...]) -> ResourceLink:
        return ResourceLink(
            resource_id=f"manual:{int(row[0])}",
            guild_id=int(row[1]),
            course_key=str(row[2] or "").strip(),
            display_code=str(row[3] or "").strip(),
            title=str(row[4] or "").strip(),
            url=str(row[5] or "").strip(),
            resource_type=str(row[6] or "notes").strip(),
            source_attribution=str(row[7] or "").strip(),
            license_note=str(row[8] or "").strip(),
            added_by=int(row[9] or 0),
            created_at=int(row[10] or 0),
        )
