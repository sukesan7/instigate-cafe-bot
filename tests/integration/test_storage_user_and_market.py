"""Storage integration tests for Database, UserStore, and FunMarketStore."""

from __future__ import annotations

import pytest

from icbot.db import Database
from icbot.services.fun_market import PRICE_FLOOR, SHORT_COLLATERAL_RATIO, FunMarketError, FunMarketStore
from icbot.services.user_service import (
    DAILY_COOLDOWN_SECONDS,
    DailyAlreadyClaimedError,
    InsufficientBalanceError,
    UserStore,
)

pytestmark = pytest.mark.storage


async def test_database_schema_creation_and_idempotent_init(tmp_db: Database):
    rows = await tmp_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {r[0] for r in rows}
    expected = {
        "meta",
        "users",
        "stock_events",
        "blackjack_users",
        "job_seen",
        "workday_cxs_cache",
        "exams",
        "fun_stocks",
        "fun_stock_events",
        "fun_positions",
        "fun_trades",
        "fun_market_weeks",
    }
    assert expected.issubset(tables)

    stock_val = await tmp_db.fetchone("SELECT value FROM meta WHERE key='stock_value'")
    assert stock_val == ("0",)

    # Re-running init on an existing database must remain safe and idempotent
    await tmp_db.init()
    rows_after = await tmp_db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    assert expected.issubset({r[0] for r in rows_after})


async def test_user_store_balances_and_daily_claim_cooldown(user_store: UserStore, frozen_time):
    uid = 800000000000000010
    profile = await user_store.ensure_user(uid, display_name="TestStudent", avatar_url="https://example.invalid/a.png")
    assert profile.balance == 1000
    assert profile.created_at == frozen_time.unix

    # Refresh display name without clobbering balance
    refreshed = await user_store.ensure_user(uid, display_name="UpdatedStudent")
    assert refreshed.display_name == "UpdatedStudent"
    assert refreshed.balance == 1000

    assert await user_store.add_balance(uid, 250) == 1250
    assert await user_store.subtract_balance(uid, 200) == 1050

    with pytest.raises(InsufficientBalanceError):
        await user_store.subtract_balance(uid, 5000)

    # Balance must be unchanged after rejected subtraction
    assert await user_store.get_balance(uid) == 1050

    # Daily claim + 24h cooldown enforcement
    amount, bal = await user_store.claim_daily(uid)
    assert amount == 100
    assert bal == 1150

    frozen_time.advance(3600)
    with pytest.raises(DailyAlreadyClaimedError) as exc_info:
        await user_store.claim_daily(uid)
    assert exc_info.value.next_claim_ts == (frozen_time.unix - 3600) + DAILY_COOLDOWN_SECONDS

    frozen_time.advance(DAILY_COOLDOWN_SECONDS)
    amount2, bal2 = await user_store.claim_daily(uid)
    assert amount2 == 100
    assert bal2 == 1250


async def test_fun_market_long_short_admin_and_leaderboard(
    tmp_db: Database,
    fun_market_store: FunMarketStore,
    seed_guild_users,
):
    alice_id = seed_guild_users["alice"].user_id
    bob_id = seed_guild_users["bob"].user_id

    active = await fun_market_store.list_stocks(active_only=True)
    symbols = {s.symbol for s in active}
    assert "IC" in symbols
    assert "DISD" in symbols
    assert "CONT" not in symbols  # inactive in catalog

    # Buy long on IC (start price = 100)
    stock, pos, cash_after_buy = await fun_market_store.buy_long(user_id=alice_id, symbol="$IC", qty=4)
    assert stock.symbol == "IC"
    assert pos.qty == 4
    assert cash_after_buy == 600

    # Admin moves IC up by +25 -> 125, mirrors legacy stock_events table
    moved = await fun_market_store.admin_move(symbol="IC", delta=25, user_id=alice_id, reason="Midterm hype")
    assert moved.price == 125
    legacy_meta = await tmp_db.fetchone("SELECT value FROM meta WHERE key='stock_value'")
    assert legacy_meta == ("125",)
    legacy_event = await tmp_db.fetchone("SELECT delta, new_value, reason FROM stock_events ORDER BY id DESC LIMIT 1")
    assert legacy_event == (25, 125, "Midterm hype")

    # Portfolio unrealized PnL (+25 * 4 = +100, equity = 4*125 = 500, net worth = 600 + 500 = 1100)
    snap = await fun_market_store.portfolio(user_id=alice_id)
    assert snap.cash == 600
    assert snap.unrealized_pnl == 100
    assert snap.net_worth == 1100

    # Sell 2 shares at 125 -> proceeds +250, realized +50
    _stk, pos_after_sell, cash_after_sell, realized = await fun_market_store.sell_long(
        user_id=alice_id, symbol="IC", qty=2
    )
    assert realized == 50
    assert cash_after_sell == 850
    assert pos_after_sell is not None and pos_after_sell.qty == 2

    # Bob opens short on DISD (price = 10), 20 shares -> notional 200, 150% collateral = 300
    _s, short_pos, bob_cash, collateral = await fun_market_store.open_short(
        user_id=bob_id, symbol="DISD", qty=20
    )
    assert collateral == int(20 * 10 * SHORT_COLLATERAL_RATIO)
    assert bob_cash == 700
    assert short_pos.collateral_locked == 300

    # Admin drops DISD by -100 -> clamps at PRICE_FLOOR (1)
    floored = await fun_market_store.admin_move(symbol="DISD", delta=-100, user_id=alice_id, reason="Crash")
    assert floored.price == PRICE_FLOOR

    # Bob covers all 20 shares at 1 -> releases 300 collateral + realized (10 - 1) * 20 = +180
    _s2, covered_pos, bob_cash_end, short_realized, released = await fun_market_store.cover_short(
        user_id=bob_id, symbol="DISD", qty=20
    )
    assert covered_pos is None
    assert released == 300
    assert short_realized == 180
    assert bob_cash_end == 700 + 300 + 180

    # Leaderboard ranks Bob (1180) ahead of Alice (850 + 2*125 = 1100)
    top = await fun_market_store.leaderboard_by_networth(limit=5)
    assert top[0].user_id == bob_id
    assert top[0].net_worth == 1180
    assert top[1].user_id == alice_id
    assert top[1].net_worth == 1100

    # Overselling raises FunMarketError cleanly without partial mutation
    with pytest.raises(FunMarketError):
        await fun_market_store.sell_long(user_id=alice_id, symbol="IC", qty=99)

