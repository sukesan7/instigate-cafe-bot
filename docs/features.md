# Instigate Cafe Bot — Features & Slash Command Reference

This reference documents every supported slash command under **`/ic`** (53 leaf commands across 17 feature modules and 5 nested subgroups: `/ic admin`, `/ic casino`, `/ic exam`, `/ic icse`, and `/ic sports`).

> **Automated Parity Guarantee:** This file is verified against the live command tree offline by [`tools/check_command_docs.py`](../tools/check_command_docs.py) and `tests/unit/test_command_docs_parity.py` (using an outbound socket blocker so importing `icbot.registry` performs zero network or Discord gateway activity). Any missing command, stale option, or mistyped route name fails CI automatically.

---

## 1. Defaults, Themes, Permissions & Disabled-Feature Behavior

### 1.1 Default Visual Theme
All command embeds use the **`instigate_cafe`** preset by default (`BOT_DISPLAY_NAME="InstigateAI"`, accent `#A64DFF` / `0xA64DFF`, footer `Instigate Cafe`). Administrators can switch per server via `/ic admin config` among the five built-in presets in `icbot/theme.py`: `instigate_cafe`, `campus_generic`, `student_clean`, `toronto_tmu`, and `emerald_academic`. See [UI Style Contract](ui-style.md) for semantic status colors (`info`, `success`, `warning`, `error`, `disabled`).

### 1.2 Disabled-Feature Behavior
Every `/ic` interaction passes through `check_command_gate()` before executing. If a command's module is turned off for the guild (via `FEATURE_PROFILE`, `DISABLED_FEATURES`, or `/ic admin features`), the bot returns an ephemeral embed titled **`Module Disabled`** (using the `disabled` status color) telling members the module is off and instructing server administrators to run `/ic admin features` or `/ic admin config` to re-enable it.

### 1.3 Scope & Optional-Key Legend
- **Global:** Works in any Discord server with only `DISCORD_TOKEN`.
- **TMU / Toronto Default:** Defaults target Toronto Metropolitan University (TMU) or Greater Toronto Area feeds; can be customized or disabled per server (see [External Providers](providers.md)).
- **Setup Required:** Needs a server channel or profile configured via `/ic admin setup`, `/ic admin config`, or `.env`.
- **Optional Key:** Requires an optional provider key (`GEMINI_API_KEY`, `GO_API_KEY`, or `ODDS_API_KEY`); degrades gracefully when blank.
- **Admin / Allowed Role:** Requires Discord `Manage Server` / `Administrator` or membership in `ALLOWED_ROLE_IDS` (`/ic admin roles`).
- **Guild-Only:** Rejects Direct Message invocations ephemerally.

### 1.4 Module Registry & Profiles
| Module ID | Default Enabled (`default`) | Scope | Optional Key / Feed | Included Profiles |
| :--- | :---: | :--- | :--- | :--- |
| `core` | Yes | Global | None | `default`, `student_core`, `toronto_tmu`, `community_full` |
| `academics` | Yes | Mixed (`/ic exam` global; `/ic notes`, `/ic grades`, default `/ic prof` target **TMU**) | None | `default`, `student_core`, `toronto_tmu`, `community_full` |
| `careers` | Yes | Global (Greenhouse, Lever, Workday ATS) | Optional board lists + `JOBS_POLL_SECONDS` | `default`, `student_core`, `toronto_tmu`, `community_full` |
| `transit` | Yes | **Toronto / GTA only** (GO Transit & TTC) | `GO_API_KEY` (GO only) + alert channels | `default`, `student_core`, `toronto_tmu`, `community_full` |
| `economy` | Yes | Global (virtual tokens) | None | `default`, `student_core`, `toronto_tmu`, `community_full` |
| `casino` | Yes | Global (unit-tested PRNG games) | None | `default`, `community_full` |
| `market` | Yes | Global (virtual ICSE + market reports) | Optional market watch / BTC channels | `default`, `community_full` |
| `ai` | **No** (opt-in) | Global (resume critique & `@mention` Q&A) | `GEMINI_API_KEY` + `LLM_ENABLED=true` | `community_full` (or manual enable) |
| `sports` | **No** (opt-in) | Global (ESPN RSS + optional odds) | `ODDS_API_KEY` (optional; RSS needs none) | `community_full` (or manual enable) |

