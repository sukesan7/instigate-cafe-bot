from __future__ import annotations
import aiosqlite
import asyncio
from typing import Any, Optional, Sequence


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            # ---- Central user profile & economy table ----
            await db.execute(
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

            await db.execute(
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
            await db.execute(
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

            # ---- Lightweight migrations (safe on existing DBs) ----
            # v0.2.1: add blackjack reset counter
            cur = await db.execute("PRAGMA table_info(blackjack_users)")
            cols = [str(r[1]) for r in (await cur.fetchall() or [])]
            if "reset_count" not in cols:
                await db.execute(
                    "ALTER TABLE blackjack_users ADD COLUMN reset_count INTEGER NOT NULL DEFAULT 0"
                )



            # Job alerts (dedupe + bootstrap checkpoints)
            await db.execute(
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
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_seen_first_seen
                ON job_seen(first_seen_unix)
                """
            )

            # Workday CXS discovery cache (additive; safe on existing DBs)
            await db.execute(
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

            # Exams (manual schedule configured by admins)
            await db.execute(
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
            await db.execute("CREATE INDEX IF NOT EXISTS idx_exams_start_ts ON exams(start_ts)")

            # ------------------------------------------------------
            # Fun Market tables (server stocks + user positions/trades)
            # ------------------------------------------------------
            await db.execute(
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

            # Additive migrations for old installs if table existed from an experimental branch
            cur = await db.execute("PRAGMA table_info(fun_stocks)")
            fun_cols = [str(r[1]) for r in (await cur.fetchall() or [])]
            if fun_cols:
                if "category" not in fun_cols:
                    await db.execute("ALTER TABLE fun_stocks ADD COLUMN category TEXT NOT NULL DEFAULT 'misc'")
                if "is_active" not in fun_cols:
                    await db.execute("ALTER TABLE fun_stocks ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
                if "week_key" not in fun_cols:
                    await db.execute("ALTER TABLE fun_stocks ADD COLUMN week_key TEXT")
                if "week_open" not in fun_cols:
                    await db.execute("ALTER TABLE fun_stocks ADD COLUMN week_open INTEGER")

            await db.execute(
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
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fun_stock_events_symbol_ts ON fun_stock_events(symbol, ts_unix DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fun_stock_events_ts ON fun_stock_events(ts_unix DESC)"
            )

            await db.execute(
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
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fun_positions_user ON fun_positions(user_id)"
            )

            # Additive migration if table existed before collateral support
            cur = await db.execute("PRAGMA table_info(fun_positions)")
            pos_cols = [str(r[1]) for r in (await cur.fetchall() or [])]
            if pos_cols and "collateral_locked" not in pos_cols:
                await db.execute(
                    "ALTER TABLE fun_positions ADD COLUMN collateral_locked INTEGER NOT NULL DEFAULT 0"
                )

            await db.execute(
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
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fun_trades_user_ts ON fun_trades(user_id, ts_unix DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fun_trades_ts ON fun_trades(ts_unix DESC)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS fun_market_weeks (
                    week_key TEXT PRIMARY KEY,
                    settled_at INTEGER NOT NULL,
                    settled_by INTEGER,
                    notes TEXT
                )
                """
            )

            # Initialize legacy $IC stock value if missing
            cur = await db.execute("SELECT value FROM meta WHERE key='stock_value'")
            row = await cur.fetchone()
            if row is None:
                await db.execute("INSERT INTO meta (key, value) VALUES ('stock_value', ?)", ("0",))

            await db.commit()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(sql, params)
                await db.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, params)
            return await cur.fetchall()

    async def execute_many(self, statements: list[tuple[str, Sequence[Any]]]) -> None:
        async with self._lock:
            async with aiosqlite.connect(self.path) as db:
                for sql, params in statements:
                    await db.execute(sql, params)
                await db.commit()
