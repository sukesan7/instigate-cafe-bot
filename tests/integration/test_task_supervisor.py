"""Integration tests for TaskSupervisor scheduling, DST math, backoff, reconcile, and redaction."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import discord
import pytest

from icbot.bot import create_bot
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.errors import ProviderError
from icbot.features import admin as admin_feat
from icbot.features import info as info_feat
from icbot.scheduler import (
    BackoffPolicy,
    next_local_run_timestamp,
    seconds_until_next_local_time,
)
from icbot.services.crypto_fng import FearGreed
from icbot.services.gemini_client import GeminiError
from icbot.services.yahoo_quotes import Quote
from icbot.tasks import job_alerts, jobs_workday_alerts, market_daily_report
from tests.helpers import FakeMessage, FakeTextChannel, FrozenClock, build_fake_member

pytestmark = [pytest.mark.discord_mock, pytest.mark.storage]


class _AcceleratedSleep:
    """Deterministic async sleep advancing a ``FrozenClock`` without real wall-clock waiting."""

    def __init__(self, clock: FrozenClock, *, yield_fn: Any = None) -> None:
        self.clock = clock
        self.delays: list[float] = []
        self._real_sleep = asyncio.sleep
        self._yield_fn = yield_fn

    async def __call__(self, delay: float) -> None:
        self.delays.append(float(delay))
        self.clock.advance(float(delay))
        if self._yield_fn is not None:
            await self._yield_fn(float(delay))
        await self._real_sleep(0)


def _synth_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "token": "",
        "guild_id": 900000000000000001,
        "db_path": str(tmp_path / "supervisor.db"),
        "task_startup_jitter_seconds": 0.0,
        "task_max_backoff_seconds": 1800.0,
        "allow_gateway": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_importing_scheduler_has_zero_side_effects():
    repo_root = Path(__file__).resolve().parents[2]
    probe = (
        "import sys, icbot.scheduler\n"
        "assert 'icbot.bot' not in sys.modules\n"
        "assert not [m for m in sys.modules if m.startswith('icbot.features.')]\n"
        "assert hasattr(icbot.scheduler, 'TaskSupervisor')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"


async def test_supervisor_registers_all_eight_tasks_and_respects_capability_gating(tmp_path: Path):
    settings = _synth_settings(
        tmp_path,
        job_alerts_channel_id=900000000000000040,
        workday_sites=["Acme|https://acme.wd3.myworkdayjobs.com/en-US/Acme"],
        jobs_poll_seconds=0,
        workday_poll_seconds=0,
        ttc_alerts_channel_id=900000000000000030,  # missing mastodon base/acct -> gated off
        go_alerts_channel_id=900000000000000050,  # missing go_api_key -> gated off
        btc_watch_enabled=True,
        btc_watch_channel_id=900000000000000060,  # missing btc_watch_addresses -> gated off
        llm_enabled=True,  # missing gemini_api_key -> disabled
    )
    bot = create_bot(settings, register_commands=False, sync_commands=False)
    await bot.setup_hook()

    try:
        assert bot.background_tasks == {}
        health = bot.task_health()
        assert set(health.keys()) == {
            "ttc",
            "go",
            "jobs",
            "workday",
            "market_watch",
            "daily_market_report",
            "btc_watch",
            "llm_assistant",
        }
        assert all(not h.enabled for h in health.values())
        assert "TTC_MASTODON_BASE" in health["ttc"].reason
        assert "GO_API_KEY" in health["go"].reason
        assert "JOBS_POLL_SECONDS=0" in health["jobs"].reason
        assert "interval 0" in health["workday"].reason
        assert "BTC_WATCH_ADDRESSES" in health["btc_watch"].reason
        assert "GEMINI_API_KEY" in health["llm_assistant"].reason
    finally:
        await bot.close()


async def test_live_reconcile_toggles_tasks_and_admin_llm_killswitch_syncs_health(
    tmp_path: Path, make_interaction, monkeypatch: pytest.MonkeyPatch
):
    clock = FrozenClock(1_760_000_000.0)
    blocker = asyncio.Event()

    async def _controlled_sleep(delay: float) -> None:
        clock.advance(delay)
        await blocker.wait()

    s_initial = _synth_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        ttc_mastodon_base="https://mastodon.example.invalid",
        ttc_mastodon_acct="TTCnotices",
        llm_enabled=True,
        gemini_api_key="synth-gem-reconcile-key",
        jobs_poll_seconds=0,
    )
    services = ServiceContainer.from_settings(s_initial, clock=clock, sleep_fn=_controlled_sleep)
    bot = create_bot(s_initial, services=services, register_commands=False, sync_commands=False)

    calls: dict[str, int] = {"ttc": 0, "jobs": 0, "market_watch": 0}

    async def _hit(name: str, _c: Any, *, raise_on_error: bool = True) -> None:
        _ = raise_on_error
        calls[name] += 1

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", lambda c, **kw: _hit("ttc", c, **kw))
    monkeypatch.setattr("icbot.bot.poll_and_post_jobs", lambda c, **kw: _hit("jobs", c, **kw))
    monkeypatch.setattr("icbot.bot.poll_and_post_market_watch", lambda c, **kw: _hit("market_watch", c, **kw))

    await bot.setup_hook()
    bot._handle_ready()
    await asyncio.sleep(0.02)

    try:
        assert set(bot.background_tasks.keys()) == {"ttc"}
        ttc_task = bot.background_tasks["ttc"]
        assert calls["ttc"] == 1
        assert bot.task_health()["llm_assistant"].state == "idle"

        # Disable TTC, enable jobs + market_watch live via reconcile_async
        s_updated = s_initial.replace(
            ttc_alerts_channel_id=0,
            job_alerts_channel_id=900000000000000040,
            jobs_poll_seconds=120,
            greenhouse_boards=["acme"],
            market_watch_enabled=True,
            market_watch_channel_id=900000000000000010,
        )
        bot.settings = s_updated
        bot.services.settings = s_updated
        active = await bot.supervisor.reconcile_async(s_updated)
        await asyncio.sleep(0.02)

        assert set(active.keys()) == {"jobs", "market_watch"}
        assert ttc_task.done() and ttc_task.cancelled()
        assert bot.task_health()["ttc"].state == "disabled"
        assert calls["jobs"] >= 1
        assert calls["market_watch"] >= 1

        # /ic admin llm_stop and llm_start immediately synchronize llm_assistant health state
        admin_member = build_fake_member(user_id=800000000000000001, administrator=True)
        stop_inter = make_interaction(client=bot, user=admin_member, settings=s_updated)
        await admin_feat.llm_stop_cmd.callback(stop_inter)  # type: ignore[arg-type]
        llm_paused = bot.task_health()["llm_assistant"]
        assert llm_paused.state == "paused"
        assert llm_paused.paused is True

        start_inter = make_interaction(client=bot, user=admin_member, settings=s_updated)
        await admin_feat.llm_start_cmd.callback(start_inter)  # type: ignore[arg-type]
        llm_active = bot.task_health()["llm_assistant"]
        assert llm_active.state == "idle"
        assert llm_active.paused is False

        # Event-driven on_message tracking records redacted errors on GeminiError
        bot_user = SimpleNamespace(id=999000000000000001, display_name="Instigator")
        bot._connection.user = bot_user  # type: ignore[assignment]
        msg: Any = FakeMessage(
            content=f"<@{bot_user.id}> hello",
            channel=FakeTextChannel(channel_id=900000000000000020, name="chat"),
            author=build_fake_member(user_id=800000000000000002),
        )
        msg.mentions = [bot_user]
        msg.reference = None

        async def _fail_mention(c, m, *, raise_on_error: bool = False):
            if raise_on_error:
                raise GeminiError(
                    "HTTP 503 calling https://api.example.invalid?key=synth-gem-reconcile-key",
                    status=503,
                )
            return False

        monkeypatch.setattr("icbot.bot.maybe_handle_llm_mention", _fail_mention)
        await bot.on_message(msg)
        llm_err = bot.task_health()["llm_assistant"]
        assert llm_err.error_count == 1
        assert llm_err.last_http_status == 503
        assert llm_err.last_error_summary is not None
        assert "synth-gem-reconcile-key" not in llm_err.last_error_summary
    finally:
        blocker.set()
        await bot.close()


async def test_startup_jitter_and_cancellation_unwind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FrozenClock(1_760_000_000.0)
    slept: list[float] = []
    first_poll = asyncio.Event()
    second_sleep = asyncio.Event()

    async def _spy_sleep(delay: float) -> None:
        slept.append(float(delay))
        clock.advance(float(delay))
        if len(slept) == 1:
            await asyncio.sleep(0)
        else:
            second_sleep.set()
            await asyncio.sleep(10)

    settings = _synth_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        ttc_mastodon_base="https://mastodon.example.invalid",
        ttc_mastodon_acct="TTCnotices",
        ttc_poll_seconds=120,
        task_startup_jitter_seconds=20.0,
    )
    rng = random.Random(42)
    expected_jitter = random.Random(42).uniform(0.0, 20.0)
    assert 0.0 <= expected_jitter <= 20.0

    services = ServiceContainer.from_settings(settings, clock=clock, sleep_fn=_spy_sleep, jitter_rng=rng)
    bot = create_bot(settings, services=services, register_commands=False, sync_commands=False)

    async def _fake_ttc(_client: Any, *, raise_on_error: bool = True) -> None:
        _ = raise_on_error
        first_poll.set()

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", _fake_ttc)
    await bot.setup_hook()
    ttc_task = bot.background_tasks["ttc"]
    bot._handle_ready()

    await asyncio.wait_for(first_poll.wait(), timeout=2.0)
    await asyncio.wait_for(second_sleep.wait(), timeout=2.0)

    assert slept[0] == pytest.approx(expected_jitter)
    assert slept[1] == pytest.approx(120.0)

    # Cancel cleanly mid-interval sleep
    await asyncio.wait_for(bot.close(), timeout=2.0)
    assert ttc_task.done() and ttc_task.cancelled()
    assert bot.background_tasks == {}


async def test_exponential_backoff_retry_after_and_recovery_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    now_dt = datetime(2026, 11, 11, 20, 0, 0, tzinfo=UTC)
    clock = FrozenClock(now_dt.timestamp())
    sleeps: list[float] = []
    done_4_cycles = asyncio.Event()
    retry_after_date = format_datetime(datetime(2026, 11, 11, 20, 2, 0, tzinfo=UTC))

    # Unit check on BackoffPolicy bounds
    policy = BackoffPolicy(base_seconds=5.0, factor=2.0, max_seconds=60.0)
    assert policy.compute_delay(1) == 5.0
    assert policy.compute_delay(2) == 10.0
    assert policy.compute_delay(5) == 60.0
    assert policy.compute_delay(1, retry_after=45.0) == 45.0
    assert policy.compute_delay(1, retry_after=999.0) == 60.0

    cycle = 0
    health_snapshots: list[tuple[int, str, int | None, int]] = []
    gemini_secret = "synth-gem-" + "secret-leak-999"

    async def _flaky_sleep(delay: float) -> None:
        sleeps.append(float(delay))
        clock.advance(float(delay))
        if len(sleeps) >= 4:
            done_4_cycles.set()
            await asyncio.sleep(10)
        await asyncio.sleep(0)

    settings = _synth_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        ttc_mastodon_base="https://mastodon.example.invalid",
        ttc_mastodon_acct="TTCnotices",
        ttc_poll_seconds=90,
        gemini_api_key=gemini_secret,
        task_startup_jitter_seconds=0.0,
        task_max_backoff_seconds=150.0,
    )
    services = ServiceContainer.from_settings(settings, clock=clock, sleep_fn=_flaky_sleep)
    bot = create_bot(settings, services=services, register_commands=False, sync_commands=False)

    async def _ttc_sequence(_client: Any, *, raise_on_error: bool = True) -> None:
        nonlocal cycle
        _ = raise_on_error
        cycle += 1
        if cycle == 1:
            raise ProviderError(
                f"rate limited url=https://mastodon.example.invalid?token={gemini_secret}",
                status=429,
                retry_after=47.0,
            )
        if cycle == 2:
            raise ProviderError("HTTP-date rate limit", status=429, retry_after=120.0)
        if cycle == 3:
            raise ProviderError("upstream 503", status=503)
        # Cycle 4 succeeds -> clears failure counter & sleeps normal interval
        return None

    # Validate IMF-fixdate parsing parity alongside loop backoff
    from icbot.errors import extract_http_error_info

    http_exc = type(
        "FakeHTTPError", (Exception,), {"status": 429, "headers": {"Retry-After": retry_after_date}}
    )()
    status_p, ra_p = extract_http_error_info(
        type("WrappedErr", (Exception,), {"original": http_exc})("wrapped"),
        now=now_dt.timestamp(),
    )
    assert status_p == 429 and ra_p == pytest.approx(120.0)

    orig_sleep = _flaky_sleep

    async def _recording_sleep(delay: float) -> None:
        h = bot.task_health()["ttc"]
        health_snapshots.append((cycle, h.state, h.last_http_status, h.consecutive_failures))
        await orig_sleep(delay)

    bot.services.sleep_fn = _recording_sleep
    bot.supervisor._sleep_fn = _recording_sleep  # sync injected test sleeper

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", _ttc_sequence)
    await bot.setup_hook()

    with caplog.at_level("WARNING"):
        bot._handle_ready()
        await asyncio.wait_for(done_4_cycles.wait(), timeout=2.0)

    try:
        # Cycle 1: max(exp=5, Retry-After=47) = 47
        # Cycle 2: max(exp=10, Retry-After=120) = 120
        # Cycle 3: exp(5 * 2^(3-1)) = 20 -> then succeeds cleanly on Cycle 4 -> normal 90s interval!
        assert sleeps[:4] == pytest.approx([47.0, 120.0, 20.0, 90.0])
        assert health_snapshots[:4] == [
            (1, "backing_off", 429, 1),
            (2, "backing_off", 429, 2),
            (3, "backing_off", 503, 3),
            (4, "sleeping", None, 0),
        ]
        recovered = bot.task_health()["ttc"]
        assert recovered.consecutive_failures == 0
        assert recovered.last_error_summary is None
        assert recovered.success_count == 1
        assert recovered.error_count == 3

        # Secret never leaks into logs
        for rec in caplog.records:
            assert gemini_secret not in rec.getMessage()
    finally:
        await bot.close()


async def test_workday_and_jobs_transient_429_timeout_do_not_blacklist_bad_sources(
    make_poller_bot,
    synthetic_settings,
    mock_aiohttp,
):
    from dataclasses import replace

    careers_url = "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"
    cxs_url = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"
    cfg = replace(
        synthetic_settings,
        workday_sites=[f"Acme|{careers_url}", "GoneCo|https://gone.wd3.myworkdayjobs.com/en-US/Missing"],
        greenhouse_boards=["rateboard", "missingboard"],
        lever_sites=[],
    )
    bot, _channels = make_poller_bot(settings=cfg)

    # Workday: Acme gets HTTP 429, GoneCo gets HTTP 404
    mock_aiohttp.get(careers_url, text="<html></html>")
    mock_aiohttp.post(cxs_url, prefix=True, status=429, headers={"Retry-After": "33"}, text="rate limited")
    mock_aiohttp.get(cxs_url, prefix=True, status=429, headers={"Retry-After": "33"}, text="rate limited")

    mock_aiohttp.get("https://gone.wd3.myworkdayjobs.com/en-US/Missing", status=404, text="not found")
    mock_aiohttp.post(
        "https://gone.wd3.myworkdayjobs.com/wday/cxs/gone/Missing/jobs",
        prefix=True,
        status=404,
        text="not found",
    )
    mock_aiohttp.get(
        "https://gone.wd3.myworkdayjobs.com/wday/cxs/gone/Missing/jobs",
        prefix=True,
        status=404,
        text="not found",
    )

    with pytest.raises(RuntimeError, match="429"):
        await jobs_workday_alerts.poll_and_post_workday_jobs(bot, raise_on_error=True)

    # GoneCo 404 is blacklisted permanently, while transient 429 Acme is NOT
    assert "workday:GoneCo" in bot._workday_bad_sources
    assert "workday:Acme" not in bot._workday_bad_sources

    # ATS jobs: missingboard 404 is blacklisted, rateboard TimeoutError raises & is NOT blacklisted
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        "https://boards-api.greenhouse.io/v1/boards/rateboard/jobs?content=true",
        exc=TimeoutError("timed out"),
    )
    mock_aiohttp.get(
        "https://boards-api.greenhouse.io/v1/boards/missingboard/jobs?content=true",
        status=404,
        text="Job not found",
    )

    with pytest.raises(ProviderError, match="TimeoutError"):
        await job_alerts.poll_and_post_jobs(bot, raise_on_error=True)

    assert "greenhouse:missingboard" in bot._jobs_bad_sources
    assert "greenhouse:rateboard" not in bot._jobs_bad_sources


def test_dst_spring_forward_and_fall_back_timestamp_math():
    tz_name = "America/Toronto"
    tz = ZoneInfo(tz_name)

    # 1. Spring-forward day (2026-03-08): 23-hour interval between 16:30 EST and 16:30 EDT
    spring_prev = datetime(2026, 3, 7, 16, 30, tzinfo=tz).timestamp()
    spring_delay = seconds_until_next_local_time(16, 30, tz_name, now_unix=spring_prev)
    assert spring_delay == pytest.approx(23 * 3600)

    # Nonexistent gap time 02:30 on 2026-03-08 resolves cleanly to transition instant 03:00 EDT
    before_gap = datetime(2026, 3, 8, 1, 55, tzinfo=tz).timestamp()
    gap_target = next_local_run_timestamp(2, 30, tz_name, now_unix=before_gap)
    assert datetime.fromtimestamp(gap_target, tz=tz).isoformat() == "2026-03-08T03:00:00-04:00"

    # 2. Fall-back day (2026-11-01): 25-hour interval between 16:30 EDT and 16:30 EST
    fall_prev = datetime(2026, 10, 31, 16, 30, tzinfo=tz).timestamp()
    fall_delay = seconds_until_next_local_time(16, 30, tz_name, now_unix=fall_prev)
    assert fall_delay == pytest.approx(25 * 3600)

    # Ambiguous wall clock 01:30 picks fold=0 (EDT) first, then advances to Nov 2 (never fires twice)
    before_ambig = datetime(2026, 11, 1, 1, 0, tzinfo=tz, fold=0).timestamp()
    ambig_first = next_local_run_timestamp(1, 30, tz_name, now_unix=before_ambig)
    assert datetime.fromtimestamp(ambig_first, tz=tz).isoformat() == "2026-11-01T01:30:00-04:00"
    ambig_next = next_local_run_timestamp(1, 30, tz_name, now_unix=ambig_first)
    assert datetime.fromtimestamp(ambig_next, tz=tz).isoformat() == "2026-11-02T01:30:00-05:00"


async def test_daily_report_dst_schedule_same_day_idempotency_and_forbidden_auto_move(
    tmp_path: Path,
):
    tz_name = "America/Toronto"
    tz = ZoneInfo(tz_name)
    start_ts = datetime(2026, 10, 31, 16, 0, tzinfo=tz).timestamp()
    clock = FrozenClock(start_ts)
    waits: list[float] = []
    posts: list[str] = []
    two_days_done = asyncio.Event()

    async def _daily_sleep(delay: float) -> None:
        if len(posts) >= 2:
            two_days_done.set()
            await asyncio.sleep(10)
        waits.append(float(delay))
        clock.advance(float(delay))
        await asyncio.sleep(0)

    async def _fake_quotes(_syms: list[str], *, debug: bool = False) -> dict[str, Quote]:
        _ = debug
        return {
            "SPY": Quote("SPY", "REGULAR", 505.0, 2.0, 10.0, None, None, None, None, None, None, 495.0),
            "AAPL": Quote("AAPL", "REGULAR", 202.0, 1.5, 3.0, None, None, None, None, None, None, 199.0),
        }

    async def _fake_fng() -> FearGreed:
        return FearGreed(value=55, value_classification="Greed", timestamp=int(clock()))

    settings = _synth_settings(
        tmp_path,
        market_daily_report_enabled=True,
        market_daily_report_channel_id=900000000000000071,
        market_daily_report_time="16:30",
        market_daily_report_tz=tz_name,
        market_daily_report_tickers=["AAPL"],
    )
    services = ServiceContainer.from_settings(
        settings,
        clock=clock,
        sleep_fn=_daily_sleep,
        fetch_quotes_fn=_fake_quotes,
        fetch_fear_greed_fn=_fake_fng,
    )
    bot = create_bot(settings, services=services, register_commands=False, sync_commands=False)

    dr_ch = FakeTextChannel(channel_id=900000000000000071, name="daily")
    bot.get_channel = lambda cid: dr_ch if int(cid) == dr_ch.id else None  # type: ignore[method-assign]

    # Run Cycle 1 under embed Send=Forbidden (verifies plain-text fallback STILL applies SP500 move!)
    orig_send = dr_ch.send

    class _ForbiddenChannel(FakeTextChannel):
        def __init__(self) -> None:
            super().__init__(channel_id=dr_ch.id, name=dr_ch.name)
            self.forbid_embeds = True

        async def send(self, content: str | None = None, *, embed: Any = None, **kwargs: Any) -> Any:
            if embed is not None and self.forbid_embeds:
                raise discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden"), "Missing Embed Links"
                )  # type: ignore[arg-type]
            label = content or getattr(embed, "title", "") or ""
            posts.append(label)
            return await super().send(content, embed=embed, **kwargs)

    forbidden_ch = _ForbiddenChannel()
    _ = orig_send
    bot.get_channel = lambda cid: forbidden_ch if int(cid) == forbidden_ch.id else None  # type: ignore[method-assign]

    await bot.setup_hook()
    store = services.fun_market_store(settings)
    await store.sync_catalog()
    ic_before = await store.get_stock("IC")
    assert ic_before is not None

    bot._handle_ready()
    await asyncio.wait_for(two_days_done.wait(), timeout=2.0)

    try:
        # Oct 31 16:00 EDT -> 16:30 EDT is 1800s; next run Nov 1 16:30 EST across fall-back is 25h (90000s)
        assert waits[:2] == pytest.approx([1800.0, 25 * 3600.0])
        assert len(posts) == 2

        # Plain-text Forbidden fallback on Oct 31 MUST have applied +2.00% S&P 500 move to $IC
        ic_after = await store.get_stock("IC")
        assert ic_after is not None and ic_after.price > ic_before.price

        # Same-day checkpoint idempotency on Nov 1 prevents duplicate direct invocation
        sent_before = len(forbidden_ch.sent)
        await market_daily_report.post_daily_market_report(bot, raise_on_error=True)
        assert len(forbidden_ch.sent) == sent_before
    finally:
        await bot.close()


async def test_botinfo_surfaces_redacted_task_health_without_leaking_secrets(
    tmp_path: Path, make_interaction, monkeypatch: pytest.MonkeyPatch
):
    secrets_dict = {
        "token": "synth-bot-" + "botinfo-tok-111",
        "gemini_api_key": "synth-gem-" + "botinfo-key-222",
        "go_api_key": "synth-go-" + "botinfo-key-333",
        "odds_api_key": "synth-odds-" + "botinfo-key-444",
    }
    clock = FrozenClock(1_760_500_000.0)
    err_logged = asyncio.Event()

    async def _sleep_after_err(delay: float) -> None:
        clock.advance(delay)
        err_logged.set()
        await asyncio.sleep(10)

    settings = _synth_settings(
        tmp_path,
        **secrets_dict,
        ttc_alerts_channel_id=900000000000000030,
        ttc_mastodon_base="https://mastodon.example.invalid",
        ttc_mastodon_acct="TTCnotices",
        llm_enabled=True,
    )
    services = ServiceContainer.from_settings(settings, clock=clock, sleep_fn=_sleep_after_err)
    bot = create_bot(settings, services=services, register_commands=False, sync_commands=False)

    async def _secret_error_ttc(_client: Any, *, raise_on_error: bool = True) -> None:
        _ = raise_on_error
        raise ProviderError(
            f"HTTP 502 token={secrets_dict['token']} key={secrets_dict['gemini_api_key']} "
            f"apiKey={secrets_dict['go_api_key']} Authorization: Bearer {secrets_dict['odds_api_key']}",
            status=502,
        )

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", _secret_error_ttc)
    await bot.setup_hook()
    bot._handle_ready()
    await asyncio.wait_for(err_logged.wait(), timeout=2.0)

    try:
        health = bot.task_health()
        ttc_h = health["ttc"]
        assert ttc_h.state == "backing_off"
        assert ttc_h.last_http_status == 502
        assert ttc_h.last_error_summary is not None

        inter = make_interaction(client=bot, command_name="ic botinfo", settings=settings)
        await info_feat.botinfo.callback(inter)  # type: ignore[arg-type]
        emb = inter.last_reply.embed
        assert emb is not None
        bg_field = next((f for f in emb.fields if f.name == "Background Tasks"), None)
        assert bg_field is not None
        assert len(bg_field.value or "") <= 1024
        assert "ttc" in (bg_field.value or "")
        assert "backing_off" in (bg_field.value or "")

        rendered = json.dumps([emb.to_dict()]) + repr(health)
        for sec in secrets_dict.values():
            assert sec not in rendered
    finally:
        await bot.close()
