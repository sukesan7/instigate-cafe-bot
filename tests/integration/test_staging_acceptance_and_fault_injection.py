"""Integration tests for startup/shutdown lifecycle, fault injection, concurrency, and SQLite recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from icbot.academics.exams_service import ExamStore
from icbot.academics.parsers import parse_notes_spa_html
from icbot.bot import create_bot
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.admin import setup_cmd
from icbot.features.exams import exams_add_cmd, exams_clear_cmd, exams_cmd, exams_export_cmd, exams_remove_cmd
from icbot.features.profile import profile_cmd
from icbot.registry import load_feature_modules, registered_qualified_names
from icbot.services.alert_dedupe import AlertDedupeManager, AlertLifecycleStatus
from icbot.services.user_service import DailyAlreadyClaimedError, UserStore
from tests.helpers import FakeClient, FakeInteraction, FakeTextChannel, build_fake_member
from tools.backup_db import backup_database, restore_database

STAGING_GUILD_ALPHA = 900_000_000_000_000_001
STAGING_GUILD_BETA = 900_000_000_000_000_002
ADMIN_ROLE_ID = 888_000_000_000_000_001


def _make_interaction(
    *,
    user: object,
    guild: object,
    settings: Settings,
    db: Database,
    command_name: str,
) -> FakeInteraction:
    client = FakeClient(settings=settings, db=db)
    return FakeInteraction(user=user, client=client, guild=guild, command_name=command_name)


@pytest.mark.asyncio
async def test_no_key_container_startup_and_command_tree_sync(tmp_path: Path) -> None:
    """Verify container startup with empty keys and command-tree sync + clean restart lifecycle."""
    db_path = tmp_path / "lifecycle.db"
    settings = Settings(
        guild_id=STAGING_GUILD_ALPHA,
        token="",
        gemini_api_key="",
        go_api_key="",
        odds_api_key="",
        db_path=str(db_path),
        allow_gateway=False,
        llm_enabled=False,
        jobs_poll_seconds=0,
    )
    db = Database(str(db_path))
    await db.init()

    try:
        services = ServiceContainer.from_settings(settings, db=db)
        u_store = services.user_store(settings)
        await u_store.ensure_user(111, "NoKeyUser")
        assert await u_store.get_balance(111) == 1000

        bot1 = create_bot(settings=settings, db=db, services=services, register_commands=True)
        bot1._register_commands()
        load_feature_modules()
        assert len(registered_qualified_names()) == 53
        assert len(bot1.tree.get_commands(guild=bot1.target_sync_guild)) > 0
        tasks1 = list(bot1.supervisor.health_snapshot().keys())
        assert len(tasks1) == 8
        assert "ttc" in tasks1
        await bot1._cleanup_runtime()

        # Re-instantiating bot after clean shutdown preserves deterministic supervisor task set
        bot2 = create_bot(settings=settings, db=db, services=services, register_commands=True)
        tasks2 = list(bot2.supervisor.health_snapshot().keys())
        assert tasks1 == tasks2
        await bot2._cleanup_runtime()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exam_crud_export_deferral_and_non_admin_denial(tmp_path: Path) -> None:
    """Verify exam add/list/export(.ics)/remove/clear workflow, profile deferral, and non-admin denial."""
    db_path = tmp_path / "exam_crud.db"
    settings = Settings(
        db_path=str(db_path),
        allow_gateway=False,
        allowed_role_ids=[ADMIN_ROLE_ID],
    )
    db = Database(str(db_path))
    await db.init()

    try:
        guild = SimpleNamespace(id=STAGING_GUILD_ALPHA, name="Staging Alpha")
        admin = build_fake_member(
            user_id=303, display_name="ExamLead", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        member = build_fake_member(
            user_id=606, display_name="RegularStudent", role_ids=[777_000_000], administrator=False
        )

        # Non-admin rejected ephemerally from admin setup
        inter_denied = _make_interaction(
            user=member, guild=guild, settings=settings, db=db, command_name="ic admin setup"
        )
        await setup_cmd.callback(inter_denied)
        msg_denied = (
            inter_denied.followup.messages[0]
            if inter_denied.followup.messages
            else inter_denied.response.sent_messages[0]
        )
        assert msg_denied.ephemeral is True
        denied_text = (msg_denied.content or "") + (msg_denied.embed.description if msg_denied.embed else "")
        assert "permission" in denied_text.lower()

        # Admin adds, lists, exports (.ics), removes, and clears exam
        inter_add = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic exam add"
        )
        await exams_add_cmd.callback(
            inter_add,
            course_code="CPS209",
            course_name="Computer Science II",
            date="2026-12-18",
            start_time="14:00",
            end_time="16:00",
            location="TRSM 1-067",
        )
        exams = await ExamStore(db).list_exams(STAGING_GUILD_ALPHA)
        assert len(exams) == 1

        inter_list = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic exam list"
        )
        await exams_cmd.callback(inter_list)
        list_msg = inter_list.followup.messages[0]
        assert list_msg.embed is not None
        assert any("CPS209" in f.name for f in list_msg.embed.fields)

        inter_exp = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic exam export"
        )
        await exams_export_cmd.callback(inter_exp)
        assert inter_exp.followup.messages[0].file is not None

        inter_rem = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic exam remove"
        )
        await exams_remove_cmd.callback(inter_rem, exam_id=exams[0].id)
        assert await ExamStore(db).list_exams(STAGING_GUILD_ALPHA) == []

        inter_clr = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic exam clear"
        )
        await exams_clear_cmd.callback(inter_clr)

        # Profile defers immediately with thinking=True before sending followup
        inter_prof = _make_interaction(
            user=member, guild=guild, settings=settings, db=db, command_name="ic profile"
        )
        await profile_cmd.callback(inter_prof)
        assert inter_prof.response.deferred is True
        assert inter_prof.response.thinking is True
        assert len(inter_prof.followup.messages) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_guild_isolation_and_alert_deduplication(tmp_path: Path) -> None:
    """Verify two-guild tenancy isolation and AlertDedupeManager cross-poll suppression."""
    db_path = tmp_path / "isolation_dedupe.db"
    db = Database(str(db_path))
    await db.init()

    try:
        u_store = UserStore(db, guild_id=STAGING_GUILD_ALPHA)
        await u_store.ensure_user(555, "MultiGuildUser", guild_id=STAGING_GUILD_ALPHA)
        await u_store.add_balance(
            555, 500, category="grant", reference_id="Alpha Grant", guild_id=STAGING_GUILD_ALPHA
        )
        await u_store.ensure_user(555, "MultiGuildUser", guild_id=STAGING_GUILD_BETA)
        assert await u_store.get_balance(555, guild_id=STAGING_GUILD_ALPHA) == 1500
        assert await u_store.get_balance(555, guild_id=STAGING_GUILD_BETA) == 1000

        deduper = AlertDedupeManager()
        channel = FakeTextChannel(channel_id=888999000, name="ttc-alerts")
        notice_id = "ttc-notice-45812"
        content_hash = "hash_45812_v1"

        status_1 = await deduper.check_alert(
            db, STAGING_GUILD_ALPHA, "ttc", channel.id, notice_id, content_hash
        )
        assert status_1 == AlertLifecycleStatus.NEW_EVENT
        await channel.send("Line 1 resumed")
        await deduper.record_delivery(db, STAGING_GUILD_ALPHA, "ttc", channel.id, notice_id, content_hash)

        status_2 = await deduper.check_alert(
            db, STAGING_GUILD_ALPHA, "ttc", channel.id, notice_id, content_hash
        )
        assert status_2 == AlertLifecycleStatus.IDENTICAL_DUPLICATE
        assert len(channel.sent) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_fault_injections_lock_disk_full_crash_and_race(tmp_path: Path) -> None:
    """Verify locked DB, simulated disk-full rollback, crash rollback across reopen, and 10-way claim race."""
    # 1. Exclusive lock contention surfaces cleanly
    lock_db_path = tmp_path / "locked.db"
    lock_db = Database(str(lock_db_path))
    await lock_db.init()
    raw_conn = sqlite3.connect(str(lock_db_path))
    raw_conn.execute("BEGIN EXCLUSIVE;")
    try:
        with pytest.raises((sqlite3.OperationalError, OSError), match=r"locked|busy"):
            await lock_db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('k', 'v');")
    finally:
        raw_conn.rollback()
        raw_conn.close()
        await lock_db.close()

    # 2. Disk-full failure mid-transaction rolls back cleanly without corrupting DB
    disk_db_path = tmp_path / "disk_full.db"
    disk_db = Database(str(disk_db_path))
    await disk_db.init()
    try:
        with pytest.raises(OSError) as exc_info:
            async with disk_db.write_transaction() as tx:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('disk_test', 'payload');")
                raise OSError(28, "No space left on device")
        assert exc_info.value.errno == 28
        assert await disk_db.fetchone("SELECT value FROM meta WHERE key='disk_test';") is None
    finally:
        await disk_db.close()

    # 3. Abrupt abort mid-transaction rolls back balance mutation upon fresh DB reopen
    crash_db_path = tmp_path / "crash.db"
    crash_db = Database(str(crash_db_path))
    await crash_db.init()
    await UserStore(crash_db, guild_id=STAGING_GUILD_ALPHA).ensure_user(808, "CrashUser")
    try:
        async with crash_db.write_transaction() as tx:
            await tx.execute("UPDATE users SET balance=balance+99999 WHERE user_id=808;")
            raise SystemExit("Simulated SIGKILL")
    except SystemExit:
        pass
    await crash_db.close()

    reopened = Database(str(crash_db_path))
    await reopened.init()
    try:
        assert await UserStore(reopened, guild_id=STAGING_GUILD_ALPHA).get_balance(808) == 1000
    finally:
        await reopened.close()

    # 4. 10 simultaneous daily claims permit exact single winner
    race_db_path = tmp_path / "race.db"
    race_db = Database(str(race_db_path))
    await race_db.init()
    try:
        r_store = UserStore(race_db, guild_id=STAGING_GUILD_ALPHA)
        await r_store.ensure_user(909, "RapidClicker")

        async def _try_claim() -> bool:
            try:
                await r_store.claim_daily(909)
                return True
            except DailyAlreadyClaimedError:
                return False

        outcomes = await asyncio.gather(*(_try_claim() for _ in range(10)))
        assert sum(1 for won in outcomes if won) == 1
        assert await r_store.get_balance(909) == 1100
    finally:
        await race_db.close()


@pytest.mark.asyncio
async def test_malformed_html_and_hot_backup_restore_schema_health(tmp_path: Path) -> None:
    """Verify malformed SPA HTML degrades cleanly and WAL hot backup/restore preserves Schema v7 health."""
    parsed = parse_notes_spa_html(
        "<html><head><title>Broken<body<div><script>var x = 1;",
        base_url="https://example.invalid",
    )
    assert parsed is not None
    assert len(parsed.links) == 0

    live_path = tmp_path / "live.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"

    db = Database(str(live_path))
    await db.init()
    health = await db.get_storage_health()
    assert health.integrity_ok is True
    assert health.foreign_keys_ok is True
    assert health.schema_version == 7

    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('backup_key', 'live_data');")
    assert backup_database(live_path, backup_path) is True
    await db.close()

    assert restore_database(backup_path, restored_path) is True
    r_conn = sqlite3.connect(str(restored_path))
    try:
        assert r_conn.execute("PRAGMA integrity_check;").fetchone() == ("ok",)
        assert r_conn.execute("SELECT value FROM meta WHERE key='backup_key';").fetchone() == ("live_data",)
    finally:
        r_conn.close()
