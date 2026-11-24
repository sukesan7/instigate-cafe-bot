"""Targeted privacy erasure, unlinkable tombstone, referential safety, backup, and migration tests.

All tests execute strictly against ephemeral temporary SQLite databases (``tmp_path``) with zero
production database, Discord gateway, or network access.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from icbot.academics.catalog import ManualResourceCatalogStore
from icbot.academics.exams_service import ExamStore, ParsedExamDraft
from icbot.casino.blackjack_store import BlackjackStore
from icbot.db import Database
from icbot.errors import IdempotencyConflict
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import (
    ANONYMIZED_USER_ID,
    UserStore,
    _scrub_uid_token,
)
from tools.backup_db import backup_database, verify_sqlite_integrity

pytestmark = pytest.mark.storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ALPHA_HEX = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
_LEGACY_ANON_RE = re.compile(r"anon_[a-p]{12}")


def _legacy_sha256_token(uid: int, guild_id: int) -> str:
    digest = hashlib.sha256(f"icbot-anon:{guild_id}:{uid}".encode()).hexdigest()[:12].translate(_ALPHA_HEX)
    return f"anon_{digest}"


# ---------------------------------------------------------------------------
# 1. Unpredictable Per-Erasure / Per-Row Tombstones (No Reidentification Map)
# ---------------------------------------------------------------------------


async def test_erasure_tokens_are_unpredictable_non_deterministic_and_unlinkable(tmp_path: Path) -> None:
    """Deleting the same (guild_id, user_id) in two DBs produces distinct CSPRNG tokens with no stored map."""
    gid = 500_000_000_000_000_001
    uid = 400_000_000_000_000_123
    legacy_hash = _legacy_sha256_token(uid, gid)

    observed_tx_sets: list[set[str]] = []
    observed_audit_details: list[str] = []

    for run_idx in range(2):
        db = Database(str(tmp_path / f"unpredictable_{run_idx}.db"))
        await db.init()
        try:
            store = UserStore(db, guild_id=gid, starting_balance=1000, clock=lambda: 1_760_000_000.0)
            await store.ensure_user(uid, display_name="TargetUser")
            await store.add_balance(uid, 200, category="reward", idempotency_key=f"credit:{gid}:{uid}:1")
            await store.add_balance(uid, 300, category="reward", idempotency_key=f"credit:{gid}:{uid}:2")
            await store.subtract_balance(uid, 100, category="wager", idempotency_key=f"debit:{gid}:{uid}:3")
            await db.execute(
                """
                INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                VALUES (?, 999999, 'grant', ?, ?, 1760000010)
                """,
                (gid, uid, json.dumps({"target": uid, "note": f"granted to {uid}"})),
            )

            await store.delete_user_data(uid, guild_id=gid, anonymize_ledger=True)

            rows = await db.fetchall(
                "SELECT transaction_id, user_id FROM economy_ledger WHERE guild_id=? ORDER BY id ASC",
                (gid,),
            )
            assert len(rows) == 3
            tx_ids = {str(r[0]) for r in rows}
            assert all(int(r[1]) == ANONYMIZED_USER_ID for r in rows)
            # Zero raw uid and zero predictable SHA-256 hash tokens
            for tx_id in tx_ids:
                assert str(uid) not in tx_id
                assert legacy_hash not in tx_id
                assert tx_id.startswith("tombstone:")
                assert not _LEGACY_ANON_RE.search(tx_id)

            # Distinct per-row tombstones (unrelated rows share NO common per-user pseudonym)
            assert len(tx_ids) == 3
            observed_tx_sets.append(tx_ids)

            audit_row = await db.fetchone(
                "SELECT target_user_id, details_json FROM admin_audit_log WHERE guild_id=? AND action='grant'",
                (gid,),
            )
            assert audit_row is not None
            assert audit_row[0] is None
            details_str = str(audit_row[1])
            assert str(uid) not in details_str
            assert legacy_hash not in details_str
            # Must remain valid JSON!
            parsed = json.loads(details_str)
            assert isinstance(parsed["target"], str) and parsed["target"].startswith("del_")
            observed_audit_details.append(details_str)

            # Verify NO reidentification map table or meta key was created
            tables = {
                str(r[0]) for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not any("reident" in t.lower() or "tombstone_map" in t.lower() for t in tables)
            meta_rows = await db.fetchall("SELECT key, value FROM meta")
            assert all(str(uid) not in str(k) and str(uid) not in str(v) for k, v in meta_rows)
        finally:
            await db.close()

    # Across two identical erasures of the same (guild_id, user_id), zero tokens repeat!
    assert observed_tx_sets[0].isdisjoint(observed_tx_sets[1])
    assert observed_audit_details[0] != observed_audit_details[1]


# ---------------------------------------------------------------------------
# 2. Two Deleted Users in Same Guild + Active Survivor + Economic Totals
# ---------------------------------------------------------------------------


async def test_two_deleted_users_same_guild_preserves_totals_uniqueness_and_survivor(tmp_path: Path) -> None:
    """Deleting two users in the same guild preserves UNIQUE constraints, referral links, economic totals, and survivor."""
    db = Database(str(tmp_path / "same_guild_two_users.db"))
    await db.init()
    try:
        gid = 600_000_000_000_000_001
        u1 = 700_000_000_000_000_001
        u2 = 700_000_000_000_000_002
        survivor = 700_000_000_000_000_003

        store = UserStore(db, guild_id=gid, starting_balance=1000, clock=lambda: 1_760_100_000.0)
        bj = BlackjackStore(db, starting_bankroll=1000, guild_id=gid, clock=lambda: 1_760_100_000.0)
        fm = FunMarketStore(db, guild_id=gid, starting_bankroll=1000, clock=lambda: 1_760_100_000.0)
        await fm.sync_catalog(guild_id=gid)

        for uid in (u1, u2, survivor):
            await store.ensure_user(uid, display_name=f"User_{uid}")
            bet_tx = f"bj_bet:bj_round:{gid}:{uid}:1760100000:aabbccdd"
            settle_tx = f"bj_settle:bj_round:{gid}:{uid}:1760100000:aabbccdd"
            await bj.reserve_wager(uid, 100, idempotency_key=bet_tx)
            await bj.apply_delta_and_stats(
                uid, 200, {"win": 1}, hands_played=1, idempotency_key=settle_tx, reference_id=bet_tx
            )
            await fm.buy_long(user_id=uid, symbol="IC", qty=2, guild_id=gid)

        # Also leave an UNSETTLED blackjack reservation for u1 to verify restart recovery won't refund user_id=0
        await bj.reserve_wager(u1, 50, idempotency_key=f"bj_bet:bj_round:{gid}:{u1}:1760100001:deadbeef")

        pre_ledger_totals = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
            (gid,),
        )
        pre_trade_totals = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(qty), 0), COALESCE(SUM(cash_delta), 0) FROM fun_trades WHERE guild_id=?",
            (gid,),
        )

        # Delete u1, then delete u2
        c1 = await store.delete_user_data(u1, guild_id=gid, anonymize_ledger=True)
        c2 = await store.delete_user_data(u2, guild_id=gid, anonymize_ledger=True)
        assert c1["economy_ledger_anonymized"] == 4
        assert c2["economy_ledger_anonymized"] == 3

        # Zero uid remnants for either deleted user
        for uid in (u1, u2):
            leaks = await db.fetchall(
                "SELECT id FROM economy_ledger WHERE guild_id=? AND (user_id=? OR transaction_id LIKE ? OR COALESCE(reference_id, '') LIKE ?)",
                (gid, uid, f"%{uid}%", f"%{uid}%"),
            )
            assert leaks == []

        # Aggregate economic totals & trade volume are 100% preserved
        assert (
            await db.fetchone(
                "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
                (gid,),
            )
            == pre_ledger_totals
        )
        assert (
            await db.fetchone(
                "SELECT COUNT(1), COALESCE(SUM(qty), 0), COALESCE(SUM(cash_delta), 0) FROM fun_trades WHERE guild_id=?",
                (gid,),
            )
            == pre_trade_totals
        )

        # Internal referral links (bet -> settle) survived for BOTH deleted users (2 linked pairs under user_id=0)
        linked_pairs = await db.fetchone(
            """
            SELECT COUNT(1) FROM economy_ledger a
            JOIN economy_ledger b ON a.reference_id = b.transaction_id
            WHERE a.guild_id=? AND a.user_id=0 AND b.user_id=0
            """,
            (gid,),
        )
        assert linked_pairs == (2,)

        # Restart recovery MUST NOT refund deleted u1's abandoned escrow into user_id=0
        unsettled = await bj.find_unsettled_reservations(guild_id=gid)
        assert unsettled == []

        # Active survivor is completely untouched
        surv_prof = await store.get_profile(survivor)
        assert surv_prof is not None and surv_prof.balance == 900
        surv_export = await store.export_user_data(survivor, guild_id=gid)
        assert len(surv_export["ledger"]) == 3
        assert len(surv_export["positions"]) == 1

        await db.check_integrity()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 3. Identical User ID Across Different Guilds + Export After Deletion + Repeated Deletion
# ---------------------------------------------------------------------------


async def test_identical_uid_across_guilds_export_after_deletion_and_repeated_deletion(
    tmp_path: Path,
) -> None:
    """Deleting the same user_id in two guilds yields disjoint tombstones, clean empty exports, and idempotent repeats."""
    db = Database(str(tmp_path / "cross_guild_identical_uid.db"))
    await db.init()
    try:
        g_alpha = 810_000_000_000_000_001
        g_beta = 810_000_000_000_000_002
        uid = 820_000_000_000_000_555

        store_a = UserStore(db, guild_id=g_alpha, starting_balance=1000, clock=lambda: 1_760_200_000.0)
        store_b = UserStore(db, guild_id=g_beta, starting_balance=1000, clock=lambda: 1_760_200_000.0)
        exam_store = ExamStore(db, clock=lambda: 1_760_200_000.0)
        cat_store = ManualResourceCatalogStore(db, clock=lambda: 1_760_200_000.0)

        for g, st in ((g_alpha, store_a), (g_beta, store_b)):
            await st.ensure_user(uid, display_name="MultiGuildUser")
            await st.add_balance(uid, 250, category="grant", idempotency_key=f"credit:{g}:{uid}:1")
            await exam_store.add_exam(
                guild_id=g,
                draft=ParsedExamDraft(
                    course_code="CPS109",
                    course_name="CS I",
                    start_ts=1_770_000_000,
                    end_ts=1_770_003_600,
                ),
                created_by=uid,
            )
            await cat_store.add_resource(
                guild_id=g,
                course_code="CPS109",
                title="Notes",
                url="https://example.invalid/notes",
                added_by=uid,
            )
        await db.set_user_job_preferences(uid, preset="tech_swe", now=1_760_200_000)

        # Delete in Guild Alpha only
        c_alpha = await store_a.delete_user_data(uid, guild_id=g_alpha, anonymize_ledger=True)
        assert c_alpha["users"] == 1
        assert c_alpha["user_job_preferences_retained_cross_guild"] == 1

        # Export in Guild Alpha after deletion is empty for guild tables; global job prefs still retained for Beta
        exp_alpha = await store_a.export_user_data(uid, guild_id=g_alpha)
        assert exp_alpha["profile"] is None
        assert exp_alpha["blackjack"] is None
        assert exp_alpha["positions"] == []
        assert exp_alpha["trades"] == []
        assert exp_alpha["ledger"] == []
        assert exp_alpha["exams_created"] == []
        assert exp_alpha["academic_resources_added"] == []
        assert exp_alpha["job_preferences"] is not None
        assert exp_alpha["truncated"] is False

        # Snapshot Alpha tombstones, then run repeated deletion in Alpha (must be an exact no-op)
        alpha_tx_before_repeat = await db.fetchall(
            "SELECT id, transaction_id, reference_id FROM economy_ledger WHERE guild_id=? ORDER BY id ASC",
            (g_alpha,),
        )
        c_repeat = await store_a.delete_user_data(uid, guild_id=g_alpha, anonymize_ledger=True)
        assert c_repeat["user_job_preferences_retained_cross_guild"] == 1
        assert all(v == 0 for k, v in c_repeat.items() if k != "user_job_preferences_retained_cross_guild")
        assert await db.fetchone(
            "SELECT COUNT(1) FROM admin_audit_log WHERE guild_id=? AND action='user_data_delete'",
            (g_alpha,),
        ) == (1,)
        alpha_tx_after_repeat = await db.fetchall(
            "SELECT id, transaction_id, reference_id FROM economy_ledger WHERE guild_id=? ORDER BY id ASC",
            (g_alpha,),
        )
        assert alpha_tx_after_repeat == alpha_tx_before_repeat

        # Delete in Guild Beta (last remaining guild -> global job preferences now deleted too)
        c_beta = await store_b.delete_user_data(uid, guild_id=g_beta, anonymize_ledger=True)
        assert c_beta["users"] == 1
        assert c_beta["user_job_preferences"] == 1

        # Repeated deletion after last guild erased returns all zeros (including cross-guild prefs)
        c_repeat_beta = await store_b.delete_user_data(uid, guild_id=g_beta, anonymize_ledger=True)
        assert all(v == 0 for v in c_repeat_beta.values())

        exp_beta = await store_b.export_user_data(uid, guild_id=g_beta)
        assert exp_beta["profile"] is None
        assert exp_beta["ledger"] == []
        assert exp_beta["job_preferences"] is None

        # Alpha and Beta tombstones are completely independent (zero cross-guild token reuse)
        alpha_ids = {str(r[1]) for r in alpha_tx_after_repeat}
        beta_rows = await db.fetchall(
            "SELECT transaction_id FROM economy_ledger WHERE guild_id=?",
            (g_beta,),
        )
        beta_ids = {str(r[0]) for r in beta_rows}
        assert alpha_ids.isdisjoint(beta_ids)
        await db.check_integrity()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 4. Exact/Boundary Token Replacement & JSON Integrity
# ---------------------------------------------------------------------------


async def test_exact_boundary_token_replacement_and_valid_audit_json(tmp_path: Path) -> None:
    """Boundary replacement scrubs exact uid tokens without corrupting longer neighboring numeric IDs or JSON."""
    # Direct boundary_only check: uid=1234 must NOT corrupt 12345678 or 91234 or 12340
    raw = "user:1234 guild:12345678 other:91234 mention:<@1234> tail:12340"
    scrubbed = _scrub_uid_token(raw, 1234, guild_id=12345678, boundary_only=True)
    assert scrubbed is not None
    assert "guild:12345678" in scrubbed
    assert "other:91234" in scrubbed
    assert "tail:12340" in scrubbed
    assert "user:del_" in scrubbed
    assert "mention:<@del_" in scrubbed
    assert re.search(r"(?<![0-9])1234(?![0-9])", scrubbed) is None

    # End-to-end admin_audit_log details_json & fun_market_weeks.notes scrubbing
    db = Database(str(tmp_path / "boundary_json.db"))
    await db.init()
    try:
        gid = 12345678
        uid = 1234
        other_admin = 1234567890
        store = UserStore(db, guild_id=gid, starting_balance=1000, clock=lambda: 1_760_300_000.0)
        await store.ensure_user(uid, display_name="ShortUidUser")

        await db.execute(
            """
            INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
            VALUES (?, ?, 'balance_grant', ?, ?, 1760300001)
            """,
            (
                gid,
                other_admin,
                uid,
                json.dumps(
                    {
                        "target": uid,
                        "admin": other_admin,
                        "guild": gid,
                        "note": f"granted to <@{uid}> by {other_admin} in {gid}",
                    }
                ),
            ),
        )
        await db.execute(
            """
            INSERT INTO fun_market_weeks (guild_id, week_key, settled_at, settled_by, notes)
            VALUES (?, '2026-W47', 1760300002, ?, ?)
            """,
            (gid, uid, f"Settled by <@{uid}> for guild {gid}"),
        )

        await store.delete_user_data(uid, guild_id=gid, anonymize_ledger=True)

        audit_row = await db.fetchone(
            "SELECT target_user_id, details_json FROM admin_audit_log WHERE guild_id=? AND action='balance_grant'",
            (gid,),
        )
        assert audit_row is not None and audit_row[0] is None
        parsed = json.loads(str(audit_row[1]))
        assert parsed["admin"] == other_admin  # Longer numeric ID untouched!
        assert parsed["guild"] == gid  # Guild ID untouched!
        assert isinstance(parsed["target"], str) and parsed["target"].startswith("del_")
        assert str(other_admin) in parsed["note"]
        assert str(gid) in parsed["note"]
        assert re.search(r"(?<![0-9])1234(?![0-9])", parsed["note"]) is None

        wk_row = await db.fetchone(
            "SELECT settled_by, notes FROM fun_market_weeks WHERE guild_id=? AND week_key='2026-W47'",
            (gid,),
        )
        assert wk_row is not None and wk_row[0] is None
        assert str(gid) in str(wk_row[1])
        assert re.search(r"(?<![0-9])1234(?![0-9])", str(wk_row[1])) is None
        await db.check_integrity()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 5. Post-Deletion Replay Safety (Opaque Idempotency Keys Fail Closed)
# ---------------------------------------------------------------------------


async def test_opaque_idempotency_key_preserved_as_tombstone_blocks_post_deletion_replay(
    tmp_path: Path,
) -> None:
    """Opaque keys without uid stay unique under user_id=0 so late retries fail closed with IdempotencyConflict."""
    db = Database(str(tmp_path / "opaque_replay_after_delete.db"))
    await db.init()
    try:
        gid = 910
        uid = 920000000000000001
        store = UserStore(db, guild_id=gid, starting_balance=1000)
        await store.ensure_user(uid)

        opaque_key = "external_order_uuid_a1b2c3d4"
        assert await store.add_balance(uid, 500, category="reward", idempotency_key=opaque_key) == 1500

        await store.delete_user_data(uid, guild_id=gid, anonymize_ledger=True)

        # Ledger row now belongs to user_id=0 and retains opaque_key
        row = await db.fetchone(
            "SELECT user_id, transaction_id FROM economy_ledger WHERE transaction_id=?",
            (opaque_key,),
        )
        assert row == (ANONYMIZED_USER_ID, opaque_key)

        # Late retry with the same opaque key MUST fail closed (not resurrect user or double-credit!)
        with pytest.raises(IdempotencyConflict):
            await store.add_balance(uid, 500, category="reward", idempotency_key=opaque_key)
        assert await store.get_profile(uid) is None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 6. Backups & Integrity Verification Around Erasure
# ---------------------------------------------------------------------------


async def test_backup_snapshots_before_and_after_erasure_integrity(tmp_path: Path) -> None:
    """Pre-erasure backup retains prior snapshot; post-erasure backup is clean and passes integrity_check."""
    db_file = tmp_path / "active.db"
    pre_backup = tmp_path / "pre_erasure.db"
    post_backup = tmp_path / "post_erasure.db"

    gid = 301
    uid = 302000000000000001
    db = Database(str(db_file))
    await db.init()
    try:
        store = UserStore(db, guild_id=gid, starting_balance=1000)
        await store.ensure_user(uid, display_name="BackupUser")
        await store.add_balance(uid, 400, category="grant", idempotency_key=f"credit:{gid}:{uid}:1")

        # Online backup BEFORE erasure
        assert backup_database(db_file, pre_backup) is True
        ok_pre, msg_pre = verify_sqlite_integrity(pre_backup)
        assert ok_pre is True and msg_pre == "ok"

        # Erase user in active DB
        await store.delete_user_data(uid, guild_id=gid, anonymize_ledger=True)

        # Online backup AFTER erasure
        assert backup_database(db_file, post_backup) is True
        ok_post, msg_post = verify_sqlite_integrity(post_backup)
        assert ok_post is True and msg_post == "ok"
    finally:
        await db.close()

    # Verify pre-erasure backup still has original user (documented backup retention boundary)
    db_pre = Database(str(pre_backup))
    try:
        assert await db_pre.fetchone(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)
        ) == (1400,)
    finally:
        await db_pre.close()

    # Verify post-erasure backup has zero raw uid remnants and preserves aggregate ledger supply
    db_post = Database(str(post_backup))
    try:
        assert (
            await db_post.fetchone("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid))
            is None
        )
        leaks = await db_post.fetchall(
            "SELECT id FROM economy_ledger WHERE user_id=? OR transaction_id LIKE ?",
            (uid, f"%{uid}%"),
        )
        assert leaks == []
        assert await db_post.fetchone(
            "SELECT COUNT(1), SUM(amount) FROM economy_ledger WHERE guild_id=?", (gid,)
        ) == (
            1,
            400,
        )
        await db_post.check_integrity()
    finally:
        await db_post.close()


# ---------------------------------------------------------------------------
# 7. Forward-Only Legacy Tombstone Migration (Dry-Run + Backup + Approval Gate)
# ---------------------------------------------------------------------------


async def test_forward_only_legacy_tombstone_migration_dry_run_backup_and_approval(tmp_path: Path) -> None:
    """Legacy anon_<sha256> tombstones require dry-run preview, verified backup, and confirmed=True."""
    db_file = tmp_path / "legacy_tombstones.db"
    backup_file = tmp_path / "pre_migration_backup.db"
    gid = 777
    deleted_uid = 888000000000000001
    active_uid = 888000000000000002
    legacy_tok = _legacy_sha256_token(deleted_uid, gid)

    db = Database(str(db_file))
    await db.init()
    try:
        store = UserStore(db, guild_id=gid, starting_balance=1000)
        await store.ensure_user(active_uid, display_name="ActiveUser")
        await store.add_balance(
            active_uid, 300, category="grant", idempotency_key=f"credit:{gid}:{active_uid}:1"
        )

        # Seed legacy SHA-256 tombstoned rows (user_id = 0) with referral link + audit log
        legacy_tx1 = f"bj_bet:{gid}:{legacy_tok}:1760000000:1:a"
        legacy_tx2 = f"bj_settle:{gid}:{legacy_tok}:1760000000:2:b"
        await db.execute(
            """
            INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
            VALUES (?, ?, 0, -100, 900, 'bet', 'blackjack', 1760000000)
            """,
            (legacy_tx1, gid),
        )
        await db.execute(
            """
            INSERT INTO economy_ledger (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
            VALUES (?, ?, 0, 200, 1100, 'payout', ?, 1760000001)
            """,
            (legacy_tx2, gid, legacy_tx1),
        )
        await db.execute(
            """
            INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
            VALUES (?, 999, 'balance_grant', NULL, ?, 1760000002)
            """,
            (gid, f'{{"note":"legacy {legacy_tok}"}}'),
        )

        # 1. Dry-run preview (default) does zero writes
        preview = await store.upgrade_legacy_erasure_tombstones(guild_id=gid, dry_run=True)
        assert preview["mode"] == "dry_run"
        assert preview["legacy_economy_ledger_rows"] == 2
        assert preview["legacy_admin_audit_rows"] == 1
        assert preview["upgraded_economy_ledger_rows"] == 0
        assert (
            await db.fetchone(
                "SELECT transaction_id FROM economy_ledger WHERE transaction_id=?", (legacy_tx1,)
            )
            is not None
        )

        # 2. Executing without confirmed=True fails closed
        with pytest.raises(ValueError, match="confirmed=True"):
            await store.upgrade_legacy_erasure_tombstones(
                guild_id=gid, dry_run=False, confirmed=False, backup_path=backup_file
            )

        # 3. Executing on file-backed DB without backup_path fails closed
        with pytest.raises(ValueError, match="backup_path"):
            await store.upgrade_legacy_erasure_tombstones(
                guild_id=gid, dry_run=False, confirmed=True, backup_path=None
            )

        # 4. Approved forward-only upgrade with backup_path succeeds atomically
        result = await store.upgrade_legacy_erasure_tombstones(
            guild_id=gid, dry_run=False, confirmed=True, backup_path=backup_file
        )
        assert result["mode"] == "applied"
        assert result["upgraded_economy_ledger_rows"] == 2
        assert result["upgraded_admin_audit_rows"] == 1
        assert result["integrity_ok"] is True
        assert backup_file.exists()
        ok_bkp, _ = verify_sqlite_integrity(backup_file)
        assert ok_bkp is True

        # Legacy anon_ tokens are completely gone
        rem_ledger = await db.fetchall(
            "SELECT transaction_id, reference_id FROM economy_ledger WHERE user_id=0 AND guild_id=?",
            (gid,),
        )
        assert len(rem_ledger) == 2
        for tx_id, ref_id in rem_ledger:
            assert not _LEGACY_ANON_RE.search(str(tx_id))
            assert str(tx_id).startswith("tombstone:")
            assert not _LEGACY_ANON_RE.search(str(ref_id or ""))

        # Referral link between the two upgraded rows survived!
        linked = await db.fetchone(
            """
            SELECT COUNT(1) FROM economy_ledger a
            JOIN economy_ledger b ON a.reference_id = b.transaction_id
            WHERE a.guild_id=? AND a.user_id=0 AND b.user_id=0
            """,
            (gid,),
        )
        assert linked == (1,)

        # Aggregate economic total unchanged: -100 + 200 + 300 = 400
        assert await db.fetchone("SELECT SUM(amount) FROM economy_ledger WHERE guild_id=?", (gid,)) == (400,)

        # Active user row is 100% untouched
        active_row = await db.fetchone(
            "SELECT transaction_id FROM economy_ledger WHERE guild_id=? AND user_id=?",
            (gid, active_uid),
        )
        assert active_row == (f"credit:{gid}:{active_uid}:1",)

        # Audit log details_json upgraded and remains valid JSON
        audit_after = await db.fetchone(
            "SELECT details_json FROM admin_audit_log WHERE guild_id=? AND action='balance_grant'",
            (gid,),
        )
        assert audit_after is not None
        assert not _LEGACY_ANON_RE.search(str(audit_after[0]))
        parsed_audit = json.loads(str(audit_after[0]))
        assert "del_" in parsed_audit["note"]

        # 5. Re-running migration is a clean idempotent no-op
        second = await store.upgrade_legacy_erasure_tombstones(
            guild_id=gid, dry_run=False, confirmed=True, backup_path=tmp_path / "bkp2.db"
        )
        assert second["upgraded_economy_ledger_rows"] == 0
        assert second["upgraded_admin_audit_rows"] == 0
        await db.check_integrity()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 8. Documentation Honesty Contract
# ---------------------------------------------------------------------------


def test_privacy_doc_honest_terminology_and_limitations() -> None:
    """docs/privacy.md must never claim SHA-256 hashing is irreversible anonymization."""
    doc = (PROJECT_ROOT / "docs/privacy.md").read_text(encoding="utf-8")
    lower = doc.lower()

    # Must NOT claim SHA-256 hash is irreversible
    assert "irreversible sha-256" not in lower
    assert "irreversible hash" not in lower

    # Must honestly disclose unlinkable tombstones, no reidentification map, and residual linkability
    assert "tombstone" in lower
    assert "reidentification map" in lower or "re-identification map" in lower
    assert "balance_after" in doc
    assert "backup" in lower
    assert "git" in lower
    assert "upgrade_legacy_erasure_tombstones" in doc
