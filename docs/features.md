# Instigate Cafe Bot — Features & Slash Command Guide

All user-facing commands live under the unified **`/ic`** slash-command namespace across **17 feature modules** and **53 registered leaf commands**.

> [!NOTE]
> **Quick Distinctions for New Readers**
> - **Zero API keys required for core features:** Core info/help, shared exam schedules, virtual economy, casino games, ICSE paper trading, and Greenhouse/Lever/Workday job feeds work with only a `DISCORD_TOKEN`.
> - **Optional API keys:** `GEMINI_API_KEY` unlocks `/ic resume` AI critiques and `@mention` Q&A; `GO_API_KEY` unlocks Metrolinx GO transit alerts; `ODDS_API_KEY` unlocks upcoming sports schedules/odds.
> - **TMU-specific vs. campus-agnostic:** Default course notes (`/ic notes`), grade distributions (`/ic grades`), default `RMP_UNIVERSITY`, and GO/TTC transit alerts target Toronto Metropolitan University (TMU) and the Greater Toronto Area. All other modules work for any Discord community.

---

## 1. Onboarding & Dynamic Discovery

- **`/ic getting-started`** launches an interactive 4-step onboarding guide (Campus Economy, Academic Toolkit, Careers & Job Alerts, Community & Casual Games) with actor-bound navigation buttons (`180s` timeout).
- **`/ic help`** dynamically reads `FEATURE_MANIFESTS` and per-guild enablement flags (`is_feature_enabled`), hiding disabled modules and omitting `/ic admin ...` pages for non-administrators.
- **Per-Server Profiles:** Server managers run **`/ic admin setup`** or **`/ic admin features`** to apply a preset profile (`default`, `student_core`, `toronto_tmu`, `community_full`) or toggle individual modules (`academics`, `careers`, `transit`, `economy`, `casino`, `market`, `ai`, `sports`).

---

## 2. Module Overview & Scope Matrix

| Module ID | Scope | Optional API Key | Background Poller | Default Enabled (`default` profile) |
| :--- | :--- | :--- | :--- | :---: |
| `core` | Generic (any server) | None | None | Yes |
| `academics` | Mixed: `/ic exam` is generic; `/ic notes`, `/ic grades`, default `/ic prof` target **TMU** | None | Optional exam reminders | Yes |
| `careers` | Generic (Greenhouse, Lever, Workday ATS) | None | `jobs`, `workday` | Yes |
| `transit` | **Toronto / GTA specific** (GO Transit & TTC) | `GO_API_KEY` (GO only; TTC needs none) | `go`, `ttc` | Yes |
| `economy` | Generic (virtual campus currency) | None | None | Yes |
| `casino` | Generic (virtual PRNG mini-games) | None | None | Yes |
| `market` | Generic (virtual ICSE exchange + market reports) | None | `market_watch`, `daily_market_report`, `btc_watch` | Yes |
| `ai` | Generic (opt-in Gemini resume review & `@mention` Q&A) | `GEMINI_API_KEY` | `llm_assistant` (event-driven) | Opt-in (`LLM_ENABLED=false`) |
| `sports` | Generic (sports news RSS + optional odds) | `ODDS_API_KEY` (optional; RSS works without key) | None | Opt-in (`community_full`) |

---

## 3. Complete 53 Slash Command Reference

### 3.1 Core, Onboarding & Diagnostics (6 Commands — `core`)
| Command | Options | Audience | Permissions | Description |
| :--- | :--- | :--- | :--- | :--- |
| `/ic getting-started` | None | Public (`180s` view) | Everyone | Interactive 4-step walkthrough for new community members. |
| `/ic help` | None | Public (`180s` view) | Everyone | Dynamic paginated command directory filtered by guild modules and caller role. |
| `/ic botinfo` | None | Public | Everyone | Bot version, active AI configuration summary, and honest privacy/retention boundaries. |
| `/ic serverinfo` | None | Public | Everyone | Server metadata summary (member counts, channels, creation date). |
| `/ic status` | `issue_report: bool` | Public / Ephemeral | Everyone (issue export: Admin/Allowed) | Module readiness, Schema v7 WAL health, background task states, and optional sanitized `status_issue_report.md` export. |
| `/ic leaderboard` | None | Public (`180s` view) | Everyone | Paginated guild net worth leaderboard (virtual cash + ICSE portfolio equity). |

