from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from icbot.db import Database, Transaction
from icbot.errors import BotError
from icbot.services.fun_market_catalog import catalog_map, normalize_symbol

log = logging.getLogger(__name__)

SHORT_COLLATERAL_RATIO = 1.5
PRICE_FLOOR = 1


class FunMarketError(BotError, RuntimeError):
    pass


@dataclass(frozen=True)
class StockRow:
    symbol: str
    name: str
    price: int
    category: str
    is_active: bool
    updated_ts: int
    week_key: str
    week_open: int
    weekly_change_abs: int
    weekly_change_pct: float


@dataclass(frozen=True)
class PositionRow:
    user_id: int
    symbol: str
    side: str  # LONG / SHORT
    qty: int
    avg_price: float
    collateral_locked: int
    market_price: int
    market_value: int
    unrealized_pnl: int
    equity_value: int


@dataclass(frozen=True)
class PortfolioSnapshot:
    user_id: int
    cash: int
    positions: list[PositionRow]
    net_worth: int
    long_value: int
    short_equity: int
    unrealized_pnl: int


class FunMarketStore:
    def __init__(
        self,
        db: Database,
        *,
        guild_id: int = 0,
        starting_bankroll: int = 1000,
        tz_name: str = "America/Toronto",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.db = db
        self.guild_id = int(guild_id or 0)
        self.starting_bankroll = int(starting_bankroll or 1000)
        self.tz_name = str(tz_name or "America/Toronto")
        self._clock = clock

    def _resolve_gid(self, guild_id: int | None) -> int:
        return int(guild_id) if guild_id is not None else self.guild_id

    # ---------------------------
    # Time / symbols
    # ---------------------------
    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    def _week_key(self) -> str:
        now_ts = self._now()
        try:
            dt = datetime.fromtimestamp(now_ts, tz=ZoneInfo(self.tz_name))
            iso = dt.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            dt = datetime.fromtimestamp(now_ts, tz=UTC)
            iso = dt.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"

    # ---------------------------
    # Internal low-level helpers
    # ---------------------------
    async def _ensure_bj_user_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        user_id: int,
        guild_id: int | None = None,
    ) -> None:
        """Ensure the central `users` row exists for this guild."""
        gid = self._resolve_gid(guild_id)
        cur = await conn.execute("SELECT user_id FROM users WHERE guild_id=? AND user_id=?", (gid, user_id))
        row = await cur.fetchone()
        if row is None:
            await conn.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, user_id, self.starting_bankroll, self._now()),
            )

    async def _get_bankroll_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        user_id: int,
        guild_id: int | None = None,
    ) -> int:
        gid = self._resolve_gid(guild_id)
        await self._ensure_bj_user_conn(conn, user_id, gid)
        cur = await conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, user_id))
        row = await cur.fetchone()
        return int(row[0]) if row else self.starting_bankroll

    async def _set_bankroll_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        user_id: int,
        bankroll: int,
        guild_id: int | None = None,
    ) -> None:
        gid = self._resolve_gid(guild_id)
        await conn.execute(
            "UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (int(bankroll), gid, user_id)
        )

    async def _get_stock_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        symbol: str,
        guild_id: int | None = None,
    ) -> tuple[Any, ...] | None:
        gid = self._resolve_gid(guild_id)
        cur = await conn.execute(
            "SELECT symbol, name, price, category, is_active, updated_ts, week_key, week_open "
            "FROM fun_stocks WHERE guild_id=? AND symbol=?",
            (gid, symbol),
        )
        row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def _maybe_rollover_week_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        guild_id: int | None = None,
    ) -> None:
        gid = self._resolve_gid(guild_id)
        wk = self._week_key()
        cur = await conn.execute(
            "SELECT COUNT(1) FROM fun_stocks WHERE guild_id=? AND is_active=1 AND COALESCE(week_key,'')<>?",
            (gid, wk),
        )
        row = await cur.fetchone()
        count = int(row[0]) if row else 0
        if count <= 0:
            return
        now = self._now()
        await conn.execute(
            "UPDATE fun_stocks SET week_key=?, week_open=price, updated_ts=? WHERE guild_id=? AND is_active=1",
            (wk, now, gid),
        )

    async def _sync_catalog_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        guild_id: int | None = None,
    ) -> None:
        gid = self._resolve_gid(guild_id)
        cat = catalog_map()
        now = self._now()
        wk = self._week_key()

        # Legacy bridge: if $IC existed before fun-market rollout, seed initial IC from meta.stock_value
        legacy_ic_value: int | None = None
        try:
            cur = await conn.execute("SELECT value FROM meta WHERE key='stock_value'")
            row = await cur.fetchone()
            if row and row[0] is not None:
                legacy_ic_value = int(str(row[0]))
        except (aiosqlite.Error, TypeError, ValueError):
            legacy_ic_value = None

        for sym, item in cat.items():
            seed_price = (
                max(PRICE_FLOOR, int(legacy_ic_value))
                if (sym == "IC" and legacy_ic_value is not None and int(legacy_ic_value) > 0)
                else max(PRICE_FLOOR, int(item.start_price))
            )
            cur = await conn.execute(
                "SELECT symbol FROM fun_stocks WHERE guild_id=? AND symbol=?",
                (gid, sym),
            )
            exists = await cur.fetchone()
            if exists is None:
                await conn.execute(
                    """
                    INSERT INTO fun_stocks
                    (guild_id, symbol, name, price, category, is_active, created_ts, updated_ts, week_key, week_open)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid,
                        sym,
                        item.name,
                        seed_price,
                        item.category,
                        1 if item.active else 0,
                        now,
                        now,
                        wk,
                        seed_price,
                    ),
                )
            else:
                await conn.execute(
                    "UPDATE fun_stocks SET name=?, category=?, is_active=?, updated_ts=? WHERE guild_id=? AND symbol=?",
                    (item.name, item.category, 1 if item.active else 0, now, gid, sym),
                )

        # Symbols removed from code are marked inactive (history preserved)
        placeholders = ",".join(["?"] * len(cat)) if cat else ""
        if cat:
            await conn.execute(
                f"UPDATE fun_stocks SET is_active=0, updated_ts=? WHERE guild_id=? AND symbol NOT IN ({placeholders})",
                (now, gid, *list(cat.keys())),
            )
        else:
            await conn.execute("UPDATE fun_stocks SET is_active=0, updated_ts=? WHERE guild_id=?", (now, gid))

        # Keep legacy meta.stock_value aligned with IC quote
        try:
            cur = await conn.execute("SELECT price FROM fun_stocks WHERE guild_id=? AND symbol='IC'", (gid,))
            row = await cur.fetchone()
            if row and row[0] is not None:
                await conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('stock_value', ?)",
                    (str(int(row[0])),),
                )
        except (aiosqlite.Error, TypeError, ValueError) as exc:
            log.debug("Skipping legacy meta.stock_value sync: %s", exc)

        # Ensure week baselines are valid
        await conn.execute(
            "UPDATE fun_stocks SET week_key=COALESCE(NULLIF(week_key,''), ?), week_open=CASE WHEN week_open IS NULL OR week_open <= 0 THEN price ELSE week_open END WHERE guild_id=?",
            (wk, gid),
        )

        await self._maybe_rollover_week_conn(conn, gid)

    async def _mirror_legacy_ic_conn(
        self,
        conn: aiosqlite.Connection | Transaction,
        *,
        user_id: int,
        delta: int,
        new_value: int,
        reason: str | None,
        guild_id: int | None = None,
    ) -> None:
        gid = self._resolve_gid(guild_id)
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('stock_value', ?)",
            (str(int(new_value)),),
        )
        await conn.execute(
            "INSERT INTO stock_events (guild_id, ts_unix, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, self._now(), int(user_id), int(delta), int(new_value), reason),
        )

    # ---------------------------
    # Public helpers
    # ---------------------------
    async def sync_catalog(self, *, guild_id: int | None = None) -> None:
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)

    async def list_stocks(
        self,
        *,
        active_only: bool = True,
        include_inactive: bool = False,
        guild_id: int | None = None,
    ) -> list[StockRow]:
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            if active_only:
                sql = (
                    "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                    "FROM fun_stocks WHERE guild_id=? AND is_active=1 ORDER BY category, symbol"
                )
                params: tuple[Any, ...] = (gid,)
            elif include_inactive:
                sql = (
                    "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                    "FROM fun_stocks WHERE guild_id=? ORDER BY is_active DESC, category, symbol"
                )
                params = (gid,)
            else:
                sql = (
                    "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                    "FROM fun_stocks WHERE guild_id=? ORDER BY category, symbol"
                )
                params = (gid,)
            cur = await tx.execute(sql, params)
            rows = await cur.fetchall()
            out: list[StockRow] = []
            for r in rows:
                sym, name, price, cat, is_active, updated_ts, week_key, week_open = r
                price_i = int(price)
                week_open_i = max(PRICE_FLOOR, int(week_open or price_i))
                abs_chg = price_i - week_open_i
                pct = ((price_i - week_open_i) / week_open_i * 100.0) if week_open_i else 0.0
                out.append(
                    StockRow(
                        symbol=str(sym),
                        name=str(name),
                        price=price_i,
                        category=str(cat or "misc"),
                        is_active=bool(is_active),
                        updated_ts=int(updated_ts or 0),
                        week_key=str(week_key or ""),
                        week_open=week_open_i,
                        weekly_change_abs=abs_chg,
                        weekly_change_pct=float(pct),
                    )
                )
            return out

    async def get_stock(self, symbol: str, *, guild_id: int | None = None) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        gid = self._resolve_gid(guild_id)
        rows = await self.list_stocks(active_only=False, include_inactive=True, guild_id=gid)
        for s in rows:
            if s.symbol == sym:
                return s
        raise FunMarketError(f"Unknown symbol `{sym}`. Edit fun_market_catalog.py and sync the catalog.")

    async def history(
        self,
        *,
        symbol: str | None = None,
        limit: int = 10,
        guild_id: int | None = None,
    ) -> list[tuple[Any, ...]]:
        gid = self._resolve_gid(guild_id)
        limit = max(1, min(int(limit), 25))
        sym = normalize_symbol(symbol) if symbol else ""
        await self.sync_catalog(guild_id=gid)
        if sym:
            return await self.db.fetchall(
                "SELECT ts_unix, symbol, user_id, delta, new_value, reason FROM fun_stock_events "
                "WHERE guild_id=? AND symbol=? ORDER BY id DESC LIMIT ?",
                (gid, sym, limit),
            )
        return await self.db.fetchall(
            "SELECT ts_unix, symbol, user_id, delta, new_value, reason FROM fun_stock_events "
            "WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (gid, limit),
        )

    async def count_open_positions(self, user_id: int, *, guild_id: int | None = None) -> int:
        gid = self._resolve_gid(guild_id)
        await self.sync_catalog(guild_id=gid)
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(qty), 0) FROM fun_positions WHERE guild_id=? AND user_id=?",
            (gid, int(user_id)),
        )
        return int(row[0]) if row and row[0] is not None else 0

    # ---------------------------
    # Admin price actions
    # ---------------------------
    async def admin_move(
        self,
        *,
        symbol: str,
        delta: int,
        user_id: int,
        reason: str | None = None,
        guild_id: int | None = None,
    ) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        delta = int(delta)
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            row = await self._get_stock_conn(tx, sym, gid)
            if row is None:
                raise FunMarketError(f"Unknown symbol `{sym}`.")
            _, _name, old_price, _cat, is_active, _uts, _wk, _wo = row
            if not bool(is_active):
                raise FunMarketError(f"`{sym}` is inactive in the code catalog.")
            new_price = max(PRICE_FLOOR, int(old_price) + delta)
            actual_delta = new_price - int(old_price)
            now = self._now()
            await tx.execute(
                "UPDATE fun_stocks SET price=?, updated_ts=? WHERE guild_id=? AND symbol=?",
                (new_price, now, gid, sym),
            )
            await tx.execute(
                "INSERT INTO fun_stock_events (guild_id, ts_unix, symbol, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, now, sym, int(user_id), actual_delta, new_price, reason),
            )
            await tx.execute(
                """
                INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                VALUES (?, ?, 'stock_move', NULL, ?, ?)
                """,
                (gid, int(user_id), f'{{"symbol":"{sym}","delta":{actual_delta},"price":{new_price}}}', now),
            )
            if sym == "IC":
                await self._mirror_legacy_ic_conn(
                    tx,
                    user_id=int(user_id),
                    delta=actual_delta,
                    new_value=new_price,
                    reason=reason,
                    guild_id=gid,
                )
        return await self.get_stock(sym, guild_id=gid)

    async def admin_set(
        self,
        *,
        symbol: str,
        new_value: int,
        user_id: int,
        reason: str | None = None,
        guild_id: int | None = None,
    ) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        new_value = max(PRICE_FLOOR, int(new_value))
        gid = self._resolve_gid(guild_id)
        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            row = await self._get_stock_conn(tx, sym, gid)
            if row is None:
                raise FunMarketError(f"Unknown symbol `{sym}`.")
            _, _name, old_price, _cat, is_active, _uts, _wk, _wo = row
            if not bool(is_active):
                raise FunMarketError(f"`{sym}` is inactive in the code catalog.")
            old_price_i = int(old_price)
            delta = new_value - old_price_i
            now = self._now()
            await tx.execute(
                "UPDATE fun_stocks SET price=?, updated_ts=? WHERE guild_id=? AND symbol=?",
                (new_value, now, gid, sym),
            )
            await tx.execute(
                "INSERT INTO fun_stock_events (guild_id, ts_unix, symbol, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, now, sym, int(user_id), delta, new_value, reason),
            )
            await tx.execute(
                """
                INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                VALUES (?, ?, 'stock_set', NULL, ?, ?)
                """,
                (gid, int(user_id), f'{{"symbol":"{sym}","delta":{delta},"price":{new_value}}}', now),
            )
            if sym == "IC":
                await self._mirror_legacy_ic_conn(
                    tx, user_id=int(user_id), delta=delta, new_value=new_value, reason=reason, guild_id=gid
                )
        return await self.get_stock(sym, guild_id=gid)

    async def week_rollover(
        self,
        *,
        user_id: int,
        note: str | None = None,
        guild_id: int | None = None,
    ) -> str:
        gid = self._resolve_gid(guild_id)
        wk = self._week_key()
        now = self._now()
        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            await tx.execute(
                "UPDATE fun_stocks SET week_key=?, week_open=price, updated_ts=? WHERE guild_id=? AND is_active=1",
                (wk, now, gid),
            )
            await tx.execute(
                "INSERT OR REPLACE INTO fun_market_weeks (guild_id, week_key, settled_at, settled_by, notes) VALUES (?, ?, ?, ?, ?)",
                (gid, wk, now, int(user_id), note),
            )
        return wk

    # ---------------------------
    # Trading (Atomic conditional SQL & transaction ledger coupled)
    # ---------------------------
    async def buy_long(
        self,
        *,
        user_id: int,
        symbol: str,
        qty: int,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[StockRow, PositionRow, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        gid = self._resolve_gid(guild_id)
        uid = int(user_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            row = await self._get_stock_conn(tx, sym, gid)
            if row is None or not bool(row[4]):
                raise FunMarketError(f"Unknown/ inactive symbol `{sym}`.")
            price = int(row[2])
            cost = price * qty

            await self._ensure_bj_user_conn(tx, uid, gid)

            # Atomic conditional SQL: deduct cost only if balance is sufficient
            cur_upd = await tx.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (cost, gid, uid, cost),
            )
            if cur_upd.rowcount == 0:
                cur_b = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                b_row = await cur_b.fetchone()
                bankroll = int(b_row[0]) if b_row is not None else self.starting_bankroll
                raise FunMarketError(f"Insufficient bankroll. Need ${cost}, have ${bankroll}.")

            cur_b2 = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            b2_row = await cur_b2.fetchone()
            new_bankroll = int(b2_row[0]) if b2_row is not None else 0

            cur = await tx.execute(
                "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='LONG'",
                (gid, uid, sym),
            )
            prow = await cur.fetchone()
            if prow is None:
                new_qty = qty
                new_avg = float(price)
                await tx.execute(
                    "INSERT INTO fun_positions (guild_id, user_id, symbol, side, qty, avg_price, collateral_locked) VALUES (?, ?, ?, 'LONG', ?, ?, 0)",
                    (gid, uid, sym, new_qty, new_avg),
                )
            else:
                old_qty = int(prow[0])
                old_avg = float(prow[1])
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_avg) + (qty * price)) / max(1, new_qty)
                await tx.execute(
                    "UPDATE fun_positions SET qty=?, avg_price=? WHERE guild_id=? AND user_id=? AND symbol=? AND side='LONG'",
                    (new_qty, float(new_avg), gid, uid, sym),
                )

            await tx.execute(
                "INSERT INTO fun_trades (guild_id, ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, 0, NULL)",
                (gid, now, uid, sym, qty, price, -cost),
            )
            tx_id = (
                str(idempotency_key)
                if idempotency_key
                else f"buy:{gid}:{uid}:{sym}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'market_buy', ?, ?)
                """,
                (tx_id, gid, uid, -cost, new_bankroll, sym, now),
            )

        stock = await self.get_stock(sym, guild_id=gid)
        pos = await self._get_position(user_id=uid, symbol=sym, side="LONG", guild_id=gid)
        if pos is None:
            raise FunMarketError(f"Failed to load LONG position for `{sym}`.")
        return stock, pos, new_bankroll

    async def sell_long(
        self,
        *,
        user_id: int,
        symbol: str,
        qty: int,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[StockRow, PositionRow | None, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        gid = self._resolve_gid(guild_id)
        uid = int(user_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            srow = await self._get_stock_conn(tx, sym, gid)
            if srow is None:
                raise FunMarketError(f"Unknown symbol `{sym}`.")
            price = int(srow[2])
            cur = await tx.execute(
                "SELECT qty, avg_price FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='LONG'",
                (gid, uid, sym),
            )
            prow = await cur.fetchone()
            if prow is None:
                raise FunMarketError(f"No long position in `{sym}`.")
            old_qty = int(prow[0])
            avg = float(prow[1])
            if qty > old_qty:
                raise FunMarketError(f"You only have {old_qty} shares long in `{sym}`.")

            # Atomic conditional update of shares
            cur_pos = await tx.execute(
                "UPDATE fun_positions SET qty = qty - ? WHERE guild_id=? AND user_id=? AND symbol=? AND side='LONG' AND qty >= ?",
                (qty, gid, uid, sym, qty),
            )
            if cur_pos.rowcount == 0:
                raise FunMarketError(f"Insufficient long shares in `{sym}`.")

            realized = round((price - avg) * qty)
            proceeds = price * qty

            await tx.execute(
                "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                (proceeds, gid, uid),
            )
            cur_b = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            b_row = await cur_b.fetchone()
            new_bankroll = int(b_row[0]) if b_row is not None else 0

            new_qty = old_qty - qty
            if new_qty <= 0:
                await tx.execute(
                    "DELETE FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='LONG'",
                    (gid, uid, sym),
                )

            await tx.execute(
                "INSERT INTO fun_trades (guild_id, ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, NULL)",
                (gid, now, uid, sym, qty, price, proceeds, realized),
            )
            tx_id = (
                str(idempotency_key)
                if idempotency_key
                else f"sell:{gid}:{uid}:{sym}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'market_sell', ?, ?)
                """,
                (tx_id, gid, uid, proceeds, new_bankroll, sym, now),
            )

        stock = await self.get_stock(sym, guild_id=gid)
        pos = await self._get_position(user_id=uid, symbol=sym, side="LONG", guild_id=gid)
        return stock, pos, new_bankroll, realized

    async def open_short(
        self,
        *,
        user_id: int,
        symbol: str,
        qty: int,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[StockRow, PositionRow, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        gid = self._resolve_gid(guild_id)
        uid = int(user_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            srow = await self._get_stock_conn(tx, sym, gid)
            if srow is None or not bool(srow[4]):
                raise FunMarketError(f"Unknown/ inactive symbol `{sym}`.")
            price = int(srow[2])
            notional = price * qty
            collateral = math.ceil(notional * SHORT_COLLATERAL_RATIO)

            await self._ensure_bj_user_conn(tx, uid, gid)

            # Atomic conditional deduction of collateral
            cur_upd = await tx.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (collateral, gid, uid, collateral),
            )
            if cur_upd.rowcount == 0:
                cur_b = await tx.execute(
                    "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
                )
                b_row = await cur_b.fetchone()
                bankroll = int(b_row[0]) if b_row is not None else self.starting_bankroll
                raise FunMarketError(
                    f"Insufficient bankroll for short collateral. Need ${collateral} ({int(SHORT_COLLATERAL_RATIO * 100)}%), have ${bankroll}."
                )

            cur_b2 = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            b2_row = await cur_b2.fetchone()
            new_bankroll = int(b2_row[0]) if b2_row is not None else 0

            cur = await tx.execute(
                "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='SHORT'",
                (gid, uid, sym),
            )
            prow = await cur.fetchone()
            if prow is None:
                new_qty = qty
                new_avg = float(price)
                new_collateral = collateral
                await tx.execute(
                    "INSERT INTO fun_positions (guild_id, user_id, symbol, side, qty, avg_price, collateral_locked) VALUES (?, ?, ?, 'SHORT', ?, ?, ?)",
                    (gid, uid, sym, new_qty, new_avg, new_collateral),
                )
            else:
                old_qty = int(prow[0])
                old_avg = float(prow[1])
                old_collateral = int(prow[2] or 0)
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_avg) + (qty * price)) / max(1, new_qty)
                new_collateral = old_collateral + collateral
                await tx.execute(
                    "UPDATE fun_positions SET qty=?, avg_price=?, collateral_locked=? WHERE guild_id=? AND user_id=? AND symbol=? AND side='SHORT'",
                    (new_qty, float(new_avg), new_collateral, gid, uid, sym),
                )

            await tx.execute(
                "INSERT INTO fun_trades (guild_id, ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, ?, 'SHORT', ?, ?, ?, 0, ?)",
                (
                    gid,
                    now,
                    uid,
                    sym,
                    qty,
                    price,
                    -collateral,
                    f"Collateral {int(SHORT_COLLATERAL_RATIO * 100)}%",
                ),
            )
            tx_id = (
                str(idempotency_key)
                if idempotency_key
                else f"short:{gid}:{uid}:{sym}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'short_open', ?, ?)
                """,
                (tx_id, gid, uid, -collateral, new_bankroll, sym, now),
            )

        stock = await self.get_stock(sym, guild_id=gid)
        pos = await self._get_position(user_id=uid, symbol=sym, side="SHORT", guild_id=gid)
        if pos is None:
            raise FunMarketError(f"Failed to load SHORT position for `{sym}`.")
        return stock, pos, new_bankroll, collateral

    async def cover_short(
        self,
        *,
        user_id: int,
        symbol: str,
        qty: int,
        guild_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[StockRow, PositionRow | None, int, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        gid = self._resolve_gid(guild_id)
        uid = int(user_id)
        now = self._now()

        async with self.db.write_transaction() as tx:
            await self._sync_catalog_conn(tx, gid)
            srow = await self._get_stock_conn(tx, sym, gid)
            if srow is None:
                raise FunMarketError(f"Unknown symbol `{sym}`.")
            price = int(srow[2])

            cur = await tx.execute(
                "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='SHORT'",
                (gid, uid, sym),
            )
            prow = await cur.fetchone()
            if prow is None:
                raise FunMarketError(f"No short position in `{sym}`.")

            old_qty = int(prow[0])
            avg = float(prow[1])
            old_collateral = int(prow[2] or 0)
            if qty > old_qty:
                raise FunMarketError(f"You only have {old_qty} shares short in `{sym}`.")

            # Proportional collateral release; exact close releases all remainder
            if qty == old_qty:
                release = old_collateral
            else:
                release = round(old_collateral * (qty / max(1, old_qty)))
                release = max(0, min(release, old_collateral))

            realized = round((avg - price) * qty)
            cash_delta = int(release + realized)

            # Update users balance (may go negative on extreme short losses per legacy invariant)
            await tx.execute(
                "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                (cash_delta, gid, uid),
            )
            cur_b = await tx.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            b_row = await cur_b.fetchone()
            new_bankroll = int(b_row[0]) if b_row is not None else 0

            new_qty = old_qty - qty
            new_collateral = old_collateral - release
            if new_qty <= 0:
                await tx.execute(
                    "DELETE FROM fun_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side='SHORT'",
                    (gid, uid, sym),
                )
            else:
                await tx.execute(
                    "UPDATE fun_positions SET qty=?, collateral_locked=? WHERE guild_id=? AND user_id=? AND symbol=? AND side='SHORT'",
                    (new_qty, max(0, new_collateral), gid, uid, sym),
                )

            await tx.execute(
                "INSERT INTO fun_trades (guild_id, ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, ?, 'COVER', ?, ?, ?, ?, NULL)",
                (gid, now, uid, sym, qty, price, cash_delta, realized),
            )
            tx_id = (
                str(idempotency_key)
                if idempotency_key
                else f"cover:{gid}:{uid}:{sym}:{now}:{time.monotonic_ns()}"
            )
            await tx.execute(
                """
                INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                VALUES (?, ?, ?, ?, ?, 'short_cover', ?, ?)
                """,
                (tx_id, gid, uid, cash_delta, new_bankroll, sym, now),
            )

        stock = await self.get_stock(sym, guild_id=gid)
        pos = await self._get_position(user_id=uid, symbol=sym, side="SHORT", guild_id=gid)
        return stock, pos, new_bankroll, realized, release

    async def _get_position(
        self,
        *,
        user_id: int,
        symbol: str,
        side: str,
        guild_id: int | None = None,
    ) -> PositionRow | None:
        gid = self._resolve_gid(guild_id)
        snap = await self.portfolio(user_id=user_id, guild_id=gid)
        for p in snap.positions:
            if p.symbol == normalize_symbol(symbol) and p.side == side.upper():
                return p
        return None

    async def portfolio(
        self,
        *,
        user_id: int,
        guild_id: int | None = None,
    ) -> PortfolioSnapshot:
        gid = self._resolve_gid(guild_id)
        uid = int(user_id)
        await self.sync_catalog(guild_id=gid)

        cash_row = await self.db.fetchone(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
            (gid, uid),
        )
        if cash_row is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, self.starting_bankroll, self._now()),
            )
            cash = self.starting_bankroll
        else:
            cash = int(cash_row[0]) if cash_row[0] is not None else self.starting_bankroll

        rows = await self.db.fetchall(
            """
            SELECT p.user_id, p.symbol, p.side, p.qty, p.avg_price, COALESCE(p.collateral_locked,0),
                   COALESCE(s.price, 0)
            FROM fun_positions p
            LEFT JOIN fun_stocks s ON s.symbol = p.symbol AND s.guild_id = p.guild_id
            WHERE p.guild_id=? AND p.user_id=? AND p.qty > 0
            ORDER BY p.side, p.symbol
            """,
            (gid, uid),
        )
        positions: list[PositionRow] = []
        long_value = 0
        short_equity = 0
        unreal = 0
        for p_uid, sym, side, qty, avg, coll, mkt in rows:
            qty_i = int(qty)
            avg_f = float(avg)
            coll_i = int(coll or 0)
            mkt_i = int(mkt or 0)
            market_value = qty_i * mkt_i
            side_s = str(side).upper()
            if side_s == "LONG":
                pnl = round((mkt_i - avg_f) * qty_i)
                equity = market_value
                long_value += market_value
            else:
                pnl = round((avg_f - mkt_i) * qty_i)
                equity = coll_i + pnl
                short_equity += equity
            unreal += pnl
            positions.append(
                PositionRow(
                    user_id=int(p_uid),
                    symbol=str(sym),
                    side=side_s,
                    qty=qty_i,
                    avg_price=avg_f,
                    collateral_locked=coll_i,
                    market_price=mkt_i,
                    market_value=market_value,
                    unrealized_pnl=pnl,
                    equity_value=equity,
                )
            )

        net_worth = int(cash + long_value + short_equity)
        return PortfolioSnapshot(
            user_id=uid,
            cash=int(cash),
            positions=positions,
            net_worth=net_worth,
            long_value=int(long_value),
            short_equity=int(short_equity),
            unrealized_pnl=int(unreal),
        )

    async def leaderboard_by_networth(
        self,
        *,
        limit: int = 10,
        guild_id: int | None = None,
    ) -> list[PortfolioSnapshot]:
        gid = self._resolve_gid(guild_id)
        limit = max(1, min(int(limit), 25))
        await self.sync_catalog(guild_id=gid)
        user_rows = await self.db.fetchall(
            """
            SELECT DISTINCT user_id FROM users WHERE guild_id=?
            UNION
            SELECT DISTINCT user_id FROM fun_positions WHERE guild_id=?
            """,
            (gid, gid),
        )
        user_ids = [int(r[0]) for r in user_rows if r and r[0] is not None]
        snaps: list[PortfolioSnapshot] = []
        for uid in user_ids:
            snaps.append(await self.portfolio(user_id=uid, guild_id=gid))
        snaps.sort(key=lambda s: s.net_worth, reverse=True)
        return snaps[:limit]

    async def recent_trades(
        self,
        *,
        user_id: int | None = None,
        limit: int = 15,
        guild_id: int | None = None,
    ) -> list[tuple[Any, ...]]:
        gid = self._resolve_gid(guild_id)
        limit = max(1, min(int(limit), 25))
        if user_id is None:
            return await self.db.fetchall(
                "SELECT ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (gid, limit),
            )
        return await self.db.fetchall(
            "SELECT ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (gid, int(user_id), limit),
        )


def format_signed_money(x: int) -> str:
    sign = "+" if int(x) >= 0 else ""
    return f"{sign}${int(x)}"
