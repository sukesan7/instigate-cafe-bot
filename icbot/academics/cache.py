"""Unified provider-scoped TTL and negative-cache layer for academic adapters.

Caches results by ``(provider_id, campus_id, query_key)`` with separate positive TTLs
and short negative-cache TTLs (``not_found`` and transient ``error``) so repeated queries
surface accurate ``fetched_at`` freshness without hammering upstream endpoints.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from icbot.academics.models import ProviderProvenance, ProviderResult

DEFAULT_POSITIVE_TTL_SECONDS = 6 * 3600  # 6 hours
DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS = 300  # 5 minutes
DEFAULT_NEGATIVE_ERROR_TTL_SECONDS = 60  # 1 minute
DEFAULT_MAX_ENTRIES = 1024


@dataclass(frozen=True)
class CacheKey:
    """Canonical composite cache key: ``(provider_id, campus_id, query_key)``."""

    provider_id: str
    campus_id: str
    query_key: str

    @classmethod
    def make(cls, provider_id: str, campus_id: str, query_key: str) -> CacheKey:
        return cls(
            provider_id=str(provider_id or "").strip().lower(),
            campus_id=str(campus_id or "").strip().lower(),
            query_key=str(query_key or "").strip().lower(),
        )


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    stored_at: float
    expires_at: float
    is_negative: bool = False


@dataclass
class AcademicProviderCache:
    """In-memory bounded TTL cache with explicit negative caching for academic providers."""

    max_entries: int = DEFAULT_MAX_ENTRIES
    _entries: dict[CacheKey, _CacheEntry] = field(default_factory=dict)

    def get(
        self,
        key: CacheKey,
        *,
        now: float | None = None,
    ) -> tuple[Any, float, bool] | None:
        """Return ``(value, stored_at, is_negative)`` if fresh, else ``None``."""
        ts = float(now if now is not None else time.time())
        entry = self._entries.get(key)
        if entry is None:
            return None
        if ts >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry.value, entry.stored_at, entry.is_negative

    def set(
        self,
        key: CacheKey,
        value: Any,
        *,
        ttl_seconds: float,
        now: float | None = None,
        is_negative: bool = False,
    ) -> None:
        """Store ``value`` with explicit TTL, evicting oldest entries when over ``max_entries``."""
        if ttl_seconds <= 0:
            self._entries.pop(key, None)
            return
        ts = float(now if now is not None else time.time())
        if len(self._entries) >= self.max_entries and key not in self._entries:
            # Evict expired entries first, then oldest entry
            expired = [k for k, v in self._entries.items() if ts >= v.expires_at]
            for k in expired:
                self._entries.pop(k, None)
            if len(self._entries) >= self.max_entries:
                oldest_key = min(self._entries, key=lambda k: self._entries[k].stored_at)
                self._entries.pop(oldest_key, None)
        self._entries[key] = _CacheEntry(
            value=value,
            stored_at=ts,
            expires_at=ts + float(ttl_seconds),
            is_negative=bool(is_negative),
        )

    def get_provider_result(
        self,
        key: CacheKey,
        *,
        now: float | None = None,
    ) -> ProviderResult[Any] | None:
        """Return a cached ``ProviderResult`` with ``provenance.cached=True`` when fresh."""
        hit = self.get(key, now=now)
        if hit is None:
            return None
        result, _stored_at, _is_neg = hit
        if not isinstance(result, ProviderResult):
            return None
        prov = result.provenance
        cached_prov = ProviderProvenance(
            provider_id=prov.provider_id,
            provider_name=prov.provider_name,
            fetched_at=prov.fetched_at,
            source_url=prov.source_url,
            attribution=prov.attribution,
            cached=True,
        )
        return ProviderResult(
            data=result.data,
            status=result.status,
            provenance=cached_prov,
            error_code=result.error_code,
            error_message=result.error_message,
        )

    def set_provider_result(
        self,
        key: CacheKey,
        result: ProviderResult[Any],
        *,
        positive_ttl: float = DEFAULT_POSITIVE_TTL_SECONDS,
        not_found_ttl: float = DEFAULT_NEGATIVE_NOT_FOUND_TTL_SECONDS,
        error_ttl: float = DEFAULT_NEGATIVE_ERROR_TTL_SECONDS,
        now: float | None = None,
    ) -> None:
        """Cache a ``ProviderResult`` using positive or negative TTL according to ``result.status``."""
        if result.status == "ok":
            self.set(key, result, ttl_seconds=positive_ttl, now=now, is_negative=False)
        elif result.status == "not_found":
            self.set(key, result, ttl_seconds=not_found_ttl, now=now, is_negative=True)
        elif result.status == "error":
            self.set(key, result, ttl_seconds=error_ttl, now=now, is_negative=True)

    def invalidate_provider(self, provider_id: str) -> int:
        """Remove all cache entries for ``provider_id`` and return count removed."""
        pid = provider_id.strip().lower()
        targets = [k for k in self._entries if k.provider_id == pid]
        for k in targets:
            self._entries.pop(k, None)
        return len(targets)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def format_freshness_line(
    provenance: ProviderProvenance,
    *,
    clock: Callable[[], float] | None = None,
) -> str:
    """Format a user-facing provenance & freshness line (last fetched + source attribution)."""
    ts = int(provenance.fetched_at or (clock() if clock is not None else time.time()))
    cache_tag = "cached" if provenance.cached else "live"
    source = provenance.attribution or provenance.provider_name
    return f"Source: **{source}** • Last fetched: <t:{ts}:R> ({cache_tag})"
