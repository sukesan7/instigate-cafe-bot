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
                    pushes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_counts (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            # Initialize stock value if missing
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