---

## 2. Core Onboarding & Diagnostics (`core` — 6 Commands)

*Global. No external API keys required.*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic getting-started` | None | Public (`180s` view) | Everyone | Interactive 4-step walkthrough covering daily tokens, academics, careers, and community features. |
| `/ic help` | None | Public (`180s` view) | Everyone | Dynamic paginated command directory filtered to enabled guild modules; hides admin commands from non-admins. |
| `/ic botinfo` | None | Public | Everyone | Shows version, active LLM model summary, and privacy/retention boundaries. |
| `/ic serverinfo` | None | Public | Everyone (Guild-Only) | Displays server member, channel, and creation metadata. |
| `/ic status` | `issue_report` (bool, default `False`) | **Ephemeral** | **Admin / Allowed Role** (Guild-Only) | Inspects module readiness, Schema v7 WAL health, and optionally attaches redacted `status_issue_report.md`. |
| `/ic leaderboard` | None | Public (`180s` view) | Everyone (Guild-Only) | Paginated guild net-worth leaderboard (virtual cash + ICSE holdings). |

**Examples:**
- Students: `/ic getting-started`, `/ic help`, `/ic leaderboard`
- Server admins: `/ic status issue_report:True`

---

## 3. Academic Toolkit (`academics` — 9 Commands)

*`/ic exam` is **global**. Default `/ic notes`, `/ic grades` (`tmu.grades.zone`), and default `RMP_UNIVERSITY` (`Toronto Metropolitan University`) are **TMU-specific** unless overridden via `RMP_UNIVERSITY`, `NOTES_SITES`, or guild catalog settings.*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic notes` | `course`* (str) | Public (`180s` view) | Everyone (**TMU default**; configurable) | Searches configured notes sites and guild catalog with course-alias normalization and SSRF-validated links. |
| `/ic prof` | `name`* (str) | Public | Everyone (**TMU default** via `RMP_UNIVERSITY`) | Looks up professor ratings via RateMyProfessors GraphQL with 6-hour caching and degraded fallback guidance. |
| `/ic grades` | `course`* (str) | Public | Everyone (**TMU-specific** `tmu.grades.zone`) | Displays cached historical course grade distributions with accessible text fallback if chart rendering fails. |
| `/ic exam list` | `course` (str), `term` (str), `upcoming_only` (bool, default `False`), `include_private` (bool, default `True`) | Public, or **Ephemeral** if private exams are included | Everyone (Guild-Only; global) | Lists shared guild exam schedules with countdowns and pagination. Automatically decides ephemeral visibility before `defer()`. |
| `/ic exam add` | `course_code`*, `course_name`*, `date`*, `start_time`, `end_time`, `location`, `notes`, `organizer`, `visibility` (`guild`/`public`/`private`, default `guild`), `term`, `all_day` (default `False`), `dst_resolution` (`reject`/`earlier`/`later`, default `reject`) | **Ephemeral** | **Admin / Allowed Role** (Guild-Only; global) | Adds an exam with strict ISO date/time validation and explicit DST ambiguity/gap handling (`EXAMS_TZ`, default `America/Toronto`). |
| `/ic exam remove` | `exam_id`* (int) | **Ephemeral** | **Admin / Allowed Role** (Guild-Only; global) | Removes a single exam record by ID for the current guild. |
| `/ic exam clear` | `term` (str), `preserve_manual` (bool, default `False`) | **Ephemeral** (`60s` confirm) | **Admin / Allowed Role** (Guild-Only; global) | Bulk-clears matching guild exams after explicit button confirmation. |
| `/ic exam import` | `fmt`* (`csv`/`ics`), `file` (attachment), `data` (str), `dst_resolution` (`reject`/`earlier`/`later`, default `reject`) | **Ephemeral** | **Admin / Allowed Role** (Guild-Only; global) | Sandboxed offline CSV/ICS batch importer with row/size limits and line-by-line error reporting. |
| `/ic exam export` | `fmt` (`ics`/`csv`, default `ics`), `course`, `term`, `upcoming_only` (default `False`), `include_private` (default `False`) | **Ephemeral** | Everyone (Guild-Only; global) | Exports the guild schedule as an RFC 5545 `.ics` calendar file or `.csv` attachment. |

