"""Shared pytest configuration and offline safety guards for Instigate Cafe Bot.

Guarantees that automated test runs never open live outbound sockets, load
private `.env` credentials, connect to Discord, or touch production SQLite DBs.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yarl

from icbot.config import Settings
from icbot.db import Database
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import UserStore
from tests.helpers import (
    FIXTURES_DIR,
    AiohttpMockRouter,
    FakeAiohttpResponse,
    FakeClient,
    FakeInteraction,
    FakeMessage,
    FakeTextChannel,
    FrozenClock,
    OfflineNetworkDisabledError,
    build_fake_member,
)


@pytest.fixture(autouse=True)
def _disable_outbound_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Fail closed on any unmocked outbound socket/HTTP/DNS call during tests."""
    is_live_smoke = request.node.get_closest_marker("live_smoke") is not None
    if is_live_smoke and os.getenv("ICBOT_ALLOW_LIVE_SMOKE", "") == "1":
        yield
        return

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def _blocked_connect(self: socket.socket, address: Any):
        if getattr(self, "family", None) in (socket.AF_INET, socket.AF_INET6):
            raise OfflineNetworkDisabledError(
                f"Outbound INET socket connection blocked in offline tests: {address!r}"
            )
        return orig_connect(self, address)

    def _blocked_connect_ex(self: socket.socket, address: Any):
        if getattr(self, "family", None) in (socket.AF_INET, socket.AF_INET6):
            raise OfflineNetworkDisabledError(
                f"Outbound INET socket connect_ex blocked in offline tests: {address!r}"
            )
        return orig_connect_ex(self, address)

    def _blocked_create_connection(address: Any, *args: Any, **kwargs: Any):
        raise OfflineNetworkDisabledError(
            f"socket.create_connection blocked in offline tests: {address!r}"
        )

    def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):
        if host in (None, "localhost", "127.0.0.1", "::1"):
            return orig_getaddrinfo(host, port, *args, **kwargs)
        raise OfflineNetworkDisabledError(
            f"Outbound DNS resolution blocked in offline tests: host={host!r}"
        )

    def _blocked_urlopen(url: Any, *args: Any, **kwargs: Any):
        target = getattr(url, "full_url", url)
        raise OfflineNetworkDisabledError(
            f"urllib.request.urlopen blocked in offline tests: {target!r}"
        )

    def _blocked_http_connect(self: Any):
        host = getattr(self, "host", "unknown")
        port = getattr(self, "port", "?")
        raise OfflineNetworkDisabledError(
            f"http.client connection blocked in offline tests: {host}:{port}"
        )

    async def _blocked_aiohttp_request(self: aiohttp.ClientSession, method: str, str_or_url: Any, **kwargs: Any):
        raise OfflineNetworkDisabledError(
            f"Unmocked aiohttp {method} request blocked in offline tests: {str(str_or_url)!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _blocked_http_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _blocked_http_connect)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", _blocked_aiohttp_request)

    yield