### 3.2 Academic Toolkit (9 Commands — `academics`)
| Command | Options | Audience | Permissions | Scope & Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `/ic notes` | `course: str` | Public (`180s` view) | Everyone | **TMU default** (`NOTES_SITES` + guild catalog). Paginated study-guide links with course alias normalization and SSRF-validated URLs. |
| `/ic prof` | `name: str` | Public | Everyone | Queries RateMyProfessors GraphQL (defaults to `RMP_UNIVERSITY="Toronto Metropolitan University"`, overridable per guild). |
| `/ic grades` | `course: str` | Public | Everyone | **TMU-specific** (`tmu.grades.zone`). Displays cached historical grade distributions with text summary fallback if chart rendering fails. |
| `/ic exam list` | `course`, `term`, `upcoming_only`, `include_private` | Public or Ephemeral | Everyone | Generic shared guild timetable. Automatically switches to **ephemeral** before `defer()` whenever private exams are included or queried in DM. |
| `/ic exam add` | `course_code`, `course_name`, `date`, `start_time`, `end_time`, `location`, `notes`, `term`, `visibility`, `dst_policy`, `tz` | Public / Ephemeral | Guild Admin / Manager (public) or User (private) | Validates ISO dates/times and DST ambiguity/gap policies (`America/Toronto` default, configurable via `EXAMS_TZ`). |
| `/ic exam remove` | `exam_id: int` | Public / Ephemeral | Owner or Guild Admin | Removes a single exam entry by ID. |
| `/ic exam clear` | `term: str`, `preserve_manual: bool` | Public (`60s` confirm) | Guild Admin / Manager | Bulk-clears exams for a term after explicit button confirmation. |
| `/ic exam import` | `fmt` (`csv`/`ics`), `file`, `data` | Ephemeral | Guild Admin / Manager | Sandboxed CSV/ICS batch importer with strict size/row bounds and line-by-line error reporting. |
| `/ic exam export` | `fmt` (`ics`/`csv`), `course`, `term`, `upcoming_only`, `include_private` | Ephemeral | Everyone | Exports clean `.ics` (RFC 5545 subset for Google/Apple Calendar) or `.csv` attachment. |

### 3.3 Careers, Job Alerts & AI Resume Critique (5 Commands — `careers` & `ai`)
| Command | Options | Audience | Permissions | Scope & Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `/ic jobs` | `limit`, `preset`, `query`, `location`, `use_my_preferences` | Public (`180s` view) | Everyone | Searches cached Greenhouse, Lever, and Workday early-career postings with HTTPS-only apply links and posted/indexed date semantics. |
| `/ic job_preferences` | `preset`, `locations`, `keywords`, `remote_only`, `clear` | Ephemeral | Everyone | Saves personal filter preferences (`user_job_preferences`, plaintext local SQLite, portable across servers sharing one bot instance). |
| `/ic job_preview` | `title`, `employer`, `location`, `preset`, `require_early_career` | Ephemeral | Everyone | Explains why a sample posting matches or is excluded by the explainable rules engine. |
| `/ic job_subscribe` | `action`, `channel`, `preset`, `role` | Ephemeral | Guild Admin / Manager | Configures per-guild channel job-alert subscriptions and validates channel permissions. |
| `/ic resume` | `file`, `discipline`, `role`, `confirm` | Ephemeral (`180s` consent / review view) | Everyone | **Requires `GEMINI_API_KEY` + `LLM_ENABLED=true` for AI critique.** Presents mandatory `ResumeConfirmView` disclosure before forwarding bytes to Gemini, or offers an instant offline **No-AI Checklist** with zero external requests. Processed in RAM only (zero local disk/SQLite persistence; Discord CDN & Gemini API terms apply). |

