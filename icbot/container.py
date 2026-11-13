"""Typed dependency-injection container and per-guild runtime helpers.

Owns runtime resources previously stored in module-level globals (HTTP session,
TTL/session/lore caches, clock callables, killswitch state, and service store
factories) while maintaining safe lazy fallback bindings for lightweight test
clients that do not subclass ``CampusBot``.
"""

from __future__ import annotations

import contextlib
import random
import sqlite3
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import aiosqlite

from icbot.config import Settings
from icbot.db import Database
from icbot.guild_config import GuildSettingsStore
from icbot.services.fun_market import FunMarketStore
from icbot.services.gemini_client import gemini_generate_text
from icbot.services.sports_client import OddsAPIClient
from icbot.services.user_service import UserStore
from icbot.services.yahoo_quotes import YahooCrumbCache

if TYPE_CHECKING:
    from icbot.features.blackjack import BlackjackStore, SessionManager


def _default_clock() -> float:
    return time.time()


def _default_monotonic() -> float:
    return time.monotonic()


@dataclass
class NotesCache:
    """In-memory course notes cache partitioned by normalized NOTES_SITES config."""

    _by_sites: dict[tuple[str, ...], tuple[float, dict[str, list[tuple[str, str]]]]] = field(
        default_factory=dict
    )

    def get(
        self,
        sites_key: tuple[str, ...],
        *,
        now: float,
        ttl_seconds: float,
    ) -> dict[str, list[tuple[str, str]]] | None:
        entry = self._by_sites.get(sites_key)
        if not entry:
            return None
        ts, data = entry
        if data and (now - ts) < ttl_seconds:
            return data
        return None

    def set(
        self,
        sites_key: tuple[str, ...],
        *,
        now: float,
        data: dict[str, list[tuple[str, str]]],
    ) -> None:
        self._by_sites[sites_key] = (float(now), data)

    def clear(self) -> None:
        self._by_sites.clear()


@dataclass
class RssCache:
    """In-memory TTL cache for parsed RSS/Atom news feeds."""

    entries: dict[str, tuple[float, list[tuple[str, str]]]] = field(default_factory=dict)

    def get(self, key: str, *, now: float, ttl_seconds: float) -> list[tuple[str, str]] | None:
        cached = self.entries.get(key)
        if cached and (now - cached[0]) <= ttl_seconds:
            return cached[1]
        return None

    def set(self, key: str, *, now: float, items: list[tuple[str, str]]) -> None:
        self.entries[key] = (float(now), items)

    def clear(self) -> None:
        self.entries.clear()


# Shared fallback caches used ONLY when an external caller creates bare FakeClient instances
# without an explicit ServiceContainer (e.g. legacy command unit tests making one FakeClient per call).
_FALLBACK_NOTES_CACHE = NotesCache()
_FALLBACK_RSS_CACHE = RssCache()


def _build_go_connector(settings: Settings) -> aiohttp.TCPConnector:
    """Build a scoped ``TCPConnector`` only when custom GO TLS settings are active."""
    verify = bool(getattr(settings, "go_ssl_verify", True))
    if not verify:
        return aiohttp.TCPConnector(ssl=False)

    cafile = str(getattr(settings, "go_ca_bundle_path", "") or "").strip()
    try:
        if cafile:
            ssl_ctx = ssl.create_default_context(cafile=cafile)
        else:
            try:
                import certifi  # type: ignore[import-untyped]

                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except (ImportError, OSError, ssl.SSLError, ValueError):
                ssl_ctx = ssl.create_default_context()
    except (OSError, ssl.SSLError, ValueError):
        ssl_ctx = ssl.create_default_context()
    return aiohttp.TCPConnector(ssl=ssl_ctx)


