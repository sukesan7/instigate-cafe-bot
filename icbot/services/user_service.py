from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from icbot.db import Database


@dataclass
class UserProfile:
    user_id: int
    display_name: str
    avatar_url: str
    balance: int
    created_at: int
    daily_claim_ts: int


class InsufficientBalanceError(RuntimeError):
    pass


class DailyAlreadyClaimedError(RuntimeError):
    """Raised when the user has already claimed their daily reward within the cooldown."""

    def __init__(self, next_claim_ts: int):
        self.next_claim_ts = next_claim_ts
        super().__init__(f"Daily already claimed. Next claim at <t:{next_claim_ts}:R>.")


DAILY_COOLDOWN_SECONDS = 24 * 60 * 60  # 24 hours


class UserStore:
    """Central user profile & economy service.

    All balance mutations flow through this class so that every feature
    (blackjack, investing, daily claims, future games) shares the same pool.
    """

    def __init__(self, db: Database, *, starting_balance: int = 1000, daily_claim_amount: int = 100):
        self.db = db
        self.starting_balance = int(starting_balance or 1000)
        self.daily_claim_amount = int(daily_claim_amount or 100)

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    async def ensure_user(
        self,
        user_id: int,
        display_name: str = "",
        avatar_url: str = "",
    ) -> UserProfile:
        """Ensure the user exists. Update display_name/avatar on every call
        so the profile stays fresh without a separate sync task.
        """
        uid = int(user_id)
        dname = str(display_name)
        aurl = str(avatar_url)
        now = int(time.time())

        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users "
                "(user_id, display_name, avatar_url, balance, created_at, daily_claim_ts) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (uid, dname, aurl, self.starting_balance, now),
            )
            if dname or aurl:
                updates: list[str] = []
                params: list[Any] = []
                if dname:
                    updates.append("display_name=?")
                    params.append(dname)
                if aurl:
                    updates.append("avatar_url=?")
                    params.append(aurl)
                params.append(uid)
                await conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE user_id=?",
                    tuple(params),
                )
            cur = await conn.execute(
                "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts "
                "FROM users WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
            await conn.commit()

        if row is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Failed to ensure user row for {uid}")
        return UserProfile(
            user_id=int(row[0]),
            display_name=str(row[1]),
            avatar_url=str(row[2]),
            balance=int(row[3]),
            created_at=int(row[4]),
            daily_claim_ts=int(row[5]),
        )

    async def get_profile(self, user_id: int) -> UserProfile | None:
        row = await self.db.fetchone(
            "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts FROM users WHERE user_id=?",
            (int(user_id),),
        )
        if row is None:
            return None
        return UserProfile(
            user_id=int(row[0]),
            display_name=str(row[1]),
            avatar_url=str(row[2]),
            balance=int(row[3]),
            created_at=int(row[4]),
            daily_claim_ts=int(row[5]),
        )

    # ------------------------------------------------------------------
    # Balance operations
    # ------------------------------------------------------------------

    async def get_balance(self, user_id: int) -> int:
        row = await self.db.fetchone("SELECT balance FROM users WHERE user_id=?", (int(user_id),))
        return int(row[0]) if row else self.starting_balance

    async def add_balance(self, user_id: int, amount: int) -> int:
        """Add to balance (winnings, daily claim, etc). Returns new balance."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Amount must be >= 1.")
        uid = int(user_id)
        now = int(time.time())

        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
                (uid, self.starting_balance, now),
            )
            await conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (amount, uid),
            )
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            await conn.commit()

        return int(row[0]) if row is not None else self.starting_balance + amount

    async def subtract_balance(self, user_id: int, amount: int) -> int:
        """Subtract from balance (bets, purchases). Raises InsufficientBalanceError."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Amount must be >= 1.")
        uid = int(user_id)
        now = int(time.time())

        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
                (uid, self.starting_balance, now),
            )
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            current = int(row[0]) if row is not None else self.starting_balance
            if current < amount:
                await conn.commit()
                raise InsufficientBalanceError(f"Insufficient balance. Need ${amount}, have ${current}.")

            new_balance = current - amount
            await conn.execute(
                "UPDATE users SET balance=? WHERE user_id=?",
                (new_balance, uid),
            )
            await conn.commit()
        return new_balance

    async def set_balance(self, user_id: int, amount: int) -> int:
        """Set balance to an exact value (admin reset)."""
        amount = int(amount)
        uid = int(user_id)
        now = int(time.time())

        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
                (uid, self.starting_balance, now),
            )
            await conn.execute(
                "UPDATE users SET balance=? WHERE user_id=?",
                (amount, uid),
            )
            await conn.commit()
        return amount

    # ------------------------------------------------------------------
    # Daily claim
    # ------------------------------------------------------------------

    async def claim_daily(self, user_id: int) -> tuple[int, int]:
        """Claim daily reward. Returns (amount_claimed, new_balance).

        Raises DailyAlreadyClaimedError if cooldown has not elapsed.
        """
        now = int(time.time())
        uid = int(user_id)

        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
                (uid, self.starting_balance, now),
            )
            cur = await conn.execute(
                "SELECT balance, daily_claim_ts FROM users WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
            current = int(row[0]) if row is not None else self.starting_balance
            last_claim = int(row[1]) if row is not None else 0

            next_available = last_claim + DAILY_COOLDOWN_SECONDS
            if now < next_available:
                await conn.commit()
                raise DailyAlreadyClaimedError(next_claim_ts=next_available)

            new_balance = current + self.daily_claim_amount
            await conn.execute(
                "UPDATE users SET balance=?, daily_claim_ts=? WHERE user_id=?",
                (new_balance, now, uid),
            )
            await conn.commit()
        return self.daily_claim_amount, new_balance