### 3.4 Virtual Economy & Casino Mini-Games (6 Commands — `economy` & `casino`)
*All balances and wagers use purely virtual community tokens with zero real-money value. Casino games use unit-tested Python PRNG engines with deterministic payout tables (not cryptographic commit-reveal).*

| Command | Options | Audience | Permissions | Description |
| :--- | :--- | :--- | :--- | :--- |
| `/ic dailyclaim` | None | Public | Everyone (Guild only) | Claims the 24-hour virtual allowance (`DAILY_CLAIM_AMOUNT`, default `100`) via an atomic SQLite transaction. |
| `/ic profile` | `user: User` | Public | Everyone (Guild only) | Displays guild-scoped virtual cash balance, ICSE holdings, and Blackjack stats. |
| `/ic casino blackjack` | `bet: int` | Public (`90s` interactive view) | Everyone (Guild only) | Interactive Blackjack (Hit, Stand, Double Down, Split) with persistent SQLite hand state and restart crash recovery. |
| `/ic casino roulette` | `bet: int`, `bet_type`, `number` | Public | Everyone (Guild only) | European roulette wheel (`0–36`, red/black, even/odd, dozens, columns) settled atomically. |
| `/ic casino slots` | `bet: int` | Public | Everyone (Guild only) | 3-reel slot machine with deterministic symbol weights and atomic settlement. |
| `/ic casino payouts` | None | Public | Everyone (Guild only) | Displays the exact slot symbol weights and multiplier table. |

### 3.5 Virtual Stock Exchange — ICSE & Market Report (11 Commands — `market`)
*Purely educational paper-trading simulation. Does not provide financial advice or execute real brokerage trades.*

| Command | Options | Audience | Permissions | Description |
| :--- | :--- | :--- | :--- | :--- |
| `/ic icse market` | None | Public | Everyone (Guild only) | Lists active guild catalog symbols, current virtual prices, and weekly `%` changes. |
| `/ic icse stock` | None | Public | Everyone (Guild only) | Displays the primary benchmark symbol summary and recent price trajectory. |
| `/ic icse quote` | `symbol: str` | Public | Everyone (Guild only) | Case-insensitive quote lookup for any listed guild symbol. |
| `/ic icse portfolio` | `user: User` | Public | Everyone (Guild only) | Displays cash balance, open long/short positions, collateral locked, and unrealized P&L. |
| `/ic icse buy` | `symbol: str`, `qty: int` | Public | Everyone (Guild only) | Atomic long equity purchase recorded in `fun_trades` and `economy_ledger`. |
| `/ic icse sell` | `symbol: str`, `qty: int` | Public | Everyone (Guild only) | Atomic long equity sale with realized P&L settlement. |
| `/ic icse short` | `symbol: str`, `qty: int` | Public | Everyone (Guild only) | Opens a short position with locked collateral requirements. |
| `/ic icse cover` | `symbol: str`, `qty: int` | Public | Everyone (Guild only) | Covers an open short position, releasing collateral and settling net P&L. |
| `/ic icse trades` | `limit: int`, `user: User` | Public | Everyone (Guild only) | Chronological trade history tape for the guild or member. |
| `/ic icse history` | `limit: int`, `symbol: str` | Public | Everyone (Guild only) | Price change events and administrative adjustment audit history. |
| `/ic report` | None | Public | Everyone (Guild only) | Generates a daily market snapshot (Yahoo Finance quotes + Alternative.me Fear & Greed index, with graceful fallback). |

