"""Storage & concurrency invariant tests for UserStore, BlackjackStore, FunMarketStore, and Database startup."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import aiosqlite
import pytest

from icbot.db import Database
from icbot.features.blackjack import BlackjackStore
from icbot.services.fun_market import (
    PRICE_FLOOR,
    SHORT_COLLATERAL_RATIO,
    FunMarketError,
    FunMarketStore,
)
from icbot.services.user_service import (
    DAILY_COOLDOWN_SECONDS,
    DailyAlreadyClaimedError,
    InsufficientBalanceError,
    UserStore,
)

pytestmark = pytest.mark.storage


# ---------------------------------------------------------------------------
# UserStore & daily cooldown boundary invariants
# ---------------------------------------------------------------------------


async def test_daily_cooldown_exact_second_boundaries(user_store: UserStore, frozen_time):
    uid = 800000000000000101
    await user_store.ensure_user(uid, display_name="ClockBound")

    amount, bal = await user_store.claim_daily(uid)
    assert amount == 100
    assert bal == 1100

    # At +86399s (1 second before 24h boundary), claim must still fail cleanly
    frozen_time.advance(DAILY_COOLDOWN_SECONDS - 1)
    with pytest.raises(DailyAlreadyClaimedError):
        await user_store.claim_daily(uid)
    assert await user_store.get_balance(uid) == 1100

    # At +86400s exact boundary, claim succeeds
    frozen_time.advance(1)
    amount2, bal2 = await user_store.claim_daily(uid)
    assert amount2 == 100
    assert bal2 == 1200


async def test_parallel_subtract_balance_prevents_overdraft_race(user_store: UserStore, frozen_time):
    uid = 800000000000000102
    await user_store.ensure_user(uid, display_name="RaceDebit")

    results = await asyncio.gather(
        user_store.subtract_balance(uid, 700),
        user_store.subtract_balance(uid, 700),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, InsufficientBalanceError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert await user_store.get_balance(uid) == 300


async def test_parallel_claim_daily_awards_exactly_once(user_store: UserStore, frozen_time):
    uid = 800000000000000103
    await user_store.ensure_user(uid, display_name="RaceDaily")

    results = await asyncio.gather(
        user_store.claim_daily(uid),
        user_store.claim_daily(uid),
        return_exceptions=True,
    )
    claims = [r for r in results if isinstance(r, tuple)]
    blocked = [r for r in results if isinstance(r, DailyAlreadyClaimedError)]

    assert len(claims) == 1
    assert len(blocked) == 1
    assert await user_store.get_balance(uid) == 1100


@pytest.mark.parametrize("bad_amount", [0, -1, -500])
async def test_user_store_rejects_non_positive_amounts(user_store: UserStore, bad_amount: int):
    uid = 800000000000000104
    await user_store.ensure_user(uid, display_name="NegAmount")

    with pytest.raises(ValueError):
        await user_store.subtract_balance(uid, bad_amount)
    with pytest.raises(ValueError):
        await user_store.add_balance(uid, bad_amount)
    assert await user_store.get_balance(uid) == 1000


async def test_parallel_ensure_user_registration_is_idempotent(user_store: UserStore):
    uid = 800000000000000105
    p1, p2 = await asyncio.gather(
        user_store.ensure_user(uid, display_name="ConcurrentOne"),
        user_store.ensure_user(uid, display_name="ConcurrentTwo"),
    )
    assert p1.user_id == uid
    assert p2.user_id == uid


async def test_parallel_blackjack_apply_delta_preserves_all_updates(tmp_db: Database):
    store = BlackjackStore(tmp_db, starting_bankroll=1000)
    uid = 800000000000000106
    await store.ensure_user(uid)

    await asyncio.gather(
        store.apply_delta_and_stats(uid, 150, {"blackjack": 1}, hands_played=1),
        store.apply_delta_and_stats(uid, -50, {"loss": 1}, hands_played=1),
    )

    row = await tmp_db.fetchone(
        "SELECT wins, losses, hands_played, bankroll FROM blackjack_users WHERE user_id=?",
        (uid,),
    )
    user_bal = await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (uid,))
    assert user_bal == (1100,)
    assert row == (1, 1, 2, 1100)


# ---------------------------------------------------------------------------
# Fun Market ledger, collateral, weighted average, debt & concurrency invariants
# ---------------------------------------------------------------------------


async def test_fun_market_weighted_average_net_worth_conservation_and_debt(
    fun_market_store: FunMarketStore,
    seed_guild_users,
):
    alice_id = seed_guild_users["alice"].user_id
    bob_id = seed_guild_users["bob"].user_id

    # 1. Buying long preserves net worth at entry price
    await fun_market_store.buy_long(user_id=alice_id, symbol="IC", qty=2)  # 2 @ $100
    snap1 = await fun_market_store.portfolio(user_id=alice_id)
    assert snap1.cash == 800
    assert snap1.long_value == 200
    assert snap1.net_worth == 1000

    # Price moves +50 -> IC=$150, second buy adds weighted avg (2*100 + 2*150)/4 = 125.0
    await fun_market_store.admin_move(symbol="IC", delta=50, user_id=alice_id, reason="Rally")
    _stk, pos_long, cash_after_b2 = await fun_market_store.buy_long(user_id=alice_id, symbol="IC", qty=2)
    assert math.isclose(pos_long.avg_price, 125.0)
    assert cash_after_b2 == 500  # 800 - 300

    # Partial sell 1 share at $150: realized = +25; full sell remaining 3: realized = +75
    _s1, rem1, cash_s1, r1 = await fun_market_store.sell_long(user_id=alice_id, symbol="IC", qty=1)
    assert r1 == 25
    assert rem1 is not None and rem1.qty == 3
    assert cash_s1 == 650

    _s2, rem2, cash_s2, r2 = await fun_market_store.sell_long(user_id=alice_id, symbol="IC", qty=3)
    assert r2 == 75
    assert rem2 is None
    assert cash_s2 == 1100  # original 1000 + (25 + 75) realized

    # 2. Short collateral ceiling, partial release, and intentional negative debt on extreme spike
    await fun_market_store.admin_set(symbol="ROBO", new_value=3, user_id=alice_id, reason="Test odd notional")
    _sa, short_odd, _cash_odd, coll_odd = await fun_market_store.open_short(
        user_id=bob_id, symbol="ROBO", qty=1
    )
    assert coll_odd == math.ceil(3 * SHORT_COLLATERAL_RATIO)  # ceil(4.5) == 5
    assert short_odd.collateral_locked == 5
    snap_bob = await fun_market_store.portfolio(user_id=bob_id)
    assert snap_bob.net_worth == 1000  # net worth conserved on short entry
    await fun_market_store.cover_short(user_id=bob_id, symbol="ROBO", qty=1)

    # Extreme short squeeze: short 6 IC @ $150 -> notional 900 -> collateral 1350? No, reset IC to 100 first
    await fun_market_store.admin_set(symbol="IC", new_value=100, user_id=alice_id, reason="Reset for short")
    _sc, pos_short, bob_cash_short, coll = await fun_market_store.open_short(
        user_id=bob_id, symbol="IC", qty=6
    )
    assert coll == 900
    assert bob_cash_short == 100
    assert pos_short.collateral_locked == 900

    # Partial cover 3 of 6 at unchanged price $100 releases half collateral (450), realized 0
    _s_pc, pos_half, bob_cash_half, r_pc, rel_pc = await fun_market_store.cover_short(
        user_id=bob_id, symbol="IC", qty=3
    )
    assert rel_pc == 450
    assert r_pc == 0
    assert pos_half is not None and pos_half.qty == 3 and pos_half.collateral_locked == 450
    assert bob_cash_half == 550

    # Spike IC +400 -> $500; covering remaining 3 loses (100 - 500)*3 = -1200 + 450 release = -750 delta
    await fun_market_store.admin_move(symbol="IC", delta=400, user_id=alice_id, reason="Short squeeze")
    _s_end, pos_end, bob_cash_final, r_end, rel_end = await fun_market_store.cover_short(
        user_id=bob_id, symbol="IC", qty=3
    )
    assert pos_end is None
    assert rel_end == 450
    assert r_end == -1200
    # Legacy debt invariant: cash can go negative on extreme short losses
    assert bob_cash_final == 550 + 450 - 1200 == -200


@pytest.mark.parametrize("bad_qty", [0, -1, -50])
async def test_fun_market_rejects_non_positive_quantities_without_mutation(
    fun_market_store: FunMarketStore,
    seed_guild_users,
    bad_qty: int,
):
    uid = seed_guild_users["alice"].user_id
    with pytest.raises(FunMarketError):
        await fun_market_store.buy_long(user_id=uid, symbol="IC", qty=bad_qty)
    with pytest.raises(FunMarketError):
        await fun_market_store.sell_long(user_id=uid, symbol="IC", qty=bad_qty)
    with pytest.raises(FunMarketError):
        await fun_market_store.open_short(user_id=uid, symbol="IC", qty=bad_qty)
    with pytest.raises(FunMarketError):
        await fun_market_store.cover_short(user_id=uid, symbol="IC", qty=bad_qty)
    with pytest.raises(FunMarketError):
        await fun_market_store.buy_long(user_id=uid, symbol="VOID", qty=1)  # inactive catalog symbol
    with pytest.raises(FunMarketError):
        await fun_market_store.buy_long(user_id=uid, symbol="NOSUCH", qty=1)

    snap = await fun_market_store.portfolio(user_id=uid)
    assert snap.cash == 1000
    assert snap.positions == []


async def test_fun_market_parallel_buy_serializes_without_overdraft(
    fun_market_store: FunMarketStore,
    seed_guild_users,
):
    uid = seed_guild_users["alice"].user_id
    results = await asyncio.gather(
        fun_market_store.buy_long(user_id=uid, symbol="IC", qty=7),  # $700
        fun_market_store.buy_long(user_id=uid, symbol="IC", qty=7),  # $700
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, tuple)]
    errs = [r for r in results if isinstance(r, FunMarketError)]

    assert len(ok) == 1
    assert len(errs) == 1
    snap = await fun_market_store.portfolio(user_id=uid)
    assert snap.cash == 300
    assert snap.positions[0].qty == 7


# ---------------------------------------------------------------------------
# Startup, catalog sync, and schema migration idempotency
# ---------------------------------------------------------------------------


async def test_startup_init_migrations_and_catalog_sync_idempotency(
    tmp_path: Path,
    tmp_db: Database,
    fun_market_store: FunMarketStore,
    seed_guild_users,
):
    uid = seed_guild_users["alice"].user_id
    await fun_market_store.buy_long(user_id=uid, symbol="IC", qty=2)
    await fun_market_store.admin_set(symbol="IC", new_value=275, user_id=uid, reason="Baseline move")

    # Insert an unlisted legacy symbol directly to verify sync marks it inactive without deleting history
    await tmp_db.execute(
        "INSERT INTO fun_stocks (symbol, name, price, category, is_active, created_ts, updated_ts, week_key, week_open) "
        "VALUES ('OLD', 'Legacy Delisted', 42, 'misc', 1, 1000, 1000, '2025-W01', 42)"
    )

    # Repeated Database.init() + FunMarketStore.sync_catalog() must preserve prices, positions, and meta
    await tmp_db.init()
    await fun_market_store.sync_catalog()
    await fun_market_store.sync_catalog()

    ic_quote = await fun_market_store.get_stock("IC")
    old_quote = await fun_market_store.get_stock("OLD")
    assert ic_quote.price == max(PRICE_FLOOR, 275)
    assert old_quote.is_active is False
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='stock_value'") == ("275",)

    snap = await fun_market_store.portfolio(user_id=uid)
    assert snap.cash == 800
    assert snap.positions[0].qty == 2

    # Legacy schema missing additive columns migrates cleanly via Database.init()
    legacy_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(legacy_path)) as conn:
        await conn.executescript(
            """
            CREATE TABLE blackjack_users (
                user_id INTEGER PRIMARY KEY,
                bankroll INTEGER NOT NULL
            );
            CREATE TABLE fun_positions (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                avg_price REAL NOT NULL,
                PRIMARY KEY (user_id, symbol, side)
            );
            """
        )
        await conn.commit()

    migrated = Database(str(legacy_path))
    await migrated.init()
    await migrated.init()  # second init must be a safe no-op
    bj_cols = [str(r[1]) for r in await migrated.fetchall("PRAGMA table_info(blackjack_users)")]
    pos_cols = [str(r[1]) for r in await migrated.fetchall("PRAGMA table_info(fun_positions)")]
    assert "reset_count" in bj_cols
    assert "collateral_locked" in pos_cols
