# Instigate Cafe Bot — Task Supervision, Scheduling & Horizontal Scaling Design

This document specifies the Phase 3C unified task supervision architecture implemented across [`icbot/scheduler.py`](../../icbot/scheduler.py), [`icbot/bot.py`](../../icbot/bot.py), [`icbot/container.py`](../../icbot/container.py), and [`icbot/config.py`](../../icbot/config.py).

## 1. Unified Task Registry (All 8 Tasks)

[`build_default_task_specs()`](../../icbot/scheduler.py) declares all background loops and event-driven AI responders under one coordinator ([`TaskSupervisor`](../../icbot/scheduler.py)):

| Task Name | Kind | Capability Gate | Schedule / Trigger | Persisted Checkpoint / State |
| :--- | :--- | :--- | :--- | :--- |
| `ttc` | `periodic` | `TTC_ALERTS_CHANNEL_ID`, `TTC_MASTODON_BASE`, `TTC_MASTODON_ACCT`, `TTC_POLL_SECONDS > 0` | Bounded `[30, 86400]s` (default `120`) | `meta.ttc_account_id`, `meta.ttc_last_status_id` |
| `go` | `periodic` | `GO_ALERTS_CHANNEL_ID` (or `TTC_ALERTS_CHANNEL_ID`), `GO_API_KEY`, `GO_POLL_SECONDS > 0` | Bounded `[60, 86400]s` (default `180`) | `meta.go_last_servicealert_code` |
| `jobs` | `periodic` | `JOB_ALERTS_CHANNEL_ID`, `JOBS_POLL_SECONDS > 0`, non-empty `GREENHOUSE_BOARDS` or `LEVER_SITES` | Bounded `0` or `[60, 86400]s` (default `600`) | `job_seen` rows + `jobs_bad_sources` (`404` only) |
| `workday` | `periodic` | `JOB_ALERTS_CHANNEL_ID`, non-empty `WORKDAY_SITES`, effective interval `> 0` | `WORKDAY_POLL_SECONDS` or inherited `JOBS_POLL_SECONDS` (`0` or `[60, 86400]s`) | `job_seen`, `workday_cxs_cache` (7d TTL), `workday_bad_sources` (permanent `404`/config errors only) |
| `market_watch` | `periodic` | `MARKET_WATCH_ENABLED=true`, watch/stock channel, `MARKET_WATCH_POLL_SECONDS > 0` | Bounded `[10, 86400]s` (default `60`) | `meta.mw_state:*`, `meta.mw_last_alert:*` |
| `btc_watch` | `periodic` | `BTC_WATCH_ENABLED=true`, BTC/watch/stock channel, `BTC_WATCH_ADDRESSES`, `BTC_WATCH_POLL_SECONDS > 0` | Bounded `[30, 86400]s` (default `300`) | `meta.btc_last_tx:*`, `meta.btc_last_alert_any` |
| `daily_market_report` | `daily_clock` | `MARKET_DAILY_REPORT_ENABLED=true`, daily/watch/stock channel | Local wall clock `MARKET_DAILY_REPORT_TIME` (`HH:MM`) in `MARKET_DAILY_REPORT_TZ` | `meta.daily_report_last_date`, `meta.fun_auto_move_last_date` |
| `llm_assistant` | `event_driven` | `LLM_ENABLED=true`, `GEMINI_API_KEY`, and `not llm_killed` | `@bot` mentions in [`CampusBot.on_message`](../../icbot/bot.py) | `meta.llm_killed` + in-memory per-user cooldown map |

## 2. Lifecycle, Capability Gating & Live Reconciliation

```text
disabled  ──reconcile(enabled) & start()──▶  waiting_ready  ──wait_until_ready()──▶  sleeping (jitter/interval/daily target)
   ▲                                                                                          │
   │                                                                                          ▼
reconcile(disabled)                                                                        running
   ▲                                                                                       /      \
   └──── stop_all() ──▶ stopped ◀──── clean exit / cancel ─────────────────────── err ◀───┘        └─── ok ──▶ sleeping
                                                                                   │
                                                                                   ▼
                                                                             backing_off (Retry-After / exp, capped)
```

- **Zero import-time side effects:** Importing `icbot.scheduler`, `icbot.bot`, or `main` neither creates `asyncio.Task` objects nor allocates HTTP clients.
- **Reconnect idempotency:** [`CampusBot.setup_hook()`](../../icbot/bot.py) starts the supervisor once (`setup_complete=True`), and reconnect `on_ready()` events never respawn duplicate tasks.
- **Live config changes:** [`CampusBot.reconcile_tasks(settings)`](../../icbot/bot.py) (`TaskSupervisor.reconcile` / `reconcile_async`) stops newly disabled tasks immediately, spawns newly enabled ones, and reschedules sleeping `daily_clock` tasks when schedule time/timezone changes.
- **Event-driven killswitch sync:** `/ic admin llm_stop` and `/ic admin llm_start` update `meta.llm_killed`, flip `container.llm_killed`, and invoke `supervisor.sync_event_states()`, transitioning `llm_assistant` health between `paused` and `idle`.
- **Observability without secret exposure:** `bot.task_health()` returns deep-copied redacted [`TaskHealth`](../../icbot/scheduler.py) records, surfaced via the `Background Tasks` field in `/ic botinfo`. All secrets, query keys, and authorization headers are scrubbed via `redact_secrets()`.

## 3. Startup Jitter, Backoff & Upstream `Retry-After`

