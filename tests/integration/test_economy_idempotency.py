"""Comprehensive idempotency, atomicity, concurrency, and conflict regression tests for virtual currency.

Uses only ephemeral temporary SQLite databases (``tmp_path`` / ``tmp_db``) with zero production DB
or network access.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from icbot.casino.blackjack_store import BlackjackStore
from icbot.db import Database, Transaction
from icbot.errors import (
    BotError,
    ConcurrencyConflictError,
    IdempotencyConflict,
    classify_failure,
    safe_error_summary,
)
from icbot.services.casino_service import CasinoService
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import InsufficientBalanceError, UserStore

pytestmark = pytest.mark.storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_error_is_safe(
    exc: IdempotencyConflict,
    *,
    forbidden_substrings: tuple[str, ...],
) -> None:
    """Assert public error string and classified summary leak no user IDs, guild IDs, keys, or secrets."""
    assert isinstance(exc, ConcurrencyConflictError)
    assert isinstance(exc, BotError)
    raw_msg = str(exc)
    summary = safe_error_summary(exc)
    classified = classify_failure(exc)
    assert classified.kind == "concurrency_conflict"
    assert classified.ux_category == "conflict"
    assert classified.ephemeral is True

    for surface in (raw_msg, summary, classified.user_message, classified.format_text()):
        for forbidden in forbidden_substrings:
            if forbidden:
                assert forbidden not in surface, f"Leaked {forbidden!r} in public error: {surface!r}"


# ---------------------------------------------------------------------------
# 1. UserStore: Exact Retry vs. All Semantic Conflict Dimensions
# ---------------------------------------------------------------------------


async def test_exact_retry_credit_and_debit_preserves_original_balance_after(tmp_db: Database) -> None:
    """Exact retries return the original transaction's balance_after even after later mutations."""
    store = UserStore(tmp_db, guild_id=101, starting_balance=1000)
    uid = 900001
    await store.ensure_user(uid)

    # Initial credit (+250 -> 1250)
    bal1 = await store.add_balance(
        uid, 250, category="reward", reference_id="quest:1", idempotency_key="idem:credit:1"
    )
    assert bal1 == 1250

    # Later unrelated debit (-100 -> 1150)
    bal_after_other = await store.subtract_balance(
        uid, 100, category="shop", reference_id="item:1", idempotency_key="idem:debit:1"
    )
    assert bal_after_other == 1150

    # Exact retry of original credit returns original balance_after (1250) without mutating current balance (1150)
    bal1_retry = await store.add_balance(
        uid, 250, category="reward", reference_id="quest:1", idempotency_key="idem:credit:1"
    )
    assert bal1_retry == 1250
    assert await store.get_balance(uid) == 1150

    # Drain balance to 50, then retry original -100 debit -> must still succeed idempotently!
    await store.subtract_balance(uid, 1100, category="drain", idempotency_key="idem:drain:1")
    assert await store.get_balance(uid) == 50

    bal_debit_retry = await store.subtract_balance(
        uid, 100, category="shop", reference_id="item:1", idempotency_key="idem:debit:1"
    )
    assert bal_debit_retry == 1150
    assert await store.get_balance(uid) == 50

    # Exactly 3 ledger rows total (no duplicates)
    cnt = await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger")
    assert cnt == (3,)


