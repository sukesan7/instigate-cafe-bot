# Instigate Cafe Bot — Manual Owner-Run Staging Acceptance Runbook

**Status:** Pending Manual Owner Execution (Not Executed by CI or Automated Agents)  
**Target Release:** `v1.0.0-rc1`  
**Audience:** Repository owner (`sukesan`) running manual live verification in an isolated private Discord test environment after all offline quality gates pass.

> [!CAUTION]
> **Preventing Production Disruption & Railway Restarts**
> 1. **Never reuse the production `DISCORD_TOKEN`:** Register a dedicated **staging Discord Application** and bot token. Running two processes simultaneously with the same token causes gateway session conflicts, duplicate replies, and double poller broadcasts.
> 2. **Verify Railway production autodeploy is OFF before pushing or testing:** Historically, GitHub pushes triggered automatic Railway redeploys. All Railway settings and deployments remain strictly manual owner actions.
> 3. **Never connect staging to a production SQLite database:** Use a fresh, isolated synthetic database path outside Git (never copy real member balances, trade ledgers, or private lore into staging).
> 4. **Never upload real student resumes or capture unredacted personal IDs/conversations** during staging or screenshot capture.

---

## 1. Pre-Flight Safety & Manual Railway Production Audit

Before starting any staging session (or pushing release commits), manually verify the production Railway service in the **Railway Web Dashboard** without making unplanned edits:

| Railway UI Setting | Required Safe State | Manual Check |
| :--- | :--- | :---: |
| **Source Repo & Branch** | Expected repository (`sukesan7/instigate-cafe-bot`) and intended branch (`main`) | `[ ]` Checked |
| **Deployment Trigger Policy** | **Autodeploy: OFF (Disabled)** — manual deploys only (note: disabling autodeploy does not stop an already running container) | `[ ]` Checked |
| **Active Deployment** | Confirm whether a legacy/production deployment is currently live and note its deployment ID before touching anything | `[ ]` Checked |
| **Start Command** | `python3 main.py --allow-gateway` | `[ ]` Checked |
| **Replica Count** | **`1`** (mandatory single-scheduler & SQLite WAL lock invariant) | `[ ]` Checked |
| **Environment Variables** | Canonical `DB_PATH=/data/campus.db`, `GRADES_DB_PATH=/data/tmu_grades.db`, `ICBOT_ALLOW_GATEWAY=true`; deprecated `SQLITE_PATH` removed once `DB_PATH` is confirmed; distinct production `DISCORD_TOKEN` (never shared with staging) | `[ ]` Checked |
| **Persisted Volume Path** | Volume mounted at **`/data`** and writable by non-root UID `10001` (`campusbot`) | `[ ]` Checked |

### 1.1 Dedicated Staging Discord Application Requirements
1. Create a separate Discord Application in the Discord Developer Portal (e.g., **`Instigate Cafe Staging`**) with a **new non-production bot token**.
2. Invite the staging bot ONLY to a **separate private testing guild** or a **deliberately isolated private test channel** (e.g., `#bot-staging-sandbox` with access restricted to the owner and a secondary test account).
3. Use **least-privilege OAuth2 scopes and permissions**:
   - Scopes: `bot`, `applications.commands`
   - Permission bitmask: **`277025507392`** (`Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`, `Use Slash Commands`, `Add Reactions`)
   - **Privileged Gateway Intents:** Leave `Server Members Intent` and `Presence Intent` **OFF**. Leave `Message Content Intent` **OFF** unless explicitly testing optional `@mention` AI replies.

---

## 2. Owner-Only Local Staging Setup, Backups, Startup & Shutdown

### 2.1 Verify Clean Offline Baseline First
From a clean checkout using managed Python 3.12 (recommended over host Arch/Garuda Python 3.14):

```bash
uv venv --python 3.12
uv sync --frozen --dev
uv run python tools/verify_offline.py
```

Confirm all 11 offline checks pass (`0` secrets leaked, `0` doc/command drift, `0` test failures) before preparing live credentials.

### 2.2 Isolated Local `.env` & Synthetic SQLite Database (Outside Git)
Because `main.py` calls `dotenv.load_dotenv(override=False)`, keep staging credentials in a local, git-ignored `.env` (`chmod 600`) pointing to an isolated directory outside the repository:

