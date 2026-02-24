from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

from icbot.services.fun_market_catalog import catalog_map, normalize_symbol


SHORT_COLLATERAL_RATIO = 1.5
PRICE_FLOOR = 1


class FunMarketError(RuntimeError):
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
    def __init__(self, db, *, starting_bankroll: int = 1000, tz_name: str = "America/Toronto"):
        self.db = db
        self.starting_bankroll = int(starting_bankroll or 1000)
        self.tz_name = str(tz_name or "America/Toronto")

    # ---------------------------
    # Time / symbols
    # ---------------------------
    def _now(self) -> int:
        return int(time.time())

    def _week_key(self) -> str:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            dt = datetime.now(ZoneInfo(self.tz_name))
            iso = dt.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        except Exception:
            from datetime import datetime
            dt = datetime.utcnow()
            iso = dt.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"

    # ---------------------------
    # Internal low-level helpers
    # ---------------------------
    async def _ensure_bj_user_conn(self, conn: aiosqlite.Connection, user_id: int) -> None:
        cur = await conn.execute("SELECT user_id FROM blackjack_users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO blackjack_users (user_id, bankroll) VALUES (?, ?)",
                (user_id, self.starting_bankroll),
            )

    async def _get_bankroll_conn(self, conn: aiosqlite.Connection, user_id: int) -> int:
        await self._ensure_bj_user_conn(conn, user_id)
        cur = await conn.execute("SELECT bankroll FROM blackjack_users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else self.starting_bankroll

    async def _set_bankroll_conn(self, conn: aiosqlite.Connection, user_id: int, bankroll: int) -> None:
        await conn.execute("UPDATE blackjack_users SET bankroll=? WHERE user_id=?", (int(bankroll), user_id))

    async def _get_stock_conn(self, conn: aiosqlite.Connection, symbol: str) -> Optional[tuple]:
        cur = await conn.execute(
            "SELECT symbol, name, price, category, is_active, updated_ts, week_key, week_open "
            "FROM fun_stocks WHERE symbol=?",
            (symbol,),
        )
        return await cur.fetchone()

    async def _maybe_rollover_week_conn(self, conn: aiosqlite.Connection) -> None:
        wk = self._week_key()
        cur = await conn.execute(
            "SELECT COUNT(1) FROM fun_stocks WHERE is_active=1 AND COALESCE(week_key,'')<>?",
            (wk,),
        )
        row = await cur.fetchone()
        count = int(row[0]) if row else 0
        if count <= 0:
            return
        now = self._now()
        await conn.execute(
            "UPDATE fun_stocks SET week_key=?, week_open=price, updated_ts=? WHERE is_active=1",
            (wk, now),
        )

    async def _sync_catalog_conn(self, conn: aiosqlite.Connection) -> None:
        cat = catalog_map()
        now = self._now()
        wk = self._week_key()

        # Legacy bridge: if $IC existed before fun-market rollout, seed initial IC from meta.stock_value.
        legacy_ic_value: int | None = None
        try:
            cur = await conn.execute("SELECT value FROM meta WHERE key='stock_value'")
            row = await cur.fetchone()
            if row and row[0] is not None:
                legacy_ic_value = int(str(row[0]))
        except Exception:
            legacy_ic_value = None

        for sym, item in cat.items():
            seed_price = (
                max(PRICE_FLOOR, int(legacy_ic_value))
                if (sym == "IC" and legacy_ic_value is not None and int(legacy_ic_value) > 0)
                else max(PRICE_FLOOR, int(item.start_price))
            )
            cur = await conn.execute(
                "SELECT symbol FROM fun_stocks WHERE symbol=?",
                (sym,),
            )
            exists = await cur.fetchone()
            if exists is None:
                await conn.execute(
                    """
                    INSERT INTO fun_stocks
                    (symbol, name, price, category, is_active, created_ts, updated_ts, week_key, week_open)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
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
                    "UPDATE fun_stocks SET name=?, category=?, is_active=?, updated_ts=? WHERE symbol=?",
                    (item.name, item.category, 1 if item.active else 0, now, sym),
                )

        # Symbols removed from code are marked inactive (history preserved)
        placeholders = ",".join(["?"] * len(cat)) if cat else ""
        if cat:
            await conn.execute(
                f"UPDATE fun_stocks SET is_active=0, updated_ts=? WHERE symbol NOT IN ({placeholders})",
                (now, *list(cat.keys())),
            )
        else:
            await conn.execute("UPDATE fun_stocks SET is_active=0, updated_ts=?", (now,))

        # Keep legacy meta.stock_value aligned with IC quote (no event history entry on sync).
        try:
            cur = await conn.execute("SELECT price FROM fun_stocks WHERE symbol='IC'")
            row = await cur.fetchone()
            if row and row[0] is not None:
                await conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('stock_value', ?)",
                    (str(int(row[0])),),
                )
        except Exception:
            pass

        # Ensure week baselines are valid
        await conn.execute(
            "UPDATE fun_stocks SET week_key=COALESCE(NULLIF(week_key,''), ?), week_open=CASE WHEN week_open IS NULL OR week_open <= 0 THEN price ELSE week_open END",
            (wk,),
        )

        await self._maybe_rollover_week_conn(conn)

    async def _mirror_legacy_ic_conn(
        self,
        conn: aiosqlite.Connection,
        *,
        user_id: int,
        delta: int,
        new_value: int,
        reason: str | None,
    ) -> None:
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('stock_value', ?)",
            (str(int(new_value)),),
        )
        await conn.execute(
            "INSERT INTO stock_events (ts_unix, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?)",
            (self._now(), int(user_id), int(delta), int(new_value), reason),
        )

    # ---------------------------
    # Public helpers
    # ---------------------------
    async def sync_catalog(self) -> None:
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                await conn.commit()

    async def list_stocks(self, *, active_only: bool = True, include_inactive: bool = False) -> list[StockRow]:
        # include_inactive kept for future convenience; active_only wins if True
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                if active_only:
                    sql = (
                        "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                        "FROM fun_stocks WHERE is_active=1 ORDER BY category, symbol"
                    )
                    params: tuple[Any, ...] = ()
                elif include_inactive:
                    sql = (
                        "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                        "FROM fun_stocks ORDER BY is_active DESC, category, symbol"
                    )
                    params = ()
                else:
                    sql = (
                        "SELECT symbol, name, price, category, is_active, updated_ts, COALESCE(week_key,''), COALESCE(week_open, price) "
                        "FROM fun_stocks ORDER BY category, symbol"
                    )
                    params = ()
                cur = await conn.execute(sql, params)
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

    async def get_stock(self, symbol: str) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        rows = await self.list_stocks(active_only=False, include_inactive=True)
        for s in rows:
            if s.symbol == sym:
                return s
        raise FunMarketError(f"Unknown symbol `{sym}`. Edit fun_market_catalog.py and sync the catalog.")

    async def history(self, *, symbol: str | None = None, limit: int = 10) -> list[tuple]:
        limit = max(1, min(int(limit), 25))
        sym = normalize_symbol(symbol) if symbol else ""
        await self.sync_catalog()
        if sym:
            return await self.db.fetchall(
                "SELECT ts_unix, symbol, user_id, delta, new_value, reason FROM fun_stock_events "
                "WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (sym, limit),
            )
        return await self.db.fetchall(
            "SELECT ts_unix, symbol, user_id, delta, new_value, reason FROM fun_stock_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def count_open_positions(self, user_id: int) -> int:
        await self.sync_catalog()
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(qty), 0) FROM fun_positions WHERE user_id=?",
            (int(user_id),),
        )
        return int(row[0]) if row and row[0] is not None else 0

    # ---------------------------
    # Admin price actions
    # ---------------------------
    async def admin_move(self, *, symbol: str, delta: int, user_id: int, reason: str | None = None) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        delta = int(delta)
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                row = await self._get_stock_conn(conn, sym)
                if row is None:
                    raise FunMarketError(f"Unknown symbol `{sym}`.")
                _, _name, old_price, _cat, is_active, _uts, _wk, _wo = row
                if not bool(is_active):
                    raise FunMarketError(f"`{sym}` is inactive in the code catalog.")
                new_price = max(PRICE_FLOOR, int(old_price) + delta)
                actual_delta = new_price - int(old_price)
                now = self._now()
                await conn.execute(
                    "UPDATE fun_stocks SET price=?, updated_ts=? WHERE symbol=?",
                    (new_price, now, sym),
                )
                await conn.execute(
                    "INSERT INTO fun_stock_events (ts_unix, symbol, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (now, sym, int(user_id), actual_delta, new_price, reason),
                )
                if sym == "IC":
                    await self._mirror_legacy_ic_conn(conn, user_id=int(user_id), delta=actual_delta, new_value=new_price, reason=reason)
                await conn.commit()
        return await self.get_stock(sym)

    async def admin_set(self, *, symbol: str, new_value: int, user_id: int, reason: str | None = None) -> StockRow:
        sym = normalize_symbol(symbol)
        if not sym:
            raise FunMarketError("Invalid symbol.")
        new_value = max(PRICE_FLOOR, int(new_value))
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                row = await self._get_stock_conn(conn, sym)
                if row is None:
                    raise FunMarketError(f"Unknown symbol `{sym}`.")
                _, _name, old_price, _cat, is_active, _uts, _wk, _wo = row
                if not bool(is_active):
                    raise FunMarketError(f"`{sym}` is inactive in the code catalog.")
                old_price_i = int(old_price)
                delta = new_value - old_price_i
                now = self._now()
                await conn.execute(
                    "UPDATE fun_stocks SET price=?, updated_ts=? WHERE symbol=?",
                    (new_value, now, sym),
                )
                await conn.execute(
                    "INSERT INTO fun_stock_events (ts_unix, symbol, user_id, delta, new_value, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (now, sym, int(user_id), delta, new_value, reason),
                )
                if sym == "IC":
                    await self._mirror_legacy_ic_conn(conn, user_id=int(user_id), delta=delta, new_value=new_value, reason=reason)
                await conn.commit()
        return await self.get_stock(sym)

    async def week_rollover(self, *, user_id: int, note: str | None = None) -> str:
        wk = self._week_key()
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                await conn.execute(
                    "UPDATE fun_stocks SET week_key=?, week_open=price, updated_ts=? WHERE is_active=1",
                    (wk, self._now()),
                )
                await conn.execute(
                    "INSERT OR REPLACE INTO fun_market_weeks (week_key, settled_at, settled_by, notes) VALUES (?, ?, ?, ?)",
                    (wk, self._now(), int(user_id), note),
                )
                await conn.commit()
        return wk

    # ---------------------------
    # Trading
    # ---------------------------
    async def buy_long(self, *, user_id: int, symbol: str, qty: int) -> tuple[StockRow, PositionRow, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                row = await self._get_stock_conn(conn, sym)
                if row is None or not bool(row[4]):
                    raise FunMarketError(f"Unknown/ inactive symbol `{sym}`.")
                price = int(row[2])
                cost = price * qty
                bankroll = await self._get_bankroll_conn(conn, user_id)
                if bankroll < cost:
                    raise FunMarketError(f"Insufficient bankroll. Need ${cost}, have ${bankroll}.")

                cur = await conn.execute(
                    "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE user_id=? AND symbol=? AND side='LONG'",
                    (int(user_id), sym),
                )
                prow = await cur.fetchone()
                if prow is None:
                    new_qty = qty
                    new_avg = float(price)
                    await conn.execute(
                        "INSERT INTO fun_positions (user_id, symbol, side, qty, avg_price, collateral_locked) VALUES (?, ?, 'LONG', ?, ?, 0)",
                        (int(user_id), sym, new_qty, new_avg),
                    )
                else:
                    old_qty = int(prow[0])
                    old_avg = float(prow[1])
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + (qty * price)) / max(1, new_qty)
                    await conn.execute(
                        "UPDATE fun_positions SET qty=?, avg_price=? WHERE user_id=? AND symbol=? AND side='LONG'",
                        (new_qty, float(new_avg), int(user_id), sym),
                    )

                new_bankroll = bankroll - cost
                await self._set_bankroll_conn(conn, user_id, new_bankroll)
                await conn.execute(
                    "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, 'BUY', ?, ?, ?, 0, NULL)",
                    (self._now(), int(user_id), sym, qty, price, -cost),
                )
                await conn.commit()

        stock = await self.get_stock(sym)
        pos = await self._get_position(user_id=user_id, symbol=sym, side="LONG")
        return stock, pos, new_bankroll

    async def sell_long(self, *, user_id: int, symbol: str, qty: int) -> tuple[StockRow, PositionRow | None, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                srow = await self._get_stock_conn(conn, sym)
                if srow is None:
                    raise FunMarketError(f"Unknown symbol `{sym}`.")
                price = int(srow[2])
                cur = await conn.execute(
                    "SELECT qty, avg_price FROM fun_positions WHERE user_id=? AND symbol=? AND side='LONG'",
                    (int(user_id), sym),
                )
                prow = await cur.fetchone()
                if prow is None:
                    raise FunMarketError(f"No long position in `{sym}`.")
                old_qty = int(prow[0])
                avg = float(prow[1])
                if qty > old_qty:
                    raise FunMarketError(f"You only have {old_qty} shares long in `{sym}`.")
                realized = int(round((price - avg) * qty))
                proceeds = price * qty
                bankroll = await self._get_bankroll_conn(conn, user_id)
                new_bankroll = bankroll + proceeds
                await self._set_bankroll_conn(conn, user_id, new_bankroll)

                new_qty = old_qty - qty
                if new_qty <= 0:
                    await conn.execute(
                        "DELETE FROM fun_positions WHERE user_id=? AND symbol=? AND side='LONG'",
                        (int(user_id), sym),
                    )
                else:
                    await conn.execute(
                        "UPDATE fun_positions SET qty=? WHERE user_id=? AND symbol=? AND side='LONG'",
                        (new_qty, int(user_id), sym),
                    )
                await conn.execute(
                    "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, NULL)",
                    (self._now(), int(user_id), sym, qty, price, proceeds, realized),
                )
                await conn.commit()

        stock = await self.get_stock(sym)
        pos = await self._get_position(user_id=user_id, symbol=sym, side="LONG")
        return stock, pos, new_bankroll, realized

    async def open_short(self, *, user_id: int, symbol: str, qty: int) -> tuple[StockRow, PositionRow, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                srow = await self._get_stock_conn(conn, sym)
                if srow is None or not bool(srow[4]):
                    raise FunMarketError(f"Unknown/ inactive symbol `{sym}`.")
                price = int(srow[2])
                notional = price * qty
                collateral = int(math.ceil(notional * SHORT_COLLATERAL_RATIO))
                bankroll = await self._get_bankroll_conn(conn, user_id)
                if bankroll < collateral:
                    raise FunMarketError(
                        f"Insufficient bankroll for short collateral. Need ${collateral} ({int(SHORT_COLLATERAL_RATIO*100)}%), have ${bankroll}."
                    )

                cur = await conn.execute(
                    "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE user_id=? AND symbol=? AND side='SHORT'",
                    (int(user_id), sym),
                )
                prow = await cur.fetchone()
                if prow is None:
                    new_qty = qty
                    new_avg = float(price)
                    new_collateral = collateral
                    await conn.execute(
                        "INSERT INTO fun_positions (user_id, symbol, side, qty, avg_price, collateral_locked) VALUES (?, ?, 'SHORT', ?, ?, ?)",
                        (int(user_id), sym, new_qty, new_avg, new_collateral),
                    )
                else:
                    old_qty = int(prow[0])
                    old_avg = float(prow[1])
                    old_collateral = int(prow[2] or 0)
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + (qty * price)) / max(1, new_qty)
                    new_collateral = old_collateral + collateral
                    await conn.execute(
                        "UPDATE fun_positions SET qty=?, avg_price=?, collateral_locked=? WHERE user_id=? AND symbol=? AND side='SHORT'",
                        (new_qty, float(new_avg), new_collateral, int(user_id), sym),
                    )

                new_bankroll = bankroll - collateral
                await self._set_bankroll_conn(conn, user_id, new_bankroll)
                await conn.execute(
                    "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, 'SHORT', ?, ?, ?, 0, ?)",
                    (self._now(), int(user_id), sym, qty, price, -collateral, f"Collateral {int(SHORT_COLLATERAL_RATIO*100)}%"),
                )
                await conn.commit()

        stock = await self.get_stock(sym)
        pos = await self._get_position(user_id=user_id, symbol=sym, side="SHORT")
        return stock, pos, new_bankroll, collateral

    async def cover_short(self, *, user_id: int, symbol: str, qty: int) -> tuple[StockRow, PositionRow | None, int, int, int]:
        sym = normalize_symbol(symbol)
        qty = int(qty)
        if qty <= 0:
            raise FunMarketError("Quantity must be at least 1.")
        async with self.db._lock:  # type: ignore[attr-defined]
            async with aiosqlite.connect(self.db.path) as conn:  # type: ignore[attr-defined]
                await self._sync_catalog_conn(conn)
                srow = await self._get_stock_conn(conn, sym)
                if srow is None:
                    raise FunMarketError(f"Unknown symbol `{sym}`.")
                price = int(srow[2])

                cur = await conn.execute(
                    "SELECT qty, avg_price, collateral_locked FROM fun_positions WHERE user_id=? AND symbol=? AND side='SHORT'",
                    (int(user_id), sym),
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
                    release = int(round(old_collateral * (qty / max(1, old_qty))))
                    release = max(0, min(release, old_collateral))

                realized = int(round((avg - price) * qty))
                cash_delta = int(release + realized)
                bankroll = await self._get_bankroll_conn(conn, user_id)
                new_bankroll = bankroll + cash_delta  # may go negative on extreme losses (intentional debt behavior)
                await self._set_bankroll_conn(conn, user_id, new_bankroll)

                new_qty = old_qty - qty
                new_collateral = old_collateral - release
                if new_qty <= 0:
                    await conn.execute(
                        "DELETE FROM fun_positions WHERE user_id=? AND symbol=? AND side='SHORT'",
                        (int(user_id), sym),
                    )
                else:
                    await conn.execute(
                        "UPDATE fun_positions SET qty=?, collateral_locked=? WHERE user_id=? AND symbol=? AND side='SHORT'",
                        (new_qty, max(0, new_collateral), int(user_id), sym),
                    )
                await conn.execute(
                    "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) VALUES (?, ?, ?, 'COVER', ?, ?, ?, ?, NULL)",
                    (self._now(), int(user_id), sym, qty, price, cash_delta, realized),
                )
                await conn.commit()

        stock = await self.get_stock(sym)
        pos = await self._get_position(user_id=user_id, symbol=sym, side="SHORT")
        return stock, pos, new_bankroll, realized, release

    async def _get_position(self, *, user_id: int, symbol: str, side: str) -> PositionRow | None:
        snap = await self.portfolio(user_id=user_id)
        for p in snap.positions:
            if p.symbol == normalize_symbol(symbol) and p.side == side.upper():
                return p
        return None

    async def portfolio(self, *, user_id: int) -> PortfolioSnapshot:
        await self.sync_catalog()
        cash_row = await self.db.fetchone(
            "SELECT bankroll FROM blackjack_users WHERE user_id=?",
            (int(user_id),),
        )
        cash = int(cash_row[0]) if cash_row and cash_row[0] is not None else self.starting_bankroll
        # ensure user row exists lazily for consistency
        if cash_row is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO blackjack_users (user_id, bankroll) VALUES (?, ?)",
                (int(user_id), self.starting_bankroll),
            )
            cash = self.starting_bankroll

        rows = await self.db.fetchall(
            """
            SELECT p.user_id, p.symbol, p.side, p.qty, p.avg_price, COALESCE(p.collateral_locked,0),
                   COALESCE(s.price, 0)
            FROM fun_positions p
            LEFT JOIN fun_stocks s ON s.symbol = p.symbol
            WHERE p.user_id=? AND p.qty > 0
            ORDER BY p.side, p.symbol
            """,
            (int(user_id),),
        )
        positions: list[PositionRow] = []
        long_value = 0
        short_equity = 0
        unreal = 0
        for uid, sym, side, qty, avg, coll, mkt in rows:
            qty_i = int(qty)
            avg_f = float(avg)
            coll_i = int(coll or 0)
            mkt_i = int(mkt or 0)
            market_value = qty_i * mkt_i
            side_s = str(side).upper()
            if side_s == "LONG":
                pnl = int(round((mkt_i - avg_f) * qty_i))
                equity = market_value
                long_value += market_value
            else:
                pnl = int(round((avg_f - mkt_i) * qty_i))
                equity = coll_i + pnl
                short_equity += equity
            unreal += pnl
            positions.append(
                PositionRow(
                    user_id=int(uid),
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
            user_id=int(user_id),
            cash=int(cash),
            positions=positions,
            net_worth=net_worth,
            long_value=int(long_value),
            short_equity=int(short_equity),
            unrealized_pnl=int(unreal),
        )

    async def leaderboard_by_networth(self, *, limit: int = 10) -> list[PortfolioSnapshot]:
        limit = max(1, min(int(limit), 25))
        await self.sync_catalog()
        user_rows = await self.db.fetchall(
            """
            SELECT DISTINCT user_id FROM blackjack_users
            UNION
            SELECT DISTINCT user_id FROM fun_positions
            """
        )
        user_ids = [int(r[0]) for r in user_rows if r and r[0] is not None]
        snaps: list[PortfolioSnapshot] = []
        for uid in user_ids:
            snaps.append(await self.portfolio(user_id=uid))
        snaps.sort(key=lambda s: s.net_worth, reverse=True)
        return snaps[:limit]

    async def recent_trades(self, *, user_id: int | None = None, limit: int = 15) -> list[tuple]:
        limit = max(1, min(int(limit), 25))
        if user_id is None:
            return await self.db.fetchall(
                "SELECT ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return await self.db.fetchall(
            "SELECT ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), limit),
        )


def format_signed_money(x: int) -> str:
    sign = "+" if int(x) >= 0 else ""
    return f"{sign}${int(x)}"
