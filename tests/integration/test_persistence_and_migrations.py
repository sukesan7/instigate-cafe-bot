"""Integration and invariant test suite for SQLite persistence, transactions, and migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from icbot.db import (
    MIGRATIONS,
    Database,
    MigrationIntegrityError,
)
from icbot.features.blackjack import BlackjackStore
from icbot.features.grades import GradesCacheStore
from icbot.services.fun_market import FunMarketError, FunMarketStore
from icbot.services.user_service import InsufficientBalanceError, UserStore

# ---------------------------------------------------------------------------
# 1. Connection management & PRAGMA configuration
# ---------------------------------------------------------------------------


async def test_managed_connection_pragmas_configured_correctly(tmp_path: Path) -> None:
    """Verify that foreign_keys, busy_timeout, WAL journal mode, and NORMAL synchronous are set."""
    db_file = tmp_path / "pragma_test.db"
    db = Database(str(db_file))
    await db.init()
    try:
        # Check foreign_keys
        fk = await db.fetchone("PRAGMA foreign_keys")
        assert fk is not None and int(fk[0]) == 1

        # Check busy_timeout
        bt = await db.fetchone("PRAGMA busy_timeout")
        assert bt is not None and int(bt[0]) >= 5000

        # Check journal_mode (disk-backed DB must be WAL)
        jm = await db.fetchone("PRAGMA journal_mode")
        assert jm is not None and str(jm[0]).lower() == "wal"

        # Check synchronous mode (NORMAL is 1)
        sync = await db.fetchone("PRAGMA synchronous")
        assert sync is not None and int(sync[0]) == 1
    finally:
        await db.close()


async def test_in_memory_connection_pragmas(tmp_path: Path) -> None:
    """In-memory databases should configure foreign keys and busy timeout safely."""
    db = Database(":memory:")
    await db.init()
    try:
        fk = await db.fetchone("PRAGMA foreign_keys")
        assert fk is not None and int(fk[0]) == 1
        bt = await db.fetchone("PRAGMA busy_timeout")
        assert bt is not None and int(bt[0]) >= 5000
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 2. Transaction Boundaries, Commit, Rollback, and Cancellation
# ---------------------------------------------------------------------------


async def test_transaction_commits_cleanly_on_exit(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "tx_commit.db"))
    await db.init()
    try:
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO meta (key, value) VALUES ('k1', 'v1')")

        row = await db.fetchone("SELECT value FROM meta WHERE key='k1'")
        assert row is not None and row[0] == "v1"
    finally:
        await db.close()


async def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "tx_err.db"))
    await db.init()
    try:
        with pytest.raises(RuntimeError, match="Simulated application error"):
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO meta (key, value) VALUES ('k_rollback', 'v_err')")
                raise RuntimeError("Simulated application error")

        row = await db.fetchone("SELECT value FROM meta WHERE key='k_rollback'")
        assert row is None
    finally:
        await db.close()


async def test_transaction_rolls_back_on_task_cancellation(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "tx_cancel.db"))
    await db.init()
    try:
        started = asyncio.Event()

        async def worker() -> None:
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO meta (key, value) VALUES ('k_cancel', 'v_cancel')")
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        row = await db.fetchone("SELECT value FROM meta WHERE key='k_cancel'")
        assert row is None
    finally:
        await db.close()


async def test_nested_transaction_raises_defensive_error(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "tx_nested.db"))
    await db.init()
    try:
        async with db.transaction() as tx:
            assert tx is not None
            with pytest.raises(RuntimeError, match="Nested transactions are not supported"):
                async with db.transaction():
                    pass
    finally:
        await db.close()


async def test_standalone_queries_inside_transaction_reuse_active_tx(tmp_path: Path) -> None:
    """When inside a transaction, db.fetchone/db.fetchall reuse the active transaction context."""
    db = Database(str(tmp_path / "tx_reuse.db"))
    await db.init()
    try:
        async with db.write_transaction() as tx:
            await tx.execute("INSERT INTO meta (key, value) VALUES ('active_tx', 'found')")
            # db.fetchone should see the uncommitted insert without deadlocking
            val = await db.fetchone("SELECT value FROM meta WHERE key='active_tx'")
            assert val is not None and val[0] == "found"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 3. Connection-local last_insert_rowid Defect Reproduction & Exams Verification
# ---------------------------------------------------------------------------


async def test_legacy_last_insert_rowid_cross_connection_defect_reproduction(tmp_path: Path) -> None:
    """Prove the historical defect: calling last_insert_rowid() on a separate connection yields 0.

    In the pre-modernized codebase, execute() opened and closed its own connection,
    causing any subsequent fetchone("SELECT last_insert_rowid()") on a new connection to return 0.
    In contrast, execute_insert() on the managed transaction returns the real inserted row ID.
    """
    db_path = str(tmp_path / "defect_repro.db")

    # Historical behavior: separate connections
    async with aiosqlite.connect(db_path) as conn1:
        await conn1.execute(
            """
            CREATE TABLE test_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        )
        await conn1.execute("INSERT INTO test_items (name) VALUES ('item_a')")
        await conn1.commit()

    async with aiosqlite.connect(db_path) as conn2:
        cur2 = await conn2.execute("SELECT last_insert_rowid()")
        row2 = await cur2.fetchone()
        # Defect proven: connection 2 has no knowledge of connection 1's insert, returning 0
        assert row2 is not None
        assert row2[0] == 0

    # Modernized behavior: managed Database execute_insert returns valid primary key ID
    db = Database(db_path)
    try:
        rowid = await db.execute_insert("INSERT INTO test_items (name) VALUES ('item_b')")
        assert rowid == 2  # item_a was 1, item_b is 2

        # Verify within write_transaction as well
        async with db.write_transaction() as tx:
            tx_rowid = await tx.execute_insert("INSERT INTO test_items (name) VALUES ('item_c')")
            assert tx_rowid == 3
    finally:
        await db.close()