**Examples:**
- Students: `/ic notes course:CPS109`, `/ic exam list upcoming_only:True`, `/ic exam export fmt:ics`
- Guild admins: `/ic exam add course_code:CS101 course_name:Intro to CS date:2026-12-10 start_time:09:00 location:Hall A`

---

## 4. Careers & Optional AI Resume Assistance (`careers` & `ai` — 5 Commands)

*Global. Job browsing requires no API key (populated by optional ATS pollers). AI resume critiques require `LLM_ENABLED=true`, `GEMINI_API_KEY`, and enabling the `ai` module.*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic jobs` | `limit` (int, default `10`), `preset` (str), `query` (str), `location` (str), `use_my_preferences` (bool, default `False`) | Public (`180s` view) | Everyone (**Optional ATS feed**) | Browses cached Greenhouse, Lever, and Workday early-career postings with HTTPS-only apply links and posted/indexed dates. |
| `/ic job_preferences` | `preset` (str), `locations` (str), `keywords` (str), `remote_only` (bool), `clear` (bool, default `False`) | **Ephemeral** | Everyone | Saves portable user-ID-scoped (`global_user`) filter preferences in local plaintext SQLite. |
| `/ic job_preview` | `title`* (str), `employer` (default `Example Corp`), `location` (default `Toronto, ON`), `preset` (default `tech_swe`), `require_early_career` (default `True`) | **Ephemeral** | Everyone | Dry-runs the explainable rules engine against a sample job title and explains why it matched or was excluded. |
| `/ic job_subscribe` | `action` (`subscribe`/`unsubscribe`/`list`, default `subscribe`), `channel`, `preset` (default `tech_swe`), `role` | **Ephemeral** | **Admin / Allowed Role** (**Setup Required**) | Configures per-guild channel job-alert subscriptions and validates bot channel permissions. |
| `/ic resume` | `file`* (attachment), `discipline` (default `general`, 10 disciplines), `role` (str), `confirm` (bool, default `False`) | **Ephemeral** (`180s` consent / pagination view) | Everyone (**Opt-in `ai` module + `GEMINI_API_KEY`** for AI; offline rubric needs no key) | Displays mandatory `ResumeConfirmView` disclosure before transmitting in-memory bytes to Gemini, or renders an instant offline **No-AI Checklist**. Zero local disk/SQLite persistence; Discord CDN & Gemini terms apply. |

**Examples:**
- Students: `/ic jobs preset:tech_swe location:Toronto`, `/ic job_preferences locations:Toronto, Remote remote_only:True`, `/ic resume file:<pdf> discipline:Software Engineering & Tech (SWE)`
- Guild admins: `/ic job_subscribe action:subscribe channel:#job-alerts preset:tech_swe`

---

## 5. Virtual Economy & Casino (`economy` & `casino` — 6 Commands)

