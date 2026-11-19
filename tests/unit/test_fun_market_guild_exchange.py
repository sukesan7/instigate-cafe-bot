"""Unit tests for guild-defined fun-stock exchange, ledger invariants, and confirmation safety.

Verifies:
1. Synthetic catalog sanitization (zero private student lore, strictly fictional sample securities).
2. Multi-guild isolation for identical stock symbols (zero bleed between guilds).
3. Guild catalog customization (add, remove, reset to defaults).
4. Mathematical portfolio invariants (net-worth conservation across long/short and partial fills).
5. Trade idempotency with durable UUID deduplication (zero phantom debits/credits).
6. Concurrency race condition safety (concurrent buys without overdraft).
7. AdminMarketConfirmView permission checks and timeout behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from icbot.config import Settings
from icbot.db import Database
from icbot.features.invest import AdminMarketConfirmView
from icbot.services.fun_market import (
    PRICE_FLOOR,
    FunMarketError,
    FunMarketStore,
)
from icbot.services.fun_market_catalog import (
    FUN_STOCK_CATALOG,
    normalize_symbol,
    resolve_symbol,
)
from tests.helpers import FakeClient, FakeInteraction, build_fake_member


def test_synthetic_catalog_sanitization():
    """Verify default catalog has exactly 10 fictional securities with zero private student references."""
    assert len(FUN_STOCK_CATALOG) == 10

    # Denylist of historical student capstone/hackathon terms
    forbidden_terms = {
        "AI Blind Stick",
        "Paramedic Dashboard",
        "Distance Detector",
        "Plot Point",
        "Continuity AI",
        "The Graduates Project",
        "Bonzai Stress Relief",
        "DISD",
        "AIBS",
        "EMVC",
        "AGIS",
        "PLPT",
        "TGP",
        "CONT",
        "BNZI",
        "HVAC",
    }

    symbols = set()
    for item in FUN_STOCK_CATALOG:
        assert item.symbol == normalize_symbol(item.symbol)
        assert item.symbol not in forbidden_terms
        assert item.name not in forbidden_terms
        assert item.start_price >= PRICE_FLOOR
        assert item.category in {"core", "campus", "academics", "tech", "social", "unlisted", "misc"}
        symbols.add(item.symbol)

    # Expected synthetic sample securities
    expected = {"IC", "CAFE", "BOOK", "BYTE", "ROBO", "LABS", "DORM", "QUIZ", "MEME", "VOID"}
    assert symbols == expected

    # Legacy symbol resolution backwards compatibility
    assert resolve_symbol("DISD") == "CAFE"
    assert resolve_symbol("$disd") == "CAFE"
    assert resolve_symbol("AGIS") == "ROBO"
    assert resolve_symbol("CONT") == "VOID"
    assert resolve_symbol("IC") == "IC"
    assert resolve_symbol("CAFE") == "CAFE"
    assert resolve_symbol("NONEXISTENT") == "NONEXISTENT"


async def test_multi_guild_isolation_identical_symbols(tmp_db: Database):
    """Verify identical stock symbols in Guild 1 and Guild 2 operate independently with zero cross-talk."""
    g1_id = 1001
    g2_id = 1002
    user_id = 500000000000000001

    store_g1 = FunMarketStore(tmp_db, guild_id=g1_id)
    store_g2 = FunMarketStore(tmp_db, guild_id=g2_id)

    # 1. Add identical custom symbol 'ABC' to both guilds with different start prices
    s1 = await store_g1.add_guild_stock(symbol="ABC", name="Alpha Guild One", start_price=20)
    s2 = await store_g2.add_guild_stock(symbol="ABC", name="Alpha Guild Two", start_price=50)

    assert s1.price == 20
    assert s2.price == 50

    # 2. Guild 1 user buys 2 ABC @ $20 (cost $40)
    _, pos_g1, cash_g1 = await store_g1.buy_long(user_id=user_id, symbol="ABC", qty=2)
    assert cash_g1 == 960
    assert pos_g1.qty == 2

    # In Guild 2, user starts with independent bankroll ($1000) and zero positions
    snap_g2_init = await store_g2.portfolio(user_id=user_id)
    assert snap_g2_init.cash == 1000
    assert len(snap_g2_init.positions) == 0

    # Guild 2 user buys 1 ABC @ $50 (cost $50)
    _, pos_g2, cash_g2 = await store_g2.buy_long(user_id=user_id, symbol="ABC", qty=1)
    assert cash_g2 == 950
    assert pos_g2.qty == 1

    # 3. Move ABC in Guild 1 to $30 (+10)
    await store_g1.admin_move(symbol="ABC", delta=10, user_id=user_id, reason="G1 rally")
    stock_g1_after = await store_g1.get_stock("ABC")
    stock_g2_after = await store_g2.get_stock("ABC")

    assert stock_g1_after.price == 30
    assert stock_g2_after.price == 50  # Guild 2 price completely untouched

    # 4. Check G1 portfolio vs G2 portfolio
    snap_g1 = await store_g1.portfolio(user_id=user_id)
    snap_g2 = await store_g2.portfolio(user_id=user_id)

    assert snap_g1.cash == 960
    assert snap_g1.long_value == 60  # 2 * 30
    assert snap_g1.unrealized_pnl == 20  # (30 - 20) * 2

    assert snap_g2.cash == 950
    assert snap_g2.long_value == 50  # 1 * 50
    assert snap_g2.unrealized_pnl == 0

    # 5. Guild 1 trade history does not contain Guild 2 trades
    trades_g1 = await store_g1.recent_trades()
    trades_g2 = await store_g2.recent_trades()

    assert all(t[2] == "ABC" and t[5] == 20 for t in trades_g1)
    assert all(t[2] == "ABC" and t[5] == 50 for t in trades_g2)

    # 6. Legacy meta.stock_value is NOT touched by non-primary guilds
    meta_val = await tmp_db.fetchone("SELECT value FROM meta WHERE key='stock_value'")
    assert meta_val == ("0",)


async def test_guild_catalog_customization_and_sync(tmp_db: Database):
    """Verify guild admins can add, remove, and reset guild custom stocks safely."""
    gid = 2001
    store = FunMarketStore(tmp_db, guild_id=gid)

    # Initially default catalog
    cat_init = await store.list_guild_catalog()
    assert len(cat_init) == 10

    # Add custom stock 'TECH'
    stk = await store.add_guild_stock(
        symbol="TECH",
        name="Campus Tech Guild",
        start_price=25,
        category="tech",
        description="Custom student tech project.",
    )
    assert stk.symbol == "TECH"
    assert stk.price == 25
    assert stk.is_active is True

    cat_after_add = await store.list_guild_catalog()
    assert len(cat_after_add) == 11
    assert any(s.symbol == "TECH" for s in cat_after_add)

    # Verify custom stock can be traded
    _, pos, cash = await store.buy_long(user_id=123, symbol="TECH", qty=2)
    assert cash == 950
    assert pos.qty == 2

    # Remove custom stock (marks inactive, preserves positions)
    rem = await store.remove_guild_stock(symbol="TECH")
    assert rem is not None
    assert rem.is_active is False

    # Inactive stock cannot be bought
    with pytest.raises(FunMarketError, match="inactive"):
        await store.buy_long(user_id=123, symbol="TECH", qty=1)

    # Position is still held and can be sold
    _, sold_pos, cash_after_sell, _ = await store.sell_long(user_id=123, symbol="TECH", qty=2)
    assert sold_pos is None
    assert cash_after_sell == 1000

    # Reset to defaults
    restored = await store.reset_guild_catalog()
    active_restored = [s for s in restored if s.is_active]
    assert len(active_restored) == 9  # 9 active, 1 VOID inactive


async def test_net_worth_conservation_and_arithmetic_invariants(tmp_db: Database):
    """Verify portfolio net worth conservation and mathematical invariants across all actions."""
    gid = 3001
    uid = 42
    store = FunMarketStore(tmp_db, guild_id=gid)

    # Step 0: Baseline
    snap0 = await store.portfolio(user_id=uid)
    assert snap0.cash == 1000
    assert snap0.net_worth == 1000
    assert snap0.long_value == 0
    assert snap0.short_equity == 0

    # Step 1: Buy long 2 IC @ $100 -> cash 800, long_value 200 => net worth 1000
    _, _pos1, _cash1 = await store.buy_long(user_id=uid, symbol="IC", qty=2)
    snap1 = await store.portfolio(user_id=uid)
    assert snap1.cash == 800
    assert snap1.long_value == 200
    assert snap1.net_worth == 1000

    # Step 2: Price moves +50 -> IC = 150
    await store.admin_move(symbol="IC", delta=50, user_id=uid, reason="Rally")
    snap2 = await store.portfolio(user_id=uid)
    assert snap2.cash == 800
    assert snap2.long_value == 300
    assert snap2.unrealized_pnl == 100
    assert snap2.net_worth == 1100

    # Step 3: Partial sell 1 share at 150 -> proceeds +150, realized +50
    _, rem_pos, cash_sell, realized = await store.sell_long(user_id=uid, symbol="IC", qty=1)
    assert realized == 50
    assert cash_sell == 950
    assert rem_pos is not None and rem_pos.qty == 1
    snap3 = await store.portfolio(user_id=uid)
    assert snap3.cash == 950
    assert snap3.long_value == 150
    assert snap3.net_worth == 1100  # Conserved at the moment of sale

    # Step 4: Open short 2 CAFE (price 10) -> notional 20, 150% collateral = 30
    _, short_pos, cash_short, coll = await store.open_short(user_id=uid, symbol="CAFE", qty=2)
    assert coll == 30
    assert cash_short == 920
    assert short_pos.collateral_locked == 30
    snap4 = await store.portfolio(user_id=uid)
    assert snap4.cash == 920
    assert snap4.long_value == 150
    assert snap4.short_equity == 30
    assert snap4.net_worth == 1100  # Conserved upon short entry

    # Step 5: Price of CAFE drops by -5 -> price 5
    await store.admin_move(symbol="CAFE", delta=-5, user_id=uid, reason="Drop")
    snap5 = await store.portfolio(user_id=uid)
    # Short pnl = (10 - 5) * 2 = +10. Short equity = 30 + 10 = 40.
    assert snap5.short_equity == 40
    assert snap5.net_worth == 920 + 150 + 40  # 1110

    # Step 6: Partial cover 1 share at price 5
    # old_qty=2, cover 1 -> release = 15, realized = (10 - 5)*1 = +5. cash_delta = +20.
    _, rem_short, cash_cover, cover_realized, release = await store.cover_short(
        user_id=uid, symbol="CAFE", qty=1
    )
    assert release == 15
    assert cover_realized == 5
    assert cash_cover == 940
    assert rem_short is not None and rem_short.qty == 1 and rem_short.collateral_locked == 15
    snap6 = await store.portfolio(user_id=uid)
    assert snap6.cash == 940
    assert snap6.long_value == 150
    assert snap6.short_equity == 20  # 15 coll + 5 pnl
    assert snap6.net_worth == 1110  # Conserved upon cover execution

    # Step 7: Price floor clamping
    clamped_move = await store.admin_move(symbol="CAFE", delta=-50, user_id=uid)
    assert clamped_move.price == PRICE_FLOOR

    clamped_set = await store.admin_set(symbol="CAFE", new_value=-10, user_id=uid)
    assert clamped_set.price == PRICE_FLOOR


async def test_trade_idempotency_uuid_dedup(tmp_db: Database):
    """Verify replay of identical idempotency_key returns cached state with zero phantom mutation."""
    gid = 4001
    uid = 50
    store = FunMarketStore(tmp_db, guild_id=gid)

    # 1. Buy long with idempotency key
    key_buy = "test-buy-uuid-1234"
    _stk1, pos1, cash1 = await store.buy_long(user_id=uid, symbol="IC", qty=2, idempotency_key=key_buy)
    assert cash1 == 800
    assert pos1.qty == 2

    # Replay identical buy
    _stk1_dup, pos1_dup, cash1_dup = await store.buy_long(
        user_id=uid, symbol="IC", qty=2, idempotency_key=key_buy
    )
    assert cash1_dup == 800
    assert pos1_dup.qty == 2
    row_count = await tmp_db.fetchone(
        "SELECT COUNT(1) FROM economy_ledger WHERE transaction_id=?", (key_buy,)
    )
    assert row_count == (1,)
    trades_count = await tmp_db.fetchone(
        "SELECT COUNT(1) FROM fun_trades WHERE user_id=? AND guild_id=?", (uid, gid)
    )
    assert trades_count == (1,)

    # 2. Sell long with idempotency key
    key_sell = "test-sell-uuid-5678"
    _, pos_sell, cash_sell, r1 = await store.sell_long(
        user_id=uid, symbol="IC", qty=1, idempotency_key=key_sell
    )
    assert cash_sell == 900
    assert r1 == 0
    assert pos_sell is not None and pos_sell.qty == 1

    # Replay identical sell
    _, pos_sell_dup, cash_sell_dup, _ = await store.sell_long(
        user_id=uid, symbol="IC", qty=1, idempotency_key=key_sell
    )
    assert cash_sell_dup == 900
    assert pos_sell_dup is not None and pos_sell_dup.qty == 1
    ledger_sell_count = await tmp_db.fetchone(
        "SELECT COUNT(1) FROM economy_ledger WHERE transaction_id=?", (key_sell,)
    )
    assert ledger_sell_count == (1,)

    # 3. Open short with idempotency key
    key_short = "test-short-uuid-9999"
    _, _pos_sh, cash_sh, coll = await store.open_short(
        user_id=uid, symbol="CAFE", qty=2, idempotency_key=key_short
    )
    assert coll == 30
    assert cash_sh == 870

    # Replay open short
    _, pos_sh_dup, cash_sh_dup, _ = await store.open_short(
        user_id=uid, symbol="CAFE", qty=2, idempotency_key=key_short
    )
    assert cash_sh_dup == 870
    assert pos_sh_dup.qty == 2

    # 4. Cover short with idempotency key
    key_cover = "test-cover-uuid-8888"
    _, pos_cov, cash_cov, _, _ = await store.cover_short(
        user_id=uid, symbol="CAFE", qty=2, idempotency_key=key_cover
    )
    assert cash_cov == 900
    assert pos_cov is None

    # Replay cover short
    _, pos_cov_dup, cash_cov_dup, _, _ = await store.cover_short(
        user_id=uid, symbol="CAFE", qty=2, idempotency_key=key_cover
    )
    assert cash_cov_dup == 900
    assert pos_cov_dup is None


async def test_concurrency_race_condition_safety(tmp_db: Database):
    """Verify concurrent buy orders against limited balance execute atomically with zero overdraft."""
    gid = 5001
    uid = 77
    store = FunMarketStore(tmp_db, guild_id=gid)

    # Seed user balance to $150 (enough for exactly ONE share of IC @ $100, but not two)
    await tmp_db.execute(
        "INSERT OR REPLACE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, 0)",
        (gid, uid, 150),
    )

    # Launch two concurrent buy tasks
    task1 = store.buy_long(user_id=uid, symbol="IC", qty=1, guild_id=gid)
    task2 = store.buy_long(user_id=uid, symbol="IC", qty=1, guild_id=gid)

    results = await asyncio.gather(task1, task2, return_exceptions=True)

    successes = [r for r in results if isinstance(r, tuple)]
    failures = [r for r in results if isinstance(r, FunMarketError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert "Insufficient bankroll" in str(failures[0])

    # Final balance must be exactly $50
    bal_row = await tmp_db.fetchone("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
    assert bal_row == (50,)

    # Exactly 1 share owned
    pos = await store._get_position(user_id=uid, symbol="IC", side="LONG", guild_id=gid)
    assert pos is not None
    assert pos.qty == 1


async def test_admin_confirmation_view_permissions_and_timeout():
    """Verify AdminMarketConfirmView enforces user permission checks and timeout safety."""
    settings = Settings(guild_id=9999)
    admin_id = 1001
    non_admin_id = 2002

    view = AdminMarketConfirmView(admin_user_id=admin_id, settings=settings, timeout=60.0)
    assert view.confirmed is None

    # 1. Non-admin user interaction check (rejected)
    client = FakeClient(settings=settings)
    interaction_non_admin = FakeInteraction(
        user=build_fake_member(user_id=non_admin_id, administrator=False),
        client=client,
    )
    can_interact_wrong = await view.interaction_check(interaction_non_admin)  # type: ignore
    assert can_interact_wrong is False
    assert "Only the admin" in (interaction_non_admin.response.sent_messages[0].content or "")

    # 2. Authorized admin user interaction check
    interaction_admin = FakeInteraction(
        user=build_fake_member(user_id=admin_id, administrator=True),
        client=client,
    )
    can_interact_admin = await view.interaction_check(interaction_admin)  # type: ignore
    assert can_interact_admin is True

    # 3. Confirm button action
    mock_inter = AsyncMock()
    await view.confirm.callback(mock_inter)
    assert view.confirmed is True
    for child in view.children:
        assert getattr(child, "disabled", False) is True

    # 4. Timeout behavior
    view_timeout = AdminMarketConfirmView(admin_user_id=admin_id, settings=settings, timeout=0.1)
    await view_timeout.on_timeout()
    assert view_timeout.confirmed is False
    for child in view_timeout.children:
        assert getattr(child, "disabled", False) is True
