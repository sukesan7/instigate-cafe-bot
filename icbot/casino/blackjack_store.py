from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

import aiosqlite

from icbot.db import Database, Transaction
from icbot.services.user_service import (
    InsufficientBalanceError,
    LedgerIdempotencyIdentity,
    is_ledger_tx_id_unique_error,
    lookup_idempotent_ledger_entry,
)

log = logging.getLogger(__name__)


class _InsufficientWagerError(InsufficientBalanceError):
    def __init__(self, available: int) -> None:
        self.available = int(available)
        super().__init__(f"Insufficient balance (available: {available}).")


class BlackjackStore:
    """Storage adapter governing Blackjack bankrolls, statistics, and ledger transactions."""

    def __init__(
        self,
        db: Database,
        starting_bankroll: int,
        *,
        guild_id: int = 0,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.guild_id = int(guild_id or 0)
        self.starting_bankroll = int(starting_bankroll)
        self._clock: Callable[[], float] = clock if clock is not None else time.time

    def _resolve_gid(self, guild_id: int | None) -> int:
        return int(guild_id) if guild_id is not None else self.guild_id

    async def _ensure_user_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        user_id: int,
        guild_id: int | None = None,
    ) -> None:
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        await conn.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
            (gid, uid, self.starting_bankroll, int(self._clock())),
        )
        cur = await conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
        bal_row = await cur.fetchone()
        bal = int(bal_row[0]) if bal_row is not None else self.starting_bankroll
        await conn.execute(
            "INSERT OR IGNORE INTO blackjack_users (guild_id, user_id, bankroll) VALUES (?, ?, ?)",
            (gid, uid, bal),
        )

    async def ensure_user(self, user_id: int, *, guild_id: int | None = None) -> None:
        """Ensure both the central `users` row and `blackjack_users` stats row exist."""
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._ensure_user_conn(tx, user_id, gid)

    async def get_bankroll(self, user_id: int, *, guild_id: int | None = None) -> int:
        """Read balance from the central `users` table for this guild."""
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._ensure_user_conn(tx, uid, gid)
            cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            row = await cur.fetchone()
        return int(row[0]) if row is not None else self.starting_bankroll

    async def get_reset_count(self, user_id: int, *, guild_id: int | None = None) -> int:
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._ensure_user_conn(tx, uid, gid)
            cur = await tx.execute(
                "SELECT reset_count FROM blackjack_users WHERE guild_id=? AND user_id=?",
                (gid, uid),
            )
            row = await cur.fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    async def reserve_wager(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Atomically deduct an active wager up front into escrow with ledger recording."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Bet must be >= 1.")
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now_ts = int(self._clock())
        key = str(idempotency_key) if idempotency_key else None
        expected = (
            LedgerIdempotencyIdentity(
                guild_id=gid,
                user_id=uid,
                amount=-amount,
                category="bet",
                reference_id="blackjack",
                operation_type="debit",
                legacy_categories=frozenset({"blackjack_wager"}),
            )
            if key is not None
            else None
        )

        async with self.db.write_transaction() as tx:
            if key is not None and expected is not None:
                matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                if matched is not None:
                    return matched.balance_after

            await tx.execute("SAVEPOINT bj_reserve")
            try:
                await self._ensure_user_conn(tx, uid, gid)

                # Atomic conditional update
                cur = await tx.execute(
                    "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                    (amount, gid, uid, amount),
                )
                if cur.rowcount == 0:
                    cur_bal = await tx.execute(
                        "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                    )
                    bal_row = await cur_bal.fetchone()
                    available = int(bal_row[0]) if bal_row is not None else self.starting_bankroll
                    raise _InsufficientWagerError(available)

                cur2 = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                row2 = await cur2.fetchone()
                new_balance = int(row2[0]) if row2 is not None else 0

                await tx.execute(
                    "UPDATE blackjack_users SET bankroll=? WHERE guild_id=? AND user_id=?",
                    (new_balance, gid, uid),
                )

                tx_id = (
                    key
                    if key is not None
                    else f"bj_bet:{gid}:{uid}:{now_ts}:{time.monotonic_ns()}:{uuid.uuid4().hex[:8]}"
                )
                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, 'bet', 'blackjack', ?)
                    """,
                    (tx_id, gid, uid, -amount, new_balance, now_ts),
                )
            except sqlite3.IntegrityError as exc:
                await tx.execute("ROLLBACK TO SAVEPOINT bj_reserve")
                await tx.execute("RELEASE SAVEPOINT bj_reserve")
                if key is not None and expected is not None and is_ledger_tx_id_unique_error(exc):
                    matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                    if matched is not None:
                        return matched.balance_after
                raise
            else:
                await tx.execute("RELEASE SAVEPOINT bj_reserve")
        return new_balance

    async def refund_wager(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Return an escrowed wager if a hand cannot start or split."""
        amount = int(amount)
        gid = self._resolve_gid(guild_id)
        if amount <= 0:
            return await self.get_bankroll(user_id, guild_id=gid)
        uid = int(user_id)
        now_ts = int(self._clock())
        key = str(idempotency_key) if idempotency_key else None
        expected = (
            LedgerIdempotencyIdentity(
                guild_id=gid,
                user_id=uid,
                amount=amount,
                category="refund",
                reference_id="blackjack",
                operation_type="credit",
                legacy_categories=frozenset({"blackjack_refund"}),
            )
            if key is not None
            else None
        )

        async with self.db.write_transaction() as tx:
            if key is not None and expected is not None:
                matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                if matched is not None:
                    return matched.balance_after

            await tx.execute("SAVEPOINT bj_refund")
            try:
                await self._ensure_user_conn(tx, uid, gid)
                await tx.execute(
                    "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                    (amount, gid, uid),
                )
                cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
                row = await cur.fetchone()
                new_balance = int(row[0]) if row is not None else self.starting_bankroll

                await tx.execute(
                    "UPDATE blackjack_users SET bankroll=? WHERE guild_id=? AND user_id=?",
                    (new_balance, gid, uid),
                )

                tx_id = (
                    key
                    if key is not None
                    else f"bj_refund:{gid}:{uid}:{now_ts}:{time.monotonic_ns()}:{uuid.uuid4().hex[:8]}"
                )
                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, 'refund', 'blackjack', ?)
                    """,
                    (tx_id, gid, uid, amount, new_balance, now_ts),
                )
            except sqlite3.IntegrityError as exc:
                await tx.execute("ROLLBACK TO SAVEPOINT bj_refund")
                await tx.execute("RELEASE SAVEPOINT bj_refund")
                if key is not None and expected is not None and is_ledger_tx_id_unique_error(exc):
                    matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                    if matched is not None:
                        return matched.balance_after
                raise
            else:
                await tx.execute("RELEASE SAVEPOINT bj_refund")
        return new_balance

    async def apply_delta_and_stats(
        self,
        user_id: int,
        delta: int,
        result_counts: dict[str, int],
        hands_played: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
        reference_id: str | None = None,
    ) -> int:
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        delta = int(delta)
        hands_played = int(hands_played)
        now_ts = int(self._clock())
        category = "payout" if delta > 0 else ("settle" if delta == 0 else "loss_settle")
        legacy_cats = (
            frozenset({"blackjack_payout"})
            if delta > 0
            else (frozenset({"blackjack_settle"}) if delta == 0 else frozenset())
        )
        ref_id = str(reference_id) if reference_id is not None else "blackjack"
        key = str(idempotency_key) if idempotency_key else None
        expected = (
            LedgerIdempotencyIdentity(
                guild_id=gid,
                user_id=uid,
                amount=delta,
                category=category,
                reference_id=ref_id,
                legacy_categories=legacy_cats,
            )
            if key is not None
            else None
        )

        async with self.db.write_transaction() as tx:
            if key is not None and expected is not None:
                matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                if matched is not None:
                    return matched.balance_after

            await tx.execute("SAVEPOINT bj_settle")
            try:
                await self._ensure_user_conn(tx, uid, gid)
                if delta != 0:
                    await tx.execute(
                        "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                        (delta, gid, uid),
                    )
                cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
                bal_row = await cur.fetchone()
                bankroll = int(bal_row[0]) if bal_row is not None else self.starting_bankroll

                cur2 = await tx.execute(
                    "SELECT wins, losses, pushes, hands_played FROM blackjack_users WHERE guild_id=? AND user_id=?",
                    (gid, uid),
                )
                row = await cur2.fetchone()
                wins, losses, pushes, played = map(int, row) if row is not None else (0, 0, 0, 0)
                wins += result_counts.get("win", 0) + result_counts.get("blackjack", 0)
                losses += result_counts.get("loss", 0) + result_counts.get("bust", 0)
                pushes += result_counts.get("push", 0)
                played += hands_played

                await tx.execute(
                    "UPDATE blackjack_users SET bankroll=?, wins=?, losses=?, pushes=?, hands_played=? WHERE guild_id=? AND user_id=?",
                    (bankroll, wins, losses, pushes, played, gid, uid),
                )

                tx_id = (
                    key
                    if key is not None
                    else f"bj_payout:{gid}:{uid}:{now_ts}:{time.monotonic_ns()}:{uuid.uuid4().hex[:8]}"
                )
                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tx_id, gid, uid, delta, bankroll, category, ref_id, now_ts),
                )
            except sqlite3.IntegrityError as exc:
                await tx.execute("ROLLBACK TO SAVEPOINT bj_settle")
                await tx.execute("RELEASE SAVEPOINT bj_settle")
                if key is not None and expected is not None and is_ledger_tx_id_unique_error(exc):
                    matched = await lookup_idempotent_ledger_entry(tx, key, expected)
                    if matched is not None:
                        return matched.balance_after
                raise
            else:
                await tx.execute("RELEASE SAVEPOINT bj_settle")
        return bankroll

    async def leaderboard(self, limit: int = 10, *, guild_id: int | None = None) -> list[tuple[int, int]]:
        """Leaderboard by balance from the central users table for this guild."""
        gid = self._resolve_gid(guild_id)
        limit = max(1, min(limit, 25))
        rows = await self.db.fetchall(
            "SELECT user_id, balance FROM users WHERE guild_id=? ORDER BY balance DESC LIMIT ?",
            (gid, limit),
        )
        return [(int(uid), int(br)) for uid, br in rows]

    async def reset_user(self, user_id: int, *, guild_id: int | None = None) -> int:
        """Reset bankroll/stats and increment the user's reset counter in this guild."""
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now_ts = int(self._clock())

        async with self.db.write_transaction() as tx:
            await self._ensure_user_conn(tx, uid, gid)
            cur = await tx.execute(
                "SELECT reset_count FROM blackjack_users WHERE guild_id=? AND user_id=?",
                (gid, uid),
            )
            row = await cur.fetchone()
            resets = (int(row[0]) if row is not None and row[0] is not None else 0) + 1

            await tx.execute(
                "UPDATE users SET balance=? WHERE guild_id=? AND user_id=?",
                (self.starting_bankroll, gid, uid),
            )
            await tx.execute(
                "UPDATE blackjack_users "
                "SET bankroll=?, hands_played=0, wins=0, losses=0, pushes=0, reset_count=? "
                "WHERE guild_id=? AND user_id=?",
                (self.starting_bankroll, resets, gid, uid),
            )
            tx_id = f"bj_reset:{gid}:{uid}:{now_ts}:{time.monotonic_ns()}:{uuid.uuid4().hex[:8]}"
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, 0, ?, 'admin_set', 'blackjack_reset', ?)
                """,
                (tx_id, gid, uid, self.starting_bankroll, now_ts),
            )
        return resets

    # --------------------------------------------------------------------------
    # Restart Recovery & Unsettled Escrow Auditing
    # --------------------------------------------------------------------------

    async def find_unsettled_reservations(
        self,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find active escrow reservations in economy_ledger that lack a resolution record."""
        gid = self._resolve_gid(guild_id) if guild_id is not None else None
        query = """
            SELECT b.transaction_id, b.guild_id, b.user_id, b.amount, b.balance_after, b.created_at
            FROM economy_ledger b
            WHERE b.category IN ('bet', 'blackjack_wager')
              AND b.transaction_id LIKE 'bj_bet:%'
              AND NOT EXISTS (
                  SELECT 1 FROM economy_ledger r
                  WHERE r.reference_id = b.transaction_id
                    AND r.category IN ('payout', 'refund', 'settle', 'loss_settle', 'blackjack_payout', 'blackjack_refund', 'blackjack_settle')
              )
        """
        params: list[Any] = []
        if gid is not None:
            query += " AND b.guild_id = ?"
            params.append(gid)
        if user_id is not None:
            query += " AND b.user_id = ?"
            params.append(int(user_id))
        query += " ORDER BY b.created_at ASC"

        rows = await self.db.fetchall(query, tuple(params))
        unsettled = []
        for r in rows:
            unsettled.append(
                {
                    "transaction_id": str(r[0]),
                    "guild_id": int(r[1]),
                    "user_id": int(r[2]),
                    "amount": abs(int(r[3])),
                    "balance_after": int(r[4]),
                    "created_at": int(r[5]),
                }
            )
        return unsettled

    async def refund_unsettled_reservation(
        self,
        *,
        transaction_id: str,
        guild_id: int,
        user_id: int,
        amount: int,
        reason: str = "Interrupted by restart",
    ) -> int:
        """Atomically refund an orphaned reservation from an interrupted round."""
        amount = int(amount)
        if amount <= 0:
            return await self.get_bankroll(user_id, guild_id=guild_id)
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now_ts = int(self._clock())
        refund_key = f"bj_recovery_refund:{transaction_id}"
        expected = LedgerIdempotencyIdentity(
            guild_id=gid,
            user_id=uid,
            amount=amount,
            category="refund",
            reference_id=str(transaction_id),
            operation_type="credit",
            legacy_categories=frozenset({"blackjack_refund"}),
        )

        async with self.db.write_transaction() as tx:
            matched = await lookup_idempotent_ledger_entry(tx, refund_key, expected)
            if matched is not None:
                return matched.balance_after

            await tx.execute("SAVEPOINT bj_recovery_refund")
            try:
                await self._ensure_user_conn(tx, uid, gid)
                await tx.execute(
                    "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                    (amount, gid, uid),
                )
                cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
                row = await cur.fetchone()
                new_balance = int(row[0]) if row is not None else self.starting_bankroll

                await tx.execute(
                    "UPDATE blackjack_users SET bankroll=? WHERE guild_id=? AND user_id=?",
                    (new_balance, gid, uid),
                )

                await tx.execute(
                    """
                    INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                    VALUES (?, ?, ?, ?, ?, 'refund', ?, ?)
                    """,
                    (refund_key, gid, uid, amount, new_balance, transaction_id, now_ts),
                )
            except sqlite3.IntegrityError as exc:
                await tx.execute("ROLLBACK TO SAVEPOINT bj_recovery_refund")
                await tx.execute("RELEASE SAVEPOINT bj_recovery_refund")
                if is_ledger_tx_id_unique_error(exc):
                    matched = await lookup_idempotent_ledger_entry(tx, refund_key, expected)
                    if matched is not None:
                        return matched.balance_after
                raise
            else:
                await tx.execute("RELEASE SAVEPOINT bj_recovery_refund")
            log.info(
                "Recovered unsettled blackjack reservation %s: refunded %d credits to user %d (guild %d). Reason: %s",
                transaction_id,
                amount,
                uid,
                gid,
                reason,
            )
        return new_balance
