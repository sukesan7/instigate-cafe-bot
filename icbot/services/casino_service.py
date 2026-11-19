from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from icbot.db import Database
from icbot.errors import BotError, InsufficientBalanceError

# Economy constants: strictly virtual play-money with zero real-world exchange value.
MIN_WAGER: int = 1
MAX_WAGER: int = 1_000_000_000  # 1 Billion virtual credits limit protects against integer overflow


class InvalidWagerError(BotError, ValueError):
    """Raised when a wager amount is non-positive, non-integer, or exceeds safe limits."""


class ConcurrentWagerError(BotError, RuntimeError):
    """Raised when a user attempts concurrent wagers in the same guild."""


@dataclass(frozen=True)
class WagerReservation:
    """Escrow reservation tracking an active virtual wager prior to outcome resolution."""

    transaction_id: str
    guild_id: int
    user_id: int
    amount: int
    game_type: str
    balance_after: int
    created_at: int


@dataclass(frozen=True)
class SettlementResult:
    """Immutable audit record of a resolved game outcome and balance update."""

    settlement_id: str
    transaction_id: str
    guild_id: int
    user_id: int
    wager_amount: int
    payout_amount: int
    net_delta: int
    balance_after: int
    outcome: str
    already_settled: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class CasinoService:
    """
    Consolidated domain service governing casino wagers, reservations, and settlements.

    Enforces:
    1. Strict play-money invariants (zero cash-out, deposits, or exchange value).
    2. Atomic conditional escrow reservations to prevent double-spending.
    3. Idempotent settlement keys with ledger auditing to eliminate replay/duplicate payouts.
    4. Active session concurrency gating per user and guild.
    """

    def __init__(
        self,
        db: Database,
        *,
        guild_id: int = 0,
        starting_bankroll: int = 1000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.db = db
        self.guild_id = int(guild_id or 0)
        self.starting_bankroll = int(starting_bankroll or 1000)
        self._clock = clock
        self._active_wagers: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    def _resolve_gid(self, guild_id: int | None) -> int:
        return int(guild_id) if guild_id is not None else self.guild_id

    # --------------------------------------------------------------------------
    # Wager Validation & Virtual Economy Guards
    # --------------------------------------------------------------------------

    @staticmethod
    def validate_wager(bet: Any) -> int:
        """
        Validate wager amount against virtual bounds and integer constraints.
        Explicitly rejects bools, floats, non-numeric values, negative numbers, and overflow limits.
        """
        if type(bet) is not int:
            raise InvalidWagerError(f"Wager must be an integer, got {type(bet).__name__}.")
        if bet < MIN_WAGER:
            raise InvalidWagerError(f"Bet must be at least ${MIN_WAGER}.")
        if bet > MAX_WAGER:
            raise InvalidWagerError(f"Bet exceeds maximum allowed limit of ${MAX_WAGER:,}.")
        return bet

    # --------------------------------------------------------------------------
    # User Profile & Balance Queries
    # --------------------------------------------------------------------------

    async def get_balance(self, user_id: int, *, guild_id: int | None = None) -> int:
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        cur = await self.db.fetchone("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
        if cur is not None and cur[0] is not None:
            return int(cur[0])
        return self.starting_bankroll

    async def ensure_user(
        self,
        user_id: int,
        display_name: str = "",
        avatar_url: str = "",
        *,
        guild_id: int | None = None,
    ) -> int:
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now_ts = self._now()
        async with self.db.write_transaction() as tx:
            await tx.execute(
                """
                INSERT INTO users (guild_id, user_id, display_name, avatar_url, balance, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE users.display_name END,
                    avatar_url   = CASE WHEN excluded.avatar_url != '' THEN excluded.avatar_url ELSE users.avatar_url END
                """,
                (gid, uid, str(display_name), str(avatar_url), self.starting_bankroll, now_ts),
            )
            cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            row = await cur.fetchone()
            return int(row[0]) if row is not None else self.starting_bankroll

    # --------------------------------------------------------------------------
    # Wager Reservation & Settlement Lifecycle
    # --------------------------------------------------------------------------

    async def reserve_wager(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        game_type: str,
        *,
        idempotency_key: str | None = None,
        display_name: str = "",
        avatar_url: str = "",
    ) -> WagerReservation:
        """
        Atomically reserve a wager amount into escrow.
        Uses conditional SQL (`balance >= ?`) and records the debit transactionally in `economy_ledger`.
        """
        valid_amount = self.validate_wager(amount)
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now_ts = self._now()

        async with self._lock:
            key = (gid, uid)
            if key in self._active_wagers and not idempotency_key:
                raise ConcurrentWagerError(
                    "You already have an active game or wager in progress. Please finish it before placing another bet."
                )
            self._active_wagers.add(key)

        tx_id = (
            str(idempotency_key)
            if idempotency_key
            else f"game:{game_type}:{gid}:{uid}:{now_ts}:{uuid.uuid4().hex[:8]}"
        )

        try:
            async with self.db.write_transaction() as tx:
                # Idempotency check: if this transaction was already processed, return existing reservation
                cur_idem = await tx.execute(
                    "SELECT balance_after, amount FROM economy_ledger WHERE transaction_id=?",
                    (tx_id,),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return WagerReservation(
                        transaction_id=tx_id,
                        guild_id=gid,
                        user_id=uid,
                        amount=valid_amount,
                        game_type=game_type,
                        balance_after=int(idem_row[0]),
                        created_at=now_ts,
                    )

                # Ensure user exists
                await tx.execute(
                    """
                    INSERT INTO users (guild_id, user_id, display_name, avatar_url, balance, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE users.display_name END,
                        avatar_url   = CASE WHEN excluded.avatar_url != '' THEN excluded.avatar_url ELSE users.avatar_url END
                    """,
                    (gid, uid, str(display_name), str(avatar_url), self.starting_bankroll, now_ts),
                )

                # Atomic conditional decrement
                cur = await tx.execute(
                    "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                    (valid_amount, gid, uid, valid_amount),
                )
                if cur.rowcount == 0:
                    cur_bal = await tx.execute(
                        "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
                        (gid, uid),
                    )
                    bal_row = await cur_bal.fetchone()
                    available = int(bal_row[0]) if bal_row is not None else self.starting_bankroll
                    raise InsufficientBalanceError(
                        f"Insufficient balance. You have **${available}**, need **${valid_amount}**."
                    )

                cur_after = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                after_row = await cur_after.fetchone()
                new_bal = int(after_row[0]) if after_row is not None else 0

                # Record reservation in ledger
                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tx_id, gid, uid, -valid_amount, new_bal, f"{game_type}_wager", None, now_ts),
                )

                return WagerReservation(
                    transaction_id=tx_id,
                    guild_id=gid,
                    user_id=uid,
                    amount=valid_amount,
                    game_type=game_type,
                    balance_after=new_bal,
                    created_at=now_ts,
                )
        except Exception:
            async with self._lock:
                self._active_wagers.discard((gid, uid))
            raise

    async def settle_wager(
        self,
        reservation: WagerReservation,
        payout_amount: int,
        net_delta: int,
        outcome: str,
        *,
        idempotency_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SettlementResult:
        """
        Settle an active wager reservation idempotently.
        Ensures winnings are credited atomically and double-settlement is prevented by unique settlement keys.
        """
        payout = max(0, int(payout_amount))
        net = int(net_delta)
        gid = reservation.guild_id
        uid = reservation.user_id
        now_ts = self._now()
        settle_id = str(idempotency_key) if idempotency_key else f"settle:{reservation.transaction_id}"

        try:
            async with self.db.write_transaction() as tx:
                # Idempotency check on settlement key
                cur_idem = await tx.execute(
                    "SELECT balance_after FROM economy_ledger WHERE transaction_id=?",
                    (settle_id,),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return SettlementResult(
                        settlement_id=settle_id,
                        transaction_id=reservation.transaction_id,
                        guild_id=gid,
                        user_id=uid,
                        wager_amount=reservation.amount,
                        payout_amount=payout,
                        net_delta=net,
                        balance_after=int(idem_row[0]),
                        outcome=outcome,
                        already_settled=True,
                        details=details or {},
                    )

                if payout > 0:
                    await tx.execute(
                        "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                        (payout, gid, uid),
                    )

                cur_bal = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
                    (gid, uid),
                )
                row_bal = await cur_bal.fetchone()
                new_bal = int(row_bal[0]) if row_bal is not None else reservation.balance_after + payout

                category = (
                    f"{reservation.game_type}_payout" if payout > 0 else f"{reservation.game_type}_settle"
                )
                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (settle_id, gid, uid, payout, new_bal, category, reservation.transaction_id, now_ts),
                )

                return SettlementResult(
                    settlement_id=settle_id,
                    transaction_id=reservation.transaction_id,
                    guild_id=gid,
                    user_id=uid,
                    wager_amount=reservation.amount,
                    payout_amount=payout,
                    net_delta=net,
                    balance_after=new_bal,
                    outcome=outcome,
                    already_settled=False,
                    details=details or {},
                )
        finally:
            async with self._lock:
                self._active_wagers.discard((gid, uid))

    async def refund_wager(
        self,
        reservation: WagerReservation,
        reason: str = "refund",
        *,
        idempotency_key: str | None = None,
    ) -> int:
        """Refund an active wager reservation back to the user balance."""
        gid = reservation.guild_id
        uid = reservation.user_id
        now_ts = self._now()
        refund_id = str(idempotency_key) if idempotency_key else f"refund:{reservation.transaction_id}"

        try:
            async with self.db.write_transaction() as tx:
                cur_idem = await tx.execute(
                    "SELECT balance_after FROM economy_ledger WHERE transaction_id=?",
                    (refund_id,),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return int(idem_row[0])

                await tx.execute(
                    "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                    (reservation.amount, gid, uid),
                )
                cur_bal = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                row_bal = await cur_bal.fetchone()
                new_bal = (
                    int(row_bal[0]) if row_bal is not None else reservation.balance_after + reservation.amount
                )

                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        refund_id,
                        gid,
                        uid,
                        reservation.amount,
                        new_bal,
                        f"{reservation.game_type}_refund",
                        reservation.transaction_id,
                        now_ts,
                    ),
                )
                return new_bal
        finally:
            async with self._lock:
                self._active_wagers.discard((gid, uid))

    async def forfeit_wager(
        self,
        reservation: WagerReservation,
        reason: str = "forfeit",
    ) -> SettlementResult:
        """Mark a wager as forfeited; reserved funds remain deducted."""
        return await self.settle_wager(
            reservation,
            payout_amount=0,
            net_delta=-reservation.amount,
            outcome=reason,
        )

    def release_active_lock(self, guild_id: int, user_id: int) -> None:
        """Manually release any held in-memory active wager lock."""
        gid = self._resolve_gid(guild_id)
        self._active_wagers.discard((gid, int(user_id)))
