# SQLite Storage Diagnostics, Online Backups, and Disaster Recovery Drill

This document defines the storage health diagnostics, online backup architecture,
documented restore drill, retention/pruning strategy, and query plan optimizations for
Instigate Cafe Bot.

---

## 1. Storage Health Diagnostics

The bot provides programmatic and operational storage diagnostics via `Database.get_storage_health()`:

```python
health: StorageHealthReport = await db.get_storage_health()
```

### Metrics Reported
| Metric | Description | Expected Value |
|---|---|---|
| `integrity_ok` | SQLite B-tree & page structure validation | `True` (`PRAGMA integrity_check` returns `ok`) |
| `foreign_keys_ok` | Foreign key referential constraint checks | `True` (`PRAGMA foreign_key_check` returns 0 violations) |
| `journal_mode` | Write-Ahead Logging status | `wal` (on disk) / `memory` (in tests) |
| `page_size` | SQLite page allocation size | `4096` bytes |
| `page_count` | Total allocated database pages | `> 0` |
| `freelist_count` | Reusable unallocated pages | Tracked for vacuum planning |
| `db_size_bytes` | Total byte size of primary database file | Monitored against disk thresholds |
| `wal_size_bytes` | Byte size of `-wal` file | Monitored for checkpoint lag |
| `schema_version` | Current `PRAGMA user_version` | Current applied migration number |
| `applied_migrations` | Historical migration IDs applied | Ordered list of version numbers |
| `table_row_counts` | Exact row counts across all application tables | Dictionary of table names to counts |
| `is_read_only` | Write capability check | `False` under normal operation |

---

## 2. Online Backup Strategy

### Architectural Principle
Never copy live SQLite database files using filesystem tools (`cp`, `rsync`) while the bot is running, as WAL mode can produce torn pages and inconsistent state.

Instead, the bot uses SQLite's native online backup API (`sqlite3_backup_*` exposed via `aiosqlite.Connection.backup()`):
1. **Thread-Safe Snapshotting**: Acquired under the database lock (`self._lock`), guaranteeing that in-flight Python operations complete or wait.
2. **Page-by-Page Streaming**: Copies pages safely while SQLite's pager enforces read locks, creating a transactionally consistent snapshot even if background tasks are running.
3. **Automated Verification**: Immediately following generation, the backup is opened in a separate connection and verified against `PRAGMA integrity_check;`. If the snapshot is corrupt, `MigrationIntegrityError` is raised immediately.

### Operational Command
```python
backup_path = await db.backup("/var/backups/icbot/snapshot_20261113_000000.db")
```

---

## 3. Documented Restore Drill

In the event of data corruption, host failure, or an operational disaster, follow this verified restore procedure:

### Step 1: Pre-flight Verification of the Backup Snapshot
Before replacing any database file, independently verify that the target backup file is intact and valid:
```bash
sqlite3 /var/backups/icbot/snapshot_20261113_000000.db "PRAGMA integrity_check;"
# Output must be: ok
```

### Step 2: Programmatic Safe Restore
The application provides an atomic restore helper `Database.restore()`:
```python
await db.restore("/var/backups/icbot/snapshot_20261113_000000.db")
```
Internally, `restore()`:
1. Validates source backup existence and executes `PRAGMA integrity_check`.
2. Drains and cleanly closes active connections under `self._lock`.
3. Streams the backup pages into the live database file using SQLite's backup API.
4. Re-establishes the managed connection, re-applies PRAGMAs (`WAL`, `foreign_keys=ON`, `busy_timeout=5000`), and verifies post-restore integrity.

### Step 3: Manual Shell Disaster Recovery
If restoring from outside the running Python process:
1. Ensure the bot process is stopped completely (`docker compose stop campus-bot` or `sudo systemctl stop campus-bot.service`).
2. Use [`tools/backup_db.py`](file:///home/sukesan/dev/discord/campus-bot/tools/backup_db.py) against the canonical `DB_PATH` (e.g., `/data/campus.db` in Docker/Railway, `/opt/campus-bot/data/campus.db` in systemd, or `ic_stock.db` in local dev) to archive the existing database, clean up `-wal`/`-shm` files, and restore with integrity verification:
   ```bash
   uv run python tools/backup_db.py --restore /var/backups/icbot/snapshot_20261113_000000.db --dest /data/campus.db
   ```
3. Or manually archive the broken database and its WAL files before copying the verified backup into place:
   ```bash
   mv /data/campus.db /data/campus.db.broken
   mv /data/campus.db-wal /data/campus.db-wal.broken 2>/dev/null || true
   mv /data/campus.db-shm /data/campus.db-shm.broken 2>/dev/null || true
   cp /var/backups/icbot/snapshot_20261113_000000.db /data/campus.db
   sqlite3 /data/campus.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
   ```
4. Start the bot with offline smoke verification before enabling live services.

---

## 4. Retention & Stale Data Pruning Policy

The bot separates transient deduplication checkpoints and stale caches from immutable financial records:

| Data Store | Retention Period | Pruning Action | Impact on Finance |
|---|---|---|---|
| `job_seen` | 90 days | Rows older than 90 days deleted | None (transient dedupe only) |
| `alert_delivery_dedupes` | 30 days | Rows older than 30 days deleted | None (transient dedupe only) |
| `workday_cxs_cache` | 14 days | Stale discovery URLs refreshed | None (cache miss triggers re-query) |
| `grades_course_cache` | 180 days | Stale course listings refreshed | None (cache miss triggers fetch) |
| `economy_ledger` | **Indefinite (Immutable)** | **NEVER PRUNED** | Full balance auditability preserved |
| `admin_audit_log` | **Indefinite (Immutable)** | **NEVER PRUNED** | Administrative action auditability |

Execute pruning safely via:
```python
pruned_counts = await db.prune_stale_data(
    job_seen_days=90,
    alert_dedupe_days=30,
    workday_cache_days=14,
    grades_cache_days=180,
)
```

---

## 5. Query Plan Analysis (`EXPLAIN QUERY PLAN`)

All high-frequency access patterns are covered by compound primary keys and targeted indexes, preventing table scans:

1. **`users`**: Lookups by `(guild_id, user_id)` use compound PK `sqlite_autoindex_users_1`.
2. **`economy_ledger`**: User transaction history uses `idx_economy_ledger_guild_user (guild_id, user_id, created_at DESC)`.
3. **`fun_positions`**: Position lookups use compound PK `sqlite_autoindex_fun_positions_1 (guild_id, user_id, symbol, side)`.
4. **`fun_trades`**: User trade queries use `idx_fun_trades_guild_user_ts (guild_id, user_id, ts_unix DESC)`.
5. **`exams`**: Chronological schedule listings use `idx_exams_guild_start (guild_id, start_ts ASC)`.
6. **`alert_delivery_dedupes`**: Deduplication checks use compound PK `sqlite_autoindex_alert_delivery_dedupes_1 (guild_id, alert_type, dedupe_key)`.

---

## 6. Resilience: Fail-Closed Migrations, Read-Only & Disk-Full Scenarios

1. **Incompatible Forward Schemas**: If a database has a `PRAGMA user_version` greater than `max(MIGRATIONS)`, `db.migrate()` aborts immediately with `MigrationError` rather than attempting unsafe mutations.
2. **Read-Only / Disk-Full Fail-Closed Behavior**: When a database cannot accept writes (e.g. disk full or read-only filesystem), SQLite raises `sqlite3.OperationalError`. The unit of work (`write_transaction`) automatically issues `ROLLBACK`, leaving existing balances and ledger entries uncorrupted.