*Global and 100% offline. Uses purely virtual tokens with zero real-money value. Casino games run on unit-tested Python PRNG engines with deterministic payout tables (not cryptographic commit-reveal).*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic dailyclaim` | None | Public | Everyone (Guild-Only) | Claims the 24-hour virtual allowance (`DAILY_CLAIM_AMOUNT`, default `100`) atomically. |
| `/ic profile` | `user` (User) | Public | Everyone (Guild-Only) | Displays virtual cash balance, ICSE holdings value, and Blackjack stats for yourself or another member. |
| `/ic casino blackjack` | `bet` (int, default `10`) | Public (`90s` interactive view) | Everyone (Guild-Only) | Interactive Blackjack (Hit, Stand, Double Down, Split) with persistent SQLite hand recovery across restarts. |
| `/ic casino roulette` | `bet`* (int), `bet_type`* (str), `number` (str) | Public | Everyone (Guild-Only) | European roulette wheel (`0–36`, color, parity, dozens, columns) settled atomically. |
| `/ic casino slots` | `bet` (int, default `10`) | Public | Everyone (Guild-Only) | 3-reel slot machine with atomic ledger settlement. |
| `/ic casino payouts` | None | Public | Everyone (Guild-Only) | Displays exact slot symbol weights and payout multipliers. |

**Examples:**
- Students: `/ic dailyclaim`, `/ic profile`, `/ic casino blackjack bet:50`, `/ic casino payouts`

---

## 6. Virtual Stock Exchange (ICSE) & Market Report (`market` — 11 Commands)

*Global educational paper-trading simulation backed by an atomic double-entry SQLite ledger. Not financial advice.*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic icse market` | None | Public | Everyone (Guild-Only) | Lists active guild catalog symbols, current virtual prices, and weekly `%` moves. |
| `/ic icse stock` | None | Public | Everyone (Guild-Only) | Displays primary benchmark symbol (`PRIMARY_STOCK_SYMBOL`, default `IC`) and recent history. |
| `/ic icse quote` | `symbol`* (str) | Public | Everyone (Guild-Only) | Case-insensitive quote lookup for a listed virtual stock. |
| `/ic icse portfolio` | `user` (User) | Public | Everyone (Guild-Only) | Shows cash, open long/short positions, locked short collateral, and unrealized P&L. |
| `/ic icse buy` | `symbol`* (str), `qty`* (int) | Public | Everyone (Guild-Only) | Buys long shares atomically, recording `fun_trades` and `economy_ledger`. |
| `/ic icse sell` | `symbol`* (str), `qty`* (int) | Public | Everyone (Guild-Only) | Sells long shares atomically and settles realized P&L. |
| `/ic icse short` | `symbol`* (str), `qty`* (int) | Public | Everyone (Guild-Only) | Opens or increases a short position with locked collateral requirements. |
| `/ic icse cover` | `symbol`* (str), `qty`* (int) | Public | Everyone (Guild-Only) | Covers an open short position, releasing collateral and settling net P&L. |
| `/ic icse trades` | `limit` (int, default `10`), `user` (User) | Public | Everyone (Guild-Only) | Displays chronological trade history for the guild or a member. |
| `/ic icse history` | `limit` (int, default `10`), `symbol` (str) | Public | Everyone (Guild-Only) | Displays recent administrative price updates and market events. |
| `/ic report` | None | Public | Everyone (Guild-Only; external quote fallback) | Generates a daily market summary (Yahoo Finance + Alternative.me Fear & Greed, with graceful fallback). |

**Examples:**
- Students: `/ic icse market`, `/ic icse buy symbol:IC qty:5`, `/ic icse portfolio`

---

## 7. Sports Schedules & Headlines (`sports` — 5 Commands)

*Global, opt-in module (`default_enabled=False`; enable via `community_full` or `/ic admin features enable:sports`). ESPN RSS news works with no key; upcoming event odds use optional `ODDS_API_KEY`.*

