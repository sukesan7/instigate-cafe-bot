# Instigate Cafe Bot — System Architecture & Developer Reference

**Release:** `v1.0.0-rc1` (Modular Service Architecture)  
**Storage Engine:** SQLite 3 in Write-Ahead Logging (WAL) mode (Schema v7)  
**Framework:** Python 3.11–3.13 / `discord.py` 2.3+ (`app_commands`)  
**Design Principles:** Zero import-time side effects, centralized `ServiceContainer` dependency injection, single-instance `TaskSupervisor`, and zero-token offline testability.

---

## 1. System Component Architecture

The modernized Instigate Cafe Bot is structured around a centralized **Service Container** and **Feature Registry** that decouple presentation (Discord UI interactions) from business logic, data persistence, and external HTTP providers.

```mermaid
flowchart TD
    subgraph DiscordPlatform["Discord Platform"]
        Gateway["Discord Gateway (WSS)"]
        Interactions["Application Commands (/ic)"]
        Channels["Text Channels / Announcements"]
    end

    subgraph CoreApplication["Bot Application Core"]
        Main["main.py (CLI Entrypoint & Safety Gates)"]
        Bot["CampusBot (discord.Client & Lifecycle Owner)"]
        Container["ServiceContainer (Dependency Injection)"]
        Registry["FeatureRegistry & Manifests (53 Commands)"]
        Supervisor["TaskSupervisor (8 Background Tasks)"]
    end

    subgraph DomainServices["Domain Engines & Services"]
        Economy["Economy & Casino Engines (Blackjack, Roulette, Slots)"]
        Academics["Academic Services (Notes, RMP, Exam Calendar)"]
        Careers["Jobs Engine & ATS Parsers (Greenhouse, Workday, Lever)"]
        LLMAssistant["Gemini Governance & Resume Engine"]
        Alerts["Deduplicated Alert Dispatcher"]
    end

    subgraph StorageLayer["Data & Persistence Layer"]
        Database["Database (aiosqlite Connection Pool)"]
        SQLiteDB[("campus.db (WAL Mode, Schema v7)")]
        AuditLedger[("Ledger & Transaction Tables")]
    end

    subgraph ExternalProviders["External Third-Party APIs"]
        GeminiAPI["Google Gemini API (REST)"]
        RMPAPI["RateMyProfessors (GraphQL)"]
        MetrolinxAPI["Metrolinx Open Data API (REST)"]
        MastodonAPI["TTC Mastodon Fediverse (REST)"]
        ATSBoards["Greenhouse / Lever / Workday (REST)"]
    end

    Gateway --> Bot
    Interactions --> Registry
    Registry --> Bot
    Bot --> Container
    Container --> Database
    Database --> SQLiteDB
    Database --> AuditLedger
    Bot --> Supervisor
    Supervisor --> Alerts
    Alerts --> Channels

    Container --> Economy
    Container --> Academics
    Container --> Careers
    Container --> LLMAssistant

    Academics -.-> RMPAPI
    Careers -.-> ATSBoards
    Alerts -.-> MetrolinxAPI
    Alerts -.-> MastodonAPI
    LLMAssistant -.-> GeminiAPI
```

---

## 2. Sequence Diagram 1: Slash Command Execution Flow

This diagram illustrates how an incoming slash command (e.g. `/ic notes`, `/ic profile`, `/ic exam list`) is routed, authenticated, guarded by module enablement rules, and resolved:

```mermaid
sequenceDiagram
    autonumber
    actor User as Discord Member
    participant Discord as Discord Gateway
    participant Tree as CommandTree (/ic)
    participant Gate as check_command_gate
    participant Feature as Feature Callback
    participant Container as ServiceContainer
    participant DB as SQLite (WAL)

    User->>Discord: Type /ic notes course:CPS109
    Discord->>Tree: Deliver Interaction Object
    Tree->>Tree: Check Cooldown & Role Permissions
    Tree->>Gate: check_command_gate(interaction, "academics")
    
    alt Feature Disabled for Guild
        Gate-->>Discord: Return Ephemeral "Feature disabled in this server"
        Discord-->>User: Display rejection notice
    else Feature Enabled
        Gate->>Feature: Invoke Command Callback
        Feature->>Discord: interaction.response.defer(thinking=True)
        Feature->>Container: Query AcademicRegistry / Cache
        Container->>DB: Check Server Manual Catalog (SELECT)
        DB-->>Container: Return catalog rows
        Container-->>Feature: Return Note URLs & Provenance
        Feature->>Discord: interaction.followup.send(embed=NotesEmbed, view=PaginationView)
        Discord-->>User: Render Rich Embed with Navigation
    end
```