async def test_conflicting_actor_guild_direction_amount_category_reference_raise_conflict(
    tmp_db: Database,
) -> None:
    """Every non-equivalent reuse of an idempotency key raises IdempotencyConflict with zero mutation."""
    gid_a = 1001
    gid_b = 2002
    alice = 700000000000000001
    bob = 700000000000000002

    store_a = UserStore(tmp_db, guild_id=gid_a, starting_balance=1000)
    store_b = UserStore(tmp_db, guild_id=gid_b, starting_balance=1000)
    await store_a.ensure_user(alice)
    await store_a.ensure_user(bob)
    await store_b.ensure_user(alice)

    secret_key = "secret_key_token_XYZ987"
    secret_ref = "ref_private_order_42"

    # Establish original credit: guild A, alice, +200, category="reward", ref=secret_ref
    assert (
        await store_a.add_balance(
            alice, 200, category="reward", reference_id=secret_ref, idempotency_key=secret_key
        )
        == 1200
    )

    forbidden = (str(gid_a), str(gid_b), str(alice), str(bob), secret_key, secret_ref, "1200")

    # 1. Conflicting actor (bob tries alice's key)
    with pytest.raises(IdempotencyConflict) as exc_actor:
        await store_a.add_balance(
            bob, 200, category="reward", reference_id=secret_ref, idempotency_key=secret_key
        )
    _assert_error_is_safe(exc_actor.value, forbidden_substrings=forbidden)
    assert "user_id" in exc_actor.value.mismatched_fields

    # 2. Conflicting guild (alice in guild B tries guild A's key)
    with pytest.raises(IdempotencyConflict) as exc_guild:
        await store_b.add_balance(
            alice, 200, category="reward", reference_id=secret_ref, idempotency_key=secret_key
        )
    _assert_error_is_safe(exc_guild.value, forbidden_substrings=forbidden)
    assert "guild_id" in exc_guild.value.mismatched_fields

    # 3. Credit-vs-debit collision (same category & reference_id, opposite direction)
    with pytest.raises(IdempotencyConflict) as exc_dir:
        await store_a.subtract_balance(
            alice, 200, category="reward", reference_id=secret_ref, idempotency_key=secret_key
        )
    _assert_error_is_safe(exc_dir.value, forbidden_substrings=forbidden)
    assert "operation_type" in exc_dir.value.mismatched_fields or "amount" in exc_dir.value.mismatched_fields

    # 4. Differing amount (+300 vs +200)
    with pytest.raises(IdempotencyConflict) as exc_amt:
        await store_a.add_balance(
            alice, 300, category="reward", reference_id=secret_ref, idempotency_key=secret_key
        )
    _assert_error_is_safe(exc_amt.value, forbidden_substrings=forbidden)
    assert "amount" in exc_amt.value.mismatched_fields

    # 5. Differing category ("grant" vs "reward")
    with pytest.raises(IdempotencyConflict) as exc_cat:
        await store_a.add_balance(
            alice, 200, category="grant", reference_id=secret_ref, idempotency_key=secret_key
        )
    _assert_error_is_safe(exc_cat.value, forbidden_substrings=forbidden)
    assert "category" in exc_cat.value.mismatched_fields

    # 6. Differing reference_id ("other_ref" vs secret_ref, and None vs secret_ref)
    for bad_ref in ("other_ref", None, ""):
        with pytest.raises(IdempotencyConflict) as exc_ref:
            await store_a.add_balance(
                alice, 200, category="reward", reference_id=bad_ref, idempotency_key=secret_key
            )
        _assert_error_is_safe(exc_ref.value, forbidden_substrings=forbidden)
        assert "reference_id" in exc_ref.value.mismatched_fields

    # Verify ZERO mutations occurred from any of the conflict attempts
    assert await store_a.get_balance(alice) == 1200
    assert await store_a.get_balance(bob) == 1000
    assert await store_b.get_balance(alice) == 1000
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (1,)

    # Establish a debit and verify reverse collision (debit -> credit) and None vs non-None reference
    debit_key = "debit_key_ABC123"
    assert (
        await store_a.subtract_balance(
            alice, 150, category="wager", reference_id=None, idempotency_key=debit_key
        )
        == 1050
    )
    with pytest.raises(IdempotencyConflict):
        await store_a.add_balance(alice, 150, category="wager", reference_id=None, idempotency_key=debit_key)
    with pytest.raises(IdempotencyConflict):
        await store_a.subtract_balance(
            alice, 150, category="wager", reference_id="extra_ref", idempotency_key=debit_key
        )
    assert await store_a.get_balance(alice) == 1050
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (2,)