| Command | Options (`*` = required) | Audience | Permissions / Constraints | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic sports nba` | None | Public | Everyone (Opt-in `sports` module) | Upcoming NBA games (when `ODDS_API_KEY` is set) and ESPN NBA RSS headlines. |
| `/ic sports nfl` | None | Public | Everyone (Opt-in `sports` module) | Upcoming NFL games and ESPN NFL RSS headlines. |
| `/ic sports mma` | None | Public | Everyone (Opt-in `sports` module) | Upcoming MMA events and RSS headlines. |
| `/ic sports ufc` | None | Public | Everyone (Opt-in `sports` module) | Upcoming UFC fight cards and RSS headlines. |
| `/ic sports boxing` | None | Public | Everyone (Opt-in `sports` module) | Upcoming boxing bouts and RSS headlines. |

**Example:** `/ic sports nba`

---

## 8. Server Administration (`core` & `market` — 11 Commands)

*All `/ic admin` commands require **Admin / Allowed Role**, are **Guild-Only**, and reply **Ephemerally**.*

| Command | Options (`*` = required) | Audience | Module | What It Does |
| :--- | :--- | :--- | :--- | :--- |
| `/ic admin setup` | None | **Ephemeral** (`180s` view) | `core` | Interactive dashboard to configure campus, timezone, theme, channels, and feature profile. |
| `/ic admin config` | `action` (View / Update / Reset), `campus`, `timezone`, `theme`, `command_channel`, `job_alerts_channel`, `transit_alerts_channel` | **Ephemeral** | `core` | Inspects, updates, or resets per-guild configuration overrides in `guild_settings`. |
| `/ic admin features` | `profile` (Default / Student Core / Community Full / Toronto TMU), `enable` (str), `disable` (str) | **Ephemeral** | `core` | Applies a feature preset or toggles individual module IDs for the server. |
| `/ic admin roles` | `action`* (Add / Remove / List / Clear), `role` (Role) | **Ephemeral** | `core` | Delegates bot management to specific roles without granting Discord Administrator rights. |
| `/ic admin audit` | None | **Ephemeral** | `core` | Shows recent guild configuration mutations from `guild_settings_audit`. |
| `/ic admin llm_start` | None | **Ephemeral** | `core` | Re-enables Gemini `@mention` assistant and AI resume review (`meta.llm_killed=0`). |
| `/ic admin llm_stop` | None | **Ephemeral** | `core` | Immediately pauses all Gemini API calls without restarting the bot (`meta.llm_killed=1`). |
| `/ic admin move` | `symbol`* (str), `change`* (str), `reason` (str) | **Ephemeral** (confirm view) | `market` | Shifts an ICSE stock price by `±Δ` with an immutable audit event. |
| `/ic admin set` | `symbol`* (str), `value`* (int), `reason` (str) | **Ephemeral** (confirm view) | `market` | Sets an ICSE stock to an exact integer price with an immutable audit event. |
| `/ic admin sync_catalog` | `reset_to_default` (bool, default `False`) | **Ephemeral** (confirm view) | `market` | Syncs guild ICSE listings or resets them to the default synthetic catalog. |
| `/ic admin weekroll` | `note` (str) | **Ephemeral** (confirm view) | `market` | Resets weekly `%` change baselines to current prices. |

**Examples:**
- Guild admins: `/ic admin setup`, `/ic admin features disable:casino`, `/ic admin config action:View Current Configuration`
- Operators (emergency pause): `/ic admin llm_stop`

---

## 9. Optional Background Alert Feeds & Mention Assistant

Configured via environment variables ([Configuration Reference](configuration.md)) and managed by `TaskSupervisor`:
- **GO Transit (`go`)** — **Toronto / GTA only**; requires `GO_API_KEY` and `GO_ALERTS_CHANNEL_ID`.
- **TTC Alerts (`ttc`)** — **Toronto only**; no API key required; requires `TTC_ALERTS_CHANNEL_ID`, `TTC_MASTODON_BASE`, and `TTC_MASTODON_ACCT`.
- **Job Pollers (`jobs`, `workday`)** — Global; no API key required; requires `JOB_ALERTS_CHANNEL_ID`, board/site lists, and poll interval `> 0`.
- **Market Watch & Daily Report (`market_watch`, `daily_market_report`)** — Global; no API key required; disabled by default.
- **Bitcoin Address Watch (`btc_watch`)** — Global; no API key required; disabled by default.
- **Mention Assistant (`llm_assistant`)** — Global; requires `LLM_ENABLED=true` and `GEMINI_API_KEY`. Uses unprivileged `messages` intent for direct `@mention` replies by default (`LLM_CHANNEL_HISTORY_ENABLED=false`, zero privileged intents); requests privileged Discord Message Content Intent only when `LLM_CHANNEL_HISTORY_ENABLED=true` at process startup.
