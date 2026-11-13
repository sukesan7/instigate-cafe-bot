from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite

# Task-local tracking of active transaction to prevent accidental nested transaction deadlocks
_current_tx: contextvars.ContextVar[Transaction | None] = contextvars.ContextVar("_current_tx", default=None)


class MigrationError(Exception):
    """Base exception for schema migration failures."""


class MigrationIntegrityError(MigrationError):
    """Raised when PRAGMA integrity_check or foreign_key_check fails."""


@dataclass(frozen=True)
class Migration:
    """Represents a discrete, versioned database migration step."""

    version: int
    name: str
    up: Callable[[Transaction], Awaitable[None]]


class Transaction:
    """Unit of work wrapping an active database transaction."""

    def __init__(self, db: Database, conn: aiosqlite.Connection) -> None:
        self.db = db
        self._conn = conn

    @property
    def conn(self) -> aiosqlite.Connection:
        """Underlying managed connection for advanced SQLite operations."""
        return self._conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement within this transaction."""
        return await self._conn.execute(sql, params)

    async def execute_insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute an INSERT statement and return the inserted rowid."""
        cur = await self._conn.execute(sql, params)
        return int(cur.lastrowid or 0)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        """Execute a query and fetch a single row within this transaction."""
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Execute a query and fetch all rows within this transaction."""
        cur = await self._conn.execute(sql, params)
        rows = list(await cur.fetchall() or [])
        return [tuple(r) for r in rows]

    async def execute_many(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """Execute multiple SQL statements sequentially within this transaction."""
        for sql, params in statements:
            await self._conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Migration steps definition
# ---------------------------------------------------------------------------


async def _migrate_0001_baseline(tx: Transaction) -> None:
    """Baseline application schema (additive; safe on legacy databases)."""
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            display_name   TEXT    NOT NULL DEFAULT '',
            avatar_url     TEXT    NOT NULL DEFAULT '',
            balance        INTEGER NOT NULL DEFAULT 1000,
            created_at     INTEGER NOT NULL DEFAULT 0,
            daily_claim_ts INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            new_value INTEGER NOT NULL,
            reason TEXT
        )
        """
    )
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS blackjack_users (
            user_id INTEGER PRIMARY KEY,
            bankroll INTEGER NOT NULL,
            hands_played INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            pushes INTEGER NOT NULL DEFAULT 0,
            reset_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Check if blackjack_users lacks reset_count on older schemas
    cur = await tx.execute("PRAGMA table_info(blackjack_users)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "reset_count" not in cols:
        await tx.execute("ALTER TABLE blackjack_users ADD COLUMN reset_count INTEGER NOT NULL DEFAULT 0")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS job_seen (
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            first_seen_unix INTEGER NOT NULL,
            title TEXT,
            url TEXT,
            PRIMARY KEY (source, external_id)
        )
        """
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_job_seen_first_seen ON job_seen(first_seen_unix)")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS workday_cxs_cache (
            careers_url TEXT PRIMARY KEY,
            api_url TEXT NOT NULL,
            tenant TEXT,
            site TEXT,
            last_updated_unix INTEGER NOT NULL
        )
        """
    )

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_code TEXT NOT NULL,
            course_name TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER,
            location TEXT,
            notes TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )
        """
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_exams_start_ts ON exams(start_ts)")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS fun_stocks (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT 'misc',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            week_key TEXT,
            week_open INTEGER
        )
        """
    )
    cur = await tx.execute("PRAGMA table_info(fun_stocks)")
    fun_cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if fun_cols:
        if "category" not in fun_cols:
            await tx.execute("ALTER TABLE fun_stocks ADD COLUMN category TEXT NOT NULL DEFAULT 'misc'")
        if "is_active" not in fun_cols:
            await tx.execute("ALTER TABLE fun_stocks ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "week_key" not in fun_cols:
            await tx.execute("ALTER TABLE fun_stocks ADD COLUMN week_key TEXT")
        if "week_open" not in fun_cols:
            await tx.execute("ALTER TABLE fun_stocks ADD COLUMN week_open INTEGER")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS fun_stock_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            new_value INTEGER NOT NULL,
            reason TEXT
        )
        """
    )
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_fun_stock_events_symbol_ts ON fun_stock_events(symbol, ts_unix DESC)"
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_fun_stock_events_ts ON fun_stock_events(ts_unix DESC)")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS fun_positions (
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            avg_price REAL NOT NULL,
            collateral_locked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, symbol, side)
        )
        """
    )
    cur = await tx.execute("PRAGMA table_info(fun_positions)")
    pos_cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if pos_cols and "collateral_locked" not in pos_cols:
        await tx.execute("ALTER TABLE fun_positions ADD COLUMN collateral_locked INTEGER NOT NULL DEFAULT 0")
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_fun_positions_user ON fun_positions(user_id)")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS fun_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price INTEGER NOT NULL,
            cash_delta INTEGER NOT NULL,
            realized_pnl INTEGER NOT NULL DEFAULT 0,
            note TEXT
        )
        """
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_fun_trades_user_ts ON fun_trades(user_id, ts_unix DESC)")
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_fun_trades_ts ON fun_trades(ts_unix DESC)")

    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS fun_market_weeks (
            week_key TEXT PRIMARY KEY,
            settled_at INTEGER NOT NULL,
            settled_by INTEGER,
            notes TEXT
        )
        """
    )

    # Legacy $IC stock value baseline in meta
    row = await tx.fetchone("SELECT value FROM meta WHERE key='stock_value'")
    if row is None:
        await tx.execute("INSERT INTO meta (key, value) VALUES ('stock_value', ?)", ("0",))

    # Guild configuration overrides
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id      INTEGER PRIMARY KEY,
            settings_json TEXT    NOT NULL,
            updated_at    INTEGER NOT NULL
        )
        """
    )


async def _migrate_0002_grades_cache(tx: Transaction) -> None:
    """Introduce namespaced grades course cache table."""
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS grades_course_cache (
            course_code TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            parsed_data TEXT NOT NULL DEFAULT '[]',
            last_updated INTEGER NOT NULL
        )
        """
    )
    # Check if a legacy un-namespaced course_cache table exists and backfill if found
    cur = await tx.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='course_cache'")
    if await cur.fetchone():
        await tx.execute(
            """
            INSERT OR IGNORE INTO grades_course_cache (course_code, title, parsed_data, last_updated)
            SELECT course_code, COALESCE(title, ''), COALESCE(parsed_data, '[]'), strftime('%s', 'now')
            FROM course_cache
            """
        )


