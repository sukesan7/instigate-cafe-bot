# SQLite Persistence, Transaction Boundaries, and Migration Architecture

This document defines the SQLite storage architecture, schema DDL, call site catalog,
transaction boundaries, migration ledger, and benchmark results for Instigate Cafe Bot.

---

## 1. Storage Architecture & Design Principles

### Managed Connection vs Connection-per-Query
Prior to Phase 4, `Database` opened a new `aiosqlite.connect()` connection on every query and closed it immediately upon completion. While simple, this had significant drawbacks:
1. **Thread/file creation overhead:** In Python, `aiosqlite` spawns an OS worker thread per connection. Connection-per-query created hundreds of ephemeral threads and re-read the SQLite file header continuously.
2. **Lack of transaction boundaries:** Multi-statement atomic operations could not be expressed through `Database`. Business stores (`FunMarketStore`, `UserStore`, `BlackjackStore`) had to reach into `db._lock` and `db.path` to manage raw connections themselves.
3. **Loss of connection-local state:** SQLite functions like `last_insert_rowid()` returned `0` when queried on a different connection than the `INSERT`.

The modernized persistence layer provides:
- **Managed connection lifecycle:** A persistent connection protected by `asyncio.Lock()`.
- **Unit of Work (`Transaction`):** Scoped via `async with db.transaction() as tx` or `async with db.write_transaction() as tx`.
- **Automatic rollback:** Automatically rolls back on any unhandled exception or `asyncio.CancelledError`.
- **PRAGMAs:** WAL mode (`PRAGMA journal_mode = WAL`), busy timeout (`PRAGMA busy_timeout = 5000`), foreign keys enabled (`PRAGMA foreign_keys = ON`), and normal synchronous mode (`PRAGMA synchronous = NORMAL`).

---

## 2. Benchmark Results

Measured via `tools/benchmark_db.py` (200 operations per phase on local SSD):

| Configuration | Metric | Connection-per-query | Managed connection | Batched transaction | Speedup vs Legacy |
|---|---|---|---|---|---|
| **DELETE Mode** | 200 writes | 0.1102s | 0.0257s | 0.0058s | **19.0x faster** |
| **DELETE Mode** | 200 reads | 0.0721s | 0.0128s | 0.0111s | **5.6x faster** |
| **WAL Mode** | 200 writes | 0.1027s | 0.0121s | 0.0052s | **19.6x faster** |
| **WAL Mode** | 200 reads | 0.0855s | 0.0095s | 0.0101s | **9.0x faster** |

### Benchmark Conclusions
- **WAL mode + managed connection** achieves a **9.0x speedup on reads** and **8.5x on non-batched writes**.
- **Batched transactions** yield a **19.6x speedup on writes** by eliminating repeated fsync checkpoints.

---

## 3. SQL DDL Catalog (Current Working Tree)

```sql
-- 1. Metadata and system flags
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 2. Core economy and user profile
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    display_name   TEXT    NOT NULL DEFAULT '',
    avatar_url     TEXT    NOT NULL DEFAULT '',
    balance        INTEGER NOT NULL DEFAULT 1000,
    created_at     INTEGER NOT NULL DEFAULT 0,
    daily_claim_ts INTEGER NOT NULL DEFAULT 0
);

-- 3. Legacy stock price history
CREATE TABLE IF NOT EXISTS stock_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    new_value  INTEGER NOT NULL,
    reason     TEXT
);

-- 4. Blackjack statistics and resets
CREATE TABLE IF NOT EXISTS blackjack_users (
    user_id      INTEGER PRIMARY KEY,
    bankroll     INTEGER NOT NULL,
    hands_played INTEGER NOT NULL DEFAULT 0,
    wins         INTEGER NOT NULL DEFAULT 0,
    losses       INTEGER NOT NULL DEFAULT 0,
    pushes       INTEGER NOT NULL DEFAULT 0,
    reset_count  INTEGER NOT NULL DEFAULT 0
);

-- 5. Job alert deduplication & checkpointing
CREATE TABLE IF NOT EXISTS job_seen (
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    first_seen_unix INTEGER NOT NULL,
    title           TEXT,
    url             TEXT,
    PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_job_seen_first_seen ON job_seen(first_seen_unix);

-- 6. Workday CXS URL discovery cache
CREATE TABLE IF NOT EXISTS workday_cxs_cache (
    careers_url       TEXT PRIMARY KEY,
    api_url           TEXT NOT NULL,
    tenant            TEXT,
    site              TEXT,
    last_updated_unix INTEGER NOT NULL
);

-- 7. Exam schedule
CREATE TABLE IF NOT EXISTS exams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_code TEXT NOT NULL,
    course_name TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER,
    location    TEXT,
    notes       TEXT,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exams_start_ts ON exams(start_ts);

-- 8. Fun Market stocks
CREATE TABLE IF NOT EXISTS fun_stocks (
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    price      INTEGER NOT NULL,
    category   TEXT NOT NULL DEFAULT 'misc',
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    week_key   TEXT,
    week_open  INTEGER
);

-- 9. Fun Market price change events
CREATE TABLE IF NOT EXISTS fun_stock_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix   INTEGER NOT NULL,
    symbol    TEXT NOT NULL,
    user_id   INTEGER NOT NULL,
    delta     INTEGER NOT NULL,
    new_value INTEGER NOT NULL,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fun_stock_events_symbol_ts ON fun_stock_events(symbol, ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_fun_stock_events_ts ON fun_stock_events(ts_unix DESC);

-- 10. Fun Market user positions (Long/Short)
CREATE TABLE IF NOT EXISTS fun_positions (
    user_id           INTEGER NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               INTEGER NOT NULL,
    avg_price         REAL NOT NULL,
    collateral_locked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, symbol, side)
);
CREATE INDEX IF NOT EXISTS idx_fun_positions_user ON fun_positions(user_id);

-- 11. Fun Market trade audit log
CREATE TABLE IF NOT EXISTS fun_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    price        INTEGER NOT NULL,
    cash_delta   INTEGER NOT NULL,
    realized_pnl INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_fun_trades_user_ts ON fun_trades(user_id, ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_fun_trades_ts ON fun_trades(ts_unix DESC);

-- 12. Weekly market settlement periods
CREATE TABLE IF NOT EXISTS fun_market_weeks (
    week_key   TEXT PRIMARY KEY,
    settled_at INTEGER NOT NULL,
    settled_by INTEGER,
    notes      TEXT
);

-- 13. Per-guild settings overrides
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id      INTEGER PRIMARY KEY,
    settings_json TEXT NOT NULL,
    updated_at    INTEGER NOT NULL
);

-- 14. Schema migration ledger
CREATE TABLE IF NOT EXISTS schema_migrations (
    version          INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    applied_at_unix  INTEGER NOT NULL,
    description      TEXT NOT NULL
);

-- 15. Namespaced grades cache (Phase 4 integration)
CREATE TABLE IF NOT EXISTS grades_course_cache (
    course_code       TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    parsed_data       JSON NOT NULL,
    last_updated_unix INTEGER NOT NULL
);
```