---

## 3. Sequence Diagram 2: Periodic Background Poller Flow

This diagram illustrates how the `TaskSupervisor` schedules and executes background loops (e.g. transit alerts, job ingestion) with circuit breakers, exponential backoff, and deduplicated channel announcements:

```mermaid
sequenceDiagram
    autonumber
    participant Clock as Asyncio Loop / Clock
    participant Supervisor as TaskSupervisor
    participant Poller as Poller Task (e.g. poll_and_post_go)
    participant Circuit as Provider Circuit Breaker
    participant Provider as External Provider API
    participant Dispatcher as AlertDispatcher
    participant Discord as Discord Text Channel

    Clock->>Supervisor: Periodic Trigger (Interval Elapsed)
    Supervisor->>Supervisor: Check Guild Enablement & Settings
    Supervisor->>Poller: Execute task iteration
    Poller->>Circuit: Check circuit state (CLOSED/HALF-OPEN/OPEN)
    
    alt Circuit Open (Outage Active)
        Circuit-->>Poller: Fast-fail (Outage Dampened, No Request)
        Poller-->>Supervisor: Record skipped iteration
    else Circuit Closed (Healthy)
        Poller->>Provider: Outbound HTTPS Request (with timeout & jitter)
        alt Request Successful
            Provider-->>Poller: 200 OK (JSON Feed Payload)
            Circuit->>Circuit: Record Success (Reset failure counter)
            Poller->>Dispatcher: dispatch_alert(feed_id, payload)
            Dispatcher->>Dispatcher: Deduplicate against sliding LRU cache
            Dispatcher->>Discord: Send Channel Embed (if not duplicate)
            Poller-->>Supervisor: Record Success Health Snapshot
        else Request Failed / Timeout / 5xx
            Provider-->>Poller: Timeout or 503 Service Unavailable
            Circuit->>Circuit: Increment failure count / Open circuit
            Poller->>Poller: Log dampened warning via ProviderOutageTracker
            Poller-->>Supervisor: Record Failure Health Snapshot
        end
    end
```

---

## 4. Sequence Diagram 3: Atomic Economy & Market Transaction Flow

This diagram illustrates how casino games (Blackjack, Roulette, Slots) and virtual stock trades (`/ic icse buy/sell`) execute with double-entry balance verification, ACID invariants, and immediate WAL synchronization:

```mermaid
sequenceDiagram
    autonumber
    actor User as Discord Member
    participant Game as Casino / Market View
    participant Engine as Pure Engine (Math / Rules)
    participant UserStore as UserStore
    participant Ledger as SQLite WAL Transaction

    User->>Game: Click "Hit" or Bet 100 on Red
    Game->>Engine: Evaluate game state & payout multipliers
    Engine-->>Game: Hand Result / Settlement Amount (+100 or -100)

    Game->>UserStore: transfer_funds(user_id, guild_id, delta, reason)
    UserStore->>Ledger: BEGIN IMMEDIATE TRANSACTION
    
    Ledger->>Ledger: SELECT balance FROM users WHERE user_id=? FOR UPDATE
    
    alt Balance Insufficient (delta < 0 and balance + delta < 0)
        Ledger-->>UserStore: Raise InsufficientFundsError
        UserStore->>Ledger: ROLLBACK
        UserStore-->>Game: Reject Action
        Game-->>User: Display "Insufficient Balance" Ephemeral Warning
    else Balance Sufficient
        Ledger->>Ledger: UPDATE users SET balance = balance + delta WHERE user_id=?
        Ledger->>Ledger: INSERT INTO transactions (user_id, delta, reason, created_at) VALUES (...)
        Ledger->>Ledger: COMMIT (WAL Checkpoint Synchronized)
        Ledger-->>UserStore: Transaction Committed (New Balance)
        UserStore-->>Game: Settlement Verified
        Game-->>User: Edit Discord Message with Updated Hand & Payout
    end
```

