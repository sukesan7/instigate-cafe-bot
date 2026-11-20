# Command Information Architecture & Interaction Conventions

## 1. Executive Summary & Design Principles

The Instigate Cafe Bot provides 53 slash commands under the unified `/ic` command tree. Originally built for a university engineering Discord community, this modernization establishes a consistent, accessible, and intuitive command architecture suitable for students across all faculties (arts, sciences, business, and engineering) as well as non-technical community members.

### Core Architecture Invariants:
1. **100% Backwards Compatibility:** Every existing leaf command is preserved with its exact qualified name and arguments. No breaking renames or deletions.
2. **Metadata-Driven Help:** Help pages are dynamically compiled from `FeatureManifest` registry definitions and per-guild feature enablement flags (`is_feature_enabled`), hiding disabled features and restricted administrative commands from unauthorized members.
3. **Low-Friction Onboarding:** `/ic getting-started` offers a multi-step interactive walkthrough designed specifically for students unfamiliar with bot commands or engineering terminology.
4. **Accessible Interaction Design:** Visual components (buttons, selects) use recognizable textual labels (e.g. `◀ Previous Page`, `Next Page ▶`, `Hit (Draw Card)`) and do not rely solely on color or emoji.
5. **Discord Limit Enforcement & Mentions Defense:** All responses route through an embeds API that strictly enforces Discord limits ($\le 256$ title/fields, $\le 4096$ description, $\le 1024$ field values, $\le 25$ fields, $\le 6000$ total characters), safely neutralizes ping injection (`@everyone`, `@here`, `<@[!&]?\d+>`), and defaults to `AllowedMentions.none()`.
6. **Interaction Lifecycle & Component Isolation:** Button views enforce an explicit 3-minute timeout (`on_timeout`) that disables interactive components cleanly. Menus track the initiating `author_id` (`interaction_check`) and refuse cross-user hijacking with ephemeral guidance.

---

## 2. Command Inventory & Audit Matrix (All 53 Leaf Commands)