```bash
# 1. Create an isolated staging data directory outside Git
mkdir -p ~/icbot-staging-data/backups
chmod 700 ~/icbot-staging-data

# 2. Copy template to local .env (NEVER commit .env)
cp .env.example .env
chmod 600 .env
```

Set **only staging values** in your local `.env`:
- `DISCORD_TOKEN=<NEW_STAGING_BOT_TOKEN>` *(never the production token)*
- `GUILD_ID=<PRIVATE_STAGING_GUILD_ID>` *(instant guild slash-command sync)*
- `DB_PATH=/home/<you>/icbot-staging-data/staging.db` *(fresh synthetic SQLite file)*
- `GRADES_DB_PATH=/home/<you>/icbot-staging-data/staging_grades.db`
- `LORE_PATH=tools/lore.example.json` *(synthetic example lore only)*
- `ICBOT_ALLOW_GATEWAY=false` *(keep `false` in `.env` and opt in explicitly via `--allow-gateway` on the CLI)*
- Keep optional pollers disabled initially (`LLM_ENABLED=false`, `MARKET_WATCH_ENABLED=false`, `MARKET_DAILY_REPORT_ENABLED=false`, `BTC_WATCH_ENABLED=false`, `JOBS_POLL_SECONDS=0`, `WORKDAY_POLL_SECONDS=0`).

Verify `.env` and staging data are ignored and clean before starting:
```bash
git status --short
uv run python tools/scan_secrets.py --enforce-clean
```

### 2.3 Startup, Bot Account Verification & Log Redaction Check
Launch the staging bot with explicit gateway opt-in:

```bash
uv run python main.py --allow-gateway
```

Immediately verify **four safety invariants** in the console logs and Discord UI:
1. **Correct bot account online:** Confirm the startup log and Discord server member list show **`Instigate Cafe Staging`** (your staging application ID), **not** the production bot account, and confirm only one bot replies to `/ic botinfo`.
2. **Database path & Schema v7 WAL:** Confirm logs show your isolated `staging.db` path (no `DeprecationWarning: SQLITE_PATH`, no stray repo-root `ic_stock.db`).
3. **Log redaction active:** Confirm `RedactingLogFilter` emits zero raw `DISCORD_TOKEN`, `GEMINI_API_KEY`, `GO_API_KEY`, or `ODDS_API_KEY` values in stdout/stderr.
4. **Online hot backup works before tests:** In a second terminal while the staging bot is running:
   ```bash
   uv run python tools/backup_db.py \
     --source ~/icbot-staging-data/staging.db \
     --dest ~/icbot-staging-data/backups/staging_pretest.db
   uv run python tools/backup_db.py --verify ~/icbot-staging-data/backups/staging_pretest.db
   ```

### 2.4 Clean Shutdown
Press `Ctrl+C` (or send `SIGTERM`) in the bot terminal. Confirm `CampusBot.close()` cancels all `TaskSupervisor` loops, closes `aiohttp` sessions, flushes SQLite WAL cleanly, and exits with code `0` without hanging.

---

## 3. Manual Acceptance Matrix by Command Family

Execute each row inside your isolated staging test channel. Record `PASS` / `FAIL` and concise notes in the last column (never paste real user IDs, tokens, or private server data).