class ServiceContainer:
    """Process-scoped dependency container attached cleanly to ``CampusBot.services``."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database | None = None,
        clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        jitter_rng: random.Random | None = None,
        http_session: aiohttp.ClientSession | None = None,
        owns_http_session: bool | None = None,
        go_http_session: aiohttp.ClientSession | None = None,
        owns_go_http_session: bool | None = None,
        notes_cache: NotesCache | None = None,
        rss_cache: RssCache | None = None,
        yahoo_crumb: YahooCrumbCache | None = None,
        lore_cache: dict[str, dict[str, Any]] | None = None,
        llm_last_reply: dict[int, float] | None = None,
        llm_killed: bool = False,
        jobs_bad_sources: set[str] | None = None,
        workday_bad_sources: set[str] | None = None,
        bj_sessions: SessionManager | None = None,
        use_module_bj_fallback: bool = False,
        gemini_generate_fn: Callable[..., Awaitable[str]] | None = None,
        fetch_quotes_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        fetch_fear_greed_fn: Callable[..., Awaitable[Any]] | None = None,
        fetch_address_txs_fn: Callable[..., Awaitable[list[Any]]] | None = None,
        odds_client_factory: Callable[[Settings], OddsAPIClient] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.clock: Callable[[], float] = clock if clock is not None else _default_clock
        self.monotonic_clock: Callable[[], float] = (
            monotonic_clock if monotonic_clock is not None else _default_monotonic
        )
        self.sleep_fn: Callable[[float], Awaitable[None]] | None = sleep_fn
        self.jitter_rng: random.Random | None = jitter_rng

        self.http_session: aiohttp.ClientSession | None = http_session
        if owns_http_session is None:
            self.owns_http_session = http_session is None
        else:
            self.owns_http_session = bool(owns_http_session)

        self.go_http_session: aiohttp.ClientSession | None = go_http_session
        if owns_go_http_session is None:
            self.owns_go_http_session = go_http_session is None
        else:
            self.owns_go_http_session = bool(owns_go_http_session)

        self.guild_settings: GuildSettingsStore | None = (
            GuildSettingsStore(db, clock=self.clock) if db is not None else None
        )
        self.notes_cache: NotesCache = notes_cache if notes_cache is not None else NotesCache()
        self.rss_cache: RssCache = rss_cache if rss_cache is not None else RssCache()
        self.yahoo_crumb: YahooCrumbCache = yahoo_crumb if yahoo_crumb is not None else YahooCrumbCache()
        self.lore_cache: dict[str, dict[str, Any]] = lore_cache if lore_cache is not None else {}
        self.llm_last_reply: dict[int, float] = llm_last_reply if llm_last_reply is not None else {}
        self.llm_killed: bool = bool(llm_killed)
        self.jobs_bad_sources: set[str] = jobs_bad_sources if jobs_bad_sources is not None else set()
        self.workday_bad_sources: set[str] = workday_bad_sources if workday_bad_sources is not None else set()
        self.jobs_mark_seen_mode: str | None = None

        self._bj_sessions: SessionManager | None = bj_sessions
        self._use_module_bj_fallback = bool(use_module_bj_fallback)
        self._grades_cache: Any = None

        self.gemini_generate_fn = gemini_generate_fn
        self.fetch_quotes_fn = fetch_quotes_fn
        self.fetch_fear_greed_fn = fetch_fear_greed_fn
        self.fetch_address_txs_fn = fetch_address_txs_fn
        self.odds_client_factory = odds_client_factory

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        db: Database | None = None,
        clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        jitter_rng: random.Random | None = None,
        http_session: aiohttp.ClientSession | None = None,
        go_http_session: aiohttp.ClientSession | None = None,
        gemini_generate_fn: Callable[..., Awaitable[str]] | None = None,
        fetch_quotes_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        fetch_fear_greed_fn: Callable[..., Awaitable[Any]] | None = None,
        fetch_address_txs_fn: Callable[..., Awaitable[list[Any]]] | None = None,
        odds_client_factory: Callable[[Settings], OddsAPIClient] | None = None,
    ) -> ServiceContainer:
        """Create an isolated ``ServiceContainer`` for a bot or test harness."""
        eff_db = db if db is not None else Database(settings.db_path)
        return cls(
            settings=settings,
            db=eff_db,
            clock=clock,
            monotonic_clock=monotonic_clock,
            sleep_fn=sleep_fn,
            jitter_rng=jitter_rng,
            http_session=http_session,
            owns_http_session=http_session is None,
            go_http_session=go_http_session,
            owns_go_http_session=go_http_session is None,
            gemini_generate_fn=gemini_generate_fn,
            fetch_quotes_fn=fetch_quotes_fn,
            fetch_fear_greed_fn=fetch_fear_greed_fn,
            fetch_address_txs_fn=fetch_address_txs_fn,
            odds_client_factory=odds_client_factory,
        )

    @property
    def bj_sessions(self) -> SessionManager:
        """Lazy blackjack session manager (avoids eager ``icbot.features.*`` import side effects)."""
        if self._use_module_bj_fallback:
            from icbot.features.blackjack import _sessions

            return _sessions
        if self._bj_sessions is None:
            from icbot.features.blackjack import SessionManager

            self._bj_sessions = SessionManager(clock=self.clock)
        return self._bj_sessions

    @property
    def grades_cache(self) -> Any:
        """Lazy grades course cache store backed by shared database."""
        if self._grades_cache is None:
            from icbot.features.grades import GradesCacheStore

            target_db = self.db if self.db is not None else self.settings.grades_db_path
            self._grades_cache = GradesCacheStore(target_db)
        return self._grades_cache

    async def ensure_http_session(self) -> aiohttp.ClientSession:
        """Return an open shared ``aiohttp.ClientSession``, creating it when owned by container."""
        if self.http_session is not None and not self.http_session.closed:
            return self.http_session
        timeout = aiohttp.ClientTimeout(total=15)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        self.owns_http_session = True
        return self.http_session

    async def ensure_go_http_session(self, settings: Settings | None = None) -> aiohttp.ClientSession | None:
        """Create a dedicated GO Transit HTTP session only when custom TLS settings require one."""
        eff_settings = settings or self.settings
        verify = bool(getattr(eff_settings, "go_ssl_verify", True))
        cafile = str(getattr(eff_settings, "go_ca_bundle_path", "") or "").strip()
        if verify and not cafile:
            return None
        if self.go_http_session is not None and not self.go_http_session.closed:
            return self.go_http_session
        connector = _build_go_connector(eff_settings)
        try:
            self.go_http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=18),
            )
        except Exception:
            await connector.close()
            raise
        self.owns_go_http_session = True
        return self.go_http_session

    async def close(self) -> None:
        """Release container-owned HTTP resources and reset volatile caches."""
        self.yahoo_crumb.clear()
        if self.owns_http_session and self.http_session is not None:
            session = self.http_session
            self.http_session = None
            if not session.closed:
                await session.close()
        if self.owns_go_http_session and self.go_http_session is not None:
            go_session = self.go_http_session
            self.go_http_session = None
            if not go_session.closed:
                await go_session.close()

    def user_store(self, settings: Settings | None = None, *, db: Database | None = None) -> UserStore:
        eff_settings = settings or self.settings
        eff_db = db if db is not None else self.db
        if eff_db is None:
            raise RuntimeError("Database is not initialized on ServiceContainer")
        return UserStore(
            eff_db,
            starting_balance=int(getattr(eff_settings, "bj_starting_bankroll", 1000) or 1000),
            daily_claim_amount=int(getattr(eff_settings, "daily_claim_amount", 100) or 100),
            clock=self.clock,
        )

    def fun_market_store(
        self, settings: Settings | None = None, *, db: Database | None = None
    ) -> FunMarketStore:
        eff_settings = settings or self.settings
        eff_db = db if db is not None else self.db
        if eff_db is None:
            raise RuntimeError("Database is not initialized on ServiceContainer")
        return FunMarketStore(
            eff_db,
            starting_bankroll=int(getattr(eff_settings, "bj_starting_bankroll", 1000) or 1000),
            tz_name=str(
                getattr(eff_settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto"
            ),
            clock=self.clock,
        )

    def blackjack_store(
        self, settings: Settings | None = None, *, db: Database | None = None
    ) -> BlackjackStore:
        from icbot.features.blackjack import BlackjackStore

        eff_settings = settings or self.settings
        eff_db = db if db is not None else self.db
        if eff_db is None:
            raise RuntimeError("Database is not initialized on ServiceContainer")
        return BlackjackStore(
            eff_db,
            int(getattr(eff_settings, "bj_starting_bankroll", 1000) or 1000),
            clock=self.clock,
        )

    def build_odds_client(self, settings: Settings | None = None) -> OddsAPIClient:
        eff_settings = settings or self.settings
        if self.odds_client_factory is not None:
            return self.odds_client_factory(eff_settings)
        return OddsAPIClient(
            api_key=(getattr(eff_settings, "odds_api_key", "") or "").strip(),
            cache_ttl_seconds=600,
            session=self.http_session if self.http_session and not self.http_session.closed else None,
            clock=self.clock,
        )

    async def gemini_generate(self, **kwargs: Any) -> str:
        fn = self.gemini_generate_fn if self.gemini_generate_fn is not None else gemini_generate_text
        if "session" not in kwargs and self.http_session is not None and not self.http_session.closed:
            kwargs["session"] = self.http_session
        return await fn(**kwargs)

    async def resolve_settings_for_guild(self, guild_id: int | None) -> Settings:
        """Resolve effective non-secret settings for ``guild_id`` merged over process defaults."""
        if not guild_id:
            return self.settings
        store = self.guild_settings
        if store is None and self.db is not None:
            store = GuildSettingsStore(self.db, clock=self.clock)
            self.guild_settings = store
        if store is None:
            return self.settings
        try:
            return await store.resolve_for_guild(self.settings, int(guild_id))
        except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
            # Tolerate partial/unmigrated test DB mocks lacking guild_settings table
            if "guild_settings" in str(exc).lower():
                return self.settings
            raise


def extract_guild_id(target: Any) -> int | None:
    """Extract numeric guild ID from an int, Discord Interaction, or Message."""
    if target is None:
        return None
    if isinstance(target, int) and not isinstance(target, bool):
        return target if target > 0 else None
    gid = getattr(target, "guild_id", None)
    if gid:
        try:
            return int(gid)
        except (TypeError, ValueError):
            pass
    guild = getattr(target, "guild", None)
    if guild is not None:
        gid2 = getattr(guild, "id", None)
        if gid2:
            try:
                return int(gid2)
            except (TypeError, ValueError):
                pass
    return None


def get_container(client: Any) -> ServiceContainer:
    """Return ``client.services`` or lazily attach a fallback container for bare clients/mocks."""
    existing = getattr(client, "services", None)
    if isinstance(existing, ServiceContainer):
        return existing

    settings = cast(Settings, getattr(client, "settings", None) or Settings())
    db = cast(Database | None, getattr(client, "db", None))
    http_session = cast(aiohttp.ClientSession | None, getattr(client, "http_session", None))

    existing_lore = getattr(client, "_lore_cache", None)
    existing_last_reply = getattr(client, "_llm_last_reply", None)
    existing_jobs_bad = getattr(client, "_jobs_bad_sources", None)
    existing_wd_bad = getattr(client, "_workday_bad_sources", None)
    existing_killed = bool(getattr(client, "_llm_killed", False))

    container = ServiceContainer(
        settings=settings,
        db=db,
        http_session=http_session,
        owns_http_session=False,
        notes_cache=_FALLBACK_NOTES_CACHE,
        rss_cache=_FALLBACK_RSS_CACHE,
        lore_cache=existing_lore if isinstance(existing_lore, dict) else {},
        llm_last_reply=existing_last_reply if isinstance(existing_last_reply, dict) else {},
        llm_killed=existing_killed,
        jobs_bad_sources=existing_jobs_bad if isinstance(existing_jobs_bad, set) else set(),
        workday_bad_sources=existing_wd_bad if isinstance(existing_wd_bad, set) else set(),
        use_module_bj_fallback=True,
    )
    if client is not None:
        with contextlib.suppress(AttributeError, TypeError):
            client.services = container
    return container


async def resolve_guild_settings(client: Any, target: Any = None) -> Settings:
    """Resolve effective non-secret settings for ``target`` (guild ID, Interaction, or Message)."""
    container = get_container(client)
    guild_id = extract_guild_id(target)
    if guild_id is None:
        return container.settings
    return await container.resolve_settings_for_guild(guild_id)
