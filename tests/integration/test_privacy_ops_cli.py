"""Targeted offline tests for the operator privacy maintenance workflow (`tools/privacy_ops.py`).

Uses only temporary SQLite databases (``tmp_path``) with zero production DB, ``.env``,
Discord gateway, or live network access.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from icbot.academics.catalog import ManualResourceCatalogStore
from icbot.academics.exams_service import ExamStore, ParsedExamDraft
from icbot.config import MAX_SNOWFLAKE
from icbot.db import Database, Transaction
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import UserStore
from tools.backup_db import verify_sqlite_integrity

pytestmark = pytest.mark.storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUILD_A = 910_000_000_000_000_001
GUILD_B = 910_000_000_000_000_002
ALICE_UID = 810_000_000_000_000_111
BOB_UID = 810_000_000_000_000_222


class _FakeTTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeNonTTYInput(io.StringIO):
    def isatty(self) -> bool:
        return False


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _seed_privacy_db(db_path: Path) -> None:
    """Create a migrated Schema v7 SQLite database with two guilds and two users."""
    db = Database(str(db_path))
    await db.init()
    try:
        exam_store = ExamStore(db, clock=lambda: 1_760_000_000.0)
        cat_store = ManualResourceCatalogStore(db, clock=lambda: 1_760_000_000.0)

        for gid in (GUILD_A, GUILD_B):
            u_store = UserStore(db, guild_id=gid, starting_balance=1000, clock=lambda: 1_760_000_000.0)
            m_store = FunMarketStore(db, guild_id=gid, starting_bankroll=1000, clock=lambda: 1_760_000_000.0)
            await m_store.sync_catalog(guild_id=gid)

            for uid, name in (
                (ALICE_UID, f"SensitiveAliceName_{gid}"),
                (BOB_UID, f"SensitiveBobName_{gid}"),
            ):
                await u_store.ensure_user(
                    uid,
                    display_name=name,
                    avatar_url=f"https://cdn.example.invalid/avatars/{uid}_secret.png",
                )
                await u_store.add_balance(uid, 200, category="grant", idempotency_key=f"credit:{gid}:{uid}:1")
                await m_store.buy_long(user_id=uid, symbol="IC", qty=1, guild_id=gid)
                await exam_store.add_exam(
                    guild_id=gid,
                    draft=ParsedExamDraft(
                        course_code=f"CPS{uid % 1000}",
                        course_name=f"PrivateExamTitle_{uid}_{gid}",
                        start_ts=1_770_000_000,
                        end_ts=1_770_003_600,
                    ),
                    created_by=uid,
                )
                await cat_store.add_resource(
                    guild_id=gid,
                    course_code=f"CPS{uid % 1000}",
                    title=f"PrivateResourceTitle_{uid}_{gid}",
                    url=f"https://notes.example.invalid/{gid}/{uid}",
                    added_by=uid,
                )
                await db.execute(
                    """
                    INSERT INTO admin_audit_log (guild_id, admin_user_id, action, target_user_id, details_json, created_at)
                    VALUES (?, ?, 'grant', ?, ?, 1760000100)
                    """,
                    (gid, BOB_UID if uid == ALICE_UID else ALICE_UID, uid, json.dumps({"target": uid})),
                )

        await db.set_user_job_preferences(
            ALICE_UID,
            preset="tech_swe",
            locations=("Toronto_PrivateLoc",),
            keywords_allow=("python",),
            now=1_760_000_000,
        )
    finally:
        await db.close()


def _checkpoint_and_close(db_path: Path, *, journal_mode: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
        if journal_mode is not None:
            conn.execute(f"PRAGMA journal_mode={journal_mode};").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Argument, Snowflake, DB Path & Schema Validation
# ---------------------------------------------------------------------------


def test_privacy_ops_rejects_missing_and_invalid_cli_syntax() -> None:
    from tools.privacy_ops import run_cli

    with pytest.raises(SystemExit) as exc_info:
        run_cli([])
    assert exc_info.value.code != 0

    with pytest.raises(SystemExit) as exc_info2:
        run_cli(
            [
                "--db-path",
                "some.db",
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "bogus",
            ]
        )
    assert exc_info2.value.code != 0


@pytest.mark.parametrize(
    "bad_db_path",
    [
        "",
        ":memory:",
        "file::memory:?cache=shared",
        "/path/to/campus.db",
        "<DB_PATH>",
        "tests/fixtures/nonexistent_fixture.db",
    ],
)
def test_privacy_ops_rejects_invalid_db_paths(bad_db_path: str) -> None:
    from tools.privacy_ops import run_cli

    rc = run_cli(
        [
            "--db-path",
            bad_db_path,
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "delete",
        ]
    )
    assert rc != 0


def test_privacy_ops_validates_snowflake_ranges_and_flag_combinations(tmp_path: Path) -> None:
    from tools.privacy_ops import run_cli

    db_file = tmp_path / "valid.db"
    asyncio.run(_seed_privacy_db(db_file))

    bad_ids = [
        ("0", str(ALICE_UID)),
        (str(GUILD_A), "0"),
        ("-5", str(ALICE_UID)),
        (str(GUILD_A), str(MAX_SNOWFLAKE + 1)),
        (str(MAX_SNOWFLAKE + 1), str(ALICE_UID)),
        ("abc", str(ALICE_UID)),
        ("+12345", str(ALICE_UID)),
    ]
    for bad_gid, bad_uid in bad_ids:
        rc = run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                bad_gid,
                "--user-id",
                bad_uid,
                "--action",
                "delete",
            ]
        )
        assert rc != 0

    out_file = tmp_path / "export.json"
    # --output with --action delete is rejected
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--output",
                str(out_file),
            ]
        )
        != 0
    )

    # --action export --no-dry-run without --output is rejected
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "export",
                "--no-dry-run",
            ]
        )
        != 0
    )

    # --confirm cannot be used in --dry-run mode or with --action export
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--confirm",
            ]
        )
        != 0
    )
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "export",
                "--no-dry-run",
                "--output",
                str(out_file),
                "--confirm",
            ]
        )
        != 0
    )
    assert not out_file.exists()


def test_privacy_ops_rejects_missing_non_sqlite_ci_fixture_and_wrong_schema(tmp_path: Path) -> None:
    from tools.privacy_ops import run_cli

    # 1. Missing DB file (unknown target)
    missing_db = tmp_path / "does_not_exist.db"
    assert (
        run_cli(
            [
                "--db-path",
                str(missing_db),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
            ]
        )
        != 0
    )
    assert not missing_db.exists()

    # 2. Directory path
    assert (
        run_cli(
            [
                "--db-path",
                str(tmp_path),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
            ]
        )
        != 0
    )

    # 3. Non-SQLite text file
    garbage = tmp_path / "not_a_db.db"
    garbage.write_text("definitely not sqlite format 3", encoding="utf-8")
    assert (
        run_cli(
            [
                "--db-path",
                str(garbage),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
            ]
        )
        != 0
    )

    # 4. Path inside tests/ (CI fixture guard)
    fixture_candidate = PROJECT_ROOT / "tests" / "fixtures" / "synthetic_db_guard_check.db"
    assert (
        run_cli(
            [
                "--db-path",
                str(fixture_candidate),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
            ]
        )
        != 0
    )

    # 5. Unmigrated / stale schema version (user_version=2)
    stale_db_path = tmp_path / "stale_v2.db"

    async def _init_v2() -> None:
        stale_db = Database(str(stale_db_path))
        await stale_db.migrate(target_version=2)
        await stale_db.close()

    asyncio.run(_init_v2())
    assert (
        run_cli(
            [
                "--db-path",
                str(stale_db_path),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
            ]
        )
        != 0
    )


# ---------------------------------------------------------------------------
# 2. Dry-Run Zero Writes & Zero Private Row Leakage
# ---------------------------------------------------------------------------


def test_privacy_ops_dry_run_is_default_zero_writes_and_no_pii_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default --dry-run prints aggregate counts only and never writes to the SQLite file or stdout JSON."""
    from tools.privacy_ops import run_cli

    db_file = tmp_path / "dry_run.db"
    asyncio.run(_seed_privacy_db(db_file))
    _checkpoint_and_close(db_file)
    pre_hash = _file_sha256(db_file)

    # Default (no --no-dry-run passed) on delete
    rc_del = run_cli(
        [
            "--db-path",
            str(db_file),
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "delete",
        ]
    )
    assert rc_del == 0
    captured_del = capsys.readouterr()
    combined_del = captured_del.out + captured_del.err
    assert "DRY-RUN" in combined_del
    assert "users_to_delete: 1" in combined_del
    assert "economy_ledger_to_tombstone: 2" in combined_del
    for sensitive in (
        f"SensitiveAliceName_{GUILD_A}",
        f"{ALICE_UID}_secret.png",
        f"PrivateExamTitle_{ALICE_UID}_{GUILD_A}",
        f"PrivateResourceTitle_{ALICE_UID}_{GUILD_A}",
        "Toronto_PrivateLoc",
    ):
        assert sensitive not in combined_del

    # Default dry-run on export
    rc_exp = run_cli(
        [
            "--db-path",
            str(db_file),
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "export",
        ]
    )
    assert rc_exp == 0
    captured_exp = capsys.readouterr()
    combined_exp = captured_exp.out + captured_exp.err
    assert "DRY-RUN" in combined_exp
    assert f"SensitiveAliceName_{GUILD_A}" not in combined_exp
    assert "Toronto_PrivateLoc" not in combined_exp

    assert _file_sha256(db_file) == pre_hash
    assert list(tmp_path.glob("*.bak*")) == []
    assert not (tmp_path / "backups").exists()