async def _migrate_0003_indexes_and_integrity(tx: Transaction) -> None:
    """Add supplementary performance indexes across core query patterns."""
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_events_user_ts ON stock_events(user_id, ts_unix DESC)"
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)")


async def _migrate_0004_guild_scoped_economy_and_ledger(tx: Transaction) -> None:
    """Add explicit guild_id compound primary keys, transaction ledger, and audit tables."""
    # 1. users table migration to compound key (guild_id, user_id)
    cur = await tx.execute("PRAGMA table_info(users)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute(
            """
            CREATE TABLE users_migrated (
                guild_id       INTEGER NOT NULL DEFAULT 0,
                user_id        INTEGER NOT NULL,
                display_name   TEXT    NOT NULL DEFAULT '',
                avatar_url     TEXT    NOT NULL DEFAULT '',
                balance        INTEGER NOT NULL DEFAULT 1000,
                created_at     INTEGER NOT NULL DEFAULT 0,
                daily_claim_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        dname_col = "display_name" if "display_name" in cols else "''"
        avatar_col = "avatar_url" if "avatar_url" in cols else "''"
        claim_col = "daily_claim_ts" if "daily_claim_ts" in cols else "0"
        balance_col = "balance" if "balance" in cols else "1000"
        created_col = "created_at" if "created_at" in cols else "0"
        await tx.execute(
            f"""
            INSERT OR IGNORE INTO users_migrated (guild_id, user_id, display_name, avatar_url, balance, created_at, daily_claim_ts)
            SELECT 0, user_id, {dname_col}, {avatar_col}, {balance_col}, {created_col}, {claim_col}
            FROM users
            """
        )
        await tx.execute("DROP TABLE users")
        await tx.execute("ALTER TABLE users_migrated RENAME TO users")
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_users_guild_balance ON users(guild_id, balance DESC)")

    # 2. blackjack_users table migration to compound key (guild_id, user_id)
    cur = await tx.execute("PRAGMA table_info(blackjack_users)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute(
            """
            CREATE TABLE blackjack_users_migrated (
                guild_id     INTEGER NOT NULL DEFAULT 0,
                user_id      INTEGER NOT NULL,
                bankroll     INTEGER NOT NULL,
                hands_played INTEGER NOT NULL DEFAULT 0,
                wins         INTEGER NOT NULL DEFAULT 0,
                losses       INTEGER NOT NULL DEFAULT 0,
                pushes       INTEGER NOT NULL DEFAULT 0,
                reset_count  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        bankroll_col = "bankroll" if "bankroll" in cols else "1000"
        hands_col = "hands_played" if "hands_played" in cols else "0"
        wins_col = "wins" if "wins" in cols else "0"
        losses_col = "losses" if "losses" in cols else "0"
        pushes_col = "pushes" if "pushes" in cols else "0"
        resets_col = "reset_count" if "reset_count" in cols else "0"
        await tx.execute(
            f"""
            INSERT OR IGNORE INTO blackjack_users_migrated (guild_id, user_id, bankroll, hands_played, wins, losses, pushes, reset_count)
            SELECT 0, user_id, {bankroll_col}, {hands_col}, {wins_col}, {losses_col}, {pushes_col}, {resets_col}
            FROM blackjack_users
            """
        )
        await tx.execute("DROP TABLE blackjack_users")
        await tx.execute("ALTER TABLE blackjack_users_migrated RENAME TO blackjack_users")

    # 3. fun_stocks table migration to compound key (guild_id, symbol)
    cur = await tx.execute("PRAGMA table_info(fun_stocks)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute(
            """
            CREATE TABLE fun_stocks_migrated (
                guild_id   INTEGER NOT NULL DEFAULT 0,
                symbol     TEXT    NOT NULL,
                name       TEXT    NOT NULL,
                price      INTEGER NOT NULL,
                category   TEXT    NOT NULL DEFAULT 'misc',
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                week_key   TEXT,
                week_open  INTEGER,
                PRIMARY KEY (guild_id, symbol)
            )
            """
        )
        name_col = "name" if "name" in cols else "symbol"
        price_col = "price" if "price" in cols else "100"
        cat_col = "category" if "category" in cols else "'misc'"
        active_col = "is_active" if "is_active" in cols else "1"
        created_col = "created_ts" if "created_ts" in cols else "0"
        updated_col = "updated_ts" if "updated_ts" in cols else "0"
        wk_col = "week_key" if "week_key" in cols else "NULL"
        wo_col = "week_open" if "week_open" in cols else "price"
        await tx.execute(
            f"""
            INSERT OR IGNORE INTO fun_stocks_migrated (guild_id, symbol, name, price, category, is_active, created_ts, updated_ts, week_key, week_open)
            SELECT 0, symbol, {name_col}, {price_col}, {cat_col}, {active_col}, {created_col}, {updated_col}, {wk_col}, {wo_col}
            FROM fun_stocks
            """
        )
        await tx.execute("DROP TABLE fun_stocks")
        await tx.execute("ALTER TABLE fun_stocks_migrated RENAME TO fun_stocks")

    # 4. fun_positions table migration to compound key (guild_id, user_id, symbol, side)
    cur = await tx.execute("PRAGMA table_info(fun_positions)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute(
            """
            CREATE TABLE fun_positions_migrated (
                guild_id          INTEGER NOT NULL DEFAULT 0,
                user_id           INTEGER NOT NULL,
                symbol            TEXT    NOT NULL,
                side              TEXT    NOT NULL,
                qty               INTEGER NOT NULL,
                avg_price         REAL    NOT NULL,
                collateral_locked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, symbol, side)
            )
            """
        )
        coll_col = "collateral_locked" if "collateral_locked" in cols else "0"
        await tx.execute(
            f"""
            INSERT OR IGNORE INTO fun_positions_migrated (guild_id, user_id, symbol, side, qty, avg_price, collateral_locked)
            SELECT 0, user_id, symbol, side, qty, avg_price, {coll_col}
            FROM fun_positions
            """
        )
        await tx.execute("DROP TABLE fun_positions")
        await tx.execute("ALTER TABLE fun_positions_migrated RENAME TO fun_positions")
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_fun_positions_guild_user ON fun_positions(guild_id, user_id)"
    )

    # 5. fun_trades table guild_id column and compound index
    cur = await tx.execute("PRAGMA table_info(fun_trades)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute("ALTER TABLE fun_trades ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_fun_trades_guild_user_ts ON fun_trades(guild_id, user_id, ts_unix DESC)"
    )

    # 6. fun_stock_events and stock_events guild_id
    cur = await tx.execute("PRAGMA table_info(fun_stock_events)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute("ALTER TABLE fun_stock_events ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_fun_stock_events_guild_sym ON fun_stock_events(guild_id, symbol, ts_unix DESC)"
    )

    cur = await tx.execute("PRAGMA table_info(stock_events)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute("ALTER TABLE stock_events ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")

    # 7. exams table guild_id column and compound index
    cur = await tx.execute("PRAGMA table_info(exams)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute("ALTER TABLE exams ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_exams_guild_start ON exams(guild_id, start_ts ASC)")

    # 8. fun_market_weeks table migration to compound key (guild_id, week_key)
    cur = await tx.execute("PRAGMA table_info(fun_market_weeks)")
    cols = [str(r[1]) for r in (await cur.fetchall() or [])]
    if "guild_id" not in cols:
        await tx.execute(
            """
            CREATE TABLE fun_market_weeks_migrated (
                guild_id   INTEGER NOT NULL DEFAULT 0,
                week_key   TEXT    NOT NULL,
                settled_at INTEGER NOT NULL,
                settled_by INTEGER,
                notes      TEXT,
                PRIMARY KEY (guild_id, week_key)
            )
            """
        )
        await tx.execute(
            """
            INSERT OR IGNORE INTO fun_market_weeks_migrated (guild_id, week_key, settled_at, settled_by, notes)
            SELECT 0, week_key, settled_at, settled_by, notes
            FROM fun_market_weeks
            """
        )
        await tx.execute("DROP TABLE fun_market_weeks")
        await tx.execute("ALTER TABLE fun_market_weeks_migrated RENAME TO fun_market_weeks")

    # 9. economy_ledger table
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS economy_ledger (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT    NOT NULL UNIQUE,
            guild_id       INTEGER NOT NULL DEFAULT 0,
            user_id        INTEGER NOT NULL,
            amount         INTEGER NOT NULL,
            balance_after  INTEGER NOT NULL,
            category       TEXT    NOT NULL,
            reference_id   TEXT,
            created_at     INTEGER NOT NULL
        )
        """
    )
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_economy_ledger_guild_user ON economy_ledger(guild_id, user_id, created_at DESC)"
    )
    await tx.execute("CREATE INDEX IF NOT EXISTS idx_economy_ledger_tx_id ON economy_ledger(transaction_id)")

    # 10. alert_delivery_dedupes table
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_delivery_dedupes (
            guild_id     INTEGER NOT NULL DEFAULT 0,
            alert_type   TEXT    NOT NULL,
            dedupe_key   TEXT    NOT NULL,
            delivered_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, alert_type, dedupe_key)
        )
        """
    )
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_delivery_guild_time ON alert_delivery_dedupes(guild_id, delivered_at DESC)"
    )

    # 11. admin_audit_log table
    await tx.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id       INTEGER NOT NULL DEFAULT 0,
            admin_user_id  INTEGER NOT NULL,
            action         TEXT    NOT NULL,
            target_user_id INTEGER,
            details_json   TEXT    NOT NULL DEFAULT '{}',
            created_at     INTEGER NOT NULL
        )
        """
    )
    await tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_guild_time ON admin_audit_log(guild_id, created_at DESC)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="0001_baseline_schema", up=_migrate_0001_baseline),
    Migration(version=2, name="0002_grades_cache", up=_migrate_0002_grades_cache),
    Migration(version=3, name="0003_indexes_and_integrity", up=_migrate_0003_indexes_and_integrity),
    Migration(
        version=4,
        name="0004_guild_scoped_economy_and_ledger",
        up=_migrate_0004_guild_scoped_economy_and_ledger,
    ),
)


# ---------------------------------------------------------------------------
# Database handle with managed connection and transaction boundaries
# ---------------------------------------------------------------------------


class Database:
    """Thread-safe SQLite database manager with WAL configuration and transactional unit-of-work."""

    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """Acquire or establish the managed aiosqlite connection with enforced PRAGMAs."""
        if self._closed:
            raise RuntimeError(f"Database({self.path}) is closed.")
        if self._conn is None:
            # Connect with autocommit mode (isolation_level=None) so Python doesn't inject
            # implicit BEGIN statements behind our back, allowing strict BEGIN/BEGIN IMMEDIATE control.
            conn = await aiosqlite.connect(self.path, isolation_level=None)
            try:
                await conn.execute("PRAGMA foreign_keys = ON;")
                await conn.execute("PRAGMA busy_timeout = 5000;")
                if self.path != ":memory:" and not self.path.startswith("file::memory:"):
                    await conn.execute("PRAGMA journal_mode = WAL;")
                    await conn.execute("PRAGMA synchronous = NORMAL;")
                self._conn = conn
            except BaseException:
                await conn.close()
                raise
        return self._conn

    async def check_integrity(self) -> None:
        """Execute SQLite PRAGMA integrity_check and foreign_key_check.

        Raises:
            MigrationIntegrityError: If corruption or foreign key violations are detected.
        """
        if self._closed:
            raise RuntimeError("Database is closed.")
        async with self._lock:
            conn = await self._ensure_connected()
            cur = await conn.execute("PRAGMA integrity_check")
            rows = list(await cur.fetchall() or [])
            if not rows or rows[0][0] != "ok":
                errors = [str(r[0]) for r in rows]
                raise MigrationIntegrityError(f"Database integrity check failed: {', '.join(errors)}")

            cur_fk = await conn.execute("PRAGMA foreign_key_check")
            fk_rows = list(await cur_fk.fetchall() or [])
            if fk_rows:
                raise MigrationIntegrityError(f"Database foreign key violations detected: {fk_rows}")

    async def plan_migrations(self, target_version: int | None = None) -> list[Migration]:
        """Return pending migrations up to target_version without applying them."""
        if self._closed:
            raise RuntimeError("Database is closed.")
        async with self._lock:
            conn = await self._ensure_connected()
            # Ensure migrations ledger table exists
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at INTEGER NOT NULL
                )
                """
            )
            cur = await conn.execute("SELECT version FROM schema_migrations")
            applied_rows = list(await cur.fetchall() or [])
            applied_versions = {int(r[0]) for r in applied_rows}

            plan: list[Migration] = []
            for m in MIGRATIONS:
                if m.version in applied_versions:
                    continue
                if target_version is not None and m.version > target_version:
                    continue
                plan.append(m)
            return plan

    async def migrate(self, *, target_version: int | None = None, dry_run: bool = False) -> list[Migration]:
        """Run preflight integrity checks and execute pending migrations sequentially.

        Returns:
            The list of applied (or planned in dry_run mode) migrations.
        """
        if self._closed:
            raise RuntimeError("Database is closed.")

        await self.check_integrity()
        plan = await self.plan_migrations(target_version=target_version)
        if dry_run:
            return plan

        for m in plan:
            async with self.write_transaction() as tx:
                await m.up(tx)
                now_ts = int(time.time())
                await tx.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (m.version, m.name, now_ts),
                )
                await tx.execute(f"PRAGMA user_version = {m.version};")

        await self.check_integrity()
        return plan

    async def backup(self, dest_path: str) -> None:
        """Create an online live backup of the database to dest_path."""
        if self._closed:
            raise RuntimeError("Database is closed.")
        async with self._lock:
            src_conn = await self._ensure_connected()
            async with aiosqlite.connect(dest_path) as dst_conn:
                await src_conn.backup(dst_conn)

    async def migrate_legacy_guild_data(
        self,
        target_guild_id: int,
        *,
        confirmed: bool = False,
    ) -> dict[str, int]:
        """Explicitly map un-scoped legacy single-server rows (guild_id=0) to an owner guild.

        Safety constraints:
            - Requires explicit operator confirmation (confirmed=True) and a valid target_guild_id > 0.
            - Never mixes or cross-pollinates private data across servers automatically.
            - Records migration confirmation timestamp and target guild in the meta table.
        """
        if self._closed:
            raise RuntimeError("Database is closed.")
        gid = int(target_guild_id)
        if gid <= 0:
            raise ValueError(f"target_guild_id must be a positive integer snowflake, got {target_guild_id}")
        if not confirmed:
            raise ValueError(
                "Legacy data migration requires explicit operator confirmation (confirmed=True). "
                "Automatic or unconfirmed mapping of legacy single-server data is forbidden."
            )

        counts: dict[str, int] = {}
        async with self.write_transaction() as tx:
            # Check if this database has already mapped legacy rows
            row = await tx.fetchone("SELECT value FROM meta WHERE key='legacy_migration_target_guild_id'")
            if row is not None and row[0] is not None:
                existing_gid = int(row[0])
                if existing_gid == gid:
                    # Already migrated to this guild, idempotent no-op
                    return {"already_migrated": 1}

            # Map legacy rows (guild_id = 0) to target_guild_id
            for table in [
                "users",
                "blackjack_users",
                "exams",
                "fun_stocks",
                "fun_positions",
                "fun_trades",
                "fun_stock_events",
                "stock_events",
                "economy_ledger",
                "alert_delivery_dedupes",
                "admin_audit_log",
            ]:
                # Check if table exists
                cur_tbl = await tx.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                if not await cur_tbl.fetchone():
                    continue

                cur_check = await tx.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id = 0")
                cnt_row = await cur_check.fetchone()
                cnt = int(cnt_row[0]) if cnt_row else 0
                if cnt > 0:
                    await tx.execute(f"UPDATE {table} SET guild_id = ? WHERE guild_id = 0", (gid,))
                counts[table] = cnt

            now_ts = int(time.time())
            await tx.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_migration_target_guild_id', ?)",
                (str(gid),),
            )
            await tx.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_migration_confirmed_at', ?)",
                (str(now_ts),),
            )
        return counts

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncIterator[Transaction]:
        """Async context manager defining a transactional boundary.

        Begins an explicit transaction (`BEGIN IMMEDIATE` if immediate else `BEGIN`),
        commits upon clean exit, and rolls back upon any Exception or cancellation.
        """
        if self._closed:
            raise RuntimeError("Database is closed.")
        if _current_tx.get() is not None:
            raise RuntimeError(
                "Nested transactions are not supported on the same database execution context."
            )

        async with self._lock:
            conn = await self._ensure_connected()
            begin_sql = "BEGIN IMMEDIATE" if immediate else "BEGIN"
            await conn.execute(begin_sql)
            tx = Transaction(self, conn)
            token = _current_tx.set(tx)
            try:
                yield tx
                if conn.in_transaction:
                    await conn.commit()
            except BaseException:
                if conn.in_transaction:
                    await conn.rollback()
                raise
            finally:
                _current_tx.reset(token)

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[Transaction]:
        """Convenience alias for `transaction(immediate=True)`."""
        async with self.transaction(immediate=True) as tx:
            yield tx

    async def init(self) -> None:
        """Open managed connection and execute forward migrations."""
        self._closed = False
        await self.migrate()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Execute a write statement inside an immediate write transaction."""
        curr = _current_tx.get()
        if curr is not None and curr.db is self:
            await curr.execute(sql, params)
            return

        async with self.write_transaction() as tx:
            await tx.execute(sql, params)

    async def execute_insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute an INSERT statement and return the inserted rowid."""
        curr = _current_tx.get()
        if curr is not None and curr.db is self:
            return await curr.execute_insert(sql, params)

        async with self.write_transaction() as tx:
            return await tx.execute_insert(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        """Fetch a single row, respecting the active transaction or acquiring the read lock."""
        if self._closed:
            raise RuntimeError("Database is closed.")
        curr = _current_tx.get()
        if curr is not None and curr.db is self:
            return await curr.fetchone(sql, params)

        async with self._lock:
            conn = await self._ensure_connected()
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Fetch all rows, respecting the active transaction or acquiring the read lock."""
        if self._closed:
            raise RuntimeError("Database is closed.")
        curr = _current_tx.get()
        if curr is not None and curr.db is self:
            return await curr.fetchall(sql, params)

        async with self._lock:
            conn = await self._ensure_connected()
            cur = await conn.execute(sql, params)
            rows = list(await cur.fetchall() or [])
            return [tuple(r) for r in rows]

    async def execute_many(self, statements: list[tuple[str, Sequence[Any]]]) -> None:
        """Execute multiple statements inside an immediate write transaction."""
        curr = _current_tx.get()
        if curr is not None and curr.db is self:
            await curr.execute_many(statements)
            return

        async with self.write_transaction() as tx:
            await tx.execute_many(statements)

    async def close(self) -> None:
        """Drain pending locks, close the managed connection, and mark database closed."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._closed = True