async def test_claim_daily_idempotency_exact_retry_and_conflicts(tmp_db: Database) -> None:
    """claim_daily validates guild_id, user_id, amount, category='daily', and reference_id=None."""
    store_g1 = UserStore(tmp_db, guild_id=11, starting_balance=1000, daily_claim_amount=100)
    store_g2 = UserStore(tmp_db, guild_id=22, starting_balance=1000, daily_claim_amount=100)
    alice = 5001
    bob = 5002
    await store_g1.ensure_user(alice)
    await store_g1.ensure_user(bob)
    await store_g2.ensure_user(alice)

    key = "daily:g1:5001:day1"
    amt1, bal1 = await store_g1.claim_daily(alice, idempotency_key=key)
    assert (amt1, bal1) == (100, 1100)

    # Exact retry returns identical result without double credit
    amt2, bal2 = await store_g1.claim_daily(alice, idempotency_key=key)
    assert (amt2, bal2) == (100, 1100)

    # Different actor collides
    with pytest.raises(IdempotencyConflict):
        await store_g1.claim_daily(bob, idempotency_key=key)

    # Different guild collides
    with pytest.raises(IdempotencyConflict):
        await store_g2.claim_daily(alice, idempotency_key=key)

    # Different configured daily amount collides
    store_g1_big = UserStore(tmp_db, guild_id=11, starting_balance=1000, daily_claim_amount=250)
    with pytest.raises(IdempotencyConflict):
        await store_g1_big.claim_daily(alice, idempotency_key=key)

    # Reusing a non-daily key in claim_daily collides
    await store_g1.subtract_balance(bob, 100, category="debit", idempotency_key="not_a_daily")
    with pytest.raises(IdempotencyConflict):
        await store_g1.claim_daily(bob, idempotency_key="not_a_daily")

    assert await store_g1.get_balance(alice) == 1100
    assert await store_g1.get_balance(bob) == 900
    assert await store_g2.get_balance(alice) == 1000


# ---------------------------------------------------------------------------
# 2. Concurrency, Uniqueness Race, Insufficient Funds & Rollback Atomicity
# ---------------------------------------------------------------------------


async def test_concurrent_duplicate_and_conflicting_requests(tmp_db: Database) -> None:
    """Concurrent exact duplicates execute exactly once; concurrent conflicting keys raise IdempotencyConflict."""
    store = UserStore(tmp_db, guild_id=303, starting_balance=1000)
    alice = 6001
    bob = 6002
    await store.ensure_user(alice)
    await store.ensure_user(bob)

    # 8 concurrent identical debits
    debit_key = "concurrent_debit_1"
    results = await asyncio.gather(
        *[
            store.subtract_balance(alice, 200, category="bet", reference_id="r1", idempotency_key=debit_key)
            for _ in range(8)
        ]
    )
    assert results == [800] * 8
    assert await store.get_balance(alice) == 800
    assert await tmp_db.fetchone(
        "SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (debit_key,)
    ) == (1,)

    # 8 concurrent identical credits
    credit_key = "concurrent_credit_1"
    c_results = await asyncio.gather(
        *[
            store.add_balance(alice, 150, category="win", reference_id="r1", idempotency_key=credit_key)
            for _ in range(8)
        ]
    )
    assert c_results == [950] * 8
    assert await store.get_balance(alice) == 950
    assert await tmp_db.fetchone(
        "SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (credit_key,)
    ) == (1,)

    # Concurrent race between alice (+100) and bob (+100) using the SAME key -> 1 succeeds, 1 conflicts
    race_key = "concurrent_race_conflict"
    mixed = await asyncio.gather(
        store.add_balance(alice, 100, category="bonus", idempotency_key=race_key),
        store.add_balance(bob, 100, category="bonus", idempotency_key=race_key),
        return_exceptions=True,
    )
    ok_vals = [r for r in mixed if isinstance(r, int)]
    conflicts = [r for r in mixed if isinstance(r, IdempotencyConflict)]
    assert len(ok_vals) == 1
    assert len(conflicts) == 1
    assert await tmp_db.fetchone(
        "SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (race_key,)
    ) == (1,)
    # Total balance across alice + bob increased by exactly 100 (950 + 1000 + 100 = 2050)
    assert (await store.get_balance(alice)) + (await store.get_balance(bob)) == 2050


