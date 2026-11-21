# Instigate Cafe Bot — Troubleshooting & Incident Runbook

This runbook helps self-hosters, server administrators, and developers diagnose common runtime, Discord permission, database, and external provider failures using the bot's built-in diagnostics and 12-family failure taxonomy.

---

## 1. Built-In Diagnostics (`/ic status`)

Start every investigation with in-Discord diagnostics:
- **`/ic status`:** Reports runtime version, active SQLite schema version (Schema v7 WAL), per-module readiness (`ready`, `disabled`, `degraded`, `missing_config`), background task supervisor states, and channel permission checks.
- **`/ic status issue_report:True`** *(requires Manage Server / Allowed Role)*: Attaches a sanitized **`status_issue_report.md`** file safe to paste into GitHub bug reports. All tokens, API keys, host filesystem paths, bound SQL parameters, and prompt payloads are scrubbed automatically via [`icbot/errors.py`](../icbot/errors.py) and [`RedactingLogFilter`](../icbot/logging.py).
- **`/ic botinfo`:** Shows active LLM model configuration and privacy/retention disclosures.

---

## 2. Startup & Configuration Errors

### 2.1 Bot Refuses to Connect to Discord (`ICBOT_ALLOW_GATEWAY`)
- **Symptom:** Running `python main.py` exits immediately with a gateway safeguard notice.
- **Cause:** By design, live Discord connections are disabled by default to prevent accidental runs in development or CI.
- **Fix:** Pass `--allow-gateway` (`uv run python main.py --allow-gateway`) or set `ICBOT_ALLOW_GATEWAY=true` in your runtime environment (Docker Compose / systemd / Railway).

### 2.2 `DatabasePathError` on Startup
- **Symptom:** Startup fails closed with `DatabasePathError` (permission denied or unwritable parent directory).
- **Cause:** `prepare_sqlite_path()` verifies that `DB_PATH`'s parent directory exists and is writable by the current user (`UID 10001` in Docker). The bot intentionally refuses to fall back silently to a different path.
- **Fix:**
  - Docker: `mkdir -p data && chmod 700 data && sudo chown 10001:10001 data` and verify `DB_PATH=/data/campus.db`.
  - systemd: Confirm `ReadWritePaths=/opt/campus-bot/data` and `chown -R campusbot:campusbot /opt/campus-bot/data`.
  - Railway: Confirm a persistent volume is mounted at `/data` and `DB_PATH=/data/campus.db` is set.

### 2.3 `DeprecationWarning: SQLITE_PATH`
- **Symptom:** Startup logs warn that `SQLITE_PATH` is deprecated.
- **Fix:** Replace `SQLITE_PATH` with `DB_PATH` in your `.env` or hosting dashboard (`SQLITE_PATH` is scheduled for removal in `v1.1.0`).

### 2.4 `ConfigValidationError`
- **Symptom:** Boot halts reporting an invalid integer, channel ID, timezone, or polling interval.
- **Fix:** Check the flagged variable against [Configuration Reference](configuration.md). Secrets are automatically redacted from validation messages.

---

## 3. Discord Permissions, Slash Commands & Interaction Issues

