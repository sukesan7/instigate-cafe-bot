"""Offline lifecycle integration tests for CampusBot startup, reconnects, sync errors, and shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from icbot.config import Settings
from icbot.db import Database
from icbot.services import btc_esplora, crypto_fng, yahoo_quotes
from tests.helpers import FakeMessage, FakeTextChannel, build_fake_member

pytestmark = [pytest.mark.discord_mock, pytest.mark.storage]


def _make_lifecycle_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "token": "",
        "guild_id": 900000000000000001,
        "user_command_channel_id": 900000000000000020,
        "ttc_alerts_channel_id": 900000000000000030,
        "ttc_mastodon_base": "https://mastodon.example.invalid",
        "ttc_mastodon_acct": "TTCnotices",
        "job_alerts_channel_id": 900000000000000040,
        "greenhouse_boards": ["acme"],
        "db_path": str(tmp_path / "lifecycle.db"),
        "task_startup_jitter_seconds": 0.0,
        "allow_gateway": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


async def test_setup_hook_initializes_once_and_repeated_on_ready_is_reconnect_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import CampusBot, create_bot

    settings = _make_lifecycle_settings(tmp_path, force_guild_resync=True)
    db = Database(settings.db_path)

    init_calls = 0
    orig_init = db.init

    async def _spy_init():
        nonlocal init_calls
        init_calls += 1
        await orig_init()

    monkeypatch.setattr(db, "init", _spy_init)

    # Seed persisted killswitch state before startup (separate connection since init hasn't run on bot yet)
    await orig_init()
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("llm_killed", "1"))

    bot: CampusBot = create_bot(settings, db=db, sync_commands=True)

    sync_targets: list[int | None] = []

    async def _fake_sync(*, guild=None):
        sync_targets.append(guild.id if guild is not None else None)
        return []

    clear_calls = 0
    orig_clear = bot.tree.clear_commands

    def _spy_clear(*, guild=None, type=discord.AppCommandType.chat_input):
        nonlocal clear_calls
        clear_calls += 1
        orig_clear(guild=guild, type=type)

    monkeypatch.setattr(bot.tree, "sync", _fake_sync)
    monkeypatch.setattr(bot.tree, "clear_commands", _spy_clear)

    poller_runs = 0

    async def _fake_ttc(client):
        nonlocal poller_runs
        poller_runs += 1

    async def _fake_jobs(client):
        nonlocal poller_runs
        poller_runs += 1

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", _fake_ttc)
    monkeypatch.setattr("icbot.bot.poll_and_post_jobs", _fake_jobs)

    # First startup initializes DB, sessions, commands, sync, and cancellable tasks once
    await bot.setup_hook()
    assert bot.setup_complete is True
    assert init_calls == 1
    assert getattr(bot, "_llm_killed", False) is True
    assert sync_targets == [900000000000000001]
    assert clear_calls == 0
    assert bot.http_session is not None and not bot.http_session.closed
    assert bot.services.http_session is bot.http_session
    assert yahoo_quotes._SESSION is None
    assert btc_esplora._SESSION is None
    assert crypto_fng._SESSION is None
    assert set(bot.background_tasks.keys()) == {"ttc", "jobs"}

    # Tasks must wait_until_ready() inside their body before executing poller cycles
    await asyncio.sleep(0)
    assert poller_runs == 0

    initial_tasks = dict(bot.background_tasks)

    # Repeated setup_hook() must remain idempotent with zero duplicate inits/tasks/syncs
    await bot.setup_hook()
    assert init_calls == 1
    assert sync_targets == [900000000000000001]
    assert clear_calls == 0
    assert bot.background_tasks == initial_tasks

    # Simulate gateway READY + 2 subsequent reconnect READY dispatches
    for expected_count in (1, 2, 3):
        bot._handle_ready()
        await bot.on_ready()
        assert bot.ready_count == expected_count
        assert init_calls == 1
        assert sync_targets == [900000000000000001]
        assert clear_calls == 0
        assert bot.background_tasks == initial_tasks

    # Yield so pollers unblock from wait_until_ready() and execute one cycle before sleeping
    await asyncio.sleep(0.05)
    assert poller_runs >= 2

    owned_session = bot.http_session
    assert owned_session is not None and not owned_session.closed

    await bot.close()

    assert bot.is_closed() is True
    assert db.is_closed is True
    assert bot.setup_complete is False
    assert bot.background_tasks == {}
    assert all(task.done() and task.cancelled() for task in initial_tasks.values())
    assert owned_session.closed is True
    assert bot.http_session is None
    assert bot.services.http_session is None
    assert yahoo_quotes._SESSION is None
    assert btc_esplora._SESSION is None
    assert crypto_fng._SESSION is None

    # Repeated close is safe and idempotent
    await bot.close()


async def test_controlled_global_sync_when_guild_id_is_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        guild_id=0,
        ttc_alerts_channel_id=0,
        job_alerts_channel_id=0,
    )
    bot = create_bot(settings, sync_commands=True)

    sync_calls: list[int | None] = []

    async def _fake_global_sync(*, guild=None):
        sync_calls.append(guild.id if guild is not None else None)
        return []

    monkeypatch.setattr(bot.tree, "sync", _fake_global_sync)

    try:
        await bot.setup_hook()
        assert sync_calls == [None]
        assert bot.commands_synced is True
        assert [c.name for c in bot.tree.get_commands()] == ["ic"]

        # Reconnect does not re-sync globally
        bot._handle_ready()
        await bot.on_ready()
        await bot.on_ready()
        assert sync_calls == [None]
    finally:
        await bot.close()


async def test_command_sync_error_keeps_local_tree_and_single_task_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        job_alerts_channel_id=0,
    )
    bot = create_bot(settings, sync_commands=True)

    async def _failing_sync(*, guild=None):
        http_exc = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),  # type: ignore[arg-type]
            {"code": 50035, "message": "Invalid Form Body"},
        )
        raise app_commands.CommandSyncFailure(http_exc, [])

    monkeypatch.setattr(bot.tree, "sync", _failing_sync)

    try:
        await bot.setup_hook()
        assert bot.setup_complete is True
        assert bot.commands_synced is False
        assert isinstance(bot.last_sync_error, app_commands.CommandSyncFailure)

        # In-memory /ic group remains intact and no duplicate tasks were started
        guild_obj = discord.Object(id=settings.guild_id)
        assert [c.name for c in bot.tree.get_commands(guild=guild_obj)] == ["ic"]
        assert list(bot.background_tasks.keys()) == ["ttc"]
    finally:
        await bot.close()


async def test_task_cancellation_during_sleep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        job_alerts_channel_id=0,
        ttc_poll_seconds=3600,
    )
    bot = create_bot(settings, sync_commands=False)

    entered_sleep = asyncio.Event()
    real_sleep = asyncio.sleep

    async def _spy_sleep(delay: float):
        if delay >= 30:
            entered_sleep.set()
        await real_sleep(delay)

    cycle_done = asyncio.Event()

    async def _fake_ttc(client):
        cycle_done.set()

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", _fake_ttc)
    monkeypatch.setattr("icbot.bot.asyncio.sleep", _spy_sleep)

    await bot.setup_hook()
    ttc_task = bot.background_tasks["ttc"]

    bot._handle_ready()
    await asyncio.wait_for(cycle_done.wait(), timeout=2.0)
    await asyncio.wait_for(entered_sleep.wait(), timeout=2.0)

    # Cancelling during a 3600s sleep must unwind cleanly and immediately
    await asyncio.wait_for(bot.close(), timeout=2.0)
    assert ttc_task.done() is True
    assert ttc_task.cancelled() is True
    assert bot.background_tasks == {}


async def test_startup_failure_rolls_back_tasks_sessions_and_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(tmp_path)
    db = Database(settings.db_path)
    bot = create_bot(settings, db=db, sync_commands=False)

    created_tasks: list[asyncio.Task[None]] = []
    orig_start_tasks = bot._start_background_tasks

    def _start_then_fail():
        orig_start_tasks()
        created_tasks.extend(bot.background_tasks.values())
        raise RuntimeError("injected post-task startup failure")

    monkeypatch.setattr(bot, "_start_background_tasks", _start_then_fail)

    with pytest.raises(RuntimeError, match="injected post-task startup failure"):
        await bot.setup_hook()

    assert bot.setup_complete is False
    assert bot.background_tasks == {}
    assert len(created_tasks) == 2
    assert all(task.done() and task.cancelled() for task in created_tasks)
    assert bot.http_session is None
    assert bot.services.http_session is None
    assert bot.services.go_http_session is None
    assert yahoo_quotes._SESSION is None
    assert btc_esplora._SESSION is None
    assert crypto_fng._SESSION is None
    assert db.is_closed is True
    await bot.close()


async def test_on_message_skips_process_commands_when_no_prefix_commands_exist_and_routes_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=0,
        job_alerts_channel_id=0,
    )
    bot = create_bot(settings, register_commands=False, sync_commands=False)

    process_calls = 0

    async def _spy_process(message):
        nonlocal process_calls
        process_calls += 1

    mention_calls: list[Any] = []

    async def _spy_mention(client, message):
        mention_calls.append(message)
        return True

    monkeypatch.setattr(bot, "process_commands", _spy_process)
    monkeypatch.setattr("icbot.bot.maybe_handle_llm_mention", _spy_mention)

    assert bot.help_command is None
    assert len(bot.commands) == 0

    fake_bot_user = SimpleNamespace(id=999000000000000001, display_name="InstigatorTest")
    bot._connection.user = fake_bot_user  # type: ignore[assignment]

    channel = FakeTextChannel(channel_id=900000000000000020, name="general")
    author = build_fake_member(user_id=800000000000000001, display_name="Alice")
    author.bot = False
    msg: Any = FakeMessage(content="!help", channel=channel, author=author)
    msg.mentions = [fake_bot_user]
    msg.reference = None

    await bot.on_message(msg)
    assert len(mention_calls) == 1
    assert process_calls == 0

    @bot.command(name="ping")
    async def _ping(ctx):
        return None

    await bot.on_message(msg)
    assert process_calls == 1
    await bot.close()


async def test_command_tree_channel_gate_reads_instance_settings(tmp_path: Path, make_interaction):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        user_command_channel_id=900000000000000020,
        ttc_alerts_channel_id=0,
        job_alerts_channel_id=0,
    )
    bot = create_bot(settings, register_commands=False, sync_commands=False)

    try:
        wrong_channel = FakeTextChannel(channel_id=900000000000000099, name="other")
        inter = make_interaction(command_name="ic profile", settings=settings)
        inter.channel = wrong_channel
        inter.channel_id = wrong_channel.id

        allowed = await bot.tree.interaction_check(inter)  # type: ignore[arg-type]
        assert allowed is False
        assert inter.last_reply.ephemeral is True
        assert "Use bot commands in <#900000000000000020>" in inter.last_reply.content
    finally:
        await bot.close()


async def test_all_seven_background_tasks_wait_until_ready_and_cancel_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=900000000000000030,
        go_alerts_channel_id=900000000000000050,
        go_api_key="synthetic-go-key",
        job_alerts_channel_id=900000000000000040,
        workday_sites=["SyntheticCo|https://example.invalid/en-US/Jobs"],
        market_watch_enabled=True,
        market_watch_channel_id=900000000000000010,
        market_daily_report_enabled=True,
        btc_watch_enabled=True,
        btc_watch_channel_id=900000000000000060,
        btc_watch_addresses=["bc1qsyntheticlifecycle00000000000"],
    )
    bot = create_bot(settings, register_commands=False, sync_commands=False)

    poller_hits: dict[str, int] = {
        "ttc": 0,
        "go": 0,
        "jobs": 0,
        "workday": 0,
        "market_watch": 0,
        "daily": 0,
        "btc": 0,
    }

    async def _hit(name: str) -> None:
        poller_hits[name] += 1

    monkeypatch.setattr("icbot.bot.poll_and_post_ttc", lambda c: _hit("ttc"))
    monkeypatch.setattr("icbot.bot.poll_and_post_go_alerts", lambda c: _hit("go"))
    monkeypatch.setattr("icbot.bot.poll_and_post_jobs", lambda c: _hit("jobs"))
    monkeypatch.setattr("icbot.bot.poll_and_post_workday_jobs", lambda c: _hit("workday"))
    monkeypatch.setattr("icbot.bot.poll_and_post_market_watch", lambda c: _hit("market_watch"))
    monkeypatch.setattr("icbot.bot.post_daily_market_report", lambda c: _hit("daily"))
    monkeypatch.setattr("icbot.bot.poll_and_post_btc_watch", lambda c: _hit("btc"))

    await bot.setup_hook()
    expected_tasks = {
        "ttc",
        "go",
        "jobs",
        "workday",
        "market_watch",
        "daily_market_report",
        "btc_watch",
    }
    assert set(bot.background_tasks.keys()) == expected_tasks

    # Every poller must block on wait_until_ready() until READY fires
    await asyncio.sleep(0)
    assert all(count == 0 for count in poller_hits.values())

    tasks_snapshot = dict(bot.background_tasks)
    bot._handle_ready()
    await bot.on_ready()
    await bot.on_ready()
    assert bot.background_tasks == tasks_snapshot

    await asyncio.sleep(0.05)
    for key in ("ttc", "go", "jobs", "workday", "market_watch", "btc"):
        assert poller_hits[key] >= 1

    health = bot.task_health()
    assert expected_tasks | {"llm_assistant"} == set(health.keys())

    await bot.close()
    assert bot.background_tasks == {}
    assert all(task.done() and task.cancelled() for task in tasks_snapshot.values())


async def test_partial_http_session_failure_rolls_back_and_allows_clean_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=0,
        job_alerts_channel_id=0,
        go_ssl_verify=False,
    )
    db = Database(settings.db_path)
    bot = create_bot(settings, db=db, register_commands=False, sync_commands=False)

    orig_go_init = bot.services.ensure_go_http_session
    should_fail = True

    async def _flaky_go_init(eff_settings=None):
        if should_fail:
            raise OSError("injected go session init failure")
        return await orig_go_init(eff_settings)

    monkeypatch.setattr(bot.services, "ensure_go_http_session", _flaky_go_init)

    with pytest.raises(OSError, match="injected go session init failure"):
        await bot.setup_hook()

    # Already-opened shared http_session and db must all be rolled back cleanly
    assert bot.setup_complete is False
    assert bot.http_session is None
    assert bot.services.http_session is None
    assert bot.services.go_http_session is None
    assert yahoo_quotes._SESSION is None
    assert btc_esplora._SESSION is None
    assert crypto_fng._SESSION is None
    assert db.is_closed is True

    # Recover cleanly on retry once fault clears
    should_fail = False
    await bot.setup_hook()
    assert bot.setup_complete is True
    assert db.is_closed is False
    assert bot.http_session is not None and not bot.http_session.closed
    assert bot.services.http_session is bot.http_session
    assert bot.services.go_http_session is not None and not bot.services.go_http_session.closed
    assert yahoo_quotes._SESSION is None
    assert btc_esplora._SESSION is None
    assert crypto_fng._SESSION is None

    shared_sess = bot.http_session
    go_sess = bot.services.go_http_session
    await bot.close()
    assert shared_sess.closed is True
    assert go_sess.closed is True
    assert bot.services.go_http_session is None


async def test_command_tree_on_error_handles_cooldown_checkfailure_and_unexpected_error(
    tmp_path: Path, make_interaction
):
    from icbot.bot import create_bot

    settings = _make_lifecycle_settings(
        tmp_path,
        ttc_alerts_channel_id=0,
        job_alerts_channel_id=0,
    )
    bot = create_bot(settings, register_commands=False, sync_commands=False)

    try:
        cooldown_inter = make_interaction(command_name="ic help", settings=settings)
        cooldown_err = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 65.0), 65.0)
        await bot.tree.on_error(cooldown_inter, cooldown_err)  # type: ignore[arg-type]
        assert cooldown_inter.last_reply.ephemeral is True
        assert "cooldown" in cooldown_inter.last_reply.content.lower()

        check_inter = make_interaction(command_name="ic help", settings=settings)
        await bot.tree.on_error(check_inter, app_commands.CheckFailure("denied"))  # type: ignore[arg-type]
        assert check_inter.all_replies == []

        err_inter = make_interaction(command_name="ic help", settings=settings)
        await bot.tree.on_error(err_inter, app_commands.AppCommandError("boom"))  # type: ignore[arg-type]
        assert err_inter.last_reply.ephemeral is True
        assert "unexpected error" in err_inter.last_reply.content.lower()
    finally:
        await bot.close()