- **Startup jitter (`TASK_STARTUP_JITTER_SECONDS`, default `15`, valid `[0, 300]`):** After gateway readiness, each periodic task sleeps a deterministic/PRNG-injected random delay in `[0, TASK_STARTUP_JITTER_SECONDS]` before its first HTTP poll so boot-time bursts against Yahoo, Mastodon, Esplora, and ATS endpoints spread cleanly.
- **Bounded exponential backoff (`TASK_MAX_BACKOFF_SECONDS`, default `1800`, valid `[30, 86400]`):** Supervised tasks invoke pollers with `raise_on_error=True` (direct standalone test callers retain the safe default `raise_on_error=False`). Upstream HTTP `429`/`5xx`, timeouts, and Discord send failures raise into `BackoffPolicy`, sleeping `min(cap, max(base * 2^(failures-1), retry_after))`. Both numeric seconds and RFC 7231 IMF-fixdate `Retry-After` headers are honored.
- **Recovery & permanent vs transient errors:** Any clean cycle resets `consecutive_failures = 0` and clears `last_error_summary`. In `job_alerts` and `jobs_workday_alerts`, only permanent `404`/config errors enter `jobs_bad_sources` / `workday_bad_sources`; transient `429`, `5xx`, and `TimeoutError` remain retryable.
- **Cancellation discipline:** `asyncio.CancelledError` always propagates immediately out of jitter sleeps, cycle execution, interval sleeps, and `stop_all()` draining.

## 4. DST-Safe Daily Scheduling (`daily_market_report`)

[`next_local_run_timestamp()`](../../icbot/scheduler.py) and [`seconds_until_next_local_time()`](../../icbot/scheduler.py) use `zoneinfo.ZoneInfo` with PEP 495 `fold` handling:

- **Fall-back repeated hours (25-hour local day, e.g. `2026-11-01` `America/Toronto`):** Explicitly binds `fold=0` so scheduled local wall times resolve to the first occurrence and never skip or fire twice.
- **Spring-forward nonexistent gap hours (23-hour local day, e.g. `2026-03-08` `America/Toronto` when `02:00 -> 03:00` vanishes):** Detects `cand1.astimezone(UTC) < cand0.astimezone(UTC)` and binary-searches the exact transition second so gap targets (like `02:30`) execute immediately at the transition edge (`03:00 EDT`) instead of silently shifting an extra hour.
- **Same-day idempotency & fallback move fix:** Even across timezone shifts or process restarts, `post_daily_market_report()` checks `meta.daily_report_last_date` and applies [`_apply_sp500_move()`](../../icbot/tasks/market_daily_report.py) on both standard embed sends and `discord.Forbidden` plain-text fallbacks.

## 5. Shared HTTP Resource Model

[`ServiceContainer`](../../icbot/container.py) now owns a single process-wide shared `aiohttp.ClientSession` (`bot.http_session`, 15s default timeout) initialized once in `CampusBot._init_http_sessions()` and closed cleanly in `close()` or startup rollback:
- `yahoo_quotes`, `btc_esplora`, `crypto_fng`, ATS job pollers, TTC, `notes`, `grades`, `prof`, `sports`, and `gemini_client` reuse `container.http_session` (without holding redundant module global `_SESSION` handles during normal bot operation).
- A second dedicated `go_http_session` connector is allocated **only** when `GO_SSL_VERIFY=false` or `GO_CA_BUNDLE_PATH` is explicitly configured; default GO TLS traffic reuses `container.http_session`.

## 6. Single-Instance Assumptions & Distributed Lease Design for Multi-Replica Scaling

### Current Railway Assumption (`replicas = 1`)
Instigate Cafe Bot intentionally assumes a **single active process** per Discord server deployment:
1. Persistence uses local SQLite (`ic_stock.db` and `tmu_grades.db`).
2. Deduplication relies on local SQLite tables (`meta`, `job_seen`, `workday_cxs_cache`) and in-memory sets (`jobs_bad_sources`, `workday_bad_sources`, `llm_last_reply`).
3. Scheduled tasks do not acquire cross-process locks before polling or posting to Discord.

### Required Extension Before `replicas > 1`
Running multiple horizontal replicas without coordination would produce duplicate Discord alert messages and SQLite write contention. Before scaling beyond one replica:
1. **Migrate shared state from local SQLite to managed Postgres (or Redis + Postgres)** so `meta`, `job_seen`, economy ledgers, and guild overrides share transactional visibility.
2. **Introduce a `task_leases` lease table (leader election per task):**
   ```sql
   CREATE TABLE task_leases (
       task_name             TEXT PRIMARY KEY,
       lease_owner           TEXT NOT NULL,
       lease_expires_at_unix DOUBLE PRECISION NOT NULL,
       fencing_token         BIGINT NOT NULL
   );
   ```
3. **Acquire / renew via atomic compare-and-swap before running a cycle:**
   - Before each `TaskSpec.run_cycle()`, attempt `INSERT ... ON CONFLICT (task_name) DO UPDATE SET lease_owner = :self, lease_expires_at_unix = :now + :ttl, fencing_token = task_leases.fencing_token + 1 WHERE task_leases.lease_expires_at_unix < :now OR task_leases.lease_owner = :self RETURNING fencing_token`.
   - Pass `fencing_token` into checkpoint writes (`meta` / `job_seen`) or require `(source, external_id)` unique constraints before posting to Discord so a paused/stale leader whose lease expired cannot overwrite checkpoints or emit duplicate alerts.
   - `event_driven` (`llm_assistant`) remains naturally horizontally scalable once gateway sharding delivers each Discord `MESSAGE_CREATE` to at most one shard; only rate-limit counters (`llm_last_reply`) need shared Redis/DB storage if cross-shard global cooldowns are desired.