def _reset_service_globals() -> None:
    """Clear cached HTTP sessions, locks bound to closed loops, and in-memory TTL/session caches."""
    if "icbot.features.notes" in sys.modules:
        mod = sys.modules["icbot.features.notes"]
        mod._NOTES_CACHE = (0.0, {})

    if "icbot.features.blackjack" in sys.modules:
        bj_mod = sys.modules["icbot.features.blackjack"]
        bj_mod._sessions.sessions.clear()

    if "icbot.tasks.job_alerts" in sys.modules:
        jobs_mod = sys.modules["icbot.tasks.job_alerts"]
        jobs_mod._MARK_SEEN_MODE = None

    for mod_name in (
        "icbot.services.yahoo_quotes",
        "icbot.services.btc_esplora",
        "icbot.services.crypto_fng",
    ):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        mod._SESSION = None
        if hasattr(mod, "_SESSION_LOCK"):
            mod._SESSION_LOCK = asyncio.Lock()
        if hasattr(mod, "_LOCK"):
            mod._LOCK = asyncio.Lock()
        if hasattr(mod, "_CRUMB"):
            mod._CRUMB = None
        if hasattr(mod, "_CRUMB_EXP_TS"):
            mod._CRUMB_EXP_TS = 0.0
        if hasattr(mod, "_CRUMB_LOCK"):
            mod._CRUMB_LOCK = asyncio.Lock()


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run with empty credentials, disabled pollers, and clean caches."""
    cleared_vars = (
        "DISCORD_TOKEN",
        "GUILD_ID",
        "ALLOWED_ROLE_IDS",
        "USER_COMMAND_CHANNEL_ID",
        "STOCK_CHANNEL_ID",
        "TTC_ALERTS_CHANNEL_ID",
        "JOB_ALERTS_CHANNEL_ID",
        "GO_ALERTS_CHANNEL_ID",
        "MARKET_WATCH_CHANNEL_ID",
        "MARKET_DAILY_REPORT_CHANNEL_ID",
        "BTC_WATCH_CHANNEL_ID",
        "GEMINI_API_KEY",
        "GO_API_KEY",
        "ODDS_API_KEY",
        "GREENHOUSE_BOARDS",
        "LEVER_SITES",
        "WORKDAY_SITES",
        "NOTES_SITES",
        "LORE_PATH",
        "TTC_MASTODON_BASE",
        "TTC_MASTODON_ACCT",
        "BTC_WATCH_ADDRESSES",
    )
    for var in cleared_vars:
        monkeypatch.setenv(var, "")

    disabled_flags = (
        "LLM_ENABLED",
        "MARKET_WATCH_ENABLED",
        "MARKET_DAILY_REPORT_ENABLED",
        "BTC_WATCH_ENABLED",
        "FORCE_GUILD_RESYNC",
        "JOBS_BOOTSTRAP_POST",
        "TTC_BOOTSTRAP_POST_LATEST",
        "GO_BOOTSTRAP_POST_LATEST",
    )
    for flag in disabled_flags:
        monkeypatch.setenv(flag, "false")

    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("JOBS_POLL_SECONDS", "0")

    try:
        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    except ImportError:
        pass

    _reset_service_globals()
    yield
    _reset_service_globals()


@pytest.fixture
def frozen_time(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    clock = FrozenClock()
    monkeypatch.setattr(time, "time", clock)
    return clock


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def synthetic_settings(tmp_path: Path) -> Settings:
    return Settings(
        token="",
        guild_id=900000000000000001,
        stock_channel_id=900000000000000010,
        user_command_channel_id=900000000000000020,
        ttc_alerts_channel_id=900000000000000030,
        job_alerts_channel_id=900000000000000040,
        go_alerts_channel_id=900000000000000050,
        allowed_role_ids=[900000000000000099],
        embed_accent_color=0xA64DFF,
        embed_footer_text="Instigate Cafe (Test)",
        daily_claim_amount=100,
        bj_starting_bankroll=1000,
        db_path=str(tmp_path / "test_ic.db"),
        exams_tz="America/Toronto",
        notes_sites=["Synthetic Notes|https://example.invalid/Notes/"],
        lore_path=str(FIXTURES_DIR / "synthetic_lore.json"),
        rmp_university="Toronto Metropolitan University",
    )


@pytest.fixture
async def tmp_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "test_ic.db"
    database = Database(str(db_file))
    await database.init()
    return database


@pytest.fixture
def user_store(tmp_db: Database) -> UserStore:
    return UserStore(tmp_db, starting_balance=1000, daily_claim_amount=100)


@pytest.fixture
def fun_market_store(tmp_db: Database) -> FunMarketStore:
    return FunMarketStore(tmp_db, starting_bankroll=1000, tz_name="America/Toronto")


@pytest.fixture
async def seed_guild_users(user_store: UserStore):
    """Seed two isolated synthetic guild users with clean initial bankrolls."""
    alice = await user_store.ensure_user(
        800000000000000001,
        display_name="SyntheticAlice",
        avatar_url="https://example.invalid/avatars/alice.png",
    )
    bob = await user_store.ensure_user(
        800000000000000002,
        display_name="SyntheticBob",
        avatar_url="https://example.invalid/avatars/bob.png",
    )
    return {"alice": alice, "bob": bob}


@pytest.fixture
def make_member():
    return build_fake_member


@pytest.fixture
def make_interaction(synthetic_settings: Settings, tmp_db: Database):
    def _factory(
        *,
        user: Any | None = None,
        settings: Settings | None = None,
        db: Database | None = None,
        administrator: bool = False,
        role_ids: list[int] | None = None,
        guild: Any | None = None,
        command_name: str = "ic test",
        message: FakeMessage | None = None,
        client: FakeClient | None = None,
    ) -> FakeInteraction:
        eff_settings = settings or synthetic_settings
        eff_db = db if db is not None else tmp_db
        eff_user = user or build_fake_member(
            administrator=administrator,
            role_ids=role_ids,
        )
        eff_client = client or FakeClient(settings=eff_settings, db=eff_db)
        return FakeInteraction(
            user=eff_user,
            client=eff_client,
            guild=guild,
            command_name=command_name,
            message=message,
        )

    return _factory


@pytest.fixture
def make_poller_bot(synthetic_settings: Settings, tmp_db: Database):
    def _factory(*, settings: Settings | None = None, db: Database | None = None):
        eff_settings = settings or synthetic_settings
        eff_db = db if db is not None else tmp_db
        bot = FakeClient(settings=eff_settings, db=eff_db)
        channels: dict[int, FakeTextChannel] = {}
        for attr in (
            "job_alerts_channel_id",
            "ttc_alerts_channel_id",
            "go_alerts_channel_id",
            "stock_channel_id",
        ):
            cid = int(getattr(eff_settings, attr, 0) or 0)
            if cid and cid not in channels:
                channels[cid] = bot.register_channel(FakeTextChannel(channel_id=cid, name=attr))
        return bot, channels

    return _factory


@pytest.fixture
def mock_aiohttp(monkeypatch: pytest.MonkeyPatch) -> AiohttpMockRouter:
    """Intercept all aiohttp requests and route them to registered local fixtures."""
    router = AiohttpMockRouter()

    async def _mock_request(
        self: aiohttp.ClientSession,
        method: str,
        str_or_url: Any,
        *,
        params: Any = None,
        data: Any = None,
        json: Any = None,
        headers: Any = None,
        **kwargs: Any,
    ) -> FakeAiohttpResponse:
        base_url = yarl.URL(str(str_or_url))
        if params:
            base_url = base_url.update_query(params)

        merged_headers: dict[str, str] = {}
        for k, v in getattr(self, "headers", {}).items():
            merged_headers[str(k)] = str(v)
        if headers:
            for k, v in dict(headers).items():
                merged_headers[str(k)] = str(v)

        payload = json if json is not None else data
        return router.dispatch(method, str(base_url), merged_headers, payload, params)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _mock_request)
    return router