| # | Command Family & Routes | Expected Result | Visibility | Error / Fallback Check | Screenshot Candidate | Synthetic Data Required | Result & Notes |
| :---: | :--- | :--- | :--- | :--- | :---: | :---: | :--- |
| **1** | **Help & Onboarding**<br>`/ic getting-started`, `/ic help` | 4-step walkthrough renders with `◀ Previous Step` / `Next Step ▶`; `/ic help` shows enabled modules only (hides `/ic admin` for non-admin test account). | Public (`180s` view) | Cross-user click rejected ephemerally; buttons disable cleanly at `180s`. | **Yes** (`01-getting-started.png`, `02-help.png`) | No | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **2** | **Theme Presets**<br>`/ic admin config` (`theme`: `instigate_cafe`, `campus_generic`, `student_clean`, `toronto_tmu`, `emerald_academic`) | Accent sidebar color and branding update per guild immediately; default `instigate_cafe` uses `#A64DFF`. | Config: **Ephemeral**; subsequent embeds: Public | Invalid theme name rejected cleanly with allowed preset guidance. | Optional | No | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **3** | **Manual Notes**<br>`/ic notes course:CPS109` | Displays paginated course study-guide links with SSRF-validated URLs and source attribution. | Public (`180s` view) | Invalid input (`!`) returns supportive format tip; unknown course returns clean empty-state embed. | **Yes** (`03-notes.png`) | Synthetic/public course code (`CPS109` or guild catalog `CS101`) | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **4** | **Exams — Public, Private, Import & Export**<br>`/ic exam add`, `/ic exam list`, `/ic exam import`, `/ic exam export`, `/ic exam remove`, `/ic exam clear` | Admin adds synthetic `guild` and `private` exams; `/ic exam list` is **Public** when only `guild`/`public` exams appear and switches to **Ephemeral** automatically when a `private` exam is included; `.ics` and `.csv` export omit private exams by default; `import` parses synthetic CSV/ICS; `clear` requires confirmation. | `add`/`remove`/`clear`/`import`/`export`: **Ephemeral**; `list`: Public or **Ephemeral** (auto) | Non-admin rejected on writes; spring-forward DST time (`2026-03-08 02:30`) rejected; invalid CSV row reports line error without crash. | **Yes** (`04-exam-list.png`, `guild` exams only) | **Yes** (2–3 synthetic exams; zero real personal notes) | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **5** | **Professor & Grades (Optional Providers)**<br>`/ic prof name:...`, `/ic grades course:...` | `/ic prof` queries configured `RMP_UNIVERSITY` (TMU default); `/ic grades` renders TMU grade histogram or cached text fallback. | Public | Unknown prof/course returns graceful not-found embed; degraded upstream returns fallback guidance (no stack trace). | Optional | Public/synthetic query (`Ada Lovelace` / `CPS109`) | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **6** | **Jobs Filters & Cached-Outage State**<br>`/ic jobs`, `/ic job_preferences`, `/ic job_preview`, `/ic job_subscribe` | `/ic jobs` displays paginated postings with HTTPS-only apply links and posted/indexed dates; `/ic job_preferences` saves portable user-scoped preferences with plaintext SQLite disclosure; `/ic job_preview` explains filter match trace; `/ic job_subscribe` validates channel permissions. | `/ic jobs`: Public (`180s`); `preferences`/`preview`/`subscribe`: **Ephemeral** | Empty cache shows clean "No Cached Listings" card (zero crash when pollers disabled); non-admin rejected on `job_subscribe`. | **Yes** (`05-jobs.png`) | Harmless public ATS listings or seeded synthetic `job_seen` rows | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **7** | **Resume Consent & Rejection**<br>`/ic resume` *(with blank dummy `sample_resume.pdf` ONLY)* | Shows ephemeral `ResumeConfirmView` privacy disclosure (`Submit for AI Review` / `View No-AI Checklist` / `Cancel`) disclosing local zero-disk handling and Discord/Gemini retention terms; clicking **View No-AI Checklist** works with zero API key; **Cancel** aborts cleanly. | **Ephemeral** (`180s` view) | Missing `GEMINI_API_KEY` or `LLM_ENABLED=false` blocks AI submission gracefully while keeping offline checklist usable; oversized/bad file rejected cleanly. | Optional (`10-resume-consent.png` — consent screen only!) | **Yes** (empty dummy `sample_resume.pdf`; **NEVER real student resume**) | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **8** | **Virtual Economy, Casino & ICSE Settlement**<br>`/ic dailyclaim`, `/ic profile`, `/ic leaderboard`, `/ic casino blackjack/roulette/slots/payouts`, `/ic icse market/quote/buy/sell/short/cover/portfolio/trades/history` | `/ic dailyclaim` credits once per 24h; Blackjack interactive buttons (`Hit`/`Stand`/`Double Down`/`Split`) settle atomically; ICSE `buy`/`sell`/`short`/`cover` update cash, collateral, and `fun_trades` with zero balance drift; `leaderboard` reflects net worth. | Public (DM invocations rejected **Ephemerally**) | Second `/ic dailyclaim` returns countdown; bet > balance or oversell rejected atomically without altering balance; `sync_catalog reset_to_default:True` uses synthetic tickers only. | Optional (`06-icse-market.png`, `07-profile-or-casino.png`) | **Yes** (clean staging test accounts + default synthetic ICSE catalog) | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **9** | **Admin Setup, Status & Feature Toggles**<br>`/ic admin setup`, `/ic admin config`, `/ic admin features`, `/ic admin roles`, `/ic admin audit`, `/ic status`, `/ic admin llm_stop`, `/ic admin llm_start` | Interactive setup dashboard updates guild settings and logs mutations to `/ic admin audit`; `/ic admin features disable:casino` blocks `/ic casino slots` with `Module Disabled` embed; `/ic status issue_report:True` attaches redacted `status_issue_report.md`; `llm_stop` pauses AI immediately. | **Ephemeral** (all routes) | Non-admin member rejected ephemerally on every `/ic admin` command and on `/ic status`; `status_issue_report.md` contains zero tokens/paths. | Optional (`08-admin-setup.png`, `09-status.png` — mask private IDs/names) | Isolated staging guild/channels | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **10** | **Transit, Market & Sports Disabled / Error Handling**<br>`/ic report`, `/ic sports nba`, background GO/TTC/market/BTC pollers | With `sports` disabled by default, `/ic sports nba` returns `Module Disabled`; when enabled without `ODDS_API_KEY`, renders RSS headlines without crashing; unconfigured transit/market/BTC pollers stay idle with zero error spam. | `/ic report` & `/ic sports`: Public; disabled gate: **Ephemeral** | Missing `GO_API_KEY` or `ODDS_API_KEY` degrades cleanly; `ProviderOutageTracker` dampens repeated upstream warnings. | No | No | `[ ] PASS / [ ] FAIL`<br>Notes: |
| **11** | **Pagination, Actor Guards & Restart / Reconnect**<br>Paginated views + `SIGTERM` restart | Clicking another user's pagination/confirmation button returns ephemeral denial without changing page; active Blackjack hand & synthetic exams/balances survive clean `SIGTERM` stop and restart; reconnect does not duplicate `/ic` commands or background loops. | View denials: **Ephemeral** | Expired views (`180s`/`90s`/`60s`) disable buttons gracefully; deleted messages do not emit unhandled tracebacks. | No | Secondary test account (for cross-user click check) + synthetic DB | `[ ] PASS / [ ] FAIL`<br>Notes: |

