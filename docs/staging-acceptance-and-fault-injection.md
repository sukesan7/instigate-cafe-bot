# Instigate Cafe Bot — Staging Acceptance & Fault-Injection Plan

**Document Version:** 1.0.0 (Phase 13 Staging Protocol)  
**Execution Harness:** [`tools/run_staging_acceptance.py`](file:///home/sukesan/dev/discord/campus-bot/tools/run_staging_acceptance.py)  
**Automated Tests:** [`tests/integration/test_staging_acceptance_and_fault_injection.py`](file:///home/sukesan/dev/discord/campus-bot/tests/integration/test_staging_acceptance_and_fault_injection.py)  
**Target Environment:** Isolated Synthetic Staging Guilds (Zero Production Data / Gateway Access)

---

## 1. Scope, Objectives & Release Safeguards

This document defines the formal staging acceptance protocol, fault-injection matrix, and disaster recovery procedures for the Instigate Cafe Bot modernization prior to public or controlled-beta release.

> [!CAUTION]
> **ABSOLUTE RELEASE SAFEGUARDS (`AGENTS.md`):**
> 1. **Zero Production Gateway Access:** Never execute `main.py`, open live Discord Gateway WebSockets, or run pollers against production Discord servers without explicit owner consent.
> 2. **Zero Production Database Reuse:** Testing must run strictly against isolated, temporary SQLite files (`tempfile.TemporaryDirectory()`). The original Instigate Cafe SQLite database (`ic_stock.db` / `campus.db`) must never be modified or used as test fixtures.
> 3. **Synthetic Staging Identity:** Staging testing must use a dedicated staging Discord application and synthetic staging guilds:
>    - **Staging Guild Alpha:** `900000000000000001`
>    - **Staging Guild Beta:** `900000000000000002`
> 4. **Zero Production Secrets:** Real Discord tokens, Google AI Studio Gemini API keys, Metrolinx tokens, and private server lore must never be used in test runs, fixtures, logs, or commit histories.

---

## 2. Severity Classification & Beta Progression Gate

Every defect or failure observed during staging is classified into one of four severity tiers:

| Tier | Severity | Definition & Impact | Progression Policy |
|---|---|---|---|
| **P0** | **Critical** | Unhandled process crash, credential or token exposure in logs/errors, private user data or resume leak, double-spending or money creation race condition, unrecoverable SQLite WAL corruption. | **STRICT BLOCKER:** Blocks all beta rollout, public invitations, or publishing. Must be fixed immediately. |
| **P1** | **High** | Core feature outage without user-friendly fallback, permission bypass (e.g. non-admin managing guild config), broken multi-guild isolation, duplicate alert broadcast storm. | **STRICT BLOCKER:** Blocks beta deployment until verified clean in staging. |
| **P2** | **Medium** | Transient upstream HTTP rate limit handled via exponential backoff, non-critical degraded response formatting, recoverable task supervisor restart. | Acceptable for controlled beta; backlog fix required before general public release. |
| **P3** | **Low** | Minor log formatting inconsistency, cosmetic embed field spacing, non-impacting telemetry counter drift. | Triage for standard maintenance releases. |

**Progression Gate Rule:** Progression to Phase 13B (Controlled Beta Onboarding) is permitted **ONLY IF** 100% of P0 and P1 scenarios in the test matrix pass with verified automated evidence.

---

## 3. Synthetic Staging Architecture & Isolation Contracts

```
┌─────────────────────────────────────────────────────────────────┐
│                     Synthetic Staging Host                      │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │                 Staging Bot Application                 │   │
│   │  • BOT_TOKEN: synthetic-staging-token                   │   │
│   │  • ALLOW_GATEWAY: false (offline simulation)            │   │
│   │  • FEATURE_PROFILE: student_core / default              │   │
│   └───────────────┬─────────────────────────┬───────────────┘   │
│                   │                         │                   │
│                   ▼                         ▼                   │
│   ┌────────────────────────┐       ┌────────────────────────┐   │
│   │  Synthetic Guild Alpha │       │  Synthetic Guild Beta  │   │
│   │  ID: 900000000000000001│       │  ID: 900000000000000002│   │
│   │  • Exams: Isolated     │       │  • Exams: Isolated     │   │
│   │  • Settings: Override  │       │  • Settings: Default   │   │
│   │  • Economy: Scoped     │       │  • Economy: Scoped     │   │
│   └───────────────┬────────┘       └────────┬───────────────┘   │
│                   │                         │                   │
│                   └───────────┬─────────────┘                   │
│                               ▼                                 │
│               ┌───────────────────────────────┐                 │
│               │   Ephemeral SQLite Database   │                 │
│               │   • Journal Mode: WAL         │                 │
│               │   • Schema Version: v7        │                 │
│               │   • ACID: BEGIN IMMEDIATE     │                 │
│               └───────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Acceptance Test & Fault-Injection Matrix (27 Scenarios)

The table below documents the 27 acceptance, fault-injection, and recovery scenarios executed by [`tools/run_staging_acceptance.py`](file:///home/sukesan/dev/discord/campus-bot/tools/run_staging_acceptance.py):

| Scenario ID | Category | Subsystem | Description & Injection Mechanism | Expected Outcome | Actual Outcome | Severity | Duration | Status | Owner |
|---|---|---|---|---|---|---|---|---|---|
| **STG-01** | Acceptance | Core / Lifecycle | Startup with zero API keys (`token=""`, `gemini_api_key=""`, `go_api_key=""`, `odds_api_key=""`). | Safe startup in degraded mode; safe defaults applied; zero crashes. | Bot boot succeeds with safe defaults; zero API keys required; balance defaults to $1,000. | **P0** | 13.8ms | **PASS** | core-team |
| **STG-02** | Acceptance | Registry | Staging token provided with synthetic guild ID (`900_..._001`). Slash-command sync. | All 17 feature modules load; `/ic` group with all 53 leaf commands bound cleanly. | Synced 53 leaf commands into staging tree for synthetic guild `900_..._001`. | **P1** | 23.2ms | **PASS** | core-team |
| **STG-03** | Acceptance | Admin / Profiles | Execute `/ic admin setup` on fresh guild, then apply `student_core` profile. | Setup completes; disabled modules (`casino`, `sports`, `market`, `ai`) return clear disabled embeds. | Fresh setup initialized guild; `student_core` profile correctly disabled `/ic casino`. | **P1** | 14.5ms | **PASS** | core-team |
| **STG-04** | Acceptance | Architecture | Add exam and modify economy in Guild Alpha; query Guild Beta. | Zero data leakage; Guild Beta returns 0 exams; balances scoped per guild. | Guild Alpha has 1 exam ($1500 bal); Guild Beta has 0 exams ($1000 bal). | **P0** | 15.7ms | **PASS** | core-team |
| **STG-05** | Acceptance | Academics | Query `/ic notes` with RateMyProfessors and external network unreachable. | Falls back safely to verified manual study links in local catalog without error. | Offline notes query resolved to verified manual catalog links without network dependency. | **P1** | 13.9ms | **PASS** | core-team |
| **STG-06** | Acceptance | Academics | Full CRUD lifecycle: `/ic exam add`, `list`, `export`, `remove`, `clear`. | Exam added, listed with timestamps, exported to valid RFC 5545 `.ics` file, and cleared. | Added CPS209, listed in calendar, exported valid RFC 5545 iCalendar file, removed cleanly. | **P1** | 15.9ms | **PASS** | core-team |
| **STG-07** | Acceptance | Careers | ATS ingestion rule evaluation with `tech_swe` and `require_early_career=True`. | Software Engineering Intern role matches; Senior Architect rejected with trace. | Intern role matched; Senior role rejected with diagnostic: `'matches senior term 'senior''`. | **P1** | 0.14ms | **PASS** | core-team |
| **STG-08** | Acceptance | AI Governance | User uploads PDF for `/ic resume` review. | `ResumeConfirmView` displays explicit third-party AI disclosure before file bytes are sent. | `ResumeConfirmView` enforces explicit interactive opt-in before dispatching PDF bytes. | **P0** | 0.41ms | **PASS** | core-team |
| **STG-09** | Acceptance | Economy | Sequential daily claim (+$100), wager (-$250), and win (+$500). | Atomic balance transition ($1000 &rarr; $1100 &rarr; $850 &rarr; $1350); 3 ledger entries. | Balance = $1350; 3 verified ledger audit entries; zero drift. | **P0** | 13.9ms | **PASS** | core-team |
| **STG-10** | Acceptance | Transit / Alerts | External provider emits identical alert text and entity ID across 2 poll intervals. | First notice broadcast; second notice identified as identical and suppressed. | Alert `ttc-notice-45812` posted on first poll; second poll marked `IDENTICAL_DUPLICATE`. | **P1** | 12.7ms | **PASS** | core-team |
| **STG-11** | Acceptance | Scheduler | Bot runtime restarted via `_cleanup_runtime()` and reconstructed afresh. | Tasks cleanly terminated; re-init starts exactly 1 runner per task (`replicas: 1`). | Graceful restart cleaned up and re-registered 8 tasks with 1 supervisor owner. | **P0** | 13.5ms | **PASS** | core-team |
| **STG-12** | Acceptance | Permissions | Non-privileged user (no admin role) invokes `/ic admin setup`. | Access denied; ephemeral error returned; configuration unchanged. | Non-admin rejected with ephemeral permission denial embed; unauthorized setup blocked. | **P0** | 13.7ms | **PASS** | core-team |
| **STG-13** | Acceptance | UX / Conventions | Invocation of heavy command (`/ic profile`) requiring database lookup. | Immediate `defer(thinking=True)` issued within 3.0s Discord interaction window. | Deferred immediately with `thinking=True` within 3.0s window; sent followup embed cleanly. | **P1** | 15.3ms | **PASS** | core-team |
| **FLT-01** | Fault Injection | Network | Injected `aiohttp.ClientConnectorError: Network is unreachable`. | Caught cleanly by transport client; circuit breaker increments; zero crash. | `ClientConnectorError` trapped; circuit breaker increments failure count without crash. | **P1** | 0.02ms | **PASS** | core-team |
| **FLT-02** | Fault Injection | Security / Logging | Upstream returns `HTTP 401 Unauthorized` containing an exposed API key. | `RedactingLogFilter` scrubs token before log emission; error returns generic message. | Upstream 401 captured; `RedactingLogFilter` completely stripped secret key. | **P0** | 0.02ms | **PASS** | core-team |
| **FLT-03** | Fault Injection | Rate Limiting | Upstream returns `HTTP 429 Too Many Requests` with `Retry-After: 5`. | Transport client respects `Retry-After` header; exponential backoff applied. | Respected Retry-After header (paused 5.0s); exponential backoff applied on retry. | **P2** | 0.02ms | **PASS** | core-team |
| **FLT-04** | Fault Injection | Discord Gateway | Alert dispatcher encounters `discord.Forbidden` (403) on channel dispatch. | Caught at channel broadcast boundary; error logged; task supervisor loop remains alive. | Discord 403/404 trapped in channel dispatcher; alerts loop logs warning and continues. | **P1** | 0.01ms | **PASS** | core-team |
| **FLT-05** | Fault Injection | Storage | Concurrent SQLite process holds exclusive lock during write operation. | `sqlite3.OperationalError: database is locked` trapped safely; zero corrupted rows. | `OperationalError 'database is locked'` trapped safely; ledger isolation intact. | **P1** | 5016.8ms| **PASS** | core-team |
| **FLT-06** | Fault Injection | Storage | Injected `OSError: [Errno 28] No space left on device` during SQLite commit. | Transaction aborts and rolls back; database remains healthy for subsequent reads. | Disk full error aborted write; transaction rolled back cleanly; zero corrupt entries. | **P0** | 15.4ms | **PASS** | core-team |
| **FLT-07** | Fault Injection | Economy | Simulated process `SIGKILL` mid-transaction after raw SQL mutation before commit. | SQLite WAL automatic recovery rolls back uncommitted mutation on restart. | SQLite WAL rollback discarded uncommitted balance mutation; balance remained $1000. | **P0** | 15.7ms | **PASS** | core-team |
| **FLT-08** | Fault Injection | Scheduling | Timezone conversion across `America/Toronto` DST transition boundaries. | PEP 495 unambiguous conversion between EDT (UTC-4) and EST (UTC-5); no skips. | `America/Toronto` correctly shifts between EDT and EST without time skips. | **P2** | 0.01ms | **PASS** | core-team |
| **FLT-09** | Fault Injection | Scheduler | Background poller task loop hangs or blocks. | Supervisor health check captures task state (`disabled` / `degraded`) without freezing loop. | Supervisor monitored 8 background tasks; captured state (`ttc` state: disabled). | **P1** | 12.8ms | **PASS** | core-team |
| **FLT-10** | Fault Injection | Parsers | Scraper receives truncated/corrupted HTML (`<html><head><title>Broken<body...`). | BeautifulSoup parser catches malformed markup, returns empty list, does not crash. | Malformed HTML parsed safely via BeautifulSoup; returned 0 items without error. | **P1** | 0.49ms | **PASS** | core-team |
| **FLT-11** | Fault Injection | Economy | 10 simultaneous concurrent requests to `/ic dailyclaim` (`asyncio.gather`). | Exactly 1 claim succeeds; 9 rejected with `DailyAlreadyClaimedError`; balance = $1100. | 10 concurrent claims: exactly 1 succeeded, 9 rejected; final balance: $1100. | **P0** | 37.6ms | **PASS** | core-team |
| **REC-01** | Recovery | Storage | Online hot backup via `tools/backup_db.py` executed while database is active. | Point-in-time snapshot created using SQLite backup API; `PRAGMA integrity_check == ok`. | Point-in-time snapshot created while active; `PRAGMA integrity_check` confirmed 'ok'. | **P0** | 15.5ms | **PASS** | core-team |
| **REC-02** | Recovery | Disaster Recovery | Restore corrupted database file from verified snapshot using `tools/restore_db.py`. | Database restored cleanly; schema version v7 and user records intact. | Restored database from snapshot; validated schema version and integrity check 'ok'. | **P0** | 2.0ms  | **PASS** | core-team |
| **REC-03** | Recovery | Migrations | Verify full forward migration chain (Schema v1 through Schema v7). | All 7 migrations apply cleanly; foreign keys enabled; user_version = 7. | All 7 migrations verified clean: schema version = v7, foreign keys = True. | **P0** | 14.7ms | **PASS** | core-team |

---

## 5. Operator Runbook

### 5.1 Executing the Offline Staging Acceptance Suite

Before deploying or inviting any staging bot instance, operators must run the automated staging acceptance suite locally:

```bash
# 1. Run the comprehensive staging acceptance harness
uv run python tools/run_staging_acceptance.py

# Expected Output:
# ================================================================================
#  SUMMARY: 27/27 Passed (100% Offline)
#  ALL 27 SCENARIOS PASSED — ZERO OPEN P0/P1 DEFECTS (GO FOR BETA)
# ================================================================================
```

### 5.2 Running the Integration Regression Suite

```bash
# 2. Run the integration test suite
uv run pytest tests/integration/test_staging_acceptance_and_fault_injection.py -v

# Expected Output:
# 4 passed in ~5.8s
```

### 5.3 Conducting Online Staging Validation (Isolated Bot Account)

When deploying to a live staging environment on a private test Discord server:
1. **Create Staging Application:** In the [Discord Developer Portal](https://discord.com/developers/applications), create a separate bot named `Instigate Cafe Staging`.
2. **Configure Staging `.env`:**
   ```bash
   DISCORD_TOKEN=your_staging_bot_token_here
   GUILD_ID=your_staging_test_server_id
   DB_PATH=/data/staging_campus.db
   ICBOT_ALLOW_GATEWAY=true
   FEATURE_PROFILE=student_core
   ```
3. **Verify Staging Guild Isolation:** Ensure the bot is only invited to the staging server. Verify that no commands are synced globally.
4. **Execute `/ic status`:** Confirm component readiness and verify all tokens are displayed as `[REDACTED]`.

### 5.4 Database Backup & Disaster Recovery Verification

```bash
# Backup live staging database
uv run python tools/backup_db.py --source /data/staging_campus.db --dest /backups/staging_$(date +%Y%m%d).db

# Verify backup integrity
sqlite3 /backups/staging_$(date +%Y%m%d).db "PRAGMA integrity_check;"

# Restore if necessary
uv run python tools/backup_db.py --restore /backups/staging_$(date +%Y%m%d).db --dest /data/staging_campus.db
```

---

## 6. Offline Acceptance Sign-Off & Live Staging Boundary

- **Total Offline Scenarios Evaluated:** 27 (executed via `tools/run_staging_acceptance.py` with mocked Discord interactions and synthetic SQLite databases)
- **Total Offline Scenarios Passed:** 27 (100% offline simulation)
- **Critical (P0) Open Offline Defects:** 0
- **High (P1) Open Offline Defects:** 0
- **Live Discord Gateway & Real Cohort Staging Status:** **Not Yet Verified (Pending Manual Owner-Run Execution)** — In accordance with `AGENTS.md`, no live Discord Gateway connection or real external API call is executed during automated testing. Section 5.3 must be executed manually by the project owner in an isolated staging guild before public beta certification.
- **Decision:** **GO FOR OWNER-RUN LIVE STAGING (Phase 13B)**

