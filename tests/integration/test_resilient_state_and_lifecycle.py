"""Integration test suite for storage resilience, online backups, disaster recovery,

retention pruning, data portability, and fail-closed lifecycle invariants.
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest

from icbot.db import (
    MIGRATIONS,
    Database,
    MigrationError,
    MigrationIntegrityError,
    StorageHealthReport,
)
from icbot.errors import ConfigValidationError
from icbot.guild_config import GuildSettingsStore
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import UserStore

pytestmark = pytest.mark.storage


# ---------------------------------------------------------------------------
# 1. Storage Health Diagnostics
# ---------------------------------------------------------------------------


async def test_storage_health_report_accuracy(tmp_path: Path) -> None:
    """Verify get_storage_health reports accurate page metrics, row counts, and integrity status."""
    db_file = tmp_path / "storage_health.db"
    db = Database(str(db_file))
    await db.init()
    try:
        # Populate sample rows
        user_store = UserStore(db, guild_id=123)
        await user_store.ensure_user(456, display_name="HealthTestUser")
        await user_store.add_balance(456, 250, category="reward", reference_id="diagnostic test")

        report: StorageHealthReport = await db.get_storage_health()
        assert isinstance(report, StorageHealthReport)
        assert report.integrity_ok is True
        assert "ok" in report.integrity_message
        assert report.foreign_keys_ok is True
        assert report.foreign_key_violations == []
        assert report.journal_mode == "wal"
        assert report.page_size >= 512
        assert report.page_count > 0
        assert report.freelist_count >= 0
        assert report.db_size_bytes > 0
        assert report.schema_version == len(MIGRATIONS)
        assert report.applied_migrations == [m.version for m in MIGRATIONS]
        assert report.table_row_counts.get("users", 0) >= 1
        assert report.table_row_counts.get("economy_ledger", 0) >= 1
        assert report.is_read_only is False
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 2. Online Backup & Restore Drill
# ---------------------------------------------------------------------------


async def test_online_backup_creates_valid_integrity_checked_snapshot(tmp_path: Path) -> None:
    """Verify online backup streams a transactionally consistent, verified snapshot to disk."""
    src_file = tmp_path / "source.db"
    backup_file = tmp_path / "backups" / "snapshot_01.db"

    db = Database(str(src_file))
    await db.init()
    try:
        # Insert test data
        u_store = UserStore(db, guild_id=555)
        await u_store.ensure_user(777, display_name="BackupUser")
        await u_store.add_balance(777, 1200, category="grant")

        market_store = FunMarketStore(db, guild_id=555)
        await market_store.sync_catalog()

        # Perform verified online backup
        result_path = await db.backup(str(backup_file), verify_integrity=True)
        assert result_path.exists()
        assert result_path == backup_file

        # Verify backup contains identical records
        async with aiosqlite.connect(str(backup_file)) as bkp_conn:
            cur = await bkp_conn.execute(
                "SELECT balance, display_name FROM users WHERE guild_id=555 AND user_id=777"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == 2200  # starting 1000 + 1200
            assert row[1] == "BackupUser"

            cur_chk = await bkp_conn.execute("PRAGMA integrity_check")
            chk_row = await cur_chk.fetchone()
            assert chk_row is not None and chk_row[0] == "ok"
    finally:
        await db.close()


async def test_online_restore_drill_restores_snapshot_and_reverts_subsequent_changes(tmp_path: Path) -> None:
    """Disaster recovery drill: restore active DB from backup and verify state reverts to snapshot."""
    active_file = tmp_path / "production.db"
    backup_file = tmp_path / "safe_checkpoint.db"

    db = Database(str(active_file))
    await db.init()
    try:
        u_store = UserStore(db, guild_id=999)
        await u_store.ensure_user(111, display_name="CheckpointUser")
        await u_store.add_balance(111, 500, category="initial_fund")

        # 1. Take clean backup at checkpoint A
        await db.backup(str(backup_file), verify_integrity=True)

        # 2. Simulate disaster or accidental corruption/deletion (Checkpoint B)
        await db.execute("DELETE FROM users WHERE guild_id=999 AND user_id=111")
        await u_store.ensure_user(222, display_name="UnwantedUser")
        assert await u_store.get_profile(111) is None
        assert (await u_store.get_profile(222)) is not None

        # 3. Perform restore drill from checkpoint A
        await db.restore(str(backup_file), verify_integrity=True)

        # 4. Verify state is fully restored to Checkpoint A
        restored_u111 = await u_store.get_profile(111)
        assert restored_u111 is not None
        assert restored_u111.balance == 1500  # 1000 + 500
        assert restored_u111.display_name == "CheckpointUser"

        # Subsequent changes from Checkpoint B must be gone
        restored_u222 = await u_store.get_profile(222)
        assert restored_u222 is None

        # Post-restore integrity check
        await db.check_integrity()
    finally:
        await db.close()


async def test_restore_rejects_corrupted_backup_file(tmp_path: Path) -> None:
    """Verify restore refuses to apply a corrupt backup and leaves live database untouched."""
    active_file = tmp_path / "live.db"
    corrupt_backup = tmp_path / "corrupt.db"

    # Write garbage into corrupt backup file
    corrupt_backup.write_bytes(b"SQLite format 3\x00this is completely corrupted garbage data\xff\xfe")

    db = Database(str(active_file))
    await db.init()
    try:
        u_store = UserStore(db, guild_id=101)
        await u_store.ensure_user(202, display_name="ProtectedUser")

        with pytest.raises(MigrationIntegrityError, match="Cannot restore from corrupt backup"):
            await db.restore(str(corrupt_backup), verify_integrity=True)

        # Live database remains intact
        prof = await u_store.get_profile(202)
        assert prof is not None
        assert prof.display_name == "ProtectedUser"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 3. Fail-Closed Migrations on Incompatible Schema
# ---------------------------------------------------------------------------


async def test_fail_closed_on_incompatible_future_schema(tmp_path: Path) -> None:
    """Verify db.migrate() aborts immediately if installed schema version > max supported migration."""
    db_file = tmp_path / "future_schema.db"

    # Create database with artificially high user_version
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.execute("PRAGMA user_version = 9999;")
        await conn.commit()

    db = Database(str(db_file))
    try:
        with pytest.raises(MigrationError, match="newer than the highest supported migration version"):
            await db.migrate()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 4. Retention & Stale Data Pruning
# ---------------------------------------------------------------------------


async def test_stale_data_pruning_removes_old_caches_and_preserves_immutable_ledger(tmp_path: Path) -> None:
    """Verify prune_stale_data purges expired transient entries while strictly preserving economy_ledger."""
    db_file = tmp_path / "prune_test.db"
    db = Database(str(db_file))
    await db.init()
    try:
        now = 1800000000
        sec_per_day = 86400

        # Insert job_seen: 1 stale (100 days old), 1 fresh (5 days old)
        await db.execute(
            "INSERT INTO job_seen (source, external_id, first_seen_unix, title, url) VALUES (?, ?, ?, ?, ?)",
            ("gh", "old_job", now - 100 * sec_per_day, "Old Job", "https://old"),
        )
        await db.execute(
            "INSERT INTO job_seen (source, external_id, first_seen_unix, title, url) VALUES (?, ?, ?, ?, ?)",
            ("gh", "fresh_job", now - 5 * sec_per_day, "Fresh Job", "https://fresh"),
        )

        # Insert alert_delivery_dedupes: 1 stale (40 days old), 1 fresh (2 days old)
        await db.execute(
            "INSERT INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at) VALUES (?, ?, ?, ?)",
            (10, "ttc", "stale_alert", now - 40 * sec_per_day),
        )
        await db.execute(
            "INSERT INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at) VALUES (?, ?, ?, ?)",
            (10, "ttc", "fresh_alert", now - 2 * sec_per_day),
        )

        # Insert workday_cxs_cache: 1 stale (20 days old), 1 fresh (1 day old)
        await db.execute(
            "INSERT INTO workday_cxs_cache (careers_url, api_url, tenant, site, last_updated_unix) VALUES (?, ?, ?, ?, ?)",
            ("https://old.wd", "https://api.old", "t1", "s1", now - 20 * sec_per_day),
        )
        await db.execute(
            "INSERT INTO workday_cxs_cache (careers_url, api_url, tenant, site, last_updated_unix) VALUES (?, ?, ?, ?, ?)",
            ("https://fresh.wd", "https://api.fresh", "t2", "s2", now - 1 * sec_per_day),
        )

        # Insert immutable economy_ledger entry
        u_store = UserStore(db, guild_id=10)
        await u_store.ensure_user(1, display_name="LedgerUser")
        await u_store.add_balance(1, 500, category="audit_grant", reference_id="must survive pruning")

        # Execute pruning with test clock
        pruned = await db.prune_stale_data(
            now_ts=now,
            job_seen_days=90,
            alert_dedupe_days=30,
            workday_cache_days=14,
            grades_cache_days=180,
        )

        assert pruned["job_seen"] == 1
        assert pruned["alert_delivery_dedupes"] == 1
        assert pruned["workday_cxs_cache"] == 1

        # Verify remaining rows
        js_rows = await db.fetchall("SELECT external_id FROM job_seen")
        assert [r[0] for r in js_rows] == ["fresh_job"]

        ad_rows = await db.fetchall("SELECT dedupe_key FROM alert_delivery_dedupes")
        assert [r[0] for r in ad_rows] == ["fresh_alert"]

        wd_rows = await db.fetchall("SELECT careers_url FROM workday_cxs_cache")
        assert [r[0] for r in wd_rows] == ["https://fresh.wd"]

        # Crucial check: economy_ledger was NOT pruned
        ledger = await u_store.get_ledger(1)
        assert len(ledger) >= 1
        assert any(e.category == "audit_grant" for e in ledger)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 5. User Data Portability & Right-to-be-Forgotten Deletion
# ---------------------------------------------------------------------------


async def test_user_data_export_and_right_to_be_forgotten_deletion(tmp_path: Path) -> None:
    """Verify GDPR export_user_data portability and delete_user_data with immutable ledger preservation."""
    db = Database(str(tmp_path / "privacy_test.db"))
    await db.init()
    try:
        gid = 888
        uid = 999
        u_store = UserStore(db, guild_id=gid)
        m_store = FunMarketStore(db, guild_id=gid)

        # Setup user profile and economy balance
        await u_store.ensure_user(uid, display_name="DataSubject", avatar_url="https://avatar.png")
        await u_store.add_balance(uid, 500, category="seed")

        # Setup market catalog and position
        await m_store.sync_catalog()
        await m_store.buy_long(user_id=uid, symbol="IC", qty=2)

        # 1. Export User Data (Data Portability)
        export = await u_store.export_user_data(uid)
        assert export["format_version"] == 2
        assert export["guild_id"] == gid
        assert export["user_id"] == uid
        assert export["profile"] is not None
        assert export["profile"]["display_name"] == "DataSubject"
        assert len(export["positions"]) == 1
        assert export["positions"][0]["symbol"] == "IC"
        assert len(export["trades"]) >= 1
        assert len(export["ledger"]) >= 1
        assert export["truncated"] is False

        # 2. Delete User Data (Right-to-be-Forgotten)
        deleted_counts = await u_store.delete_user_data(uid, anonymize_ledger=True)
        assert deleted_counts["users"] == 1
        assert deleted_counts["fun_positions"] == 1
        assert deleted_counts["fun_trades"] == 1
        assert deleted_counts["fun_trades_anonymized"] == 1
        assert deleted_counts["economy_ledger_preserved"] >= 1
        assert deleted_counts["economy_ledger_anonymized"] >= 1

        # Personal profile and game state must be gone
        assert await u_store.get_profile(uid) is None
        portfolio = await m_store.portfolio(user_id=uid)
        assert len(portfolio.positions) == 0

        # Original user_id has zero ledger rows; ledger records remain intact under sentinel 0
        assert await u_store.get_ledger(uid) == []
        ledger_after = await u_store.get_ledger(0)
        assert len(ledger_after) >= 1
        assert any(e.category == "seed" for e in ledger_after)
        assert all(str(uid) not in e.transaction_id for e in ledger_after)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 6. Guild Configuration Export / Import with Boundaries
# ---------------------------------------------------------------------------


async def test_guild_config_export_and_import_with_safety_boundaries(tmp_path: Path) -> None:
    """Verify non-destructive guild config export/import and rejection of forbidden secret keys."""
    db = Database(str(tmp_path / "guild_cfg.db"))
    await db.init()
    try:
        g_store = GuildSettingsStore(db)
        gid = 777111

        # Set initial overrides
        await g_store.set_overrides(
            gid,
            {
                "embed_footer_text": "Custom Footer",
                "daily_claim_amount": 500,
                "bj_starting_bankroll": 3000,
            },
        )

        # 1. Export
        exported = await g_store.export_guild_config(gid)
        assert exported["format_version"] == 1
        assert exported["guild_id"] == gid
        assert exported["overrides"]["embed_footer_text"] == "Custom Footer"
        assert exported["overrides"]["daily_claim_amount"] == 500

        # 2. Import non-destructive merge
        await g_store.import_guild_config(
            gid,
            {
                "overrides": {
                    "bj_starting_bankroll": 5000,
                    "embed_accent_color": "#00FF00",
                }
            },
            overwrite=False,
        )

        merged = await g_store.get(gid)
        assert merged is not None
        assert merged.overrides["embed_footer_text"] == "Custom Footer"  # preserved
        assert merged.overrides["bj_starting_bankroll"] == 5000  # updated
        assert merged.overrides["embed_accent_color"] == 0x00FF00  # added

        # 3. Reject process-only key or secret key
        with pytest.raises(ConfigValidationError, match="Secret key 'token'"):
            await g_store.import_guild_config(gid, {"overrides": {"token": "malicious-secret"}})

        with pytest.raises(ConfigValidationError, match="Process-level key 'db_path'"):
            await g_store.import_guild_config(gid, {"overrides": {"db_path": "/etc/passwd"}})
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 7. Fail-Safe Read-Only and Write Failure Simulation
# ---------------------------------------------------------------------------


async def test_simulated_write_failure_preserves_balance_integrity(tmp_path: Path) -> None:
    """Verify rollback on unexpected write failure prevents balance corruption or partial ledger."""
    db = Database(str(tmp_path / "rollback_test.db"))
    await db.init()
    try:
        u_store = UserStore(db, guild_id=303)
        await u_store.ensure_user(404, display_name="AtomicUser")

        initial_bal = await u_store.get_balance(404)
        assert initial_bal == 1000

        # Simulate exception during multi-step operation
        with pytest.raises(RuntimeError, match="Simulated crash mid-transaction"):
            async with db.write_transaction() as tx:
                await tx.execute(
                    "UPDATE users SET balance = balance + 5000 WHERE guild_id=303 AND user_id=404"
                )
                raise RuntimeError("Simulated crash mid-transaction")

        # Balance remains uncorrupted
        final_bal = await u_store.get_balance(404)
        assert final_bal == 1000

        # No orphaned ledger records
        ledger = await u_store.get_ledger(404)
        assert len(ledger) == 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 8. EXPLAIN QUERY PLAN Validation for Hot Queries
# ---------------------------------------------------------------------------


async def test_explain_query_plan_validates_indexed_lookups(tmp_path: Path) -> None:
    """Assert that all high-frequency lookups utilize indexes rather than unindexed table scans."""
    db = Database(str(tmp_path / "query_plan.db"))
    await db.init()
    try:
        hot_queries = [
            ("users", "SELECT * FROM users WHERE guild_id = ? AND user_id = ?"),
            (
                "economy_ledger",
                "SELECT * FROM economy_ledger WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            ),
            (
                "fun_positions",
                "SELECT * FROM fun_positions WHERE guild_id = ? AND user_id = ? AND symbol = ? AND side = ?",
            ),
            (
                "fun_trades",
                "SELECT * FROM fun_trades WHERE guild_id = ? AND user_id = ? ORDER BY ts_unix DESC",
            ),
            ("exams", "SELECT * FROM exams WHERE guild_id = ? ORDER BY start_ts ASC"),
            ("job_seen", "SELECT * FROM job_seen WHERE source = ? AND external_id = ?"),
            (
                "alert_delivery_dedupes",
                "SELECT * FROM alert_delivery_dedupes WHERE guild_id = ? AND alert_type = ? AND dedupe_key = ?",
            ),
            ("guild_settings", "SELECT * FROM guild_settings WHERE guild_id = ?"),
        ]

        for table, query in hot_queries:
            conn = await db._ensure_connected()
            cur = await conn.execute(f"EXPLAIN QUERY PLAN {query}", (1, 2, 3, 4)[: query.count("?")])
            rows = await cur.fetchall()
            plan_str = " ".join(str(r) for r in rows)
            assert "SCAN" not in plan_str or "USING INDEX" in plan_str or "SEARCH" in plan_str, (
                f"Query on {table} performed an unindexed scan: {plan_str}"
            )
            assert "SEARCH" in plan_str or "USING INDEX" in plan_str or "INTEGER PRIMARY KEY" in plan_str
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 9. Database Connection Open/Close Overhead Benchmark
# ---------------------------------------------------------------------------


async def test_database_open_close_overhead_measurement(tmp_path: Path) -> None:
    """Measure connection open/close overhead, asserting managed connection is significantly faster."""
    db_file = tmp_path / "bench.db"
    db = Database(str(db_file))
    await db.init()
    try:
        iterations = 50

        # Measure connection-per-query
        t0 = time.perf_counter()
        for _ in range(iterations):
            async with aiosqlite.connect(str(db_file)) as c:
                cur = await c.execute("SELECT 1")
                await cur.fetchone()
        ephemeral_time = time.perf_counter() - t0

        # Measure managed connection
        t1 = time.perf_counter()
        for _ in range(iterations):
            await db.fetchone("SELECT 1")
        managed_time = time.perf_counter() - t1

        # Managed connection must be faster than ephemeral connection-per-query
        assert managed_time < ephemeral_time
        assert ephemeral_time > 0
    finally:
        await db.close()