async def test_exams_insert_returns_valid_id_and_atomic_remove(tmp_path: Path) -> None:
    """Verify exam addition returns a valid >0 ID and removal succeeds by that exact ID."""
    db = Database(str(tmp_path / "exams_test.db"))
    await db.init()
    try:
        now = 1700000000
        exam_id = await db.execute_insert(
            """
            INSERT INTO exams (course_code, course_name, start_ts, end_ts, location, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CPS109",
                "Computer Science I",
                now + 3600,
                now + 7200,
                "ENG 101",
                "Calculator allowed",
                12345,
                now,
            ),
        )
        assert exam_id > 0

        # Verify query by ID
        exam = await db.fetchone(
            "SELECT course_code, location FROM exams WHERE id=?",
            (exam_id,),
        )
        assert exam is not None
        assert exam[0] == "CPS109"
        assert exam[1] == "ENG 101"

        # Verify removal by ID
        async with db.write_transaction() as tx:
            row = await tx.fetchone("SELECT id FROM exams WHERE id=?", (exam_id,))
            assert row is not None
            await tx.execute("DELETE FROM exams WHERE id=?", (exam_id,))

        # Verify gone
        post_remove = await db.fetchone("SELECT id FROM exams WHERE id=?", (exam_id,))
        assert post_remove is None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 4. Migrations, Ledger, Idempotency, and Dry-Run
# ---------------------------------------------------------------------------


async def test_migrations_applied_sequentially_and_recorded(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "mig_seq.db"))
    applied = await db.migrate()
    try:
        assert len(applied) == len(MIGRATIONS)
        assert [m.version for m in applied] == [1, 2, 3, 4, 5]

        # Verify ledger table
        rows = await db.fetchall("SELECT version, name FROM schema_migrations ORDER BY version ASC")
        assert len(rows) == 5
        assert rows[0] == (1, "0001_baseline_schema")
        assert rows[1] == (2, "0002_grades_cache")
        assert rows[2] == (3, "0003_indexes_and_integrity")
        assert rows[3] == (4, "0004_guild_scoped_economy_and_ledger")
        assert rows[4] == (5, "0005_guild_settings_audit")

        # Verify user_version pragma matches latest migration
        uv = await db.fetchone("PRAGMA user_version")
        assert uv is not None and uv[0] == 5
    finally:
        await db.close()


async def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "mig_idempotent.db"))
    await db.init()
    try:
        # Second migration attempt should detect no pending migrations
        plan = await db.plan_migrations()
        assert len(plan) == 0

        second_applied = await db.migrate()
        assert len(second_applied) == 0
    finally:
        await db.close()


async def test_migrate_dry_run_does_not_mutate_schema(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "mig_dryrun.db"))
    try:
        # Dry run against uninitialized database
        plan = await db.migrate(dry_run=True)
        assert len(plan) == len(MIGRATIONS)

        # Confirm that application tables were NOT created
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        )
        assert len(tables) == 0
    finally:
        await db.close()


async def test_forward_upgrade_from_synthetic_legacy_database(tmp_path: Path) -> None:
    """Verify safe forward migration on a synthetic database created before the migration ledger existed."""
    legacy_file = tmp_path / "legacy_cafe.db"

    # Create synthetic legacy database with pre-migration table structures and existing data
    async with aiosqlite.connect(str(legacy_file)) as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await raw_conn.execute("INSERT INTO meta (key, value) VALUES ('stock_value', '42')")

        await raw_conn.execute(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 1000,
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await raw_conn.execute(
            "INSERT INTO users (user_id, balance, created_at) VALUES (1001, 5555, 1600000000)"
        )

        # Legacy course_cache without namespaced table
        await raw_conn.execute(
            """
            CREATE TABLE course_cache (
                course_code TEXT PRIMARY KEY,
                title TEXT,
                parsed_data JSON,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await raw_conn.execute(
            "INSERT INTO course_cache (course_code, title, parsed_data) VALUES ('cps109', 'CS I', '[{\"name\": \"Prof A\"}]')"
        )
        await raw_conn.commit()

    # Open with modernized Database and run migrations
    db = Database(str(legacy_file))
    await db.init()
    try:
        # Verify pre-existing data is completely preserved
        user_row = await db.fetchone("SELECT user_id, balance FROM users WHERE user_id=1001")
        assert user_row == (1001, 5555)

        stock_val = await db.fetchone("SELECT value FROM meta WHERE key='stock_value'")
        assert stock_val == ("42",)

        # Verify backfill of namespaced grades_course_cache from legacy table
        cached = await db.fetchone(
            "SELECT course_code, title FROM grades_course_cache WHERE course_code='cps109'"
        )
        assert cached == ("cps109", "CS I")

        # Verify ledger contains all migrations
        ledger = await db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        assert [r[0] for r in ledger] == [1, 2, 3, 4, 5]
    finally:
        await db.close()


async def test_preflight_integrity_check_rejects_corrupt_database(tmp_path: Path) -> None:
    """Preflight check must fail with MigrationIntegrityError if the SQLite header or file is corrupt."""
    corrupt_file = tmp_path / "corrupt.db"
    # Write garbage bytes into the database file
    corrupt_file.write_bytes(b"Corrupted sqlite header content and invalid data blocks" * 20)

    db = Database(str(corrupt_file))
    try:
        with pytest.raises((MigrationIntegrityError, aiosqlite.DatabaseError)):
            await db.migrate()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 5. Hot Backup Utility
# ---------------------------------------------------------------------------


async def test_database_backup_creates_consistent_hot_copy(tmp_path: Path) -> None:
    src_file = tmp_path / "primary.db"
    dst_file = tmp_path / "backup.db"

    db = Database(str(src_file))
    await db.init()
    try:
        await db.execute("INSERT INTO meta (key, value) VALUES ('backup_check', 'valid')")
        await db.execute("INSERT INTO users (user_id, balance) VALUES (999, 8888)")

        # Execute online backup
        await db.backup(str(dst_file))
    finally:
        await db.close()

    # Verify backup database independently
    backup_db = Database(str(dst_file))
    try:
        meta_row = await backup_db.fetchone("SELECT value FROM meta WHERE key='backup_check'")
        assert meta_row == ("valid",)

        user_row = await backup_db.fetchone("SELECT balance FROM users WHERE user_id=999")
        assert user_row == (8888,)

        # Preflight checks on backup copy
        await backup_db.check_integrity()
    finally:
        await backup_db.close()


# ---------------------------------------------------------------------------
# 6. Domain Store Transactional Atomicity
# ---------------------------------------------------------------------------


async def test_user_store_subtract_balance_atomic_reversion_on_insufficient_funds(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "user_atomic.db"))
    await db.init()
    try:
        store = UserStore(db, starting_balance=100)
        await store.ensure_user(1)

        with pytest.raises(InsufficientBalanceError):
            await store.subtract_balance(1, 200)

        # Balance remains untouched
        prof = await store.get_profile(1)
        assert prof.balance == 100
    finally:
        await db.close()


async def test_blackjack_store_reserve_wager_atomicity(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "bj_atomic.db"))
    await db.init()
    try:
        store = BlackjackStore(db, starting_bankroll=250)
        await store.ensure_user(42)

        # Insufficient wager raises error and leaves bankroll unchanged
        with pytest.raises(InsufficientBalanceError):
            await store.reserve_wager(42, 500)

        bankroll = await store.get_bankroll(42)
        assert bankroll == 250

        # Valid wager reserves atomically
        new_br = await store.reserve_wager(42, 100)
        assert new_br == 150
        assert await store.get_bankroll(42) == 150

        # Refund restores bankroll
        refunded = await store.refund_wager(42, 100)
        assert refunded == 250
        assert await store.get_bankroll(42) == 250
    finally:
        await db.close()