---

## 5. Lifecycle Management, Command Registry & Clean Shutdown

### 5.1 Zero Import-Time Side Effects & Gateway Guard
- Importing [`icbot/`](../icbot/__init__.py) or [`main.py`](../main.py) performs **zero** outbound network requests, **zero** SQLite writes, and **zero** `.env` credential exposure.
- [`icbot/registry.py`](../icbot/registry.py) explicitly registers all **17 feature modules** and **53 leaf slash commands** onto the root `/ic` command group (`icbot/app.py`).
- Live Discord gateway connections require explicit opt-in via `--allow-gateway` or `ICBOT_ALLOW_GATEWAY=true` (`validate_gateway_launch()`).

### 5.2 Clean Shutdown Sequence
[`CampusBot._cleanup_runtime()`](../icbot/bot.py) handles `SIGTERM` / `SIGINT` deterministically:
1. **Signal Interception:** `SIGTERM` or `SIGINT` triggers `CampusBot.close()`.
2. **Supervisor Cancellation:** All active background asyncio tasks are cancelled with `supervisor.stop_all()`.
3. **HTTP Session Teardown:** Open `aiohttp.ClientSession` pools are drained and closed.
4. **Database Checkpointing:** SQLite WAL frames are flushed and the managed connection is closed cleanly.

---

## 6. Unified Task Supervision (`TaskSupervisor`)

[`build_default_task_specs()`](../icbot/scheduler.py) manages all **8 background and event-driven tasks** under one coordinator with startup jitter (`TASK_STARTUP_JITTER_SECONDS`), exponential backoff (`TASK_MAX_BACKOFF_SECONDS`), and live capability reconciliation:

| Task Name | Kind | Capability Gate | Schedule / Trigger | Persisted Checkpoint |
| :--- | :--- | :--- | :--- | :--- |
| `ttc` | `periodic` | `TTC_ALERTS_CHANNEL_ID`, `TTC_MASTODON_BASE`, `TTC_MASTODON_ACCT`, `TTC_POLL_SECONDS > 0` | `[30, 86400]s` (default `120`) | `meta.ttc_account_id`, `meta.ttc_last_status_id` |
| `go` | `periodic` | `GO_ALERTS_CHANNEL_ID` (or `TTC_ALERTS_CHANNEL_ID`), `GO_API_KEY`, `GO_POLL_SECONDS > 0` | `[60, 86400]s` (default `180`) | `meta.go_last_servicealert_code` |
| `jobs` | `periodic` | `JOB_ALERTS_CHANNEL_ID`, `JOBS_POLL_SECONDS > 0`, `GREENHOUSE_BOARDS` or `LEVER_SITES` | `0` or `[60, 86400]s` | `job_seen`, `alert_delivery_dedupes` |
| `workday` | `periodic` | `JOB_ALERTS_CHANNEL_ID`, `WORKDAY_SITES`, effective interval `> 0` | `WORKDAY_POLL_SECONDS` or `JOBS_POLL_SECONDS` | `job_seen`, `workday_cxs_cache` (7d TTL) |
| `market_watch` | `periodic` | `MARKET_WATCH_ENABLED=true`, watch/stock channel, `MARKET_WATCH_POLL_SECONDS > 0` | `[10, 86400]s` (default `60`) | `meta.mw_state:*`, `meta.mw_last_alert:*` |
| `btc_watch` | `periodic` | `BTC_WATCH_ENABLED=true`, channel, `BTC_WATCH_ADDRESSES`, `BTC_WATCH_POLL_SECONDS > 0` | `[30, 86400]s` (default `300`) | `meta.btc_last_tx:*`, `meta.btc_last_alert_any` |
| `daily_market_report` | `daily_clock` | `MARKET_DAILY_REPORT_ENABLED=true`, channel | Wall clock `MARKET_DAILY_REPORT_TIME` in `MARKET_DAILY_REPORT_TZ` (DST-safe) | `meta.daily_report_last_date`, `meta.fun_auto_move_last_date` |
| `llm_assistant` | `event_driven` | `LLM_ENABLED=true`, `GEMINI_API_KEY`, `not llm_killed` | `@bot` mentions in `CampusBot.on_message` | `meta.llm_killed` + in-memory per-user cooldown |