---

## 4. Safe Private-Environment Failure Injection

Perform **only** these low-impact, safe failure checks in your isolated staging environment. **Do not** run stress tests, request floods, or abusive scraping against Discord's API or third-party providers (RMP, Metrolinx, TTC, Yahoo, Gemini, ATS boards):

1. **Missing optional API keys (Safe Live Test):**
   - Keep `GEMINI_API_KEY=""`, `GO_API_KEY=""`, and `ODDS_API_KEY=""` in staging `.env`.
   - Verify `/ic status` reports degraded/missing-config cleanly, `/ic resume` still serves the offline No-AI checklist, `/ic sports nba` (if enabled) degrades to RSS headlines, and logs contain zero unhandled tracebacks.
2. **Invalid user inputs & permission denials (Safe Live Test):**
   - Invoke `/ic notes course:!`, `/ic exam add ... date:2026-03-08 start_time:02:30` (DST spring-forward gap), `/ic icse buy symbol:IC qty:999999`, and an `/ic admin config` call from a non-admin test account.
   - Confirm every case returns a clear ephemeral error embed with zero SQL/path/token leakage.
3. **Rate limits (`HTTP 429`), network partitions & upstream outages (Offline Mocked Suite ONLY):**
   - Simulate `429 Retry-After`, `5xx`, timeouts, and circuit-breaker open/half-open recovery **exclusively offline** using mocked HTTP routers:
     ```bash
     uv run pytest -v tests/unit/test_failure_taxonomy_and_reliability.py tests/integration/test_pollers_and_http_errors.py
     ```
