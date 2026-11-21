"""tests/unit/test_privacy_model.py — Specification tests for export, deletion, and anonymization.

Validates the two-tier privacy model against an isolated in-memory SQLite database (:memory:):
1. Tier 1: Hard deletion of PII and user-specific state (users, blackjack_users, fun_positions).
2. Tier 2: Anonymization and tombstoning of audit records and shared schedules (fun_trades, exams).
"""

from __future__ import annotations

import sqlite3
import unittest

try:
    import pytest

    pytestmark = pytest.mark.unit
except ImportError:  # pragma: no cover - fallback when executed via stdlib unittest only
    pass


def init_test_db(conn: sqlite3.Connection) -> None:
    """Initialize schema matching icbot/db.py."""
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE users (
            user_id        INTEGER PRIMARY KEY,
            display_name   TEXT    NOT NULL DEFAULT '',
            avatar_url     TEXT    NOT NULL DEFAULT '',
            balance        INTEGER NOT NULL DEFAULT 1000,
            created_at     INTEGER NOT NULL DEFAULT 0,
            daily_claim_ts INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE blackjack_users (
            user_id INTEGER PRIMARY KEY,
            bankroll INTEGER NOT NULL,
            hands_played INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            pushes INTEGER NOT NULL DEFAULT 0,
            reset_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE fun_positions (
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            avg_price REAL NOT NULL,
            collateral_locked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, symbol, side)
        );

        CREATE TABLE fun_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price INTEGER NOT NULL,
            cash_delta INTEGER NOT NULL,
            realized_pnl INTEGER NOT NULL DEFAULT 0,
            note TEXT
        );

        CREATE TABLE exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_code TEXT NOT NULL,
            course_name TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER,
            location TEXT,
            notes TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def export_user_data(conn: sqlite3.Connection, user_id: int) -> dict:
    """Testable export routine simulating /ic privacy export."""
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts FROM users WHERE user_id = ?",
        (user_id,),
    )
    user_row = cur.fetchone()
    if not user_row:
        return {}

    cur.execute(
        "SELECT bankroll, hands_played, wins, losses, pushes, reset_count FROM blackjack_users WHERE user_id = ?",
        (user_id,),
    )
    bj_row = cur.fetchone()

    cur.execute(
        "SELECT symbol, side, qty, avg_price, collateral_locked FROM fun_positions WHERE user_id = ?",
        (user_id,),
    )
    positions = [
        {"symbol": r[0], "side": r[1], "qty": r[2], "avg_price": r[3], "collateral": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT id, ts_unix, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades WHERE user_id = ?",
        (user_id,),
    )
    trades = [
        {
            "id": r[0],
            "ts": r[1],
            "symbol": r[2],
            "side": r[3],
            "qty": r[4],
            "price": r[5],
            "cash_delta": r[6],
            "pnl": r[7],
            "note": r[8],
        }
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT id, course_code, course_name, start_ts, location FROM exams WHERE created_by = ?",
        (user_id,),
    )
    exams = [
        {"id": r[0], "course_code": r[1], "course_name": r[2], "start_ts": r[3], "location": r[4]}
        for r in cur.fetchall()
    ]

    return {
        "user_id": user_row[0],
        "display_name": user_row[1],
        "avatar_url": user_row[2],
        "economy": {
            "balance": user_row[3],
            "created_at": user_row[4],
            "daily_claim_ts": user_row[5],
            "blackjack": {
                "bankroll": bj_row[0] if bj_row else 0,
                "hands_played": bj_row[1] if bj_row else 0,
                "wins": bj_row[2] if bj_row else 0,
                "losses": bj_row[3] if bj_row else 0,
            }
            if bj_row
            else None,
        },
        "fun_market": {
            "positions": positions,
            "trades": trades,
        },
        "exams_created": exams,
    }


def delete_and_anonymize_user(conn: sqlite3.Connection, user_id: int) -> dict:
    """Execute two-tier atomic deletion and anonymization transaction."""
    cur = conn.cursor()
    cur.execute("BEGIN TRANSACTION")
    try:
        # Tier 1: Hard delete personal PII and direct balances
        cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        users_deleted = cur.rowcount

        cur.execute("DELETE FROM blackjack_users WHERE user_id = ?", (user_id,))
        bj_deleted = cur.rowcount

        cur.execute("DELETE FROM fun_positions WHERE user_id = ?", (user_id,))
        positions_deleted = cur.rowcount

        # Tier 2: Anonymize transactional audit ledgers and community items
        cur.execute(
            "UPDATE fun_trades SET user_id = 0, note = '[ANONYMIZED_USER]' WHERE user_id = ?",
            (user_id,),
        )
        trades_anonymized = cur.rowcount

        cur.execute(
            "UPDATE exams SET created_by = NULL WHERE created_by = ?",
            (user_id,),
        )
        exams_anonymized = cur.rowcount

        conn.commit()
        return {
            "users_deleted": users_deleted,
            "bj_deleted": bj_deleted,
            "positions_deleted": positions_deleted,
            "trades_anonymized": trades_anonymized,
            "exams_anonymized": exams_anonymized,
        }
    except Exception:
        conn.rollback()
        raise


class TestPrivacyModel(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_test_db(self.conn)

        self.user_a = 987654321012345678
        self.user_b = 111111111111111111

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id, display_name, avatar_url, balance, created_at, daily_claim_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.user_a,
                "SyntheticAlice",
                "https://cdn.discordapp.com/avatars/alice.png",
                2500,
                1700000000,
                1700086400,
            ),
        )
        cur.execute(
            "INSERT INTO blackjack_users (user_id, bankroll, hands_played, wins, losses, pushes, reset_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.user_a, 2500, 20, 12, 7, 1, 0),
        )
        cur.execute(
            "INSERT INTO fun_positions (user_id, symbol, side, qty, avg_price, collateral_locked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.user_a, "TEST", "long", 10, 100.0, 0),
        )
        cur.execute(
            "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1700000100, self.user_a, "TEST", "buy", 10, 100, -1000, 0, "Alice initial trade"),
        )
        cur.execute(
            "INSERT INTO exams (course_code, course_name, start_ts, location, notes, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "COE817",
                "Applied Cryptography",
                1715000000,
                "ENG101",
                "Formula sheet allowed",
                self.user_a,
                1700000000,
            ),
        )

        cur.execute(
            "INSERT INTO users (user_id, display_name, avatar_url, balance, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.user_b, "SyntheticBob", "https://cdn.discordapp.com/avatars/bob.png", 1000, 1700000000),
        )
        cur.execute(
            "INSERT INTO fun_trades (ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1700000200, self.user_b, "TEST", "buy", 5, 100, -500, 0, "Bob trade"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_export_data(self):
        """Verify export routine captures complete user profile and state."""
        data = export_user_data(self.conn, self.user_a)
        self.assertEqual(data["user_id"], self.user_a)
        self.assertEqual(data["display_name"], "SyntheticAlice")
        self.assertEqual(data["economy"]["balance"], 2500)
        self.assertEqual(len(data["fun_market"]["positions"]), 1)
        self.assertEqual(len(data["fun_market"]["trades"]), 1)
        self.assertEqual(len(data["exams_created"]), 1)

    def test_deletion_and_anonymization(self):
        """Verify Tier 1 hard deletion and Tier 2 tombstone anonymization."""
        summary = delete_and_anonymize_user(self.conn, self.user_a)

        self.assertEqual(summary["users_deleted"], 1)
        self.assertEqual(summary["bj_deleted"], 1)
        self.assertEqual(summary["positions_deleted"], 1)
        self.assertEqual(summary["trades_anonymized"], 1)
        self.assertEqual(summary["exams_anonymized"], 1)

        cur = self.conn.cursor()

        cur.execute("SELECT * FROM users WHERE user_id = ?", (self.user_a,))
        self.assertIsNone(cur.fetchone())

        cur.execute("SELECT * FROM blackjack_users WHERE user_id = ?", (self.user_a,))
        self.assertIsNone(cur.fetchone())

        cur.execute("SELECT * FROM fun_positions WHERE user_id = ?", (self.user_a,))
        self.assertEqual(len(cur.fetchall()), 0)

        cur.execute(
            "SELECT user_id, symbol, qty, price, note FROM fun_trades WHERE symbol = 'TEST' ORDER BY id ASC"
        )
        trades = cur.fetchall()
        self.assertEqual(len(trades), 2, "Total trade records in ledger must remain intact")

        self.assertEqual(trades[0][0], 0, "Alice user_id must be zeroed")
        self.assertEqual(trades[0][4], "[ANONYMIZED_USER]", "Note containing PII must be scrubbed")
        self.assertEqual(trades[0][2], 10, "Trade quantity must be preserved for volume")
        self.assertEqual(trades[0][3], 100, "Trade price must be preserved")

        self.assertEqual(trades[1][0], self.user_b, "Bob user_id must be untouched")
        self.assertEqual(trades[1][4], "Bob trade")

        cur.execute("SELECT course_code, created_by FROM exams WHERE course_code = 'COE817'")
        exam = cur.fetchone()
        self.assertIsNotNone(exam)
        self.assertIsNone(exam[1], "Exam created_by must be set to NULL")

    def test_nonexistent_user_deletion(self):
        """Verify deletion routine handles non-existent users gracefully without errors."""
        summary = delete_and_anonymize_user(self.conn, 999999999999999999)
        self.assertEqual(summary["users_deleted"], 0)
        self.assertEqual(summary["trades_anonymized"], 0)


# ---------------------------------------------------------------------------
# Comprehensive Production Schema Privacy Contract Tests (UserStore + Database)
# ---------------------------------------------------------------------------


GUILD_ALPHA = 900_000_000_000_000_001
GUILD_BETA = 900_000_000_000_000_002
USER_ALICE = 800_000_000_000_000_111
USER_BOB = 800_000_000_000_000_222


async def _populate_two_guilds_two_users_all_tables(db):
    """Populate every user-addressable and system table across two guilds and two users."""
    from icbot.academics.catalog import ManualResourceCatalogStore
    from icbot.academics.exams_service import ExamStore, ParsedExamDraft
    from icbot.guild_config import GuildSettingsStore
    from icbot.services.fun_market import FunMarketStore
    from icbot.services.user_service import UserStore

    exam_store = ExamStore(db, clock=lambda: 1_760_000_000.0)
    catalog_store = ManualResourceCatalogStore(db, clock=lambda: 1_760_000_000.0)
    gs_store = GuildSettingsStore(db, clock=lambda: 1_760_000_000.0)

    for gid in (GUILD_ALPHA, GUILD_BETA):
        u_store = UserStore(db, guild_id=gid, starting_balance=10_000, clock=lambda: 1_760_000_000.0)
        m_store = FunMarketStore(db, guild_id=gid, starting_bankroll=10_000, clock=lambda: 1_760_000_000.0)
        await m_store.sync_catalog(guild_id=gid)

        for uid, name in ((USER_ALICE, f"Alice_{gid}"), (USER_BOB, f"Bob_{gid}")):
            await u_store.ensure_user(uid, display_name=name, avatar_url=f"https://example.invalid/{uid}.png")
            await db.execute(
                """
                INSERT OR REPLACE INTO blackjack_users
                (guild_id, user_id, bankroll, hands_played, wins, losses, pushes, reset_count)
                VALUES (?, ?, 10000, 12, 7, 4, 1, 0)
                """,
                (gid, uid),
            )
            # Open LONG and SHORT positions
            await m_store.buy_long(user_id=uid, symbol="IC", qty=2, guild_id=gid)
            await m_store.open_short(user_id=uid, symbol="IC", qty=1, guild_id=gid)

            # Seed 130 additional trades and 130 additional ledger entries (>100 to test pagination)
            trade_stmts = [
                (
                    """
                    INSERT INTO fun_trades
                    (guild_id, ts_unix, user_id, symbol, side, qty, price, cash_delta, realized_pnl, note)
                    VALUES (?, ?, ?, 'IC', 'BUY', 1, 10, -10, 0, ?)
                    """,
                    (gid, 1_760_000_100 + i, uid, f"Trade #{i} by {uid}"),
                )
                for i in range(130)
            ]
            await db.execute_many(trade_stmts)

            ledger_stmts = []
            for i in range(130):
                tx_id = f"bj_bet:{gid}:{uid}:1760000000:{i}"
                ref_id = f"ref:{gid}:{uid}:{i}" if i % 2 == 0 else f"bj_bet:{gid}:{uid}:1760000000:{i - 1}"
                ledger_stmts.append(
                    (
                        """
                        INSERT INTO economy_ledger
                        (transaction_id, guild_id, user_id, amount, balance_after, category, reference_id, created_at)
                        VALUES (?, ?, ?, ?, 10000, 'bonus', ?, ?)
                        """,
                        (tx_id, gid, uid, 5, ref_id, 1_760_000_100 + i),
                    )
                )
            await db.execute_many(ledger_stmts)

            # Exams created by user
            await exam_store.add_exam(
                guild_id=gid,
                draft=ParsedExamDraft(
                    course_code=f"CPS{uid % 1000}",
                    course_name=f"Course by {name}",
                    start_ts=1_770_000_000 + (uid % 1000),
                    end_ts=1_770_003_600 + (uid % 1000),
                ),
                created_by=uid,
            )

            # Academic resources contributed by user
            await catalog_store.add_resource(
                guild_id=gid,
                course_code=f"CPS{uid % 1000}",
                title=f"Study Guide by {name}",
                url=f"https://docs.example.invalid/{gid}/{uid}",
                added_by=uid,
            )

            # Stock events & market week settlement
            await m_store.admin_move(
                symbol="IC",
                delta=1,
                user_id=uid,
                reason=f"Move by {uid}",
                guild_id=gid,
            )
            await db.execute(
                "INSERT INTO stock_events (guild_id, ts_unix, user_id, delta, new_value, reason) VALUES (?, ?, ?, 1, 12, ?)",
                (gid, 1_760_000_500, uid, f"Legacy event by {uid}"),
            )

            # Admin audit log where user is target_user_id
            await db.execute(
                """
                INSERT INTO admin_audit_log
                (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                VALUES (?, ?, 'balance_grant', ?, ?, 1760000600)
                """,
                (
                    gid,
                    USER_BOB if uid == USER_ALICE else USER_ALICE,
                    uid,
                    f'{{"target":{uid},"note":"granted to {uid}"}}',
                ),
            )

            # Guild settings audit where user is actor_id
            await gs_store.update_overrides(
                gid,
                {"daily_claim_amount": 120 + (uid % 10)},
                actor_id=uid,
            )

        # Week rollover settled by Alice in each guild
        await m_store.week_rollover(user_id=USER_ALICE, note="Weekly settlement", guild_id=gid)

        # Active guild job subscription
        await db.set_guild_job_subscription(gid, channel_id=gid + 50, preset="tech_swe", enabled=True)

    # Cross-guild global job preferences for Alice and Bob
    await db.set_user_job_preferences(
        USER_ALICE,
        preset="tech_swe",
        locations=("Toronto", "Remote"),
        keywords_allow=("python",),
        remote_only=True,
        now=1_760_000_000,
    )
    await db.set_user_job_preferences(
        USER_BOB,
        preset="business_finance",
        locations=("New York",),
        keywords_allow=("analyst",),
        remote_only=False,
        now=1_760_000_000,
    )


@pytest.mark.asyncio
async def test_export_user_data_paginated_completeness_and_bounded_truncation_warnings(tmp_path):
    """Verify export_user_data exports >100 rows without silent truncation and warns when bounded."""
    from icbot.db import Database
    from icbot.services.user_service import UserStore

    db = Database(str(tmp_path / "privacy_export.db"))
    await db.init()
    try:
        await _populate_two_guilds_two_users_all_tables(db)
        store_alpha = UserStore(db, guild_id=GUILD_ALPHA, clock=lambda: 1_760_100_000.0)

        # 1. Full export using small batch_size=25 to exercise multi-page iteration
        full_export = await store_alpha.export_user_data(
            USER_ALICE,
            guild_id=GUILD_ALPHA,
            batch_size=25,
            max_rows_per_table=5000,
        )
        assert full_export["format_version"] == 2
        assert full_export["guild_id"] == GUILD_ALPHA
        assert full_export["user_id"] == USER_ALICE
        assert full_export["truncated"] is False
        assert full_export["warnings"] == []
        assert full_export["profile"] is not None
        assert full_export["blackjack"] is not None
        assert len(full_export["positions"]) == 2  # 1 LONG + 1 SHORT
        assert len(full_export["trades"]) == 132  # 2 initial + 130 seeded (> 100 legacy cap!)
        assert len(full_export["ledger"]) == 132  # 2 initial + 130 seeded (> 100 legacy cap!)
        assert len(full_export["exams_created"]) == 1
        assert len(full_export["academic_resources_added"]) == 1
        assert full_export["job_preferences"] is not None
        assert full_export["job_preferences"]["scope"] == "global_user"
        assert full_export["job_preferences"]["preset"] == "tech_swe"

        # Verify deterministic ordering (descending by timestamp/id for trades & ledger)
        trade_ids = [t["id"] for t in full_export["trades"]]
        assert trade_ids == sorted(trade_ids, reverse=True)
        ledger_ids = [le["id"] for le in full_export["ledger"]]
        assert ledger_ids == sorted(ledger_ids, reverse=True)

        # 2. Intentionally bounded export (max_rows_per_table=50) emits honest truncation metadata & warnings
        bounded_export = await store_alpha.export_user_data(
            USER_ALICE,
            guild_id=GUILD_ALPHA,
            batch_size=20,
            max_rows_per_table=50,
        )
        assert bounded_export["truncated"] is True
        assert len(bounded_export["trades"]) == 50
        assert len(bounded_export["ledger"]) == 50
        assert bounded_export["pagination"]["total_counts"]["trades"] == 132
        assert bounded_export["pagination"]["total_counts"]["ledger"] == 132
        assert bounded_export["pagination"]["truncated_tables"]["trades"] is True
        assert bounded_export["pagination"]["truncated_tables"]["ledger"] is True
        assert bounded_export["pagination"]["truncated_tables"]["positions"] is False
        assert len(bounded_export["warnings"]) == 2
        assert any("trades" in w and "50 of 132" in w for w in bounded_export["warnings"])
        assert any("ledger" in w and "50 of 132" in w for w in bounded_export["warnings"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exhaustive_multi_guild_deletion_anonymization_supply_invariants_and_idempotence(tmp_path):
    """Verify delete_user_data across two guilds, two users, long ledgers, subscriptions, and audits."""
    from icbot.db import Database
    from icbot.services.user_service import UserStore

    db = Database(str(tmp_path / "privacy_delete.db"))
    await db.init()
    try:
        await _populate_two_guilds_two_users_all_tables(db)
        store_alpha = UserStore(db, guild_id=GUILD_ALPHA, clock=lambda: 1_760_200_000.0)
        store_beta = UserStore(db, guild_id=GUILD_BETA, clock=lambda: 1_760_200_000.0)

        # Capture pre-deletion aggregate ledger sums and row counts for both guilds
        pre_alpha_ledger_sum = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
            (GUILD_ALPHA,),
        )
        pre_beta_ledger_sum = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
            (GUILD_BETA,),
        )
        pre_alpha_trade_vol = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(qty), 0), COALESCE(SUM(cash_delta), 0) FROM fun_trades WHERE guild_id=?",
            (GUILD_ALPHA,),
        )

        # ------------------------------------------------------------------
        # Phase 1: Delete Alice ONLY in Guild Alpha (default cross-guild & admin audit policy)
        # ------------------------------------------------------------------
        counts_alpha = await store_alpha.delete_user_data(
            USER_ALICE,
            guild_id=GUILD_ALPHA,
            anonymize_ledger=True,
            delete_global_preferences=False,
            retain_admin_actor_audit=True,
        )

        assert counts_alpha["users"] == 1
        assert counts_alpha["blackjack_users"] == 1
        assert counts_alpha["fun_positions"] == 2
        assert counts_alpha["fun_trades_anonymized"] == 132
        assert counts_alpha["exams_anonymized"] == 1
        assert counts_alpha["academic_resources_anonymized"] == 1
        assert counts_alpha["fun_stock_events_anonymized"] == 1
        assert counts_alpha["stock_events_anonymized"] == 1
        assert counts_alpha["fun_market_weeks_anonymized"] == 1
        assert counts_alpha["economy_ledger_preserved"] == 132
        assert counts_alpha["economy_ledger_anonymized"] == 132
        assert counts_alpha["admin_audit_target_anonymized"] == 1
        # Alice still has an active profile in Guild Beta, so global job preferences are preserved!
        assert counts_alpha["user_job_preferences"] == 0
        assert counts_alpha["user_job_preferences_retained_cross_guild"] == 1
        assert await db.get_user_job_preferences(USER_ALICE) is not None

        # 1. Verify hard-deleted rows in Guild Alpha for Alice
        assert await store_alpha.get_profile(USER_ALICE) is None
        assert (
            await db.fetchone(
                "SELECT 1 FROM blackjack_users WHERE guild_id=? AND user_id=?", (GUILD_ALPHA, USER_ALICE)
            )
            is None
        )
        assert (
            await db.fetchone(
                "SELECT 1 FROM fun_positions WHERE guild_id=? AND user_id=?", (GUILD_ALPHA, USER_ALICE)
            )
            is None
        )

        # 2. Verify economy_ledger in Guild Alpha has ZERO rows with Alice's user_id or embedded uid string
        alice_uid_str = str(USER_ALICE)
        alpha_alice_ledger = await db.fetchall(
            "SELECT id, user_id, transaction_id, reference_id FROM economy_ledger "
            "WHERE guild_id=? AND (user_id=? OR transaction_id LIKE ? OR COALESCE(reference_id, '') LIKE ?)",
            (GUILD_ALPHA, USER_ALICE, f"%{alice_uid_str}%", f"%{alice_uid_str}%"),
        )
        assert alpha_alice_ledger == []

        # Verify internal referral linkage between consecutive ledger entries survived scrubbing
        anon_ref_matches = await db.fetchone(
            """
            SELECT COUNT(1) FROM economy_ledger a
            JOIN economy_ledger b ON a.reference_id = b.transaction_id
            WHERE a.guild_id = ? AND a.user_id = 0 AND b.user_id = 0
            """,
            (GUILD_ALPHA,),
        )
        assert anon_ref_matches is not None and int(anon_ref_matches[0]) == 65

        # 3. Verify aggregate ledger token supply and trade volume in Guild Alpha are 100% unchanged
        post_alpha_ledger_sum = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
            (GUILD_ALPHA,),
        )
        assert post_alpha_ledger_sum == pre_alpha_ledger_sum

        post_alpha_trade_vol = await db.fetchone(
            "SELECT COUNT(1), COALESCE(SUM(qty), 0), COALESCE(SUM(cash_delta), 0) FROM fun_trades WHERE guild_id=?",
            (GUILD_ALPHA,),
        )
        assert post_alpha_trade_vol == pre_alpha_trade_vol
        assert (
            await db.fetchone(
                "SELECT 1 FROM fun_trades WHERE guild_id=? AND (user_id=? OR COALESCE(note, '') LIKE ?)",
                (GUILD_ALPHA, USER_ALICE, f"%{alice_uid_str}%"),
            )
            is None
        )

        # 4. Verify shared exams, resources, stock events, and market weeks in Guild Alpha are de-identified
        exam_row = await db.fetchone(
            "SELECT created_by, course_code FROM exams WHERE guild_id=? AND course_code=?",
            (GUILD_ALPHA, f"CPS{USER_ALICE % 1000}"),
        )
        assert exam_row == (0, f"CPS{USER_ALICE % 1000}")

        res_row = await db.fetchone(
            "SELECT added_by FROM academic_resources WHERE guild_id=? AND course_key=?",
            (GUILD_ALPHA, f"CPS{USER_ALICE % 1000}"),
        )
        assert res_row == (0,)

        fse_row = await db.fetchone(
            "SELECT COUNT(1) FROM fun_stock_events WHERE guild_id=? AND (user_id=? OR COALESCE(reason, '') LIKE ?)",
            (GUILD_ALPHA, USER_ALICE, f"%{alice_uid_str}%"),
        )
        assert fse_row == (0,)

        se_row = await db.fetchone(
            "SELECT COUNT(1) FROM stock_events WHERE guild_id=? AND (user_id=? OR COALESCE(reason, '') LIKE ?)",
            (GUILD_ALPHA, USER_ALICE, f"%{alice_uid_str}%"),
        )
        assert se_row == (0,)

        fmw_row = await db.fetchone(
            "SELECT settled_by FROM fun_market_weeks WHERE guild_id=?",
            (GUILD_ALPHA,),
        )
        assert fmw_row == (None,)

        # 5. Verify admin_audit_log did NOT re-expose Alice's user_id as target_user_id or in details_json
        target_leaks = await db.fetchall(
            "SELECT id, action, target_user_id, details_json FROM admin_audit_log "
            "WHERE guild_id=? AND (target_user_id=? OR details_json LIKE ?)",
            (GUILD_ALPHA, USER_ALICE, f"%{alice_uid_str}%"),
        )
        assert target_leaks == []
        del_receipts = await db.fetchall(
            "SELECT target_user_id, details_json FROM admin_audit_log WHERE guild_id=? AND action='user_data_delete'",
            (GUILD_ALPHA,),
        )
        assert len(del_receipts) == 1
        assert del_receipts[0][0] is None
        assert "erasure_receipt" in str(del_receipts[0][1])
        assert alice_uid_str not in str(del_receipts[0][1])

        # Privileged admin actor IDs were retained under retain_admin_actor_audit=True
        actor_rows = await db.fetchall(
            "SELECT 1 FROM admin_audit_log WHERE guild_id=? AND admin_user_id=?",
            (GUILD_ALPHA, USER_ALICE),
        )
        assert len(actor_rows) >= 1

        # 6. Verify ZERO cross-guild loss for Alice in Guild Beta and ZERO impact on Bob in Guild Alpha
        assert await store_beta.get_profile(USER_ALICE) is not None
        assert await store_alpha.get_profile(USER_BOB) is not None
        bob_alpha_export = await store_alpha.export_user_data(USER_BOB, guild_id=GUILD_ALPHA)
        assert len(bob_alpha_export["trades"]) == 132
        assert len(bob_alpha_export["ledger"]) == 132
        assert len(bob_alpha_export["positions"]) == 2

        alice_beta_export = await store_beta.export_user_data(USER_ALICE, guild_id=GUILD_BETA)
        assert len(alice_beta_export["trades"]) == 132
        assert len(alice_beta_export["ledger"]) == 132
        assert len(alice_beta_export["positions"]) == 2
        assert (
            await db.fetchone(
                "SELECT COUNT(1), COALESCE(SUM(amount), 0) FROM economy_ledger WHERE guild_id=?",
                (GUILD_BETA,),
            )
            == pre_beta_ledger_sum
        )

        # Guild job subscriptions in both guilds remain active
        assert len(await db.get_guild_job_subscriptions(GUILD_ALPHA)) == 1
        assert len(await db.get_guild_job_subscriptions(GUILD_BETA)) == 1

        # ------------------------------------------------------------------
        # Phase 2: Idempotence — calling delete_user_data again for Alice in Guild Alpha is a clean no-op
        # ------------------------------------------------------------------
        idem_counts = await store_alpha.delete_user_data(USER_ALICE, guild_id=GUILD_ALPHA)
        assert idem_counts["users"] == 0
        assert idem_counts["blackjack_users"] == 0
        assert idem_counts["fun_positions"] == 0
        assert idem_counts["fun_trades_anonymized"] == 0
        assert idem_counts["economy_ledger_anonymized"] == 0
        # No duplicate user_data_delete receipt inserted on no-op
        del_receipts_after_idem = await db.fetchall(
            "SELECT id FROM admin_audit_log WHERE guild_id=? AND action='user_data_delete'",
            (GUILD_ALPHA,),
        )
        assert len(del_receipts_after_idem) == 1

        # ------------------------------------------------------------------
        # Phase 3: Delete Alice in Guild Beta (last remaining guild + retain_admin_actor_audit=False)
        # ------------------------------------------------------------------
        counts_beta = await store_beta.delete_user_data(
            USER_ALICE,
            guild_id=GUILD_BETA,
            anonymize_ledger=True,
            retain_admin_actor_audit=False,
        )
        assert counts_beta["users"] == 1
        # Because Alice now has 0 remaining guild profiles, global user_job_preferences is automatically deleted
        assert counts_beta["user_job_preferences"] == 1
        assert await db.get_user_job_preferences(USER_ALICE) is None
        # Bob's global job preferences remain untouched
        assert await db.get_user_job_preferences(USER_BOB) is not None
        # Admin actor IDs in Guild Beta were also zeroed
        assert counts_beta["admin_audit_actor_anonymized"] >= 1
        assert counts_beta["guild_settings_audit_actor_anonymized"] >= 1
        assert (
            await db.fetchone(
                "SELECT 1 FROM admin_audit_log WHERE guild_id=? AND admin_user_id=?",
                (GUILD_BETA, USER_ALICE),
            )
            is None
        )
        assert (
            await db.fetchone(
                "SELECT 1 FROM guild_settings_audit WHERE guild_id=? AND actor_id=?",
                (GUILD_BETA, USER_ALICE),
            )
            is None
        )

        # Foreign key and B-tree integrity check across the entire database
        await db.check_integrity()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_user_data_atomic_rollback_on_mid_transaction_crash(tmp_path, monkeypatch):
    """Verify that an exception mid-deletion rolls back all tables cleanly with zero partial data loss."""
    from icbot.db import Database, Transaction
    from icbot.services.user_service import UserStore

    db = Database(str(tmp_path / "privacy_rollback.db"))
    await db.init()
    try:
        await _populate_two_guilds_two_users_all_tables(db)
        store_alpha = UserStore(db, guild_id=GUILD_ALPHA, clock=lambda: 1_760_300_000.0)

        orig_execute = Transaction.execute

        async def _faulty_execute(self_tx, sql: str, params=()):
            if "UPDATE economy_ledger SET user_id=?" in sql:
                raise RuntimeError("Simulated mid-deletion crash during ledger anonymization")
            return await orig_execute(self_tx, sql, params)

        monkeypatch.setattr(Transaction, "execute", _faulty_execute)

        with pytest.raises(RuntimeError, match="Simulated mid-deletion crash"):
            await store_alpha.delete_user_data(USER_ALICE, guild_id=GUILD_ALPHA, anonymize_ledger=True)

        monkeypatch.setattr(Transaction, "execute", orig_execute)

        # Verify full rollback: Alice's profile, blackjack stats, positions, trades, and ledger are intact
        prof = await store_alpha.get_profile(USER_ALICE)
        assert prof is not None
        assert prof.display_name == f"Alice_{GUILD_ALPHA}"

        export_after_crash = await store_alpha.export_user_data(USER_ALICE, guild_id=GUILD_ALPHA)
        assert export_after_crash["blackjack"] is not None
        assert len(export_after_crash["positions"]) == 2
        assert len(export_after_crash["trades"]) == 132
        assert len(export_after_crash["ledger"]) == 132
        assert len(export_after_crash["exams_created"]) == 1
        assert len(export_after_crash["academic_resources_added"]) == 1
        await db.check_integrity()
    finally:
        await db.close()