---

## 7. SQLite WAL Persistence & Schema Migrations (v1–v7)

[`icbot/db.py`](../icbot/db.py) maintains a managed `aiosqlite` connection protected by `asyncio.Lock` with `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`, and `PRAGMA synchronous=NORMAL`. Multi-statement mutations execute inside `async with db.write_transaction()` (`BEGIN IMMEDIATE`) with automatic rollback on exception or `asyncio.CancelledError`.

### Forward Migration Ledger (`PRAGMA user_version` + `schema_migrations`)
Migrations run atomically and idempotently during `Database.init()` (inspectable beforehand via `db.plan_migrations()`):

| Version | Migration Name | Summary |
| :---: | :--- | :--- |
| `1` | `0001_baseline_schema` | Core tables (`meta`, `users`, `stock_events`, `blackjack_users`, `job_seen`, `workday_cxs_cache`, `exams`, `fun_stocks`, `fun_stock_events`, `fun_positions`, `fun_trades`, `fun_market_weeks`, `guild_settings`, `schema_migrations`). |
| `2` | `0002_grades_cache` | Adds namespaced `grades_course_cache` table. |
| `3` | `0003_indexes_and_integrity` | Adds performance indexes across trades, stock events, and exams. |
| `4` | `0004_guild_scoped_economy_and_ledger` | Multi-guild tenancy (`guild_id` partitioning) across users, Blackjack, positions, trades, stocks, plus immutable `economy_ledger`, `admin_audit_log`, and `guild_stock_catalog`. |
| `5` | `0005_guild_settings_audit` | Adds `guild_settings_audit` for configuration mutation accountability. |
| `6` | `0006_academic_catalog_and_exam_fields` | Adds `academic_courses`, `academic_resources`, and multi-campus exam metadata columns. |
| `7` | `0007_exam_scheduling_and_reminders` | Adds exam visibility (`public`/`private`), reminder checkpoints, `user_job_preferences`, `guild_job_subscriptions`, `job_alert_outbox`, and `alert_delivery_dedupes`. |

---

## 8. Subsystem Architecture Highlights

- **Dependency Injection (`ServiceContainer` — [`icbot/container.py`](../icbot/container.py)):** Owns the shared `Database`, `aiohttp.ClientSession`, `GuildConfigStore`, `AcademicRegistry`, `ProviderCircuitBreaker` instances, and `UserStore`/`FunMarketStore` factories. Eliminates global mutable singletons across tests and runtime.
- **Jobs Ingestion Pipeline ([`icbot/services/jobs/pipeline.py`](../icbot/services/jobs/pipeline.py)):** Consolidates Greenhouse, Lever, and Workday fetching behind a shared concurrency semaphore, normalized `JobPosting` models, per-source `SourceHealth` (`Retry-After` cooldowns + permanent 404/410 quarantine), explainable filter classification, and two-layer deduplication (`job_seen` + `alert_delivery_dedupes`).
- **Academic Registry & URL Safety ([`icbot/academics/`](../icbot/academics/providers.py)):** Normalizes cross-listed course aliases and enforces SSRF host/scheme validation (`validate_fetch_url`) before querying or rendering external study links.
- **Discord UI & Failure Normalization ([`icbot/ui.py`](../icbot/ui.py), [`icbot/errors.py`](../icbot/errors.py)):** Shared `BasePaginationView` and `BaseConfirmView` enforce actor ownership, guild checks, and 180s/60s timeouts; `respond_with_failure` maps all 12 failure families into safe, redacted embeds. See [UI Style Contract](ui-style.md) and [Troubleshooting](troubleshooting.md).

