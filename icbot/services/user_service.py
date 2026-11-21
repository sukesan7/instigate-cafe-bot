from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from icbot.db import Database
from icbot.errors import BotError
from icbot.errors import InsufficientBalanceError as BaseInsufficientBalanceError

ANONYMIZED_USER_ID: int = 0
ANONYMIZED_NOTE_SENTINEL: str = "[ANONYMIZED_USER]"
DEFAULT_EXPORT_BATCH_SIZE: int = 250
DEFAULT_EXPORT_MAX_ROWS: int = 5000


_ALPHA_HEX = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def _scrub_uid_token(raw: str | None, uid: int, *, guild_id: int | None = None) -> str | None:
    """Replace occurrences of ``uid`` in transaction IDs, reference IDs, or notes with irreversible tokens."""
    if raw is None:
        return None
    s = str(raw)
    uid_str = str(int(uid))
    if uid_str not in s:
        return s
    gid_part = f"{int(guild_id)}:" if guild_id is not None else ""
    digest = hashlib.sha256(f"icbot-anon:{gid_part}{uid_str}".encode()).hexdigest()[:12].translate(_ALPHA_HEX)
    # Replace exact token boundaries (colon-delimited, <@uid>, or word boundary)
    pattern = re.compile(rf"(?<![0-9]){re.escape(uid_str)}(?![0-9])")
    scrubbed = pattern.sub(f"anon_{digest}", s)
    if uid_str in scrubbed:
        scrubbed = scrubbed.replace(uid_str, digest)
    return scrubbed


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

    async def _fetch_paginated_rows(
        self,
        sql_without_limit: str,
        params: tuple[Any, ...],
        *,
        batch_size: int = DEFAULT_EXPORT_BATCH_SIZE,
        max_rows: int | None = DEFAULT_EXPORT_MAX_ROWS,
    ) -> tuple[list[tuple[Any, ...]], int, bool]:
        """Deterministically fetch rows in bounded batches with explicit truncation detection."""
        eff_batch = max(1, min(int(batch_size), 1000))
        eff_max = int(max_rows) if (max_rows is not None and int(max_rows) > 0) else None
        collected: list[tuple[Any, ...]] = []
        offset = 0
        truncated = False

        while True:
            fetch_limit = eff_batch
            if eff_max is not None:
                remaining = eff_max - len(collected)
                if remaining <= 0:
                    # Probe 1 row to detect if more rows exist beyond max_rows
                    probe = await self.db.fetchall(
                        f"{sql_without_limit} LIMIT 1 OFFSET ?",
                        (*params, offset),
                    )
                    if probe:
                        truncated = True
                    break
                fetch_limit = min(eff_batch, remaining)

            batch = await self.db.fetchall(
                f"{sql_without_limit} LIMIT ? OFFSET ?",
                (*params, fetch_limit, offset),
            )
            if not batch:
                break
            collected.extend(batch)
            offset += len(batch)
            if len(batch) < fetch_limit:
                break

        total_matching = len(collected)
        if truncated:
            # Count exact total rows so the caller receives an honest total vs exported count
            # Strip trailing ORDER BY clause for count query if needed, or wrap in subquery
            cnt_row = await self.db.fetchone(
                f"SELECT COUNT(1) FROM ({sql_without_limit})",
                params,
            )
            if cnt_row and cnt_row[0] is not None:
                total_matching = int(cnt_row[0])

        return collected, total_matching, truncated

    async def export_user_data(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
        include_global_preferences: bool = True,
        batch_size: int = DEFAULT_EXPORT_BATCH_SIZE,
        max_rows_per_table: int | None = DEFAULT_EXPORT_MAX_ROWS,
    ) -> dict[str, Any]:
        """Export all user-addressable data across guild-scoped and global tables in a portable format.

        Uses deterministic batched pagination (no silent ``LIMIT 100`` truncation) and reports explicit
        ``truncated`` and ``warnings`` metadata if ``max_rows_per_table`` is reached.
        """
        now = self._now()
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        truncation_map: dict[str, bool] = {}
        total_counts: dict[str, int] = {}
        export_warnings: list[str] = []

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
        pos_rows, pos_total, pos_trunc = await self._fetch_paginated_rows(
            """
            SELECT symbol, side, qty, avg_price, collateral_locked
            FROM fun_positions
            WHERE guild_id=? AND user_id=?
            ORDER BY symbol ASC, side ASC
            """,
            (gid, uid),
            batch_size=batch_size,
            max_rows=max_rows_per_table,
        )
        truncation_map["positions"] = pos_trunc
        total_counts["positions"] = pos_total
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

        # 4. Market trades (paginated, deterministic chronological/ID sort)
        trade_rows, trade_total, trade_trunc = await self._fetch_paginated_rows(
            """
            SELECT id, ts_unix, symbol, side, qty, price, cash_delta, realized_pnl, note
            FROM fun_trades
            WHERE guild_id=? AND user_id=?
            ORDER BY ts_unix DESC, id DESC
            """,
            (gid, uid),
            batch_size=batch_size,
            max_rows=max_rows_per_table,
        )
        truncation_map["trades"] = trade_trunc
        total_counts["trades"] = trade_total
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

        # 5. Economy ledger entries (paginated, no hidden 100-row cap)
        ledger_rows, ledger_total, ledger_trunc = await self._fetch_paginated_rows(
            """
            SELECT id, transaction_id, amount, balance_after, category, reference_id, created_at
            FROM economy_ledger
            WHERE guild_id=? AND user_id=?
            ORDER BY created_at DESC, id DESC
            """,
            (gid, uid),
            batch_size=batch_size,
            max_rows=max_rows_per_table,
        )
        truncation_map["ledger"] = ledger_trunc
        total_counts["ledger"] = ledger_total
        ledger_data = [
            {
                "id": int(r[0]),
                "transaction_id": str(r[1]),
                "amount": int(r[2]),
                "balance_after": int(r[3]),
                "category": str(r[4]),
                "reference_id": str(r[5]) if r[5] is not None else None,
                "created_at": int(r[6]),
            }
            for r in ledger_rows
        ]

        # 6. Exams created by user in this guild
        exam_rows, exam_total, exam_trunc = await self._fetch_paginated_rows(
            """
            SELECT id, course_code, course_name, start_ts, end_ts, location, term, visibility, created_at
            FROM exams
            WHERE guild_id=? AND created_by=?
            ORDER BY start_ts ASC, id ASC
            """,
            (gid, uid),
            batch_size=batch_size,
            max_rows=max_rows_per_table,
        )
        truncation_map["exams_created"] = exam_trunc
        total_counts["exams_created"] = exam_total
        exams_data = [
            {
                "id": int(r[0]),
                "course_code": str(r[1]),
                "course_name": str(r[2]),
                "start_ts": int(r[3]),
                "end_ts": int(r[4]) if r[4] is not None else None,
                "location": str(r[5] or ""),
                "term": str(r[6] or ""),
                "visibility": str(r[7] or "guild"),
                "created_at": int(r[8]),
            }
            for r in exam_rows
        ]

        # 7. Academic resources contributed by user in this guild
        res_rows, res_total, res_trunc = await self._fetch_paginated_rows(
            """
            SELECT id, course_key, display_code, title, url, resource_type, source_attribution, created_at
            FROM academic_resources
            WHERE guild_id=? AND added_by=?
            ORDER BY created_at DESC, id DESC
            """,
            (gid, uid),
            batch_size=batch_size,
            max_rows=max_rows_per_table,
        )
        truncation_map["academic_resources_added"] = res_trunc
        total_counts["academic_resources_added"] = res_total
        resources_data = [
            {
                "id": int(r[0]),
                "course_key": str(r[1]),
                "display_code": str(r[2]),
                "title": str(r[3]),
                "url": str(r[4]),
                "resource_type": str(r[5]),
                "source_attribution": str(r[6]),
                "created_at": int(r[7]),
            }
            for r in res_rows
        ]

        # 8. Cross-guild / global user job preferences (explicitly labeled as global scope)
        job_prefs: dict[str, Any] | None = None
        if include_global_preferences and hasattr(self.db, "get_user_job_preferences"):
            raw_prefs = await self.db.get_user_job_preferences(uid)
            if raw_prefs is not None:
                job_prefs = {
                    **raw_prefs,
                    "scope": "global_user",
                }

        for table_key, is_trunc in truncation_map.items():
            if is_trunc:
                export_warnings.append(
                    f"Table '{table_key}' exceeded max_rows_per_table={max_rows_per_table} "
                    f"(exported {max_rows_per_table} of {total_counts[table_key]} rows)."
                )

        return {
            "format_version": 2,
            "exported_at": now,
            "guild_id": gid,
            "user_id": uid,
            "profile": profile_data,
            "blackjack": bj_data,
            "positions": positions_data,
            "trades": trades_data,
            "ledger": ledger_data,
            "exams_created": exams_data,
            "academic_resources_added": resources_data,
            "job_preferences": job_prefs,
            "pagination": {
                "batch_size": max(1, min(int(batch_size), 1000)),
                "max_rows_per_table": max_rows_per_table,
                "total_counts": total_counts,
                "truncated_tables": truncation_map,
            },
            "truncated": any(truncation_map.values()),
            "warnings": export_warnings,
        }

    async def delete_user_data(
        self,
        user_id: int,
        *,
        guild_id: int | None = None,
        anonymize_ledger: bool = True,
        delete_global_preferences: bool = False,
        retain_admin_actor_audit: bool = True,
    ) -> dict[str, int]:
        """Delete or irreversibly de-identify user-addressable records (Right-to-be-Forgotten).

        Scope & Privacy Contract:
            1. Hard-Deleted (Guild-Scoped Personal State):
               - ``users``: Deletes the user's profile, display_name, avatar_url, balance, and daily_claim_ts.
               - ``blackjack_users``: Deletes the user's blackjack bankroll and win/loss/reset counters.
               - ``fun_positions``: Deletes open long/short positions in the target guild.
            2. Conditionally Hard-Deleted (Cross-Guild / Global User State):
               - ``user_job_preferences``: Keyed globally by ``user_id`` across guilds. Deleted automatically
                 when the user has no remaining guild profiles in ``users``, or when
                 ``delete_global_preferences=True`` is explicitly passed. Preserved if the user still has
                 active profiles in other guilds and ``delete_global_preferences=False``.
            3. Tombstone-Anonymized (Financial Ledgers & Shared Community Content):
               - ``economy_ledger``: Preserves all rows and exact token amounts so aggregate token supply
                 reconciles, while replacing ``user_id`` with ``0`` (``ANONYMIZED_USER_ID``) and scrubbing
                 embedded ``user_id`` substrings from ``transaction_id`` and ``reference_id`` when
                 ``anonymize_ledger=True``.
               - ``fun_trades``: Preserves trade volume/price history for market integrity while setting
                 ``user_id = 0`` and ``note = '[ANONYMIZED_USER]'`` (when ``anonymize_ledger=True``;
                 otherwise hard-deleted).
               - ``fun_stock_events`` & ``stock_events``: Replaces ``user_id`` with ``0`` and scrubs any
                 user-identifying reason string.
               - ``fun_market_weeks``: Sets ``settled_by = NULL`` where ``settled_by = user_id``.
               - ``exams``: Sets ``created_by = 0`` where ``created_by = user_id`` so shared guild exam
                 calendars remain intact without creator attribution.
               - ``academic_resources``: Sets ``added_by = 0`` where ``added_by = user_id`` so shared course
                 links remain intact without contributor attribution.
            4. Security & Administrative Audit Logs:
               - ``admin_audit_log``: Scrubs ``target_user_id = NULL`` and redacts ``user_id`` from
                 ``details_json`` where the user was the target of an action. If
                 ``retain_admin_actor_audit=False``, also anonymizes ``admin_user_id = 0`` and
                 ``guild_settings_audit.actor_id = 0``; when ``retain_admin_actor_audit=True`` (default),
                 privileged administrator actor IDs are retained for security accountability.
               - Deletion Audit Event: Records a ``user_data_delete`` event in ``admin_audit_log`` with
                 ``target_user_id = NULL`` and an opaque random ``erasure_receipt`` UUID (never re-inserting
                 the deleted ``user_id`` into the audit log).
        """
        now = self._now()
        uid = int(user_id)
        gid = self._resolve_gid(guild_id)
        deleted_counts: dict[str, int] = {}

        async with self.db.write_transaction() as tx:
            # 1. Hard-delete guild-scoped user profile
            cur_u = await tx.execute("DELETE FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            deleted_counts["users"] = cur_u.rowcount if cur_u.rowcount >= 0 else 0

            # 2. Hard-delete guild-scoped blackjack stats
            cur_bj = await tx.execute(
                "DELETE FROM blackjack_users WHERE guild_id=? AND user_id=?", (gid, uid)
            )
            deleted_counts["blackjack_users"] = cur_bj.rowcount if cur_bj.rowcount >= 0 else 0

            # 3. Hard-delete guild-scoped fun market positions
            cur_pos = await tx.execute("DELETE FROM fun_positions WHERE guild_id=? AND user_id=?", (gid, uid))
            deleted_counts["fun_positions"] = cur_pos.rowcount if cur_pos.rowcount >= 0 else 0

            # 4. Handle fun_trades (anonymize to preserve market trade tape or delete if anonymize_ledger=False)
            if anonymize_ledger:
                cur_tr_anom = await tx.execute(
                    "UPDATE fun_trades SET user_id=?, note=? WHERE guild_id=? AND user_id=?",
                    (ANONYMIZED_USER_ID, ANONYMIZED_NOTE_SENTINEL, gid, uid),
                )
                anon_trades = cur_tr_anom.rowcount if cur_tr_anom.rowcount >= 0 else 0
                deleted_counts["fun_trades_anonymized"] = anon_trades
                deleted_counts["fun_trades"] = anon_trades
            else:
                cur_tr = await tx.execute("DELETE FROM fun_trades WHERE guild_id=? AND user_id=?", (gid, uid))
                deleted_counts["fun_trades"] = cur_tr.rowcount if cur_tr.rowcount >= 0 else 0
                deleted_counts["fun_trades_anonymized"] = 0

            # 5. De-identify shared academic & market event tables in this guild
            cur_ex = await tx.execute(
                "UPDATE exams SET created_by=0 WHERE guild_id=? AND created_by=?",
                (gid, uid),
            )
            deleted_counts["exams_anonymized"] = cur_ex.rowcount if cur_ex.rowcount >= 0 else 0

            cur_ar = await tx.execute(
                "UPDATE academic_resources SET added_by=0 WHERE guild_id=? AND added_by=?",
                (gid, uid),
            )
            deleted_counts["academic_resources_anonymized"] = cur_ar.rowcount if cur_ar.rowcount >= 0 else 0

            cur_fse = await tx.execute(
                "UPDATE fun_stock_events SET user_id=0, reason=? WHERE guild_id=? AND user_id=?",
                (ANONYMIZED_NOTE_SENTINEL, gid, uid),
            )
            deleted_counts["fun_stock_events_anonymized"] = cur_fse.rowcount if cur_fse.rowcount >= 0 else 0

            cur_se = await tx.execute(
                "UPDATE stock_events SET user_id=0, reason=? WHERE guild_id=? AND user_id=?",
                (ANONYMIZED_NOTE_SENTINEL, gid, uid),
            )
            deleted_counts["stock_events_anonymized"] = cur_se.rowcount if cur_se.rowcount >= 0 else 0

            cur_fmw = await tx.execute(
                "UPDATE fun_market_weeks SET settled_by=NULL WHERE guild_id=? AND settled_by=?",
                (gid, uid),
            )
            deleted_counts["fun_market_weeks_anonymized"] = cur_fmw.rowcount if cur_fmw.rowcount >= 0 else 0

            # 6. Cross-guild user_job_preferences scope check
            other_guilds_row = await tx.fetchone(
                "SELECT COUNT(1) FROM users WHERE user_id=? AND guild_id!=?",
                (uid, gid),
            )
            remaining_guild_profiles = int(other_guilds_row[0]) if other_guilds_row else 0
            if delete_global_preferences or remaining_guild_profiles == 0:
                cur_prefs = await tx.execute(
                    "DELETE FROM user_job_preferences WHERE user_id=?",
                    (uid,),
                )
                deleted_counts["user_job_preferences"] = cur_prefs.rowcount if cur_prefs.rowcount >= 0 else 0
                deleted_counts["user_job_preferences_retained_cross_guild"] = 0
            else:
                pref_exists = await tx.fetchone(
                    "SELECT 1 FROM user_job_preferences WHERE user_id=?",
                    (uid,),
                )
                deleted_counts["user_job_preferences"] = 0
                deleted_counts["user_job_preferences_retained_cross_guild"] = (
                    1 if pref_exists is not None else 0
                )

            # 7. Economy ledger preservation & irreversible de-identification
            ledger_rows = await tx.fetchall(
                "SELECT id, transaction_id, reference_id FROM economy_ledger WHERE guild_id=? AND user_id=?",
                (gid, uid),
            )
            ledger_count = len(ledger_rows)
            if anonymize_ledger and ledger_rows:
                # Build old->new transaction_id mapping so any reference_id pointing to an earlier tx stays linked
                tx_id_map: dict[str, str] = {}
                for row_id, orig_tx_id, _orig_ref in ledger_rows:
                    scrubbed_tx = _scrub_uid_token(str(orig_tx_id), uid, guild_id=gid) or str(orig_tx_id)
                    if scrubbed_tx == str(orig_tx_id):
                        tx_id_map[str(orig_tx_id)] = scrubbed_tx
                    else:
                        row_tag = f"{int(row_id):x}".translate(_ALPHA_HEX)
                        tx_id_map[str(orig_tx_id)] = f"{scrubbed_tx}:{row_tag}"

                for row_id, orig_tx_id, orig_ref in ledger_rows:
                    new_tx_id = tx_id_map[str(orig_tx_id)]
                    if orig_ref is not None and str(orig_ref) in tx_id_map:
                        new_ref_id: str | None = tx_id_map[str(orig_ref)]
                    else:
                        new_ref_id = _scrub_uid_token(
                            str(orig_ref) if orig_ref is not None else None,
                            uid,
                            guild_id=gid,
                        )
                    await tx.execute(
                        "UPDATE economy_ledger SET user_id=?, transaction_id=?, reference_id=? WHERE id=?",
                        (ANONYMIZED_USER_ID, new_tx_id, new_ref_id, int(row_id)),
                    )
                deleted_counts["economy_ledger_anonymized"] = ledger_count
            else:
                deleted_counts["economy_ledger_anonymized"] = 0
            deleted_counts["economy_ledger_preserved"] = ledger_count

            # 8. Admin & settings audit logs: de-identify target_user_id and optional actor_id
            target_audit_rows = await tx.fetchall(
                "SELECT id, details_json FROM admin_audit_log WHERE guild_id=? AND target_user_id=?",
                (gid, uid),
            )
            for audit_id, details_raw in target_audit_rows:
                scrubbed_details = _scrub_uid_token(str(details_raw or "{}"), uid, guild_id=gid) or "{}"
                await tx.execute(
                    "UPDATE admin_audit_log SET target_user_id=NULL, details_json=? WHERE id=?",
                    (scrubbed_details, int(audit_id)),
                )
            deleted_counts["admin_audit_target_anonymized"] = len(target_audit_rows)

            if not retain_admin_actor_audit:
                cur_adm_actor = await tx.execute(
                    "UPDATE admin_audit_log SET admin_user_id=0 WHERE guild_id=? AND admin_user_id=?",
                    (gid, uid),
                )
                deleted_counts["admin_audit_actor_anonymized"] = (
                    cur_adm_actor.rowcount if cur_adm_actor.rowcount >= 0 else 0
                )
                cur_gs_actor = await tx.execute(
                    "UPDATE guild_settings_audit SET actor_id=0 WHERE guild_id=? AND actor_id=?",
                    (gid, uid),
                )
                deleted_counts["guild_settings_audit_actor_anonymized"] = (
                    cur_gs_actor.rowcount if cur_gs_actor.rowcount >= 0 else 0
                )
            else:
                deleted_counts["admin_audit_actor_anonymized"] = 0
                deleted_counts["guild_settings_audit_actor_anonymized"] = 0

            # 9. Record privacy deletion audit receipt WITHOUT re-exposing the deleted user_id
            had_any_activity = any(
                deleted_counts.get(k, 0) > 0
                for k in (
                    "users",
                    "blackjack_users",
                    "fun_positions",
                    "fun_trades",
                    "exams_anonymized",
                    "academic_resources_anonymized",
                    "fun_stock_events_anonymized",
                    "stock_events_anonymized",
                    "fun_market_weeks_anonymized",
                    "user_job_preferences",
                    "economy_ledger_anonymized",
                    "admin_audit_target_anonymized",
                    "admin_audit_actor_anonymized",
                    "guild_settings_audit_actor_anonymized",
                )
            )
            if anonymize_ledger and had_any_activity:
                receipt_payload = json.dumps(
                    {
                        "status": "deleted",
                        "erasure_receipt": uuid.uuid4().hex[:16],
                        "anonymized_ledger_rows": ledger_count,
                    },
                    sort_keys=True,
                )
                await tx.execute(
                    """
                    INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                    VALUES (?, 0, 'user_data_delete', NULL, ?, ?)
                    """,
                    (gid, receipt_payload, now),
                )

        return deleted_counts
