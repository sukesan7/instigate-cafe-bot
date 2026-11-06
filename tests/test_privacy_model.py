"""tests/test_privacy_model.py — Testable specification for export, deletion, and anonymization.

Validates the two-tier privacy model against an isolated in-memory SQLite database (:memory:):
1. Tier 1: Hard deletion of PII and user-specific state (users, blackjack_users, fun_positions).
2. Tier 2: Anonymization and tombstoning of audit records and shared schedules (fun_trades, exams, stock_events).
"""

from __future__ import annotations

import json
import sqlite3
import unittest


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

    cur.execute("SELECT user_id, display_name, avatar_url, balance, created_at, daily_claim_ts FROM users WHERE user_id = ?", (user_id,))
    user_row = cur.fetchone()
    if not user_row:
        return {}

    cur.execute("SELECT bankroll, hands_played, wins, losses, pushes, reset_count FROM blackjack_users WHERE user_id = ?", (user_id,))
    bj_row = cur.fetchone()

    cur.execute("SELECT symbol, side, qty, avg_price, collateral_locked FROM fun_positions WHERE user_id = ?", (user_id,))
    positions = [
        {"symbol": r[0], "side": r[1], "qty": r[2], "avg_price": r[3], "collateral": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute("SELECT id, ts_unix, symbol, side, qty, price, cash_delta, realized_pnl, note FROM fun_trades WHERE user_id = ?", (user_id,))
    trades = [
        {"id": r[0], "ts": r[1], "symbol": r[2], "side": r[3], "qty": r[4], "price": r[5], "cash_delta": r[6], "pnl": r[7], "note": r[8]}
        for r in cur.fetchall()
    ]

    cur.execute("SELECT id, course_code, course_name, start_ts, location FROM exams WHERE created_by = ?", (user_id,))
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
            } if bj_row else None,
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

        # Seed synthetic test user
        self.user_a = 987654321012345678
        self.user_b = 111111111111111111

        cur = self.conn.cursor()
        # User A data
        cur.execute(
            "INSERT INTO users (user_id, display_name, avatar_url, balance, created_at, daily_claim_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.user_a, "SyntheticAlice", "https://cdn.discordapp.com/avatars/alice.png", 2500, 1700000000, 1700086400),
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
            ("COE817", "Applied Cryptography", 1715000000, "ENG101", "Formula sheet allowed", self.user_a, 1700000000),
        )

        # User B data (control)
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

        # 1. User profile and direct PII are completely gone
        cur.execute("SELECT * FROM users WHERE user_id = ?", (self.user_a,))
        self.assertIsNone(cur.fetchone())

        cur.execute("SELECT * FROM blackjack_users WHERE user_id = ?", (self.user_a,))
        self.assertIsNone(cur.fetchone())

        cur.execute("SELECT * FROM fun_positions WHERE user_id = ?", (self.user_a,))
        self.assertEqual(len(cur.fetchall()), 0)

        # 2. Transactional ledger rows are preserved for accounting, but user_id is zeroed and note scrubbed
        cur.execute("SELECT user_id, symbol, qty, price, note FROM fun_trades WHERE symbol = 'TEST' ORDER BY id ASC")
        trades = cur.fetchall()
        self.assertEqual(len(trades), 2, "Total trade records in ledger must remain intact")

        # Alice's trade is anonymized
        self.assertEqual(trades[0][0], 0, "Alice user_id must be zeroed")
        self.assertEqual(trades[0][4], "[ANONYMIZED_USER]", "Note containing PII must be scrubbed")
        self.assertEqual(trades[0][2], 10, "Trade quantity must be preserved for volume")
        self.assertEqual(trades[0][3], 100, "Trade price must be preserved")

        # Bob's trade is unchanged
        self.assertEqual(trades[1][0], self.user_b, "Bob user_id must be untouched")
        self.assertEqual(trades[1][4], "Bob trade")

        # 3. Exam schedule is preserved for fellow students, but creator link is removed
        cur.execute("SELECT course_code, created_by FROM exams WHERE course_code = 'COE817'")
        exam = cur.fetchone()
        self.assertIsNotNone(exam)
        self.assertIsNone(exam[1], "Exam created_by must be set to NULL")

    def test_nonexistent_user_deletion(self):
        """Verify deletion routine handles non-existent users gracefully without errors."""
        summary = delete_and_anonymize_user(self.conn, 999999999999999999)
        self.assertEqual(summary["users_deleted"], 0)
        self.assertEqual(summary["trades_anonymized"], 0)


if __name__ == "__main__":
    unittest.main()