4. **Backup, clean stop, restore & restart drill (Safe Local Staging Test):**
   - Add a synthetic exam (`TEST999`), run `tools/backup_db.py --source ~/icbot-staging-data/staging.db --dest ~/icbot-staging-data/backups/drill.db`, remove the exam, stop the staging bot with `SIGTERM`, restore via `tools/backup_db.py --restore ~/icbot-staging-data/backups/drill.db --dest ~/icbot-staging-data/staging.db`, restart with `uv run python main.py --allow-gateway`, and confirm `TEST999` reappears intact in `/ic exam list` and `/ic status` reports Schema v7 WAL `ok`.

---

## 5. Staging Screenshot Privacy & Redaction Rules

Follow [Screenshots Capture & Redaction Guide](screenshots-guide.md) strictly before saving any image under `assets/screenshots/`:
- **Never capture:** genuine users' resumes, real Discord user IDs/handles/avatars, administrative channel IDs, sensitive role lists, private server sidebars, `.env` terminals, or private community conversations.
- Crop strictly to the bot's slash-command header + embed + button row, verify `uv run python tools/scan_secrets.py --enforce-clean` and `uv run python tools/check_docs.py`, and keep `README.md` gallery links unpopulated until all captured PNGs are inspected and approved.

---

## 6. Immediate Stop & Remediation Checklist

Stop staging immediately (`Ctrl+C` / `SIGTERM`) if **any** of the following occur:

- [ ] **Wrong bot account online or duplicate command replies:** Stop the local process immediately; verify `DISCORD_TOKEN` in `.env` belongs strictly to the staging application, never production.
- [ ] **Unexpected Railway restart or deploy activity:** Check Railway Dashboard deployment history immediately; confirm Autodeploy is **OFF** and roll back to the previous known-good snapshot if an unintended deploy started.
- [ ] **Accidental token/secret exposure in terminal log, Discord message, or screenshot:** Immediately **revoke and rotate** the credential at the provider portal (Discord Developer Portal / Google AI Studio / Metrolinx / Odds API) per [Deployment Credential Rotation](deployment.md#81-emergency-token-and-key-rotation) and [`SECURITY.md`](../SECURITY.md).
- [ ] **SQLite `database is locked` or integrity error:** Confirm only one local process is accessing `staging.db` (`ps aux | grep main.py`), run `uv run python tools/backup_db.py --verify ~/icbot-staging-data/staging.db`, and restore from `staging_pretest.db` if needed.
- [ ] **Unintended AI or poller traffic:** Run `/ic admin llm_stop` immediately (halts all Gemini calls without restart) and verify poller intervals are set to `0` / `false` in `.env`.

---

## 7. Manual Evidence Record & Human Sign-Off Gate

*To be completed manually by the repository owner after executing this runbook. Leave unchecked until real staging evidence is recorded.*

- **Staging Session Date (UTC):** `YYYY-MM-DD`
- **Git Commit SHA Tested:** `<commit-sha>`
- **GitHub Actions CI Run URL (`ci.yml`):** `<https://github.com/sukesan7/instigate-cafe-bot/actions/runs/...>`
- **Staging Bot Application Name / ID (Non-Production):** `<staging-app-name>` *(do not commit real tokens)*
- **Command Families Passed (`1–11`):** `___ / 11`
- **Open Defects Found (P0/P1/P2):** `<none or list issue links>`

### Owner Release Sign-Off Gate
- [ ] All 11 command families verified on isolated staging bot with zero P0/P1 defects.
- [ ] Safe failure-injection checks and backup/restore/restart drill completed cleanly.
- [ ] Railway production settings manually verified (Autodeploy OFF, `replicas = 1`, `/data` volume & `DB_PATH` confirmed).
- [ ] Pre-publication release blockers in [Release Verification Ledger](release-verification.md#4-release-blockers-checklist-owner-sign-off-required) (including Git history sensitivity decision) reviewed and resolved.
- [ ] **Owner Decision (`GO` / `NO-GO`):** `__________` — **Signed by:** `__________` — **Date:** `__________`