# ---------------------------------------------------------------------------
# 3. CI Safety Guard & Interactive Confirmation for --confirm / Deletion
# ---------------------------------------------------------------------------


def test_confirm_blocked_under_automated_ci_and_allowed_when_non_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--confirm must fail closed when any automated CI or pytest environment marker is active."""
    from tools.privacy_ops import CI_ENV_MARKERS, run_cli

    db_file = tmp_path / "ci_guard.db"
    asyncio.run(_seed_privacy_db(db_file))

    # 1. With CI env vars, --confirm MUST fail closed
    for env_key in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.setenv(env_key, "true")
        rc = run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
                "--confirm",
                "--backup-path",
                str(tmp_path / "should_not_create.db"),
            ]
        )
        assert rc != 0
        assert not (tmp_path / "should_not_create.db").exists()
        monkeypatch.delenv(env_key, raising=False)

    # 2. Even with CI unset, PYTEST_CURRENT_TEST blocks --confirm
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
                "--confirm",
            ]
        )
        != 0
    )

    # 3. Without --confirm, non-TTY stdin fails closed on real deletion
    monkeypatch.setattr("sys.stdin", _FakeNonTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
            ]
        )
        != 0
    )

    # 4. TTY stdin with wrong confirmation phrase aborts cleanly before backup/mutation
    monkeypatch.setattr("sys.stdin", _FakeTTYInput("yes\n"))
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
                "--backup-path",
                str(tmp_path / "abort_bkp.db"),
            ]
        )
        != 0
    )
    assert not (tmp_path / "abort_bkp.db").exists()

    # 5. Explicitly clearing all CI markers simulates a local operator shell where --confirm is allowed
    for marker in CI_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    bkp_non_ci = tmp_path / "non_ci_confirm.db"
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
                "--confirm",
                "--backup-path",
                str(bkp_non_ci),
            ]
        )
        == 0
    )
    assert bkp_non_ci.is_file()

    async def _check_erased() -> None:
        db = Database(str(db_file), read_only=True)
        try:
            assert await UserStore(db, guild_id=GUILD_A).get_profile(ALICE_UID) is None
        finally:
            await db.close()

    asyncio.run(_check_erased())


# ---------------------------------------------------------------------------
# 4. Export, Real Deletion with Backup, Wrong-Guild Isolation, No-Op & Repeat
# ---------------------------------------------------------------------------


def test_privacy_ops_export_isolated_delete_backup_noop_and_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify full export-to-file (no stdout JSON), backup-gated delete, isolation, no-op, and repeat."""
    from tools.privacy_ops import run_cli

    db_file = tmp_path / "ops_lifecycle.db"
    asyncio.run(_seed_privacy_db(db_file))
    export_file = tmp_path / "alice_export.json"
    backup_file = tmp_path / "backups" / "pre_delete_alice.db"

    # 1. Real export to file (without dumping JSON/PII to stdout)
    rc_exp = run_cli(
        [
            "--db-path",
            str(db_file),
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "export",
            "--no-dry-run",
            "--output",
            str(export_file),
        ]
    )
    assert rc_exp == 0
    assert export_file.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(export_file.stat().st_mode) == 0o600
    exp_out = capsys.readouterr()
    assert f"SensitiveAliceName_{GUILD_A}" not in (exp_out.out + exp_out.err)
    assert "Toronto_PrivateLoc" not in (exp_out.out + exp_out.err)
    exported = json.loads(export_file.read_text(encoding="utf-8"))
    assert exported["format_version"] == 2
    assert exported["guild_id"] == GUILD_A
    assert exported["user_id"] == ALICE_UID
    assert exported["profile"]["display_name"] == f"SensitiveAliceName_{GUILD_A}"
    assert len(exported["ledger"]) == 2

    # Refuses to silently overwrite existing --output file
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "export",
                "--no-dry-run",
                "--output",
                str(export_file),
            ]
        )
        != 0
    )

    # 2. Real deletion in Guild A with explicit interactive confirmation + backup
    monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))
    rc_del = run_cli(
        [
            "--db-path",
            str(db_file),
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "delete",
            "--no-dry-run",
            "--backup-path",
            str(backup_file),
        ]
    )
    assert rc_del == 0
    del_out = capsys.readouterr()
    assert f"SensitiveAliceName_{GUILD_A}" not in (del_out.out + del_out.err)

    assert backup_file.is_file()
    ok_bkp, _ = verify_sqlite_integrity(backup_file)
    assert ok_bkp is True

    async def _verify_post_delete() -> None:
        bkp_db = Database(str(backup_file), read_only=True)
        try:
            assert await UserStore(bkp_db, guild_id=GUILD_A).get_profile(ALICE_UID) is not None
        finally:
            await bkp_db.close()

        active_db = Database(str(db_file), read_only=True)
        try:
            assert await UserStore(active_db, guild_id=GUILD_A).get_profile(ALICE_UID) is None
            assert await UserStore(active_db, guild_id=GUILD_B).get_profile(ALICE_UID) is not None
            assert await UserStore(active_db, guild_id=GUILD_A).get_profile(BOB_UID) is not None
            assert await active_db.get_user_job_preferences(ALICE_UID) is not None
            await active_db.check_integrity()
        finally:
            await active_db.close()

    asyncio.run(_verify_post_delete())

    # 3. Idempotent repeat deletion for Alice in Guild A
    backup_file_2 = tmp_path / "backups" / "repeat_alice.db"
    monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(ALICE_UID),
                "--action",
                "delete",
                "--no-dry-run",
                "--backup-path",
                str(backup_file_2),
            ]
        )
        == 0
    )

    # 4. No-op user (never existed in Guild A or B)
    unknown_uid = 810_000_000_000_000_999
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(unknown_uid),
                "--action",
                "delete",
            ]
        )
        == 0
    )
    monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{unknown_uid}\n"))
    assert (
        run_cli(
            [
                "--db-path",
                str(db_file),
                "--guild-id",
                str(GUILD_A),
                "--user-id",
                str(unknown_uid),
                "--action",
                "delete",
                "--no-dry-run",
                "--backup-path",
                str(tmp_path / "backups" / "noop.db"),
            ]
        )
        == 0
    )