| Qualified Command | Category | Module | Options & Types | Visibility | Button Lifetime | Auth / Permissions | User-Facing Error & Recovery Path |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `/ic getting-started` | Core / Onboarding | `core` | None | Public | 180s | Everyone | Friendly prompt to review guide or run `/ic help`. |
| `/ic help` | Core / Help | `core` | None | Public | 180s | Everyone | Displays enabled modules; administrator page visible only to guild managers. |
| `/ic profile` | Student Economy | `economy` | `user: User (opt)` | Public | None | Everyone | Defaults to caller; helpful notice if queried user has no profile yet. |
| `/ic dailyclaim` | Student Economy | `economy` | None | Public | None | Everyone | Returns countdown timestamp if claimed within past 24 hours. |
| `/ic leaderboard` | Student Economy | `core` | None | Public | None | Everyone | Empty state embed if no accounts exist. |
| `/ic serverinfo` | Community & Info | `core` | None | Public | None | Everyone | Graceful fallback if guild metadata is incomplete. |
| `/ic botinfo` | Community & Info | `core` | None | Public | None | Everyone | Shows bot version, latency, and system diagnostics. |
| `/ic status` | Community & Info | `core` | None | Public | None | Everyone | Color-coded status badges with actionable guidance for degraded modules. |
| `/ic notes` | Academic Toolkit | `academics` | `course: str (req)` | Public | 180s | Everyone | Rejects inputs $<3$ chars; friendly guidance with examples (e.g. `CPS109`, `MTH140`). |
| `/ic prof` | Academic Toolkit | `academics` | `name: str (req)` | Public | None | Everyone | Informative message if professor not found or campus unconfigured. |
| `/ic grades` | Academic Toolkit | `academics` | `course: str (req)` | Public | None | Everyone | Validates code $\ge 2$ chars; accessible text summary fallback if chart render fails. |
| `/ic exam list` | Academic Toolkit | `academics` | `course: str (opt)`, `term: str (opt)`, `upcoming_only: bool (opt)` | Public | 180s | Everyone | Empty state embed with instructions to add exams. |
| `/ic exam add` | Academic Toolkit | `academics` | 11 options (course, dates, times, dst, etc.) | Public | None | Guild Admin / Manager | Validates ISO date and time format; explains DST resolution choices. |
| `/ic exam remove` | Academic Toolkit | `academics` | `exam_id: int (req)` | Public | None | Guild Admin / Manager | Returns `not_found` notice if exam ID does not exist. |
| `/ic exam clear` | Academic Toolkit | `academics` | `term: str (req)`, `preserve_manual: bool (opt)` | Public | None | Guild Admin / Manager | Requires explicit term matching; confirms deleted count. |
| `/ic exam import` | Academic Toolkit | `academics` | `fmt: str`, `file: Attachment (opt)`, `data: str (opt)` | Ephemeral | None | Guild Admin / Manager | Reports syntax/parsing errors line-by-line without crashing. |
| `/ic exam export` | Academic Toolkit | `academics` | `fmt: str`, `course: str`, `term: str`, `upcoming_only: bool` | Ephemeral | None | Everyone | Delivers clean CSV or `.ics` attachment for import into Google/Apple calendar. |
| `/ic jobs` | Careers | `careers` | `limit: int`, `preset: str`, `query: str`, `location: str`, `use_my_preferences: bool` | Public | 180s | Everyone | Empty state embed if no jobs match; suggests broadening keywords or location. |
| `/ic job_preferences` | Careers | `careers` | `preset: str`, `locations: str`, `keywords: str`, `remote_only: bool`, `clear: bool` | Ephemeral | None | Everyone | Confidential confirmation of private filter preferences. |
| `/ic job_preview` | Careers | `careers` | `title: str`, `employer: str`, `location: str`, `preset: str`, `require_early_career: bool` | Ephemeral | None | Everyone | Explains why a sample posting matched or was filtered out. |
| `/ic job_subscribe` | Careers | `careers` | `action: str`, `channel: Channel`, `preset: str`, `role: Role (opt)` | Ephemeral | None | Guild Admin / Manager | Validates channel permissions and manages subscription records. |
| `/ic resume` | AI Assistant | `ai` | `file: Attachment (req)`, `discipline: str`, `role: str`, `confirm: bool` | Ephemeral | None | Everyone | Validates PDF/TXT format, bounds check $\le 2\text{MB}$, requires privacy confirmation. |
| `/ic icse market` | Stock Market | `market` | None | Public | None | Everyone | Shows active listings, current prices, and weekly % change. |
| `/ic icse quote` | Stock Market | `market` | `symbol: str (req)` | Public | None | Everyone | Case-insensitive lookup; lists valid symbols if not found. |
| `/ic icse portfolio` | Stock Market | `market` | `user: User (opt)` | Public | None | Everyone | Shows cash balance, stock holdings, and unrealized P&L. |
| `/ic icse buy` | Stock Market | `market` | `symbol: str (req)`, `qty: int (req)` | Public | None | Everyone | Atomic settlement; verifies funds, quantity $>0$, rejects insufficient balance. |
| `/ic icse sell` | Stock Market | `market` | `symbol: str (req)`, `qty: int (req)` | Public | None | Everyone | Atomic settlement; verifies share ownership, rejects overselling. |
| `/ic icse short` | Stock Market | `market` | `symbol: str (req)`, `qty: int (req)` | Public | None | Everyone | Verifies maintenance margin and risk rules. |
| `/ic icse cover` | Stock Market | `market` | `symbol: str (req)`, `qty: int (req)` | Public | None | Everyone | Verifies open short position; settles net profit or loss atomically. |
| `/ic icse trades` | Stock Market | `market` | `limit: int (opt)`, `user: User (opt)` | Public | None | Everyone | Shows chronological trade log with transaction hashes. |
| `/ic icse history` | Stock Market | `market` | `limit: int (opt)`, `symbol: str (opt)` | Public | None | Everyone | Shows recent market events and admin price revisions. |
| `/ic icse stock` | Stock Market | `market` | None | Public | None | Everyone | Benchmark stock summary and historical trajectory. |
| `/ic report` | Stock Market | `market` | None | Public | None | Everyone | Immediately renders daily market overview with gainers/losers. |
| `/ic casino blackjack` | Cafe Casino | `casino` | `bet: int (req)` | Public | 90s | Everyone | Validates bet $>0$ and $\le$ balance; interactive buttons for Hit, Stand, Double Down, Split. |
| `/ic casino roulette` | Cafe Casino | `casino` | `bet: int (req)`, `bet_type: str (req)`, `number: str (opt)` | Public | None | Everyone | Validates roulette bet types and straight-up number bounds (0-36). |
| `/ic casino slots` | Cafe Casino | `casino` | `bet: int (req)` | Public | None | Everyone | Validates bet $>0$ and $\le$ balance; animated reels and payout multipliers. |
| `/ic casino payouts` | Cafe Casino | `casino` | None | Public | None | Everyone | Explains all slot symbols, win lines, and multiplier ratios. |
| `/ic sports nba` | Sports Headlines | `sports` | None | Public | None | Everyone | Shows upcoming NBA matchups, tip-off times, and news. |
| `/ic sports nfl` | Sports Headlines | `sports` | None | Public | None | Everyone | Shows upcoming NFL football games and injury headlines. |
| `/ic sports mma` | Sports Headlines | `sports` | None | Public | None | Everyone | Shows upcoming mixed martial arts fight cards. |
| `/ic sports ufc` | Sports Headlines | `sports` | None | Public | None | Everyone | Shows upcoming UFC championship events and betting odds. |
| `/ic sports boxing` | Sports Headlines | `sports` | None | Public | None | Everyone | Shows upcoming professional boxing matches. |
| `/ic admin setup` | Administration | `core` | None | Ephemeral | None | Guild Admin / Manager | Interactive configuration wizard with channel dropdowns and preset buttons. |
| `/ic admin config` | Administration | `core` | 7 options (action, campus, timezone, theme, channels) | Ephemeral | None | Guild Admin / Manager | Allows inspecting or overriding individual guild settings safely. |
| `/ic admin features` | Administration | `core` | `profile: str (opt)`, `enable: str (opt)`, `disable: str (opt)` | Ephemeral | None | Guild Admin / Manager | Toggles feature modules (`academics`, `careers`, `market`, `casino`, `ai`, `sports`). |
| `/ic admin roles` | Administration | `core` | `action: str (req)`, `role: Role (opt)` | Ephemeral | None | Guild Admin / Manager | Adds/removes allowed roles without granting Discord Administrator permission. |
| `/ic admin audit` | Administration | `core` | None | Ephemeral | None | Guild Admin / Manager | Paginated view of recent administrative configuration changes. |
| `/ic admin llm_start` | Administration | `core` | None | Ephemeral | None | Guild Admin / Manager | Re-enables Gemini mention assistant and resume review tools. |
| `/ic admin llm_stop` | Administration | `core` | None | Ephemeral | None | Guild Admin / Manager | Pauses Gemini AI tools immediately without restarting the bot. |
| `/ic admin move` | Administration | `market` | `symbol: str`, `change: str`, `reason: str` | Ephemeral | None | Guild Admin / Manager | Shifts stock price by $\pm\Delta$ with immutable audit log row. |
| `/ic admin set` | Administration | `market` | `symbol: str`, `value: int`, `reason: str` | Ephemeral | None | Guild Admin / Manager | Overwrites stock price with immutable audit log row. |
| `/ic admin sync_catalog` | Administration | `market` | `reset_to_default: bool (opt)` | Ephemeral | None | Guild Admin / Manager | Re-syncs database stocks with default synthetic catalog. |
| `/ic admin weekroll` | Administration | `market` | `note: str (opt)` | Ephemeral | None | Guild Admin / Manager | Resets weekly % change baseline to current prices. |

