from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from icbot.errors import parse_retry_after, redact_secrets
from icbot.services.http_transport import ProviderProvenance, ProviderResult, TransportError

log = logging.getLogger(__name__)

ODDS_BASE_URL = "https://api.the-odds-api.com/v4"


@dataclass(frozen=True)
class SportEvent:
    id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str


class OddsAPIError(TransportError):
    pass


class OddsAPIClient:
    """
    Minimal Odds API v4 client.

    Uses /events for schedules (no odds) which is ideal for:
      /ic nba -> upcoming 5 games

    Later, you can add /ic sportsbook by using /odds (quota-based).
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 10,
        cache_ttl_seconds: int = 600,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._session = session
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_from_cache: bool = False

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.time()

    def _cache_get(self, key: str) -> Any | None:
        v = self._cache.get(key)
        if not v:
            return None
        ts, data = v
        if (self._now() - ts) <= self.cache_ttl_seconds:
            return data
        self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, data: Any) -> None:
        self._cache[key] = (self._now(), data)

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key:
            raise OddsAPIError("Missing ODDS_API_KEY")

        url = f"{ODDS_BASE_URL}{path}"
        req_params = dict(params or {})
        cache_key = f"{url}?{sorted(req_params.items())}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._last_from_cache = True
            return cached
        self._last_from_cache = False

        req_params["apiKey"] = self.api_key
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "User-Agent": "InstigateCafeBot/1.0 (+discord bot; personal project)",
            "Accept": "application/json",
        }

        owns_session = self._session is None or self._session.closed
        active_session = (
            self._session if not owns_session else aiohttp.ClientSession(timeout=timeout, headers=headers)
        )
        assert active_session is not None
        try:
            async with active_session.get(url, params=req_params, timeout=timeout, headers=headers) as resp:
                # Helpful error text (Odds API uses structured JSON errors often)
                text = await resp.text()
                if resp.status >= 400:
                    safe_text = redact_secrets(text[:400], (self.api_key,))
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    raise OddsAPIError(
                        f"Odds API error {resp.status}: {safe_text}",
                        status=resp.status,
                        retry_after=retry_after,
                    )

                data = await resp.json()
        finally:
            if owns_session:
                await active_session.close()

        self._cache_set(cache_key, data)
        return data

    @staticmethod
    def _parse_dt(s: str) -> datetime:
        # Odds API returns ISO8601 e.g. "2026-01-29T00:00:00Z"
        # fromisoformat doesn't like 'Z' in py<3.11; normalize anyway.
        s = (s or "").strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    async def list_sports(self) -> Any:
        # GET /v4/sports (docs note this endpoint doesn't count against quota)
        return await self._get_json("/sports", params={})

    async def get_events(self, sport_key: str, *, days_from: int = 7) -> list[SportEvent]:
        """
        GET /v4/sports/{sport_key}/events
        Returns upcoming events (no odds). Great for schedules.
        """
        raw = await self._get_json(f"/sports/{sport_key}/events", params={"daysFrom": days_from})

        events: list[SportEvent] = []
        for e in raw or []:
            try:
                events.append(
                    SportEvent(
                        id=str(e.get("id", "")),
                        sport_key=str(e.get("sport_key", sport_key)),
                        commence_time=self._parse_dt(e.get("commence_time", "")),
                        home_team=str(e.get("home_team", "")),
                        away_team=str(e.get("away_team", "")),
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                # Skip malformed item
                log.debug("Skipping malformed sports event: %s", exc)
                continue

        events.sort(key=lambda x: x.commence_time)
        return events

    async def get_events_provenance(
        self, sport_key: str, *, days_from: int = 7
    ) -> ProviderResult[list[SportEvent]]:
        """Fetch upcoming events with provider provenance tracking and cache awareness."""
        events = await self.get_events(sport_key, days_from=days_from)
        now = self._now()
        source_ts = events[0].commence_time.timestamp() if events else None
        prov = ProviderProvenance(
            source="the_odds_api",
            source_url=f"{ODDS_BASE_URL}/sports/{sport_key}/events",
            fetched_at=now,
            source_timestamp=source_ts,
            from_cache=self._last_from_cache,
            status_code=200,
            content_length=len(events),
        )
        return ProviderResult(data=events, provenance=prov)

    async def get_upcoming(self, sport_key: str, *, limit: int = 5, days_from: int = 7) -> list[SportEvent]:
        events = await self.get_events(sport_key, days_from=days_from)
        return events[: max(0, limit)]

    async def get_odds(
        self,
        sport_key: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "american",
        date_format: str = "iso",
    ) -> Any:
        """
        Quota-based endpoint for future /ic sportsbook:
        GET /v4/sports/{sport_key}/odds
        """
        return await self._get_json(
            f"/sports/{sport_key}/odds",
            params={
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "dateFormat": date_format,
            },
        )