| Symptom | Likely Cause | Resolution |
| :--- | :--- | :--- |
| `/ic` commands do not appear in your server | Missing `applications.commands` OAuth2 scope or stale command cache | Re-invite with scopes `bot` + `applications.commands` (bitmask `277025507392`). For guild-scoped dev servers, set `GUILD_ID` and boot once with `FORCE_GUILD_RESYNC=true`, then reset to `false`. |
| Background alert channel receives nothing | Missing `Send Messages` / `Embed Links` permission or channel ID set to `0` | Verify channel IDs via `/ic admin config` or `/ic status`, and check channel role overrides. |
| `"This feature is disabled in this server"` | Module disabled by `FEATURE_PROFILE` or `DISABLED_FEATURES` / guild override | Run `/ic admin features` to inspect active modules and enable the desired subsystem. |
| `"Only the user who ran this command can interact..."` | Another member clicked buttons on an actor-bound pagination/confirmation view | Expected security isolation (see [UI Style & Interaction-Audience Matrix](ui-style.md#6-interaction-audience-matrix-component-lifetimes)). Run your own command to open a personal view. |
| `"This command can only be used within a server"` | Economy, casino, ICSE, or admin command invoked in Direct Messages | Run the command inside a guild text channel. |
| `@mention` AI assistant does not reply | `LLM_ENABLED=false`, `/ic admin llm_stop` active, missing `GEMINI_API_KEY`, or **Message Content Intent** disabled in Discord Developer Portal | Enable `LLM_ENABLED=true`, supply `GEMINI_API_KEY`, run `/ic admin llm_start`, and enable Message Content Intent in the Developer Portal (self-hosted servers only). |

---

## 4. 12-Family Failure Taxonomy & Provider Outages

All command handlers and background pollers normalize failures into 12 typed families in [`icbot/errors.py`](../icbot/errors.py):

| Failure Family | User Experience | Operator Action |
| :--- | :--- | :--- |
| `validation` / `permission` / `setup_required` / `feature_disabled` | Ephemeral guidance embed explaining the missing input, role, setup step, or module flag. | Adjust `/ic admin setup`, `/ic admin roles`, or `/ic admin features`. |
| `missing_api_key` | Clear notice stating which optional feature is unconfigured (never leaks secret values). | Set the optional key (`GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`) or keep the module disabled. |
| `rate_limit` (`HTTP 429`) | Advises retrying after cooldown; pollers honor upstream `Retry-After` headers. | Increase poll intervals (`JOBS_POLL_SECONDS`, `GO_POLL_SECONDS`) or lower `LLM_COOLDOWN_SECONDS` exposure. |
| `timeout` / `network` / `upstream_http` / `malformed_payload` | Graceful fallback embed (cached data or offline checklist) with zero raw stack traces. | Check upstream service status; review redacted logs with correlation IDs. |
| `storage` (`sqlite3.Error`) | Ephemeral error notice; atomic transactions roll back cleanly so balances never drift. | Check disk space, permissions, single-replica lock, and `PRAGMA integrity_check` (Section 5). |
| `internal` | Safe generic error embed with correlation ID; full redacted traceback logged server-side. | Attach `/ic status issue_report:True` and redacted log excerpt to a bug report. |

### 4.1 Outage Dampening & Circuit Breakers
- **`ProviderOutageTracker`:** Logs the first upstream failure at `WARNING`, suppresses repeated failures during a sustained outage to `DEBUG`, and emits an `INFO` log when the upstream recovers.
- **`ProviderCircuitBreaker` ([`icbot/services/http_transport.py`](../icbot/services/http_transport.py)):** Opens after consecutive upstream failures (`failure_threshold`, default `3`–`5` depending on client) for a cooldown window (default `60s`), fast-failing requests without blocking the asyncio event loop before probing recovery in `HALF_OPEN` state.

---

## 5. SQLite WAL Locks & Database Integrity Issues

### 5.1 `database is locked`
1. **Confirm single replica:** Ensure only **one** bot process is running against `DB_PATH` (`docker ps`, `systemctl status`, Railway replica count `= 1`).
2. **Confirm local disk:** SQLite WAL locks fail on NFS/SMB network mounts; move `DB_PATH` to a local volume.
3. **Never copy live WAL DBs with `cp`:** Use `tools/backup_db.py` for online snapshots.

### 5.2 Suspected Corruption or Failed Migration
1. Stop the bot process cleanly (`docker compose stop` or `systemctl stop`).
2. Verify database integrity:
   ```bash
   uv run python tools/backup_db.py --verify data/campus.db
   ```
3. If integrity fails, restore the latest verified snapshot following the [Restore Procedure](deployment.md#72-restore-procedure-requires-service-stop).

---

## 6. Emergency Incident Controls

- **Immediate AI Pause (No Restart):** Run `/ic admin llm_stop` in Discord to set `meta.llm_killed=1` and halt all Gemini chat and resume API calls immediately.
- **Compromised Credential:** Revoke the token/key immediately at the upstream provider and follow [Credential Rotation](deployment.md#81-emergency-token-and-key-rotation) and [`SECURITY.md`](../SECURITY.md).
