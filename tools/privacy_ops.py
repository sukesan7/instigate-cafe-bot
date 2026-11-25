#!/usr/bin/env python3
"""Operator privacy maintenance CLI for Instigate Cafe Bot.

Safely wraps ``UserStore.export_user_data()``, ``UserStore.preview_delete_user_data()``,
``UserStore.delete_user_data()``, and ``tools/backup_db.py`` with strict pre-flight
validation:

- Dry-run is ON by default (read-only count inspection; zero SQLite writes).
- Validates DB path (rejects memory URIs, placeholders, CI/test fixtures, missing files,
  non-SQLite files, unsupported schema versions, and missing filesystem permissions).
- Validates Discord guild & user snowflake IDs strictly within ``1..MAX_SNOWFLAKE``.
- Never loads ``.env``, never starts the Discord gateway, and never prints PII, user
  documents, credentials, or raw export JSON to terminals/logs.
- Real deletion requires explicit ``--no-dry-run``, a verified online SQLite backup
  via ``tools/backup_db.py``, and either interactive TTY phrase confirmation or
  ``--confirm`` (which strictly fails closed under automated CI environments).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from icbot.config_parsers import MAX_SNOWFLAKE, is_memory_sqlite_path, is_valid_snowflake  # noqa: E402
from icbot.db import MIGRATIONS, Database  # noqa: E402
from icbot.errors import BotError, redact_secrets  # noqa: E402
from icbot.services.user_service import UserStore  # noqa: E402
from tools.backup_db import backup_database, verify_sqlite_integrity  # noqa: E402

SQLITE_HEADER = b"SQLite format 3\x00"
EXPECTED_SCHEMA_VERSION = max(m.version for m in MIGRATIONS)
REQUIRED_PRIVACY_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "users",
    "blackjack_users",
    "fun_positions",
    "fun_trades",
    "economy_ledger",
    "exams",
    "academic_resources",
    "fun_stock_events",
    "stock_events",
    "fun_market_weeks",
    "user_job_preferences",
    "admin_audit_log",
    "guild_settings_audit",
)

# Standard CI / test-runner markers under which non-interactive --confirm must never run.
CI_ENV_MARKERS: tuple[str, ...] = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "CIRCLECI",
    "TRAVIS",
    "BUILDKITE",
    "TF_BUILD",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
    "CONTINUOUS_INTEGRATION",
    "CODEBUILD_BUILD_ID",
    "PYTEST_CURRENT_TEST",
)

_PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    "<db_path>",
    "<guild_id>",
    "<user_id>",
    "/path/to/",
    "your_db",
    "placeholder",
)


class PrivacyOpsError(ValueError):
    """Raised when pre-flight validation or safety checks reject an operator request."""


def _is_automated_ci_env() -> str | None:
    """Return the first active CI/test-runner environment marker, or ``None``."""
    for var in CI_ENV_MARKERS:
        raw = os.environ.get(var, "").strip()
        if raw and raw.lower() not in ("0", "false", "no"):
            return var
    return None


def _parse_snowflake_arg(raw: str, *, label: str) -> int:
    text = str(raw or "").strip()
    if not text or text.startswith("+") or not text.isdigit():
        raise PrivacyOpsError(
            f"Invalid {label}: expected a positive Discord snowflake integer (1..{MAX_SNOWFLAKE})."
        )
    value = int(text, 10)
    if not is_valid_snowflake(value, allow_zero=False):
        raise PrivacyOpsError(
            f"Out-of-range {label}: must be in 1..{MAX_SNOWFLAKE} (0 tombstone/legacy ID is forbidden)."
        )
    return value


def validate_target_db_path(raw_path: str, *, require_write: bool) -> Path:
    """Validate an explicit target SQLite path against placeholders, CI fixtures, and permissions."""
    raw = str(raw_path or "").strip()
    if not raw or "\x00" in raw:
        raise PrivacyOpsError("--db-path must be a non-empty filesystem path.")

    if is_memory_sqlite_path(raw):
        raise PrivacyOpsError(
            "Refusing in-memory DB_PATH/target; specify an explicit on-disk SQLite database file."
        )

    lowered = raw.lower()
    if any(tok in lowered for tok in _PLACEHOLDER_PATTERNS) or ("<" in raw and ">" in raw):
        raise PrivacyOpsError(
            f"Refusing placeholder --db-path value ({raw!r}); replace with your actual database file path."
        )

    expanded = Path(os.path.expanduser(raw))
    try:
        resolved = expanded.resolve()
    except OSError as exc:
        raise PrivacyOpsError(f"Cannot resolve --db-path: {exc}") from exc

    # Block repo test/CI directories so automated fixtures are never confused with operator data.
    tests_dir = (PROJECT_ROOT / "tests").resolve()
    if resolved == tests_dir or tests_dir in resolved.parents:
        raise PrivacyOpsError("Refusing --db-path inside repository tests/ directory (CI fixture guard).")

    if not expanded.exists():
        raise PrivacyOpsError(f"Target SQLite database does not exist (unknown target): {resolved}")
    if expanded.is_dir() or not expanded.is_file():
        raise PrivacyOpsError(f"Target --db-path must point to an existing file, not a directory: {resolved}")

    if not os.access(resolved, os.R_OK):
        raise PrivacyOpsError(f"Target database is not readable by UID {os.getuid()}: {resolved}")

    if require_write:
        if not os.access(resolved, os.W_OK):
            raise PrivacyOpsError(f"Target database is not writable by UID {os.getuid()}: {resolved}")
        if not os.access(resolved.parent, os.W_OK | os.X_OK):
            raise PrivacyOpsError(
                f"Target database directory is not writable for SQLite WAL/journal operations: {resolved.parent}"
            )
        for sidecar_ext in ("-wal", "-shm"):
            sidecar = resolved.with_name(resolved.name + sidecar_ext)
            if sidecar.exists() and not os.access(sidecar, os.R_OK | os.W_OK):
                raise PrivacyOpsError(f"SQLite sidecar file is not read/write accessible: {sidecar}")

    # Verify SQLite 3 file magic header without writing.
    try:
        with resolved.open("rb") as fh:
            header = fh.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise PrivacyOpsError(f"Unable to read target database header: {exc}") from exc

    if header != SQLITE_HEADER:
        raise PrivacyOpsError(f"Target file is not a valid SQLite 3 database: {resolved}")

    return resolved


def validate_database_schema_readonly(db_path: Path) -> int:
    """Verify schema version, required tables, and integrity using a strictly read-only SQLite URI."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise PrivacyOpsError(f"Failed opening database read-only: {exc}") from exc

    try:
        conn.execute("PRAGMA query_only = ON;")
        uv_row = conn.execute("PRAGMA user_version;").fetchone()
        user_version = int(uv_row[0]) if uv_row else 0

        existing_tables = {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing_tables = [t for t in REQUIRED_PRIVACY_TABLES if t not in existing_tables]
        if missing_tables:
            raise PrivacyOpsError(
                f"Database schema incomplete (missing tables: {', '.join(missing_tables)})."
            )

        applied_versions = {
            int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        expected_versions = {m.version for m in MIGRATIONS}
        if user_version != EXPECTED_SCHEMA_VERSION or not expected_versions.issubset(applied_versions):
            raise PrivacyOpsError(
                f"Unsupported database schema version (user_version={user_version}, expected={EXPECTED_SCHEMA_VERSION}). "
                "Migrate the database before running privacy maintenance."
            )

        qc_rows = conn.execute("PRAGMA quick_check;").fetchall()
        if not qc_rows or str(qc_rows[0][0]) != "ok":
            raise PrivacyOpsError("Database failed PRAGMA quick_check; restore or repair before proceeding.")

        fk_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_rows:
            raise PrivacyOpsError("Database has foreign key integrity violations; aborting.")

        return user_version
    except sqlite3.Error as exc:
        raise PrivacyOpsError(f"Schema inspection failed: {exc}") from exc
    finally:
        conn.close()


def _validate_output_path(raw_output: str) -> Path:
    text = str(raw_output or "").strip()
    if not text or text == "-" or "\x00" in text:
        raise PrivacyOpsError("--output must specify a filesystem JSON path (stdout '-' is forbidden).")
    if "<" in text and ">" in text:
        raise PrivacyOpsError(f"Refusing placeholder --output path ({text!r}).")

    out_path = Path(os.path.expanduser(text)).resolve()
    if out_path.exists():
        raise PrivacyOpsError(f"Refusing to overwrite existing --output file: {out_path}")

    parent = out_path.parent
    if parent.exists():
        if not parent.is_dir():
            raise PrivacyOpsError(f"--output parent is not a directory: {parent}")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise PrivacyOpsError(f"--output parent directory is not writable: {parent}")
    else:
        ancestor = parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise PrivacyOpsError(f"Cannot create --output parent directory: {parent}")
    return out_path


def _resolve_backup_path(db_path: Path, raw_backup: str | None) -> Path:
    if raw_backup is not None:
        text = str(raw_backup).strip()
        if not text or "\x00" in text or ("<" in text and ">" in text):
            raise PrivacyOpsError(f"Invalid --backup-path: {raw_backup!r}")
        dest = Path(os.path.expanduser(text)).resolve()
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        dest = (db_path.parent / "backups" / f"{db_path.stem}_pre_delete_{stamp}.db").resolve()

    if dest == db_path.resolve():
        raise PrivacyOpsError("--backup-path must not overwrite the live target --db-path.")
    if dest.exists():
        raise PrivacyOpsError(f"Refusing to overwrite existing --backup-path: {dest}")

    parent = dest.parent
    if parent.exists():
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            raise PrivacyOpsError(f"Backup parent directory is not writable: {parent}")
    else:
        ancestor = parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise PrivacyOpsError(f"Cannot create backup parent directory: {parent}")
    return dest


def _write_json_restricted(out_path: Path, payload: dict[str, Any]) -> None:
    """Write export JSON atomically with restrictive 0o600 permissions without printing contents."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(out_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
    except BaseException:
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        raise


def _require_delete_confirmation(guild_id: int, user_id: int, *, confirm_flag: bool) -> None:
    """Enforce interactive TTY confirmation or non-CI --confirm opt-in for destructive deletion."""
    if confirm_flag:
        ci_marker = _is_automated_ci_env()
        if ci_marker is not None:
            raise PrivacyOpsError(
                f"--confirm is forbidden inside automated CI/test environments (detected {ci_marker})."
            )
        return

    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        raise PrivacyOpsError(
            "Destructive deletion requires an interactive TTY confirmation prompt (or explicit --confirm outside CI)."
        )

    expected_phrase = f"DELETE {guild_id}:{user_id}"
    print(
        f"CONFIRMATION REQUIRED: Type exactly '{expected_phrase}' to proceed after backup: ",
        file=sys.stderr,
        flush=True,
    )
    entered = stdin.readline()
    if entered.strip() != expected_phrase:
        raise PrivacyOpsError(
            "Confirmation phrase did not match; aborting before backup or database mutation."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safe self-hosted privacy export and Right-to-be-Forgotten deletion CLI. "
            "Runs read-only --dry-run by default and never prints PII or export JSON to stdout."
        )
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Explicit path to target SQLite database file (never reads ambient DB_PATH or :memory:).",
    )
    parser.add_argument(
        "--guild-id",
        required=True,
        help=f"Target Discord Guild ID snowflake (1..{MAX_SNOWFLAKE}).",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help=f"Target Discord User ID snowflake (1..{MAX_SNOWFLAKE}).",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=("export", "delete"),
        help="Operation to preview or execute: 'export' or 'delete'.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preview affected counts read-only with zero DB/file writes (default: True). Pass --no-dry-run to execute.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Destination JSON file path for '--action export --no-dry-run' (0o600 permissions; forbidden for delete).",
    )
    parser.add_argument(
        "--backup-path",
        default=None,
        help="Verified online backup destination for '--action delete --no-dry-run' (defaults to <db_dir>/backups/...).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Non-interactive confirmation for '--action delete --no-dry-run' (blocked under automated CI).",
    )
    parser.add_argument(
        "--anonymize-ledger",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preserve ledger/trade aggregates using CSPRNG tombstones (default: True).",
    )
    parser.add_argument(
        "--delete-global-preferences",
        action="store_true",
        default=False,
        help="Also delete global user_job_preferences even if profiles remain in other guilds.",
    )
    parser.add_argument(
        "--retain-admin-actor-audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retain privileged admin actor IDs in audit trails for non-repudiation (default: True).",
    )
    return parser


def _print_counts_summary(header: str, counts: dict[str, Any]) -> None:
    print(header)
    for key in sorted(counts):
        val = counts[key]
        if isinstance(val, (int, bool)):
            print(f"  - {key}: {val}")


async def execute_privacy_ops(args: argparse.Namespace) -> int:
    action = str(args.action)
    dry_run = bool(args.dry_run)
    guild_id = _parse_snowflake_arg(args.guild_id, label="--guild-id")
    user_id = _parse_snowflake_arg(args.user_id, label="--user-id")

    if action == "delete" and args.output is not None:
        raise PrivacyOpsError("--output is only valid with --action export.")
    if dry_run and args.confirm:
        raise PrivacyOpsError(
            "--confirm cannot be combined with --dry-run (pass --no-dry-run intentionally)."
        )
    if action == "export" and args.confirm:
        raise PrivacyOpsError("--confirm is only valid with --action delete --no-dry-run.")
    if action == "export" and args.backup_path is not None:
        raise PrivacyOpsError("--backup-path is only used with --action delete.")
    if action == "export" and not dry_run and not args.output:
        raise PrivacyOpsError(
            "--output <file.json> is required for '--action export --no-dry-run' (export JSON is never printed to stdout)."
        )

    require_write = action == "delete" and not dry_run
    db_path = validate_target_db_path(args.db_path, require_write=require_write)
    schema_version = validate_database_schema_readonly(db_path)

    # Read-only operations (any --dry-run, or --action export) use strict SQLite read-only URI mode.
    if dry_run or action == "export":
        out_path = _validate_output_path(args.output) if (action == "export" and not dry_run) else None
        db = Database(str(db_path), read_only=True)
        try:
            store = UserStore(db, guild_id=guild_id)
            if action == "delete":
                preview = await store.preview_delete_user_data(
                    user_id,
                    guild_id=guild_id,
                    anonymize_ledger=bool(args.anonymize_ledger),
                    delete_global_preferences=bool(args.delete_global_preferences),
                    retain_admin_actor_audit=bool(args.retain_admin_actor_audit),
                )
                print(f"[DRY-RUN] Read-only deletion preview (schema v{schema_version}): zero changes made.")
                _print_counts_summary("Affected record counts:", preview["counts"])
                print(
                    f"  policy: anonymize_ledger={preview['anonymize_ledger']}, "
                    f"delete_global_preferences={preview['delete_global_preferences']}, "
                    f"retain_admin_actor_audit={preview['retain_admin_actor_audit']}, "
                    f"would_mutate={preview['would_mutate']}"
                )
                return 0

            export_payload = await store.export_user_data(user_id, guild_id=guild_id)
            total_counts = dict(export_payload.get("pagination", {}).get("total_counts", {}))
            total_counts["profile_present"] = 1 if export_payload.get("profile") is not None else 0
            total_counts["blackjack_present"] = 1 if export_payload.get("blackjack") is not None else 0
            total_counts["job_preferences_present"] = (
                1 if export_payload.get("job_preferences") is not None else 0
            )
            if dry_run:
                print(f"[DRY-RUN] Read-only export preview (schema v{schema_version}): no output written.")
                _print_counts_summary("Exportable record counts:", total_counts)
                print(f"  truncated: {bool(export_payload.get('truncated', False))}")
                return 0

            assert out_path is not None
            _write_json_restricted(out_path, export_payload)
            print(f"[OK] Export written (mode 0600, schema v{schema_version}) to: {out_path}")
            _print_counts_summary("Exported summary counts:", total_counts)
            print(f"  truncated: {bool(export_payload.get('truncated', False))}")
            return 0
        finally:
            await db.close()

    # Destructive path: --action delete --no-dry-run
    backup_dest = _resolve_backup_path(db_path, args.backup_path)
    _require_delete_confirmation(guild_id, user_id, confirm_flag=bool(args.confirm))

    if not backup_database(db_path, backup_dest):
        raise PrivacyOpsError(f"Pre-deletion online backup failed; aborting without mutating {db_path}.")
    ok_bkp, msg_bkp = verify_sqlite_integrity(backup_dest)
    if not ok_bkp:
        raise PrivacyOpsError(f"Pre-deletion backup failed integrity verification ({msg_bkp}); aborting.")

    db_rw = Database(str(db_path), read_only=False)
    try:
        store_rw = UserStore(db_rw, guild_id=guild_id)
        counts = await store_rw.delete_user_data(
            user_id,
            guild_id=guild_id,
            anonymize_ledger=bool(args.anonymize_ledger),
            delete_global_preferences=bool(args.delete_global_preferences),
            retain_admin_actor_audit=bool(args.retain_admin_actor_audit),
        )
        await db_rw.check_integrity()
    finally:
        await db_rw.close()

    print(f"[OK] Deletion completed atomically (schema v{schema_version}). Backup verified: {backup_dest}")
    _print_counts_summary("Deletion summary counts:", counts)
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, enforce safety guards, and run the requested privacy maintenance action."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(execute_privacy_ops(args))
    except (PrivacyOpsError, BotError, OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {redact_secrets(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_cli())
