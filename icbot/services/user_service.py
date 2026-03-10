from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


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

    def __init__(self, db, *, starting_balance: int = 1000, daily_claim_amount: int = 100):
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
        now = int(time.time())
        row = await self.db.fetchone(
            "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts FROM users WHERE user_id=?",
            (int(user_id),),
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO users (user_id, display_name, avatar_url, balance, created_at, daily_claim_ts) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (int(user_id), str(display_name), str(avatar_url), self.starting_balance, now),
            )
            return UserProfile(
                user_id=int(user_id),
                display_name=str(display_name),
                avatar_url=str(avatar_url),
                balance=self.starting_balance,
                created_at=now,
                daily_claim_ts=0,
            )

        # Refresh display info on every interaction
        if display_name or avatar_url:
            updates = []
            params: list = []
            if display_name:
                updates.append("display_name=?")
                params.append(str(display_name))
            if avatar_url:
                updates.append("avatar_url=?")
                params.append(str(avatar_url))
            if updates:
                params.append(int(user_id))
                await self.db.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE user_id=?",
                    tuple(params),
                )

        return UserProfile(
            user_id=int(row[0]),
            display_name=str(display_name or row[1]),
            avatar_url=str(avatar_url or row[2]),
            balance=int(row[3]),
            created_at=int(row[4]),
            daily_claim_ts=int(row[5]),
        )

    async def get_profile(self, user_id: int) -> Optional[UserProfile]:
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
        row = await self.db.fetchone(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        )
        return int(row[0]) if row else self.starting_balance

    async def add_balance(self, user_id: int, amount: int) -> int:
        """Add to balance (winnings, daily claim, etc). Returns new balance."""
        amount = int(amount)
        row = await self.db.fetchone(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        )
        if row is None:
            await self.ensure_user(user_id)
            current = self.starting_balance
        else:
            current = int(row[0])

        new_balance = current + amount
        await self.db.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, int(user_id)),
        )
        return new_balance

    async def subtract_balance(self, user_id: int, amount: int) -> int:
        """Subtract from balance (bets, purchases). Raises InsufficientBalanceError."""
        amount = int(amount)
        row = await self.db.fetchone(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        )
        if row is None:
            await self.ensure_user(user_id)
            current = self.starting_balance
        else:
            current = int(row[0])

        if current < amount:
            raise InsufficientBalanceError(
                f"Insufficient balance. Need ${amount}, have ${current}."
            )

        new_balance = current - amount
        await self.db.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, int(user_id)),
        )
        return new_balance

    async def set_balance(self, user_id: int, amount: int) -> int:
        """Set balance to an exact value (admin reset)."""
        amount = int(amount)
        await self.db.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (amount, int(user_id)),
        )
        return amount

    # ------------------------------------------------------------------
    # Daily claim
    # ------------------------------------------------------------------

    async def claim_daily(self, user_id: int) -> tuple[int, int]:
        """Claim daily reward. Returns (amount_claimed, new_balance).

        Raises DailyAlreadyClaimedError if cooldown has not elapsed.
        """
        now = int(time.time())
        row = await self.db.fetchone(
            "SELECT balance, daily_claim_ts FROM users WHERE user_id=?",
            (int(user_id),),
        )
        if row is None:
            await self.ensure_user(user_id)
            current = self.starting_balance
            last_claim = 0
        else:
            current = int(row[0])
            last_claim = int(row[1])

        next_available = last_claim + DAILY_COOLDOWN_SECONDS
        if now < next_available:
            raise DailyAlreadyClaimedError(next_claim_ts=next_available)

        new_balance = current + self.daily_claim_amount
        await self.db.execute(
            "UPDATE users SET balance=?, daily_claim_ts=? WHERE user_id=?",
            (new_balance, now, int(user_id)),
        )
        return self.daily_claim_amount, new_balance