async def test_uniqueness_race_integrity_error_rolls_back_mutation_before_returning_or_raising(
    tmp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate sqlite3.IntegrityError race after pre-check: SAVEPOINT must roll back balance update."""
    store = UserStore(tmp_db, guild_id=404, starting_balance=1000)
    alice = 7001
    bob = 7002
    await store.ensure_user(alice)
    await store.ensure_user(bob)

    # Pre-commit an existing ledger row for alice (+300 -> 1300)
    race_key = "race_key_sim_1"
    assert (
        await store.add_balance(alice, 300, category="payout", reference_id="h1", idempotency_key=race_key)
        == 1300
    )
    assert await store.get_balance(alice) == 1300

    # Wrap Transaction.execute so the FIRST SELECT on economy_ledger returns empty (simulating pre-check miss),
    # forcing execution into UPDATE users + INSERT INTO economy_ledger -> real SQLite UNIQUE IntegrityError!
    orig_execute = Transaction.execute
    skip_first_lookup = {"remaining": 1}

    async def _simulated_race_execute(self: Transaction, sql: str, params: Any = ()) -> Any:
        if "FROM economy_ledger WHERE transaction_id" in sql and skip_first_lookup["remaining"] > 0:
            skip_first_lookup["remaining"] -= 1
            return await orig_execute(self, "SELECT 1 WHERE 0", ())
        return await orig_execute(self, sql, params)

    monkeypatch.setattr(Transaction, "execute", _simulated_race_execute)

    # Case A: Matching retry hits IntegrityError -> SAVEPOINT rolls back duplicate +300, returns 1300!
    bal_match = await store.add_balance(
        alice, 300, category="payout", reference_id="h1", idempotency_key=race_key
    )
    assert bal_match == 1300
    assert await store.get_balance(alice) == 1300  # NOT 1600!

    # Case B: Conflicting actor hits IntegrityError -> SAVEPOINT rolls back bob's +300, raises IdempotencyConflict!
    skip_first_lookup["remaining"] = 1
    with pytest.raises(IdempotencyConflict):
        await store.add_balance(bob, 300, category="payout", reference_id="h1", idempotency_key=race_key)
    assert await store.get_balance(bob) == 1000  # NOT 1300!
    assert await store.get_balance(alice) == 1300
    assert await tmp_db.fetchone(
        "SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (race_key,)
    ) == (1,)

    # Case C: Same IntegrityError race on subtract_balance (match & conflict)
    sub_race_key = "race_key_sim_sub_1"
    assert (
        await store.subtract_balance(
            alice, 200, category="wager", reference_id="h2", idempotency_key=sub_race_key
        )
        == 1100
    )
    skip_first_lookup["remaining"] = 1
    assert (
        await store.subtract_balance(
            alice, 200, category="wager", reference_id="h2", idempotency_key=sub_race_key
        )
        == 1100
    )
    assert await store.get_balance(alice) == 1100  # NOT 900!

    skip_first_lookup["remaining"] = 1
    with pytest.raises(IdempotencyConflict):
        await store.subtract_balance(
            bob, 200, category="wager", reference_id="h2", idempotency_key=sub_race_key
        )
    assert await store.get_balance(bob) == 1000  # NOT 800!
    assert await tmp_db.fetchone(
        "SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (sub_race_key,)
    ) == (1,)


async def test_insufficient_funds_does_not_mutate_or_consume_idempotency_key(tmp_db: Database) -> None:
    """InsufficientBalanceError leaves balance unchanged, writes no ledger row, and does not consume the key."""
    store = UserStore(tmp_db, guild_id=505, starting_balance=100)
    uid = 8001
    await store.ensure_user(uid)

    key = "debit_need_250"
    with pytest.raises(InsufficientBalanceError):
        await store.subtract_balance(uid, 250, category="purchase", reference_id="sku1", idempotency_key=key)

    assert await store.get_balance(uid) == 100
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (0,)

    # Top up user, then retry with the same key -> now succeeds cleanly
    await store.add_balance(uid, 200, category="topup", idempotency_key="topup_1")
    new_bal = await store.subtract_balance(
        uid, 250, category="purchase", reference_id="sku1", idempotency_key=key
    )
    assert new_bal == 50
    assert await store.get_balance(uid) == 50
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (key,)) == (1,)


async def test_failed_ledger_insert_rolls_back_balance_atomically(tmp_db: Database) -> None:
    """If INSERT INTO economy_ledger fails, users.balance update must roll back completely."""
    store = UserStore(tmp_db, guild_id=606, starting_balance=1000)
    uid = 8002
    await store.ensure_user(uid)

    # Inject a trigger that aborts any insert into economy_ledger
    await tmp_db.execute(
        """
        CREATE TRIGGER fail_ledger_insert
        BEFORE INSERT ON economy_ledger
        BEGIN
            SELECT RAISE(ABORT, 'simulated ledger write failure');
        END;
        """
    )

    try:
        with pytest.raises(sqlite3.IntegrityError, match="simulated ledger write failure"):
            await store.add_balance(uid, 500, category="reward", idempotency_key="fail_credit_1")
        assert await store.get_balance(uid) == 1000

        with pytest.raises(sqlite3.IntegrityError, match="simulated ledger write failure"):
            await store.subtract_balance(uid, 400, category="bet", idempotency_key="fail_debit_1")
        assert await store.get_balance(uid) == 1000
        assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (0,)
    finally:
        await tmp_db.execute("DROP TRIGGER IF EXISTS fail_ledger_insert")

    # After fault clears, retrying with the same keys applies cleanly once
    assert await store.add_balance(uid, 500, category="reward", idempotency_key="fail_credit_1") == 1500
    assert await store.subtract_balance(uid, 400, category="bet", idempotency_key="fail_debit_1") == 1100
    assert await store.get_balance(uid) == 1100
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (2,)


async def test_reconciliation_after_simulated_crash_and_db_reopen(tmp_path: Path) -> None:
    """Simulate post-commit crash & restart on file-backed SQLite DB: exact retry reconciles, mismatch conflicts."""
    db_file = tmp_path / "crash_reconcile.db"
    gid = 707
    uid = 8003
    key = "tx:reconcile:001"

    db1 = Database(str(db_file))
    await db1.init()
    try:
        store1 = UserStore(db1, guild_id=gid, starting_balance=1000)
        await store1.ensure_user(uid)
        # Commit succeeds, then suppose caller crashes before observing result
        bal_committed = await store1.subtract_balance(
            uid, 350, category="wager", reference_id="round:99", idempotency_key=key
        )
        assert bal_committed == 650
    finally:
        await db1.close()

    # Process restarts with a fresh Database handle on the same SQLite file
    db2 = Database(str(db_file))
    await db2.init()
    try:
        store2 = UserStore(db2, guild_id=gid, starting_balance=1000)
        # Exact retry reconciles cleanly without double-debit
        bal_reconciled = await store2.subtract_balance(
            uid, 350, category="wager", reference_id="round:99", idempotency_key=key
        )
        assert bal_reconciled == 650
        assert await store2.get_balance(uid) == 650

        # Non-equivalent retry after restart fails closed
        with pytest.raises(IdempotencyConflict):
            await store2.subtract_balance(
                uid, 400, category="wager", reference_id="round:99", idempotency_key=key
            )
        assert await store2.get_balance(uid) == 650
        assert await db2.fetchone("SELECT COUNT(*) FROM economy_ledger WHERE transaction_id=?", (key,)) == (
            1,
        )
    finally:
        await db2.close()


# ---------------------------------------------------------------------------
# 3. Caller Settlement Paths: CasinoService, BlackjackStore, FunMarketStore
# ---------------------------------------------------------------------------


async def test_casino_service_idempotency_conflict_and_exact_replay(tmp_db: Database) -> None:
    """CasinoService reserve/settle/refund reject cross-actor, cross-guild, cross-amount, cross-op collisions."""
    svc = CasinoService(tmp_db, guild_id=100, starting_bankroll=1000)
    alice = 9101
    bob = 9102
    await svc.ensure_user(alice, guild_id=100)
    await svc.ensure_user(bob, guild_id=100)
    await svc.ensure_user(alice, guild_id=200)

    res = await svc.reserve_wager(100, alice, 200, "roulette", idempotency_key="cas:res:1")
    assert res.balance_after == 800

    # Exact replay succeeds
    res_dup = await svc.reserve_wager(100, alice, 200, "roulette", idempotency_key="cas:res:1")
    assert res_dup.balance_after == 800

    # Conflicting actor, guild, amount, or game_type -> IdempotencyConflict
    with pytest.raises(IdempotencyConflict):
        await svc.reserve_wager(100, bob, 200, "roulette", idempotency_key="cas:res:1")
    with pytest.raises(IdempotencyConflict):
        await svc.reserve_wager(200, alice, 200, "roulette", idempotency_key="cas:res:1")
    with pytest.raises(IdempotencyConflict):
        await svc.reserve_wager(100, alice, 300, "roulette", idempotency_key="cas:res:1")
    with pytest.raises(IdempotencyConflict):
        await svc.reserve_wager(100, alice, 200, "slots", idempotency_key="cas:res:1")

    # Trying to settle using the reservation's key collides (debit vs credit, category, reference_id)
    with pytest.raises(IdempotencyConflict):
        await svc.settle_wager(
            res, payout_amount=400, net_delta=200, outcome="win", idempotency_key="cas:res:1"
        )

    # Proper settlement + exact replay + conflicting payout
    settle1 = await svc.settle_wager(
        res, payout_amount=400, net_delta=200, outcome="win", idempotency_key="cas:settle:1"
    )
    assert settle1.balance_after == 1200
    assert settle1.already_settled is False

    settle2 = await svc.settle_wager(
        res, payout_amount=400, net_delta=200, outcome="win", idempotency_key="cas:settle:1"
    )
    assert settle2.balance_after == 1200
    assert settle2.already_settled is True

    with pytest.raises(IdempotencyConflict):
        await svc.settle_wager(
            res, payout_amount=500, net_delta=300, outcome="win", idempotency_key="cas:settle:1"
        )
    with pytest.raises(IdempotencyConflict):
        await svc.refund_wager(res, idempotency_key="cas:settle:1")

    assert await svc.get_balance(alice, guild_id=100) == 1200
    assert await svc.get_balance(bob, guild_id=100) == 1000
    assert await svc.get_balance(alice, guild_id=200) == 1000


async def test_blackjack_store_idempotency_conflicts_and_legacy_category_compatibility(
    tmp_db: Database,
) -> None:
    """BlackjackStore rejects non-equivalent key collisions while staying compatible with legacy category names."""
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=50)
    alice = 9201
    bob = 9202
    await store.ensure_user(alice)
    await store.ensure_user(bob)

    bet_key = "bj_bet:bj_round:50:9201:1700000000:abcd1234"
    bal = await store.reserve_wager(alice, 100, idempotency_key=bet_key)
    assert bal == 900

    # Exact retry succeeds
    assert await store.reserve_wager(alice, 100, idempotency_key=bet_key) == 900

    # Conflicting actor / amount / guild / credit-vs-debit refund with same key -> IdempotencyConflict
    with pytest.raises(IdempotencyConflict):
        await store.reserve_wager(bob, 100, idempotency_key=bet_key)
    with pytest.raises(IdempotencyConflict):
        await store.reserve_wager(alice, 200, idempotency_key=bet_key)
    with pytest.raises(IdempotencyConflict):
        await store.reserve_wager(alice, 100, guild_id=51, idempotency_key=bet_key)
    with pytest.raises(IdempotencyConflict):
        await store.refund_wager(alice, 100, idempotency_key=bet_key)

    # Settlement exact retry vs conflicting delta / reference_id
    settle_key = "bj_settle:bj_round:50:9201:1700000000:abcd1234"
    bal_settled = await store.apply_delta_and_stats(
        alice, 200, {"win": 1}, hands_played=1, idempotency_key=settle_key, reference_id=bet_key
    )
    assert bal_settled == 1100
    assert (
        await store.apply_delta_and_stats(
            alice, 200, {"win": 1}, hands_played=1, idempotency_key=settle_key, reference_id=bet_key
        )
        == 1100
    )
    with pytest.raises(IdempotencyConflict):
        await store.apply_delta_and_stats(
            alice, 300, {"win": 1}, hands_played=1, idempotency_key=settle_key, reference_id=bet_key
        )
    with pytest.raises(IdempotencyConflict):
        await store.apply_delta_and_stats(
            alice, 200, {"win": 1}, hands_played=1, idempotency_key=settle_key, reference_id="bj_bet:other"
        )

    # Legacy row compatibility: pre-existing row written with legacy category 'blackjack_wager'
    legacy_bet_key = "bj_bet:legacy_round_001"
    await tmp_db.execute(
        """
        INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
        VALUES (?, 50, ?, -75, 1025, 'blackjack_wager', 'blackjack', 1600000000)
        """,
        (legacy_bet_key, alice),
    )
    # Documented legacy category alias accepted when guild, user, signed amount, and reference match
    assert await store.reserve_wager(alice, 75, idempotency_key=legacy_bet_key) == 1025
    # Still rejects mismatched amount or actor on that legacy row
    with pytest.raises(IdempotencyConflict):
        await store.reserve_wager(alice, 80, idempotency_key=legacy_bet_key)
    with pytest.raises(IdempotencyConflict):
        await store.reserve_wager(bob, 75, idempotency_key=legacy_bet_key)

    # Historical rows remain intact and readable via UserStore.get_ledger
    ustore = UserStore(tmp_db, guild_id=50)
    entries = await ustore.get_ledger(alice, limit=20)
    assert any(e.transaction_id == legacy_bet_key and e.category == "blackjack_wager" for e in entries)

    # UserStore enforces strict category match against historical rows rather than turning collisions into success
    with pytest.raises(IdempotencyConflict):
        await ustore.subtract_balance(
            alice, 75, category="debit", reference_id="blackjack", idempotency_key=legacy_bet_key
        )
    # Exact category + guild + user + signed amount + reference on UserStore still succeeds idempotently
    assert (
        await ustore.subtract_balance(
            alice, 75, category="blackjack_wager", reference_id="blackjack", idempotency_key=legacy_bet_key
        )
        == 1025
    )


async def test_fun_market_idempotency_conflicts_and_no_inventory_corruption(tmp_db: Database) -> None:
    """FunMarketStore rejects cross-user, cross-guild, cross-symbol, cross-qty, and cross-side key collisions."""
    fm_g1 = FunMarketStore(tmp_db, guild_id=801)
    fm_g2 = FunMarketStore(tmp_db, guild_id=802)
    alice = 9301
    bob = 9302

    # Alice buys 2 IC @ 100 (-200 -> 800)
    buy_key = "fm:buy:1"
    _s, pos, cash = await fm_g1.buy_long(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)
    assert cash == 800
    assert pos.qty == 2

    # Exact retry returns consistent balance_after=800 without double buy
    _s2, pos2, cash2 = await fm_g1.buy_long(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)
    assert cash2 == 800
    assert pos2.qty == 2

    # Conflicting actor, guild, symbol, qty, or side (sell/short/cover) -> IdempotencyConflict with zero mutation
    with pytest.raises(IdempotencyConflict):
        await fm_g1.buy_long(user_id=bob, symbol="IC", qty=2, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g2.buy_long(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g1.buy_long(user_id=alice, symbol="CAFE", qty=2, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g1.buy_long(user_id=alice, symbol="IC", qty=3, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g1.sell_long(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g1.open_short(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)
    with pytest.raises(IdempotencyConflict):
        await fm_g1.cover_short(user_id=alice, symbol="IC", qty=2, idempotency_key=buy_key)

    # Verify zero position/balance/trade corruption
    snap_alice = await fm_g1.portfolio(user_id=alice)
    assert snap_alice.cash == 800
    assert len(snap_alice.positions) == 1
    assert snap_alice.positions[0].qty == 2
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM fun_trades") == (1,)
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger") == (1,)

    # Short + Cover idempotency conflict check (including closed-position replay vs mismatched qty)
    short_key = "fm:short:1"
    await fm_g1.open_short(user_id=alice, symbol="CAFE", qty=2, idempotency_key=short_key)
    cover_key = "fm:cover:1"
    _, pos_cov, cash_cov, _, _ = await fm_g1.cover_short(
        user_id=alice, symbol="CAFE", qty=2, idempotency_key=cover_key
    )
    assert pos_cov is None
    # Exact replay of closed short succeeds
    _, pos_cov2, cash_cov2, _, _ = await fm_g1.cover_short(
        user_id=alice, symbol="CAFE", qty=2, idempotency_key=cover_key
    )
    assert pos_cov2 is None
    assert cash_cov2 == cash_cov
    # Mismatched qty on closed short key raises IdempotencyConflict
    with pytest.raises(IdempotencyConflict):
        await fm_g1.cover_short(user_id=alice, symbol="CAFE", qty=1, idempotency_key=cover_key)
    # Cross-user on cover key raises IdempotencyConflict
    with pytest.raises(IdempotencyConflict):
        await fm_g1.cover_short(user_id=bob, symbol="CAFE", qty=2, idempotency_key=cover_key)


# ---------------------------------------------------------------------------
# 4. Deterministic Property Test: Ledger Conservation Under Retries & Collisions
# ---------------------------------------------------------------------------


async def test_property_ledger_conservation_under_retries_and_collisions(tmp_db: Database) -> None:
    """Seeded randomized sequence of credits, debits, retries, and collisions preserves exact accounting invariants."""
    rng = random.Random(20261123)
    starting = 1000
    guilds = (10, 20)
    users = (101, 102, 103)
    categories_credit = ("credit", "reward", "payout")
    categories_debit = ("debit", "wager", "purchase")
    refs: tuple[str | None, ...] = (None, "ref_a", "ref_b")

    stores = {g: UserStore(tmp_db, guild_id=g, starting_balance=starting) for g in guilds}
    expected_bal: dict[tuple[int, int], int] = {}
    for g in guilds:
        for u in users:
            await stores[g].ensure_user(u)
            expected_bal[(g, u)] = starting

    # Track committed operations by key: key -> (op, gid, uid, amt, cat, ref, balance_after)
    committed: dict[str, tuple[str, int, int, int, str, str | None, int]] = {}
    key_counter = 0

    for _step in range(120):
        action = rng.choices(
            ["fresh_credit", "fresh_debit", "exact_retry", "conflict_retry"],
            weights=[35, 35, 15, 15],
            k=1,
        )[0]

        if action in ("exact_retry", "conflict_retry") and not committed:
            action = "fresh_credit"

        if action == "fresh_credit":
            key_counter += 1
            key = f"prop:k:{key_counter}"
            g = rng.choice(guilds)
            u = rng.choice(users)
            amt = rng.randint(10, 300)
            cat = rng.choice(categories_credit)
            ref = rng.choice(refs)
            bal = await stores[g].add_balance(u, amt, category=cat, reference_id=ref, idempotency_key=key)
            expected_bal[(g, u)] += amt
            assert bal == expected_bal[(g, u)]
            committed[key] = ("credit", g, u, amt, cat, ref, bal)

        elif action == "fresh_debit":
            key_counter += 1
            key = f"prop:k:{key_counter}"
            g = rng.choice(guilds)
            u = rng.choice(users)
            amt = rng.randint(10, 1500)
            cat = rng.choice(categories_debit)
            ref = rng.choice(refs)
            if amt > expected_bal[(g, u)]:
                with pytest.raises(InsufficientBalanceError):
                    await stores[g].subtract_balance(
                        u, amt, category=cat, reference_id=ref, idempotency_key=key
                    )
                assert await stores[g].get_balance(u) == expected_bal[(g, u)]
            else:
                bal = await stores[g].subtract_balance(
                    u, amt, category=cat, reference_id=ref, idempotency_key=key
                )
                expected_bal[(g, u)] -= amt
                assert bal == expected_bal[(g, u)]
                committed[key] = ("debit", g, u, amt, cat, ref, bal)

        elif action == "exact_retry":
            key = rng.choice(list(committed.keys()))
            op, g, u, amt, cat, ref, orig_bal = committed[key]
            if op == "credit":
                ret = await stores[g].add_balance(u, amt, category=cat, reference_id=ref, idempotency_key=key)
            else:
                ret = await stores[g].subtract_balance(
                    u, amt, category=cat, reference_id=ref, idempotency_key=key
                )
            assert ret == orig_bal

        else:  # conflict_retry
            key = rng.choice(list(committed.keys()))
            op, g, u, amt, cat, ref, _orig_bal = committed[key]
            mutation_kind = rng.choice(["actor", "guild", "direction", "amount", "category", "reference"])
            c_op, c_g, c_u, c_amt, c_cat, c_ref = op, g, u, amt, cat, ref
            if mutation_kind == "actor":
                c_u = rng.choice([x for x in users if x != u])
            elif mutation_kind == "guild":
                c_g = rng.choice([x for x in guilds if x != g])
            elif mutation_kind == "direction":
                c_op = "debit" if op == "credit" else "credit"
            elif mutation_kind == "amount":
                c_amt = amt + rng.randint(1, 50)
            elif mutation_kind == "category":
                c_cat = f"{cat}_mutated"
            else:
                c_ref = "ref_mutated" if ref != "ref_mutated" else None

            with pytest.raises(IdempotencyConflict):
                if c_op == "credit":
                    await stores[c_g].add_balance(
                        c_u, c_amt, category=c_cat, reference_id=c_ref, idempotency_key=key
                    )
                else:
                    await stores[c_g].subtract_balance(
                        c_u, c_amt, category=c_cat, reference_id=c_ref, idempotency_key=key
                    )

    # Final conservation assertions across every (guild, user)
    total_rows = await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger")
    assert total_rows == (len(committed),)

    for g in guilds:
        for u in users:
            actual_bal = await stores[g].get_balance(u)
            assert actual_bal == expected_bal[(g, u)]
            assert actual_bal >= 0

            rows = await tmp_db.fetchall(
                "SELECT amount, balance_after FROM economy_ledger WHERE guild_id=? AND user_id=? ORDER BY id ASC",
                (g, u),
            )
            running = starting
            for amt_delta, bal_after in rows:
                running += int(amt_delta)
                assert int(bal_after) == running
            assert running == actual_bal