### 3.6 Sports Schedules & Headlines (5 Commands — `sports`)
| Command | Options | Audience | Permissions | Description |
| :--- | :--- | :--- | :--- | :--- |
| `/ic sports nba` | None | Public | Everyone | Upcoming NBA games (when `ODDS_API_KEY` is configured) and ESPN RSS headlines. |
| `/ic sports nfl` | None | Public | Everyone | Upcoming NFL matchups and RSS news headlines. |
| `/ic sports mma` | None | Public | Everyone | Upcoming MMA fight cards and headlines. |
| `/ic sports ufc` | None | Public | Everyone | Upcoming UFC events, odds (optional key), and news. |
| `/ic sports boxing` | None | Public | Everyone | Upcoming boxing bouts and RSS headlines. |

### 3.7 Server Administration & Governance (11 Commands — `core` & `market`)
*All `/ic admin ...` commands require Discord `Manage Server` / `Administrator` permissions or membership in `ALLOWED_ROLE_IDS`, and reject DM invocations ephemerally.*

| Command | Options | Audience | Description |
| :--- | :--- | :--- | :--- |
| `/ic admin setup` | None | Ephemeral (`180s` wizard) | Guided interactive setup wizard for channels, campus preset, and feature profiles. |
| `/ic admin config` | `action`, `campus`, `timezone`, `theme`, channels | Ephemeral | Inspects or updates guild-level configuration overrides stored in `guild_settings`. |
| `/ic admin features` | `profile`, `enable`, `disable` | Ephemeral | Applies a feature profile or toggles specific feature modules for the server. |
| `/ic admin roles` | `action`, `role` | Ephemeral | Adds or removes bot-operator roles without granting Discord Administrator rights. |
| `/ic admin audit` | None | Ephemeral | Views recent administrative configuration changes from `guild_settings_audit`. |
| `/ic admin llm_start` | None | Ephemeral | Re-enables Gemini `@mention` Q&A and `/ic resume` AI critiques (`meta.llm_killed=0`). |
| `/ic admin llm_stop` | None | Ephemeral | Immediately pauses all Gemini API calls without restarting the bot (`meta.llm_killed=1`). |
| `/ic admin move` | `symbol`, `change`, `reason` | Ephemeral (confirm view) | Adjusts an ICSE stock price by `±Δ` with an immutable audit entry. |
| `/ic admin set` | `symbol`, `value`, `reason` | Ephemeral (confirm view) | Sets an ICSE stock price directly with an immutable audit entry. |
| `/ic admin sync_catalog` | `reset_to_default` | Ephemeral (confirm view) | Synchronizes guild ICSE listings with the default synthetic catalog. |
| `/ic admin weekroll` | `note` | Ephemeral (confirm view) | Snapshots current ICSE prices as the new weekly `%` baseline. |

---

## 4. Background Alert Channels (No Slash Commands Required)

When configured by the operator, background pollers post deduplicated alerts directly into designated Discord channels:
- **GO Transit (`go`)** *(Toronto / GTA, requires `GO_API_KEY`):* Metrolinx rail/bus service advisories with `major`/`all` filtering.
- **TTC Alerts (`ttc`)** *(Toronto, no API key required):* Public Mastodon `@TTCnotices` subway/streetcar/bus advisories.
- **Job Alerts (`jobs`, `workday`)** *(Generic, no API key required):* Early-career tech postings from Greenhouse, Lever, and Workday CXS.
- **Market Watch & Daily Report (`market_watch`, `daily_market_report`)** *(Generic, no API key required):* VIX/index volatility alerts and scheduled end-of-day summaries.
- **Bitcoin Watch (`btc_watch`)** *(Generic, no API key required):* Optional Esplora UTXO address notifications.
- **Mention Assistant (`llm_assistant`)** *(Generic, requires `LLM_ENABLED=true` + `GEMINI_API_KEY`):* Event-driven `@bot` Q&A with per-user cooldowns and `LLM_CHANNEL_HISTORY_ENABLED=false` privacy default.
