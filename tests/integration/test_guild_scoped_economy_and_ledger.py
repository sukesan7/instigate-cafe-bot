"""Integration and invariant test suite for atomic, guild-scoped virtual economy and ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from icbot.db import Database
from icbot.features.blackjack import BlackjackStore
from icbot.features.exams import _fetch_exams
from icbot.services.fun_market import (
    FunMarketStore,
)
from icbot.services.user_service import (
    DailyAlreadyClaimedError,
    InsufficientBalanceError,
    UserStore,
)

pytestmark = pytest.mark.storage


# ---------------------------------------------------------------------------
# 1. Multi-Guild Isolation: Same User ID and Stock Symbol
# ---------------------------------------------------------------------------


async def test_two_guilds_same_user_and_stock_isolation(tmp_db: Database) -> None:
    """Verify that two synthetic guilds with the same user ID and stock symbol are 100% isolated."""
    guild_alpha = 111111111111111111
    guild_beta = 222222222222222222
    shared_user_id = 999000000000000001

    store_alpha = UserStore(tmp_db, guild_id=guild_alpha, starting_balance=1000)
    store_beta = UserStore(tmp_db, guild_id=guild_beta, starting_balance=2500)

    # 1. User profiles in each guild
    p_alpha = await store_alpha.ensure_user(shared_user_id, display_name="AlphaUser")
    p_beta = await store_beta.ensure_user(shared_user_id, display_name="BetaUser")

    assert p_alpha.guild_id == guild_alpha
    assert p_alpha.balance == 1000
    assert p_beta.guild_id == guild_beta
    assert p_beta.balance == 2500

    # 2. Daily claim in Guild Alpha must NOT affect Guild Beta
    amount, bal_alpha = await store_alpha.claim_daily(shared_user_id)
    assert amount == 100
    assert bal_alpha == 1100
    assert await store_beta.get_balance(shared_user_id) == 2500

    # User can still claim in Guild Beta independently
    _, bal_beta = await store_beta.claim_daily(shared_user_id)
    assert bal_beta == 2600
    assert await store_alpha.get_balance(shared_user_id) == 1100

    # 3. Stock catalog and prices isolation
    fm_alpha = FunMarketStore(tmp_db, guild_id=guild_alpha)
    fm_beta = FunMarketStore(tmp_db, guild_id=guild_beta)

    await fm_alpha.sync_catalog()
    await fm_beta.sync_catalog()

    # Move IC stock in Guild Alpha to $200
    await fm_alpha.admin_set(symbol="IC", new_value=200, user_id=shared_user_id)
    ic_alpha = await fm_alpha.get_stock("IC")
    ic_beta = await fm_beta.get_stock("IC")

    assert ic_alpha.price == 200
    assert ic_beta.price == 100  # Beta baseline untouched

    # 4. Trading positions isolation
    await fm_alpha.buy_long(user_id=shared_user_id, symbol="IC", qty=2)  # cost 400
    snap_alpha = await fm_alpha.portfolio(user_id=shared_user_id)
    snap_beta = await fm_beta.portfolio(user_id=shared_user_id)

    assert snap_alpha.cash == 700  # 1100 - 400
    assert len(snap_alpha.positions) == 1
    assert snap_alpha.positions[0].qty == 2

    assert snap_beta.cash == 2600  # untouched
    assert len(snap_beta.positions) == 0

    # 5. Blackjack stats & leaderboard isolation
    bj_alpha = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=guild_alpha)
    bj_beta = BlackjackStore(tmp_db, starting_bankroll=2500, guild_id=guild_beta)

    await bj_alpha.ensure_user(shared_user_id)
    await bj_beta.ensure_user(shared_user_id)

    await bj_alpha.apply_delta_and_stats(shared_user_id, delta=300, result_counts={"win": 1}, hands_played=1)
    assert await bj_alpha.get_bankroll(shared_user_id) == 1000  # 700 + 300
    assert await bj_beta.get_bankroll(shared_user_id) == 2600  # Beta untouched

    lb_alpha = await bj_alpha.leaderboard()
    lb_beta = await bj_beta.leaderboard()
    assert lb_alpha[0] == (shared_user_id, 1000)
    assert lb_beta[0] == (shared_user_id, 2600)


async def test_exams_isolation_between_guilds(tmp_db: Database) -> None:
    """Verify exams added to Guild 1 are invisible in Guild 2, and clearing Guild 1 spares Guild 2."""
    g1 = 1001
    g2 = 1002

    now = 1750000000
    # Add exam to Guild 1
    id1 = await tmp_db.execute_insert(
        """
        INSERT INTO exams (guild_id, course_code, course_name, start_ts, end_ts, location, notes, created_by, created_at)
        VALUES (?, 'CPS109', 'CS I', ?, ?, 'ENG 101', 'Calculator', 1, ?)
        """,
        (g1, now + 3600, now + 7200, now),
    )

    # Add exam to Guild 2
    id2 = await tmp_db.execute_insert(
        """
        INSERT INTO exams (guild_id, course_code, course_name, start_ts, end_ts, location, notes, created_by, created_at)
        VALUES (?, 'CPS209', 'CS II', ?, ?, 'ENG 202', 'Closed book', 2, ?)
        """,
        (g2, now + 8000, now + 12000, now),
    )

    # Fetch per-guild
    exams_g1 = await _fetch_exams(tmp_db, guild_id=g1)
    exams_g2 = await _fetch_exams(tmp_db, guild_id=g2)

    assert len(exams_g1) == 1 and exams_g1[0].id == id1 and exams_g1[0].course_code == "CPS109"
    assert len(exams_g2) == 1 and exams_g2[0].id == id2 and exams_g2[0].course_code == "CPS209"

    # Clear Guild 1 exams
    await tmp_db.execute("DELETE FROM exams WHERE guild_id = ?", (g1,))

    assert len(await _fetch_exams(tmp_db, guild_id=g1)) == 0
    assert len(await _fetch_exams(tmp_db, guild_id=g2)) == 1


# ---------------------------------------------------------------------------
# 2. Concurrency & Overdraft Prevention (Atomic Conditional SQL)
# ---------------------------------------------------------------------------


async def test_concurrent_debits_prevent_overdraft_strictly(tmp_db: Database) -> None:
    """3 parallel debits of $400 against a $1000 balance must allow exactly 2 and reject 1."""
    store = UserStore(tmp_db, guild_id=555, starting_balance=1000)
    uid = 999000000000000002
    await store.ensure_user(uid)

    results = await asyncio.gather(
        store.subtract_balance(uid, 400),
        store.subtract_balance(uid, 400),
        store.subtract_balance(uid, 400),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, InsufficientBalanceError)]

    assert len(successes) == 2
    assert len(failures) == 1
    assert await store.get_balance(uid) == 200  # 1000 - 800


async def test_concurrent_daily_claims_pay_exactly_once(tmp_db: Database) -> None:
    """5 simultaneous daily claim attempts must award exactly once without race conditions."""
    store = UserStore(tmp_db, guild_id=555, starting_balance=1000, daily_claim_amount=100)
    uid = 999000000000000003
    await store.ensure_user(uid)

    results = await asyncio.gather(
        store.claim_daily(uid),
        store.claim_daily(uid),
        store.claim_daily(uid),
        store.claim_daily(uid),
        store.claim_daily(uid),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, tuple)]
    failures = [r for r in results if isinstance(r, DailyAlreadyClaimedError)]

    assert len(successes) == 1
    assert len(failures) == 4
    assert await store.get_balance(uid) == 1100

    # Verify exactly one ledger entry was written for daily claim
    ledger = await store.get_ledger(uid)
    daily_entries = [e for e in ledger if e.category == "daily"]
    assert len(daily_entries) == 1
    assert daily_entries[0].amount == 100


# ---------------------------------------------------------------------------
# 3. Invalid Negative, Zero, and Boundary Values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_val", [0, -1, -500])
async def test_user_store_rejects_negative_or_zero_amounts(tmp_db: Database, bad_val: int) -> None:
    store = UserStore(tmp_db, guild_id=777, starting_balance=1000)
    uid = 999000000000000004
    await store.ensure_user(uid)

    with pytest.raises(ValueError, match="Amount must be >= 1"):
        await store.subtract_balance(uid, bad_val)

    with pytest.raises(ValueError, match="Amount must be >= 1"):
        await store.add_balance(uid, bad_val)

    assert await store.get_balance(uid) == 1000


@pytest.mark.parametrize("bad_bet", [0, -1, -100])
async def test_blackjack_store_rejects_non_positive_wagers(tmp_db: Database, bad_bet: int) -> None:
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=777)
    uid = 999000000000000005
    await store.ensure_user(uid)

    with pytest.raises(ValueError, match="Bet must be >= 1"):
        await store.reserve_wager(uid, bad_bet)

    assert await store.get_bankroll(uid) == 1000


# ---------------------------------------------------------------------------
# 4. Idempotency & Replay Semantics
# ---------------------------------------------------------------------------


async def test_idempotent_debit_replay_preserves_balance(tmp_db: Database) -> None:
    """Replaying subtract_balance with the same idempotency_key returns identical balance without double-debiting."""
    store = UserStore(tmp_db, guild_id=888, starting_balance=1000)
    uid = 999000000000000006
    await store.ensure_user(uid)

    idem_key = "bet_uuid_12345"
    bal1 = await store.subtract_balance(uid, 200, idempotency_key=idem_key)
    assert bal1 == 800

    # Replay with same key
    bal2 = await store.subtract_balance(uid, 200, idempotency_key=idem_key)
    assert bal2 == 800
    assert await store.get_balance(uid) == 800

    # Verify ledger contains only one row for this transaction_id
    ledger = await store.get_ledger(uid)
    matched = [e for e in ledger if e.transaction_id == idem_key]
    assert len(matched) == 1


async def test_idempotent_credit_replay_preserves_balance(tmp_db: Database) -> None:
    """Replaying add_balance with the same idempotency_key returns identical balance without double-crediting."""
    store = UserStore(tmp_db, guild_id=888, starting_balance=1000)
    uid = 999000000000000007
    await store.ensure_user(uid)

    idem_key = "payout_uuid_67890"
    bal1 = await store.add_balance(uid, 500, idempotency_key=idem_key)
    assert bal1 == 1500

    # Replay
    bal2 = await store.add_balance(uid, 500, idempotency_key=idem_key)
    assert bal2 == 1500
    assert await store.get_balance(uid) == 1500


# ---------------------------------------------------------------------------
# 5. Crash / Exception Simulation & Rollback Integrity
# ---------------------------------------------------------------------------


async def test_transaction_rollback_prevents_partial_settlement(tmp_db: Database) -> None:
    """A crash/exception during multi-step settlement leaves balance and ledger completely unmutated."""
    store = UserStore(tmp_db, guild_id=999, starting_balance=1000)
    uid = 999000000000000008
    await store.ensure_user(uid)

    with pytest.raises(RuntimeError, match="Simulated crash during game settlement"):
        async with tmp_db.write_transaction() as tx:
            await tx.execute(
                "UPDATE users SET balance = balance - 300 WHERE guild_id=999 AND user_id=?", (uid,)
            )
            # Simulate failure before completing payout or ledger write
            raise RuntimeError("Simulated crash during game settlement")

    # State must be completely restored
    assert await store.get_balance(uid) == 1000
    ledger = await store.get_ledger(uid)
    assert len(ledger) == 0


# ---------------------------------------------------------------------------
# 6. Explicit Legacy Migration with Owner Approval Gate
# ---------------------------------------------------------------------------


async def test_legacy_migration_requires_confirmation_and_maps_cleanly(tmp_path: Path) -> None:
    """Unconfirmed legacy migration must be rejected; confirmed migration maps guild_id=0 rows cleanly."""
    legacy_file = tmp_path / "legacy_migration_test.db"

    # 1. Initialize modern database
    db = Database(str(legacy_file))
    await db.init()
    try:
        # 2. Insert synthetic legacy records with guild_id = 0
        await db.execute(
            "INSERT INTO users (guild_id, user_id, balance, created_at) VALUES (0, 1001, 7777, 1600000000)"
        )
        await db.execute("INSERT INTO blackjack_users (guild_id, user_id, bankroll) VALUES (0, 1001, 7777)")
        await db.execute(
            "INSERT INTO exams (guild_id, course_code, course_name, start_ts, created_at) VALUES (0, 'CPS109', 'CS', 1700000000, 1600000000)"
        )

        # 3. Attempting migration without confirmation must fail cleanly
        with pytest.raises(ValueError, match="explicit operator confirmation"):
            await db.migrate_legacy_guild_data(target_guild_id=987654321, confirmed=False)

        # Confirm rows are still under guild_id = 0
        u0 = await db.fetchone("SELECT balance FROM users WHERE guild_id = 0 AND user_id = 1001")
        assert u0 == (7777,)

        # 4. Execute confirmed migration
        result = await db.migrate_legacy_guild_data(target_guild_id=987654321, confirmed=True)
        assert result["users"] == 1
        assert result["blackjack_users"] == 1
        assert result["exams"] == 1

        # Verify rows now belong to 987654321 and guild_id = 0 is empty
        u_mapped = await db.fetchone(
            "SELECT balance FROM users WHERE guild_id = 987654321 AND user_id = 1001"
        )
        assert u_mapped == (7777,)
        assert await db.fetchone("SELECT balance FROM users WHERE guild_id = 0 AND user_id = 1001") is None

        # Verify audit record in meta table
        target_meta = await db.fetchone("SELECT value FROM meta WHERE key='legacy_migration_target_guild_id'")
        assert target_meta == ("987654321",)

        # 5. Subsequent run is idempotent
        res2 = await db.migrate_legacy_guild_data(target_guild_id=987654321, confirmed=True)
        assert res2.get("already_migrated") == 1
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 7. Database Lock Contention Under Parallel Workload
# ---------------------------------------------------------------------------


async def test_database_contention_under_parallel_workers(tmp_db: Database) -> None:
    """20 parallel workers concurrently performing economy and market mutations succeed without lock errors."""
    gid = 333
    fm = FunMarketStore(tmp_db, guild_id=gid)
    await fm.sync_catalog()

    async def worker(uid: int) -> None:
        ustore = UserStore(tmp_db, guild_id=gid, starting_balance=2000)
        await ustore.ensure_user(uid)
        # Debit
        await ustore.subtract_balance(uid, 100)
        # Credit
        await ustore.add_balance(uid, 50)
        # Fun market buy & sell
        await fm.buy_long(user_id=uid, symbol="IC", qty=1)
        await fm.sell_long(user_id=uid, symbol="IC", qty=1)

    workers = [worker(5000 + i) for i in range(20)]
    await asyncio.gather(*workers)

    # Verify all 20 workers finished with balance 1950 (2000 - 100 + 50)
    for i in range(20):
        uid = 5000 + i
        ustore = UserStore(tmp_db, guild_id=gid)
        bal = await ustore.get_balance(uid)
        assert bal == 1950
