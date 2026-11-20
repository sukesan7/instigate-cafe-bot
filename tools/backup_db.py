#!/usr/bin/env python3
"""Online hot backup and restore utility for Instigate Cafe Bot SQLite databases in WAL mode.

In SQLite WAL mode, simply copying the main `.db` file using `cp` can result in incomplete
or corrupt snapshots because active writes reside in `.sqlite-wal`. This utility uses SQLite's
native online backup API (`sqlite3.connect().backup()`) to create consistent, zero-downtime
point-in-time snapshots and verify integrity.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def verify_sqlite_integrity(db_path: Path) -> tuple[bool, str]:
    """Run PRAGMA integrity_check on the specified SQLite database file."""
    if not db_path.exists():
        return False, f"Database file does not exist: {db_path}"

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        rows = cursor.fetchall()
        conn.close()

        if len(rows) == 1 and rows[0][0] == "ok":
            return True, "ok"
        return False, "; ".join(r[0] for r in rows)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return False, str(exc)


def backup_database(
    source_path: Path,
    dest_path: Path,
    *,
    pages_per_step: int = 100,
    sleep_between_steps: float = 0.01,
) -> bool:
    """Perform a consistent, non-blocking online hot backup of source_path to dest_path."""
    if not source_path.exists():
        print(f"[ERROR] Source database does not exist: {source_path}", file=sys.stderr)
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest_path.with_suffix(dest_path.suffix + f".tmp.{int(time.time())}")

    try:
        src_conn = sqlite3.connect(str(source_path), timeout=30.0)
        dst_conn = sqlite3.connect(str(temp_dest), timeout=30.0)

        with dst_conn:
            src_conn.backup(dst_conn, pages=pages_per_step, sleep=sleep_between_steps)

        dst_conn.close()
        src_conn.close()

        # Verify integrity of the backup before moving into place
        ok, msg = verify_sqlite_integrity(temp_dest)
        if not ok:
            print(f"[ERROR] Backup integrity check failed: {msg}", file=sys.stderr)
            if temp_dest.exists():
                temp_dest.unlink()
            return False

        # Atomic rename to final destination
        if dest_path.exists():
            dest_path.unlink()
        temp_dest.rename(dest_path)

        size_kb = dest_path.stat().st_size / 1024.0
        print(f"[OK] Successfully backed up {source_path} -> {dest_path} ({size_kb:.1f} KB, integrity ok)")
        return True
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        print(f"[ERROR] Database backup failed: {exc}", file=sys.stderr)
        if temp_dest.exists():
            temp_dest.unlink()
        return False


def restore_database(backup_path: Path, dest_path: Path) -> bool:
    """Restore database from verified backup_path to dest_path with safety checks."""
    if not backup_path.exists():
        print(f"[ERROR] Backup file does not exist: {backup_path}", file=sys.stderr)
        return False

    # 1. Verify backup file integrity first
    ok, msg = verify_sqlite_integrity(backup_path)
    if not ok:
        print(f"[ERROR] Refusing to restore from corrupt backup: {msg}", file=sys.stderr)
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. If destination exists, archive it first
    if dest_path.exists():
        archive_path = dest_path.with_suffix(dest_path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(dest_path, archive_path)
        print(f"[INFO] Existing database archived to: {archive_path}")

        # Also remove existing wal/shm files to prevent state mismatch
        for ext in ("-wal", "-shm"):
            wal_file = dest_path.with_name(dest_path.name + ext)
            if wal_file.exists():
                wal_file.unlink()

    # 3. Copy backup file to destination
    try:
        shutil.copy2(backup_path, dest_path)
        ok, msg = verify_sqlite_integrity(dest_path)
        if not ok:
            print(f"[ERROR] Restored database failed integrity check: {msg}", file=sys.stderr)
            return False

        print(f"[OK] Successfully restored database from {backup_path} -> {dest_path}")
        return True
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        print(f"[ERROR] Restore failed: {exc}", file=sys.stderr)
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instigate Cafe Bot SQLite Online Backup & Restore.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=Path, help="Path to source SQLite database for backup.")
    group.add_argument("--restore", type=Path, help="Path to backup file to restore from.")
    group.add_argument("--verify", type=Path, help="Verify integrity of an existing database or backup.")

    parser.add_argument("--dest", type=Path, help="Destination path for backup or restore.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verify:
        ok, msg = verify_sqlite_integrity(args.verify)
        if ok:
            print(f"[OK] Integrity check passed for {args.verify}")
            return 0
        else:
            print(f"[FAIL] Integrity check failed for {args.verify}: {msg}", file=sys.stderr)
            return 1

    if args.source:
        if not args.dest:
            print("[ERROR] --dest is required when running --source backup.", file=sys.stderr)
            return 1
        return 0 if backup_database(args.source, args.dest) else 1

    if args.restore:
        if not args.dest:
            print("[ERROR] --dest is required when running --restore.", file=sys.stderr)
            return 1
        return 0 if restore_database(args.restore, args.dest) else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