---

## 3. Dynamic Help & Discovery Model

Static help documentation often drifts out of sync with code or displays commands that are disabled on a specific server. The dynamic help engine in `icbot/features/help.py`:
1. **Reads Runtime Metadata:** Iterates through `FEATURE_MANIFESTS` and checks `is_feature_enabled(manifest.module_id, settings)`.
2. **Filters Unauthorized Commands:** Commands requiring guild administration privileges (`can_manage_guild`) are automatically omitted from standard user help menus and only shown when a manager invokes the command.
3. **Supplies Concrete Examples:** Every command entry includes an actionable, syntax-accurate example (e.g. `/ic notes course:CPS109`, `/ic jobs query:intern`, `/ic prof name:Smith`).
4. **Accessible Pagination:** Buttons use explicit text labels (`◀ Previous Page`, `Next Page ▶`). When the 180-second lifetime expires, buttons are disabled gracefully to prevent Discord interaction failures.
5. **Cross-User Protection:** Clicking another user's help pagination returns an ephemeral rejection notice (`"Only the person who requested this help menu can navigate it."`) without altering the active display.

---

## 4. `/ic getting-started` Onboarding Architecture

New community members frequently struggle with bot prefixes and technical syntax. The `/ic getting-started` command provides a 4-step interactive walkthrough:

* **Step 1: Welcome & Campus Economy**  
  Introduces the bot's purpose and instructs the user to claim their daily virtual allowance with `/ic dailyclaim` and view their profile with `/ic profile`. Clarifies that currency is 100% virtual and risk-free.
* **Step 2: Academic Toolkit**  
  Explains how to search peer-contributed study guides (`/ic notes`), look up professor ratings (`/ic prof`), and track exams (`/ic exam list`). Emphasizes that course codes are case-insensitive and apply to standard course formats across arts, business, science, and engineering.
