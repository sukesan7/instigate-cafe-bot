# Instigate Cafe Bot — Operations, Maintenance & Incident Runbook

**Document Version:** 2.0.0 (Phase 12 Operations Specification)  
**Target Audience:** Server administrators, devops engineers, and self-hosted bot maintainers.  
**Key Invariants:** Single scheduler process ownership, automated log redaction, online hot backups, and outage-dampened circuit breakers.

---

## 1. System Health Monitoring & Diagnostics

### 1.1 In-Discord Diagnostics
Server administrators can inspect live runtime health directly within Discord:
- **`/ic status`:** Displays runtime version, active SQLite schema version (e.g. v7 WAL), module enablement states, active background poller states, and a channel permission audit.
- **`/ic status issue_report:True`:** Generates and attaches a sanitized diagnostic markdown file (`status_issue_report.md`) with all API keys, bot tokens, and user PII completely redacted. This file can be safely attached to GitHub issues.
- **`/ic botinfo`:** Public informational diagnostic displaying active LLM model, integration readiness, and data privacy policies.

### 1.2 Log Monitoring & Secret Redaction
All standard output and file logs pass through [`RedactingLogFilter`](../icbot/logging.py):
- **Automated Scrubbing:** Discord bot tokens, Gemini API keys, AWS keys, GitHub tokens, Bearer headers, query string keys (`?key=...`), and user mentions (`<@123>`) are replaced with `[REDACTED]` prior to emission.
- **Structured Correlation IDs:** Log entries include correlation IDs (`interaction_id`, `task_name`, `provider`) allowing maintainers to trace individual user requests across async boundaries.

---

## 2. Third-Party Outage Management & Circuit Breakers

External APIs (Metrolinx, TTC Mastodon, RateMyProfessors, Gemini) occasionally experience latency or 5xx server outages. The bot handles these gracefully:

### 2.1 Outage Dampening via `ProviderOutageTracker`
- **Initial Failure:** Logged once at `WARNING` severity.
- **Sustained Outage:** Subsequent repeated failures are suppressed and logged only at `DEBUG` level to prevent log file flooding.
- **Recovery:** When the provider recovers, an `INFO` log entry announces that the outage has resolved.

### 2.2 Circuit Breakers
- If a provider fails 3 consecutive times, its circuit breaker opens for 60 seconds.
- While the circuit is OPEN, the bot fast-fails requests immediately without dispatching network calls, conserving system resources and preventing event loop starvation.

---

## 3. Database Migrations & Versioning

The bot uses a forward-migrating SQLite schema system managed in [`icbot/db.py`](../icbot/db.py):

### 3.1 Schema Version Invariant
- Every migration increments `PRAGMA user_version`.
- Current release runs on **Schema v7** with Write-Ahead Logging (`PRAGMA journal_mode=WAL`).
- Migrations are idempotent and run automatically inside an atomic transaction on startup during `Database.init()`.

### 3.2 WAL Mode Maintenance
WAL mode enables concurrent reads without blocking writes. In production:
- WAL checkpoints run automatically via SQLite's default passive checkpointing.
- The database is cleanly flushed during graceful shutdown (`CampusBot.close()`).

---

## 4. Backup & Disaster Recovery Runbook

### 4.1 Scheduled Backups
In SQLite WAL mode, copying the file using standard shell `cp` while the bot is running can yield corrupt snapshots. Backups must be performed using SQLite's native online backup mechanism (either via `VACUUM INTO 'backup.db'` or Python's `sqlite3.connect().backup()`).

Use [`tools/backup_db.py`](../tools/backup_db.py) to perform non-blocking online hot backups while the bot is active:

```bash
# Add to host crontab (runs daily at 03:00)
0 3 * * * /opt/campus-bot/.venv/bin/python /opt/campus-bot/tools/backup_db.py --source /opt/campus-bot/data/campus.db --dest /backups/campus_$(date +\%Y\%m\%d).db >> /var/log/campus-bot-backup.log 2>&1
```

### 4.2 Database Restore Procedure
If database corruption occurs or a rollback is required:
1. Stop the bot service:
   ```bash
   docker compose stop campus-bot
   # or: sudo systemctl stop campus-bot.service
   ```
2. Restore from a verified backup (automatically archives current DB to `.bak`):
   ```bash
   uv run python tools/backup_db.py --restore /backups/campus_20261119.db --dest data/campus.db
   ```
3. Restart the service:
   ```bash
   docker compose start campus-bot
   # or: sudo systemctl start campus-bot.service
   ```

---

## 5. Single Active Scheduler Process Ownership

> [!CAUTION]
> **Horizontal Autoscaling Prohibition:**  
> The Instigate Cafe Bot must **NEVER** run with more than one active replica per database.

**Why single-instance ownership is mandatory:**
1. **Duplicate Broadcasts:** Multiple running instances will poll transit and job feeds simultaneously, posting duplicate announcements into Discord channels.
2. **Double-Spending & Settlement Races:** Casino games and stock trades would execute against non-coordinated memory states, causing balance drift and double settlement.
3. **SQLite Database Locks:** Multiple processes writing concurrently to a shared SQLite file over network mounts (NFS, SMB) can trigger `database is locked` errors and WAL corruption.

In Docker Compose and Railway, always pin `replicas: 1`.