# ---------------------------------------------------------------------------
# 5. Rollback on Induced Failure & Permission Restrictions
# ---------------------------------------------------------------------------


def test_privacy_ops_rollback_on_induced_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Induced mid-transaction exception leaves DB completely unmutated."""
    from tools.privacy_ops import run_cli

    db_file = tmp_path / "rollback.db"
    asyncio.run(_seed_privacy_db(db_file))
    backup_file = tmp_path / "rollback_bkp.db"

    orig_execute = Transaction.execute

    async def _boom(self_tx: Transaction, sql: str, params: Any = ()) -> Any:
        if "UPDATE economy_ledger SET user_id=?" in sql:
            raise RuntimeError("Induced failure during delete_user_data")
        return await orig_execute(self_tx, sql, params)

    monkeypatch.setattr(Transaction, "execute", _boom)
    monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))

    rc = run_cli(
        [
            "--db-path",
            str(db_file),
            "--guild-id",
            str(GUILD_A),
            "--user-id",
            str(ALICE_UID),
            "--action",
            "delete",
            "--no-dry-run",
            "--backup-path",
            str(backup_file),
        ]
    )
    assert rc != 0
    monkeypatch.setattr(Transaction, "execute", orig_execute)

    async def _verify_rolled_back() -> None:
        db = Database(str(db_file), read_only=True)
        try:
            st = UserStore(db, guild_id=GUILD_A)
            assert await st.get_profile(ALICE_UID) is not None
            exp = await st.export_user_data(ALICE_UID, guild_id=GUILD_A)
            assert len(exp["ledger"]) == 2
            assert len(exp["positions"]) == 1
            await db.check_integrity()
        finally:
            await db.close()

    asyncio.run(_verify_rolled_back())


def test_privacy_ops_permission_restrictions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only DB allows dry-run and export, but blocks real deletion pre-flight."""
    from tools.privacy_ops import run_cli

    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("POSIX file permission test skipped under root UID 0")

    db_file = tmp_path / "perm_ro.db"
    asyncio.run(_seed_privacy_db(db_file))
    _checkpoint_and_close(db_file, journal_mode="DELETE")
    db_file.chmod(0o444)

    ro_bkp_dir = tmp_path / "ro_bkp_dir"
    ro_bkp_dir.mkdir()
    ro_bkp_dir.chmod(0o555)

    try:
        # 1. Read-only dry-run succeeds on chmod 0o444 DB
        assert (
            run_cli(
                [
                    "--db-path",
                    str(db_file),
                    "--guild-id",
                    str(GUILD_A),
                    "--user-id",
                    str(ALICE_UID),
                    "--action",
                    "delete",
                ]
            )
            == 0
        )

        # 2. Read-only export succeeds on chmod 0o444 DB
        out_ro = tmp_path / "ro_export.json"
        assert (
            run_cli(
                [
                    "--db-path",
                    str(db_file),
                    "--guild-id",
                    str(GUILD_A),
                    "--user-id",
                    str(ALICE_UID),
                    "--action",
                    "export",
                    "--no-dry-run",
                    "--output",
                    str(out_ro),
                ]
            )
            == 0
        )
        assert out_ro.is_file()

        # 3. Real deletion on chmod 0o444 DB fails pre-flight without creating backup
        monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))
        bkp_ro = tmp_path / "ro_bkp.db"
        assert (
            run_cli(
                [
                    "--db-path",
                    str(db_file),
                    "--guild-id",
                    str(GUILD_A),
                    "--user-id",
                    str(ALICE_UID),
                    "--action",
                    "delete",
                    "--no-dry-run",
                    "--backup-path",
                    str(bkp_ro),
                ]
            )
            != 0
        )
        assert not bkp_ro.exists()

        # 4. Real deletion on writable DB with unwritable backup directory fails pre-flight
        db_file.chmod(0o600)
        monkeypatch.setattr("sys.stdin", _FakeTTYInput(f"DELETE {GUILD_A}:{ALICE_UID}\n"))
        assert (
            run_cli(
                [
                    "--db-path",
                    str(db_file),
                    "--guild-id",
                    str(GUILD_A),
                    "--user-id",
                    str(ALICE_UID),
                    "--action",
                    "delete",
                    "--no-dry-run",
                    "--backup-path",
                    str(ro_bkp_dir / "blocked_bkp.db"),
                ]
            )
            != 0
        )
    finally:
        db_file.chmod(0o600)
        ro_bkp_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# 6. Documentation Workflow Contract
# ---------------------------------------------------------------------------


def test_privacy_doc_reproducible_cli_workflow_and_controller_boundaries() -> None:
    """docs/privacy.md must replace non-runnable purge_member snippet with tools/privacy_ops.py workflow."""
    doc = (PROJECT_ROOT / "docs/privacy.md").read_text(encoding="utf-8")
    assert "purge_member(GUILD_ID, USER_ID)" not in doc
    assert "tools/privacy_ops.py" in doc
    assert "--dry-run" in doc
    assert "--no-dry-run" in doc
    assert "tools/backup_db.py" in doc
    assert "Railway" in doc
    assert "Data Controller" in doc