* **Step 3: Careers & Job Alerts**  
  Outlines early-career discovery with `/ic jobs`, setting personalized filters with `/ic job_preferences`, and confidential AI resume critiques with `/ic resume`. Reassures students that resume reviews are strictly private.
* **Step 4: Community & Casual Games**  
  Introduces the virtual stock exchange (`/ic icse market`), casino mini-games (`/ic casino blackjack`), and server leaderboard (`/ic leaderboard`). Points the student to `/ic help` for the exhaustive command directory.

---

## 5. Unified Embeds API & Safety Constraints

All embeds generated by the bot adhere to the contracts in `icbot/embeds.py`:

```
+-------------------------------------------------------------+
| Author: <Bot Display Name> (icon: <Logo URL>)               |
| Title:  <Sanitized Title (Max 256 chars)>                   |
| Subtitle: *<Optional Subtitle / Badge>*                     |
+-------------------------------------------------------------+
| Description:                                                |
| <Sanitized Description Body (Max 4096 chars)>               |
|                                                             |
| Fields (Max 25 fields, each name <= 256, value <= 1024):    |
| - Field 1: Value 1                                          |
| - Field 2: Value 2                                          |
+-------------------------------------------------------------+
| Footer: <Footer Text> • <Kind> • <Page Hint> (Max 256 chars)|
| Timestamp: <UTC Instant>                                    |
+-------------------------------------------------------------+
```

### Mentions Defense & Truncation Invariants:
* **Mass Ping Neutralization:** `sanitize_text` inserts a zero-width space into `@everyone` and `@here` (`@​everyone`, `@​here`).
* **Direct Mention Neutralization:** Regex `_USER_ROLE_PING_RE` catches `<@123>`, `<@!123>`, and `<@&123>`, inserting zero-width spaces (`<@​123>`) to disarm raw user and role pings.
* **Allowed Mentions Policy:** `safe_allowed_mentions()` produces `discord.AllowedMentions.none()` to ensure outbound messages never trigger unintended push notifications.
* **Stale Provider Notice:** `stale_data_notice(source, last_updated_unix)` formats an accessible badge:
  `⚠️ [Stale / Cached Data] Sourced from {source}. Last updated: <t:{unix}:R> (<t:{unix}:f>).`
* **Disabled Feature Notice:** `disabled_feature_embed(settings, feature_name)` informs users when a feature is turned off on the guild, offering guidance for administrators.

---

## 6. Synthetic New-User Walkthrough & Friction Log

An automated synthetic walkthrough test (`test_synthetic_new_user_walkthrough_and_friction_points`) exercises the complete new-user journey:

1. **Step 1: First Onboarding Touchpoint**
   * Invokes `/ic getting-started`.
   * *Observed Experience:* Clean, welcoming overview. Navigation buttons (`◀ Previous Step`, `Next Step ▶`) allow browsing all 4 steps without reading wall-of-text documentation.
2. **Step 2: Claiming Daily Allowance**
   * Invokes `/ic dailyclaim`.
   * *Observed Experience:* Instantly credits `$100` and displays current balance. When re-invoked immediately, returns an unambiguous countdown timer indicating when the next claim is unlocked.
3. **Step 3: Academic Exploration & Invalid Input Handling**
   * Student searches for a course with missing or malformed codes (e.g. `/ic notes course:!`, `/ic grades course:?`).
   * *Observed Experience:* Prior behavior returned curt rejection messages (`❌ Please provide a more specific course code.`). Improved behavior offers supportive recovery suggestions (`❌ Please provide a valid course code with at least 3 letters/digits (e.g. CPS109, MTH140, or CS135)`), avoiding frustration for non-engineering majors.
4. **Step 4: Job Search with Empty Results**
   * Student searches for an overly restrictive query (`/ic jobs query:nonexistentquery123`).
   * *Observed Experience:* Generates an empty state embed with a tip suggesting broader keywords or locations, rather than an unhandled error or blank screen.
5. **Step 5: Help Exploration & Administrative Shielding**
   * Standard student invokes `/ic help`.
   * *Observed Experience:* Only user-accessible categories (Academic, Careers, Economy, Market, Casino, Info) are rendered. Server Administration tools (`/ic admin ...`) are omitted, preventing confusion about commands they cannot run.
6. **Step 6: Cross-User Interaction Guard**
   * Another user attempts to click the student's active navigation buttons.
   * *Observed Experience:* Disallowed with an ephemeral response, preserving the original student's current view.

**Conclusion:** The updated information architecture provides an intuitive, accessible, and robust user experience while strictly maintaining 100% backwards compatibility.