async def test_fun_market_store_buy_atomic_rollback_on_failure(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "market_atomic.db"))
    await db.init()
    try:
        store = FunMarketStore(db, starting_bankroll=50)
        user_store = UserStore(db, starting_balance=50)
        await user_store.ensure_user(77)

        # Attempting to buy with insufficient bankroll must raise FunMarketError and leave bankroll and positions untouched
        with pytest.raises(FunMarketError, match="Insufficient bankroll"):
            # Stock price is ~10-100, buying 100 shares exceeds $50 bankroll
            await store.buy_long(user_id=77, symbol="IC", qty=100)

        # Bankroll untouched
        snap = await store.portfolio(user_id=77)
        assert snap.cash == 50
        assert len(snap.positions) == 0
    finally:
        await db.close()


async def test_grades_cache_store_roundtrip_and_fallback(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "grades_store.db"))
    await db.init()
    try:
        store = GradesCacheStore(db)
        await store.init()

        # Cache miss
        assert await store.get_course("cps999") is None

        # Write and cache hit
        sample_profs = [{"name": "Prof Alan Turing", "avg": "A", "common": "A", "students": "60", "dist": []}]
        await store.set_course("cps999", "Theoretical Computer Science", sample_profs)

        cached = await store.get_course("CPS999")
        assert cached is not None
        assert cached["title"] == "Theoretical Computer Science"
        assert len(cached["professors"]) == 1
        assert cached["professors"][0]["name"] == "Prof Alan Turing"
    finally:
        await db.close()
