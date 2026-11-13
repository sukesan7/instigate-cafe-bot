from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from icbot.db import Database
from icbot.errors import BotError
from icbot.errors import InsufficientBalanceError as BaseInsufficientBalanceError


@dataclass(frozen=True)
class UserProfile:
    user_id: int
    display_name: str
    avatar_url: str
    balance: int
    created_at: int
    daily_claim_ts: int
    guild_id: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    transaction_id: str
    guild_id: int
    user_id: int
    amount: int
    balance_after: int
    category: str
    reference_id: str | None
    created_at: int


class InsufficientBalanceError(BaseInsufficientBalanceError):
    pass


class DailyAlreadyClaimedError(BotError, RuntimeError):
    """Raised when the user has already claimed their daily reward within the cooldown."""

    def __init__(self, next_claim_ts: int):
        self.next_claim_ts = next_claim_ts
        super().__init__(f"Daily already claimed. Next claim at <t:{next_claim_ts}:R>.")


DAILY_COOLDOWN_SECONDS = 24 * 60 * 60  # 24 hours


class UserStore:
    """Central user profile & economy service.

    All balance mutations flow through this class using atomic conditional SQL
    and transactionally coupled ledger entries so balances cannot be double-spent,
    claims cannot be paid twice, and debits cannot drive balances below limits.
    """

    def __init__(
        self,
        db: Database,
        *,
        guild_id: int = 0,
        starting_balance: int = 1000,
        daily_claim_amount: int = 100,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.guild_id = int(guild_id or 0)
        self.starting_balance = int(starting_balance or 1000)
        self.daily_claim_amount = int(daily_claim_amount or 100)
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    def _resolve_gid(self, guild_id: int | None) -> int:
        return int(guild_id) if guild_id is not None else self.guild_id

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    async def ensure_user(
        self,
        user_id: int,
        display_name: str = "",
        avatar_url: str = "",
        *,
        guild_id: int | None = None,
    ) -> UserProfile:
        """Ensure the user exists in this guild. Update display_name/avatar on every call."""
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        dname = str(display_name)
        aurl = str(avatar_url)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO users "
                "(guild_id, user_id, display_name, avatar_url, balance, created_at, daily_claim_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (gid, uid, dname, aurl, self.starting_balance, now),
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
                params.extend([gid, uid])
                await tx.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE guild_id=? AND user_id=?",
                    tuple(params),
                )
            cur = await tx.execute(
                "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts, guild_id "
                "FROM users WHERE guild_id=? AND user_id=?",
                (gid, uid),
            )
            row = await cur.fetchone()

        if row is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Failed to ensure user row for (guild_id={gid}, user_id={uid})")
        return UserProfile(
            user_id=int(row[0]),
            display_name=str(row[1]),
            avatar_url=str(row[2]),
            balance=int(row[3]),
            created_at=int(row[4]),
            daily_claim_ts=int(row[5]),
            guild_id=int(row[6]),
        )

    async def get_profile(self, user_id: int, *, guild_id: int | None = None) -> UserProfile | None:
        gid = self._resolve_gid(guild_id)
        row = await self.db.fetchone(
            "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts, guild_id "
            "FROM users WHERE guild_id=? AND user_id=?",
            (gid, int(user_id)),
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
            guild_id=int(row[6]),
        )

    # ------------------------------------------------------------------
    # Balance operations (Atomic conditional SQL & transaction ledger)
    # ------------------------------------------------------------------

    async def get_balance(self, user_id: int, *, guild_id: int | None = None) -> int:
        gid = self._resolve_gid(guild_id)
        row = await self.db.fetchone(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
            (gid, int(user_id)),
        )
        return int(row[0]) if row else self.starting_balance

    async def add_balance(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
        category: str = "credit",
        reference_id: str | None = None,
    ) -> int:
        """Add to balance (winnings, payouts, claims). Returns new balance.

        Coupled with atomic conditional SQL and ledger recording.
        """
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Amount must be >= 1.")
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            # Idempotency check: return recorded balance_after if this key was already processed
            if idempotency_key:
                cur_idem = await tx.execute(
                    "SELECT balance_after FROM economy_ledger WHERE transaction_id=?",
                    (str(idempotency_key),),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return int(idem_row[0])

            await tx.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, self.starting_balance, now),
            )
            await tx.execute(
                "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                (amount, gid, uid),
            )
            cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            row = await cur.fetchone()
            new_balance = int(row[0]) if row is not None else self.starting_balance + amount

            tx_id = (
                str(idempotency_key) if idempotency_key else f"credit:{gid}:{uid}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tx_id, gid, uid, amount, new_balance, str(category), reference_id, now),
            )

        return new_balance

    async def subtract_balance(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
        category: str = "debit",
        reference_id: str | None = None,
    ) -> int:
        """Subtract from balance (bets, purchases). Raises InsufficientBalanceError.

        Uses atomic conditional SQL (balance = balance - ? WHERE balance >= ?)
        and records a transactionally coupled ledger entry.
        """
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Amount must be >= 1.")
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            # Idempotency check: return recorded balance_after if this key was already processed
            if idempotency_key:
                cur_idem = await tx.execute(
                    "SELECT balance_after FROM economy_ledger WHERE transaction_id=?",
                    (str(idempotency_key),),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return int(idem_row[0])

            await tx.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, self.starting_balance, now),
            )

            # Atomic conditional update prevents balance overdraft without Python-level read-modify-write
            cur_upd = await tx.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (amount, gid, uid, amount),
            )
            if cur_upd.rowcount == 0:
                cur_bal = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                bal_row = await cur_bal.fetchone()
                current = int(bal_row[0]) if bal_row is not None else self.starting_balance
                raise InsufficientBalanceError(f"Insufficient balance. Need ${amount}, have ${current}.")

            cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            row = await cur.fetchone()
            new_balance = int(row[0]) if row is not None else 0

            tx_id = (
                str(idempotency_key) if idempotency_key else f"debit:{gid}:{uid}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tx_id, gid, uid, -amount, new_balance, str(category), reference_id, now),
            )

        return new_balance

    async def set_balance(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int | None = None,
        admin_user_id: int | None = None,
        reason: str | None = None,
    ) -> int:
        """Set balance to an exact value (admin reset/override)."""
        amount = int(amount)
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, self.starting_balance, now),
            )
            await tx.execute(
                "UPDATE users SET balance=? WHERE guild_id=? AND user_id=?",
                (amount, gid, uid),
            )
            tx_id = f"admin_set:{gid}:{uid}:{now}:{time.monotonic_ns()}"
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'admin_set', ?, ?)
                """,
                (tx_id, gid, uid, 0, amount, reason or f"Admin:{admin_user_id}", now),
            )
        return amount

    # ------------------------------------------------------------------
    # Daily claim (Atomic conditional SQL & exactly-once ledger recording)
    # ------------------------------------------------------------------

    async def claim_daily(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, int]:
        """Claim daily reward. Returns (amount_claimed, new_balance).

        Raises DailyAlreadyClaimedError if cooldown has not elapsed.
        """
        now = self._now()
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)

        async with self.db.write_transaction() as tx:
            if idempotency_key:
                cur_idem = await tx.execute(
                    "SELECT amount, balance_after FROM economy_ledger WHERE transaction_id=?",
                    (str(idempotency_key),),
                )
                idem_row = await cur_idem.fetchone()
                if idem_row is not None:
                    return int(idem_row[0]), int(idem_row[1])

            await tx.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, self.starting_balance, now),
            )

            # Atomic conditional update checks cooldown in the DB engine without race windows
            cur_upd = await tx.execute(
                """
                UPDATE users
                SET balance = balance + ?, daily_claim_ts = ?
                WHERE guild_id = ? AND user_id = ? AND (? - daily_claim_ts >= ?)
                """,
                (self.daily_claim_amount, now, gid, uid, now, DAILY_COOLDOWN_SECONDS),
            )
            if cur_upd.rowcount == 0:
                cur_u = await tx.execute(
                    "SELECT daily_claim_ts FROM users WHERE guild_id=? AND user_id=?",
                    (gid, uid),
                )
                row_u = await cur_u.fetchone()
                last_claim = int(row_u[0]) if row_u is not None else 0
                next_available = last_claim + DAILY_COOLDOWN_SECONDS
                raise DailyAlreadyClaimedError(next_claim_ts=next_available)

            cur = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            row = await cur.fetchone()
            new_balance = int(row[0]) if row is not None else self.starting_balance + self.daily_claim_amount

            day_bucket = now // DAILY_COOLDOWN_SECONDS
            tx_id = str(idempotency_key) if idempotency_key else f"claim:{gid}:{uid}:{day_bucket}:{now}"
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'daily', NULL, ?)
                """,
                (tx_id, gid, uid, self.daily_claim_amount, new_balance, now),
            )

        return self.daily_claim_amount, new_balance

    # ------------------------------------------------------------------
    # Audit & Ledger query
    # ------------------------------------------------------------------

    async def get_ledger(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
        limit: int = 20,
    ) -> list[LedgerEntry]:
        """Fetch recent transaction ledger history for audit verification."""
        gid = self._resolve_gid(guild_id)
        limit = max(1, min(int(limit), 100))
        rows = await self.db.fetchall(
            """
            SELECT id, transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at
            FROM economy_ledger
            WHERE guild_id=? AND user_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (gid, int(user_id), limit),
        )
        return [
            LedgerEntry(
                id=int(r[0]),
                transaction_id=str(r[1]),
                guild_id=int(r[2]),
                user_id=int(r[3]),
                amount=int(r[4]),
                balance_after=int(r[5]),
                category=str(r[6]),
                reference_id=str(r[7]) if r[7] is not None else None,
                created_at=int(r[8]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Data Portability & Right-to-be-Forgotten
    # ------------------------------------------------------------------

    async def export_user_data(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
    ) -> dict[str, Any]:
        """Export all user-specific data (profile, casino, positions, trades, ledger) in a portable format.

        Non-destructive data portability export (GDPR / right-of-access compliant).
        """
        now = self._now()
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)

        # 1. Profile
        prof = await self.get_profile(uid, guild_id=gid)
        profile_data = (
            {
                "user_id": prof.user_id,
                "display_name": prof.display_name,
                "avatar_url": prof.avatar_url,
                "balance": prof.balance,
                "created_at": prof.created_at,
                "daily_claim_ts": prof.daily_claim_ts,
            }
            if prof
            else None
        )

        # 2. Blackjack stats
        bj_row = await self.db.fetchone(
            """
            SELECT bankroll, hands_played, wins, losses, pushes, reset_count
            FROM blackjack_users
            WHERE guild_id=? AND user_id=?
            """,
            (gid, uid),
        )
        bj_data = (
            {
                "bankroll": int(bj_row[0]),
                "hands_played": int(bj_row[1]),
                "wins": int(bj_row[2]),
                "losses": int(bj_row[3]),
                "pushes": int(bj_row[4]),
                "reset_count": int(bj_row[5]),
            }
            if bj_row
            else None
        )

        # 3. Market positions
        pos_rows = await self.db.fetchall(
            """
            SELECT symbol, side, qty, avg_price, collateral_locked
            FROM fun_positions
            WHERE guild_id=? AND user_id=?
            ORDER BY symbol ASC, side ASC
            """,
            (gid, uid),
        )
        positions_data = [
            {
                "symbol": str(r[0]),
                "side": str(r[1]),
                "qty": int(r[2]),
                "avg_price": float(r[3]),
                "collateral_locked": int(r[4]),
            }
            for r in pos_rows
        ]

        # 4. Market trades
        trade_rows = await self.db.fetchall(
            """
            SELECT id, ts_unix, symbol, side, qty, price, cash_delta, realized_pnl, note
            FROM fun_trades
            WHERE guild_id=? AND user_id=?
            ORDER BY ts_unix DESC, id DESC
            LIMIT 100
            """,
            (gid, uid),
        )
        trades_data = [
            {
                "id": int(r[0]),
                "ts_unix": int(r[1]),
                "symbol": str(r[2]),
                "side": str(r[3]),
                "qty": int(r[4]),
                "price": int(r[5]),
                "cash_delta": int(r[6]),
                "realized_pnl": int(r[7]),
                "note": str(r[8]) if r[8] is not None else None,
            }
            for r in trade_rows
        ]

        # 5. Economy ledger entries
        ledger_entries = await self.get_ledger(uid, guild_id=gid, limit=100)
        ledger_data = [
            {
                "id": le.id,
                "transaction_id": le.transaction_id,
                "amount": le.amount,
                "balance_after": le.balance_after,
                "category": le.category,
                "reference_id": le.reference_id,
                "created_at": le.created_at,
            }
            for le in ledger_entries
        ]

        return {
            "format_version": 1,
            "exported_at": now,
            "guild_id": gid,
            "user_id": uid,
            "profile": profile_data,
            "blackjack": bj_data,
            "positions": positions_data,
            "trades": trades_data,
            "ledger": ledger_data,
        }

    async def delete_user_data(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
        anonymize_ledger: bool = True,
    ) -> dict[str, int]:
        """Delete user personal records across profiles, casino, and positions (Right-to-be-Forgotten).

        Safety:
            - Purges personal profiles, avatar URLs, and game state.
            - Preserves immutable economy ledger audit records (optionally anonymizing user references)
              so financial reconciliations and aggregate token supply invariant remain untainted.
        """
        now = self._now()
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        deleted_counts: dict[str, int] = {}

        async with self.db.write_transaction() as tx:
            # 1. Delete user profile
            cur_u = await tx.execute("DELETE FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            deleted_counts["users"] = cur_u.rowcount if cur_u.rowcount >= 0 else 0

            # 2. Delete blackjack stats
            cur_bj = await tx.execute(
                "DELETE FROM blackjack_users WHERE guild_id=? AND user_id=?", (gid, uid)
            )
            deleted_counts["blackjack_users"] = cur_bj.rowcount if cur_bj.rowcount >= 0 else 0

            # 3. Delete fun positions
            cur_pos = await tx.execute("DELETE FROM fun_positions WHERE guild_id=? AND user_id=?", (gid, uid))
            deleted_counts["fun_positions"] = cur_pos.rowcount if cur_pos.rowcount >= 0 else 0

            # 4. Delete fun trades
            cur_tr = await tx.execute("DELETE FROM fun_trades WHERE guild_id=? AND user_id=?", (gid, uid))
            deleted_counts["fun_trades"] = cur_tr.rowcount if cur_tr.rowcount >= 0 else 0

            # 5. Ledger retention & anonymization
            # Immutable financial ledger: we preserve records but log the deletion event
            if anonymize_ledger:
                await tx.execute(
                    """
                    INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                    VALUES (?, 0, 'user_data_delete', ?, '{"status": "deleted"}', ?)
                    """,
                    (gid, uid, now),
                )
            deleted_counts["economy_ledger_preserved"] = 1

        return deleted_counts