---

## 4. Call Site Inventory

Every query and persistence call across the codebase routes through the persistence layer:

1. **`icbot/guild_config.py`**:
   - `fetchone("SELECT settings_json FROM guild_settings WHERE guild_id=?", ...)`
   - `execute("INSERT INTO guild_settings ... ON CONFLICT ...")`
   - `execute("DELETE FROM guild_settings WHERE guild_id=?", ...)`
2. **`icbot/bot.py`**:
   - `fetchone("SELECT value FROM meta WHERE key='llm_killed'")`
   - `db.init()`, `db.close()`
3. **`icbot/features/admin.py`**:
   - `execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('llm_killed', ?)", ...)`
4. **`icbot/features/exams.py`**:
   - `execute_insert("INSERT INTO exams ...") -> exam_id` (eliminates cross-connection `last_insert_rowid` bug)
   - `fetchall("SELECT ... FROM exams ORDER BY start_ts ASC")`
   - `fetchone("SELECT id FROM exams WHERE id = ?")`
   - `execute("DELETE FROM exams WHERE id = ?")`
   - `execute("DELETE FROM exams")`
5. **`icbot/features/grades.py`**:
   - `GradesCacheStore`: `course_cache` and `grades_course_cache` caching via managed store.
6. **`icbot/features/profile.py`**:
   - Reads positions and balances via `UserStore` and `FunMarketStore`.
7. **`icbot/features/jobs.py`**:
   - `fetchall("PRAGMA table_info(job_seen)")`
   - `fetchall("SELECT source, COUNT(1) FROM job_seen GROUP BY source")`
8. **`icbot/services/user_service.py` (`UserStore`)**:
   - `ensure_user`, `add_balance`, `subtract_balance`, `claim_daily`, `get_balance`, `export_user_data`, `delete_user_data` wrapped in atomic `db.transaction(immediate=True)`.
9. **`icbot/services/fun_market.py` (`FunMarketStore`)**:
   - `admin_move`, `admin_set`, `week_rollover`, `buy_long`, `sell_long`, `open_short`, `cover_short`, `sync_catalog`, `portfolio` wrapped in atomic `db.transaction(immediate=True)`.
10. **`icbot/features/blackjack.py` (`BlackjackStore`)**:
    - `ensure_user`, `get_bankroll`, `get_reset_count`, `reserve_wager`, `refund_wager`, `apply_delta_and_stats`, `reset_bankroll` wrapped in atomic `db.transaction(immediate=True)`.
11. **Supervised Tasks (`icbot/tasks/*`)**:
    - `ttc_alerts.py`, `market_watch.py`, `market_daily_report.py`, `go_alerts.py`, `btc_watch.py`: `meta` table checkpoint reads and writes.
    - `job_alerts.py`, `jobs_workday_alerts.py`: `job_seen` and `workday_cxs_cache` reads and batch writes.

---

## 5. Migration Ledger & Forward Upgrade Safety

### Ledger Definitions
- **Version 1 (`0001_baseline_schema`):** Baseline schema defining all 13 core application tables, primary keys, and indexes. Idempotent on existing installs.
- **Version 2 (`0002_grades_course_cache`):** Namespaced grades cache table (`grades_course_cache`) enabling clean integration of educational tools without detached raw SQLite files.
- **Version 3 (`0003_add_foreign_keys_and_indexes`):** Performance indexes for `fun_trades(symbol)`, `stock_events(user_id)`, and `exams(course_code)`.

### Safety Checks & Preflight Protocol
1. **Preflight integrity validation:**
   - `PRAGMA integrity_check;` must return `ok`.
   - `PRAGMA foreign_key_check;` must return 0 violations.
2. **Dry-run planning:**
   - Operators can execute `db.plan_migrations()` to inspect pending versions and statements before execution without touching database state.
3. **Atomic execution:**
   - Each migration runs inside `BEGIN IMMEDIATE` with automatic rollback on failure.
   - `PRAGMA user_version` and `schema_migrations` are updated atomically with the DDL changes.
4. **Offline backup procedure:**
   - `await db.backup(destination_path)` invokes the SQLite Online Backup API after a WAL checkpoint, guaranteeing crash-consistent snapshots.

