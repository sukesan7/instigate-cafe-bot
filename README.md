# Modular Discord Bot (v0.2.16)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-active-a)

A modular Discord bot built with Python and `discord.py` that combines:
- Slash-command utilities under a single root command group (`/ic`)
- Background pollers for alerts and reporting
- Persistent storage using SQLite for state, checkpoints, leaderboards, schedules, and game/economy data
- An optional mention-based LLM assistant powered by the Gemini API (text + image)

This codebase is structured to be easy to extend: **features** (slash commands), **tasks** (polling loops), and **services** (API clients) are separated cleanly.

---

## Highlights

### Command features (`icbot/features/`)
- **Jobs**: `/ic jobs` shows the most recent SWE/CS/hardware jobs the bot has ingested (Workday + Greenhouse + Lever).
- **Market report**: `/ic report` generates a real-time market snapshot and report.
- **Exam schedule**: `/ic exams` shows upcoming exams (admin-managed via `/ic exams_add`, `/ic exams_remove`, `/ic exams_clear`).
- **Virtual stock ticker**: legacy `$IC` commands to view/manage the server “stock” price and history (now backed by the fun market system for compatibility).
- **Fun market / investing system**: `/ic invest ...` commands for server-specific stocks (e.g., capstone teams, meme stocks), long/short positions, portfolios, trade history, and leaderboards.
- **Blackjack**: interactive button-based blackjack with persistent bankroll/stats, leaderboard, and reset counters.
- **Activity leaderboard**: `/ic leaderboard` and `/ic reset` (admin/allowed roles) for activity tracking.
- **Sports utility**: `/ic nba`, `/ic nfl`, `/ic ufc`, `/ic mma`, `/ic boxing` to show upcoming events and related headlines.

### Background tasks (`icbot/tasks/`)
- **TTC alerts**: polls TTC/Mastodon notices and posts new service alerts.
- **GO Transit alerts**: polls Metrolinx GO Open Data API and posts alerts, optionally filtered to specific lines.
- **Jobs pollers**: polls Greenhouse/Lever/Workday sources and posts new jobs after filtering.
- **Market watch**: volatility watcher (VIX threshold and large index/stock moves) with anti-spam latching + cooldown.
- **Daily market report**: scheduled daily summary at a configured local time (default 16:30 America/Toronto), including the server’s stock close and a fun-market weekly snapshot.
- **BTC address watch**: monitors a configured set of BTC addresses (e.g., whale/institution wallets) for activity.
- **LLM mention assistant**: responds only when explicitly mentioned, using recent channel context and optional lore.

### External integrations (`icbot/services/`)
- **Gemini API** (LLM): text + image understanding, mention-triggered only.
- **Workday** job search (CXS endpoints) + URL normalization.
- **Greenhouse** and **Lever** job board clients.
- **Yahoo finance-style quotes** client for tickers/VIX/index moves (via a lightweight HTTP client).
- **Metrolinx GO Open Data API** for GO alerts.
- **Mastodon** client for TTC alerts.
- **Blockstream Esplora-compatible** API for BTC address activity.
- **Sports schedule/news** client for NBA/NFL/UFC/boxing utilities.
- **Crypto Fear & Greed** support (if enabled by your reporting flow).
- **Fun market service layer** for server stock catalog sync, positions, trades, portfolios, and weekly rollovers.

---

## Command reference

All slash commands are under the `/ic` root group.

### General utilities
- `/ic report` — Generate the current market report.
- `/ic jobs` — Show the most recent matching jobs the bot has seen.
- `/ic exams` — Show the configured exam schedule.

### Exams (admin/allowed roles)
- `/ic exams_add` — Add an exam entry.
- `/ic exams_remove` — Remove an exam entry by ID.
- `/ic exams_clear` — Remove all exam entries.

### Virtual stock ticker (legacy compatibility)
- `/ic stock` — Show the current server stock price (`$IC`).
- `/ic stock_history` — Show recent `$IC` price changes.
- `/ic stock_change` — Change `$IC` price by a delta (admin/allowed roles).
- `/ic stock_set` — Set `$IC` price to an exact value (admin/allowed roles).

> Note: These legacy `$IC` commands are preserved for compatibility and now use the newer fun-market backend internally.

### Fun market / investing (`/ic invest ...`)
Server-specific paper-trading style market using the **same economy bankroll** used by blackjack.

#### Market & portfolio
- `/ic invest market` — Show all active fun stocks (price + weekly move snapshot).
- `/ic invest quote <symbol>` — Show a single stock quote and weekly change (e.g., `IC`, `DISD`, `AGIS`).
- `/ic invest portfolio [user]` — Show open positions, cash, and net worth.
- `/ic invest leaderboard [limit]` — Show fun-market net-worth leaderboard.
- `/ic invest trades [limit] [user]` — Show recent fun-market trades.
- `/ic invest history [limit] [symbol]` — Show recent admin price-change history.

#### Trading
- `/ic invest buy <symbol> <qty>` — Buy (long) shares/contracts.
- `/ic invest sell <symbol> <qty>` — Sell long shares/contracts.
- `/ic invest short <symbol> <qty>` — Open a short position (collateral required).
- `/ic invest cover <symbol> <qty>` — Close (cover) part/all of a short position.

#### Admin / market controls (admin/allowed roles)
- `/ic invest move <symbol> <delta> [reason]` — Move a stock price by a delta.
- `/ic invest set <symbol> <value> [reason]` — Set a stock to an exact price.
- `/ic invest sync` — Sync the code-defined stock catalog into the database.
- `/ic invest weekroll [note]` — Manually roll/reset weekly baselines (for weekly change tracking).

#### Catalog behavior (code-defined stocks)
- Fun stocks are defined in code (e.g., `icbot/services/fun_market_catalog.py`).
- Add/remove/update symbols in code, then run `/ic invest sync`.
- Missing symbols are marked inactive on sync (history/positions preserved).

### Games and leaderboards
- `/ic blackjack` — Start an interactive blackjack hand (buttons).
  - Persistent bankroll and stats per user.
  - Leaderboard view (top bankrolls).
  - Reset counter tracking on resets.
- `/ic leaderboard` — Activity leaderboard (monthly + all-time).
- `/ic reset` — Reset activity counts (admin/allowed roles).

> Note: bankroll resets are guarded to prevent economy exploits while a user still has open fun-market positions.

### Sports utilities
- `/ic nba` — Upcoming NBA games + latest headlines.
- `/ic nfl` — Upcoming NFL games + latest headlines.
- `/ic ufc` / `/ic mma` — Upcoming MMA events + latest headlines.
- `/ic boxing` — Upcoming boxing bouts + latest headlines.

---

## LLM assistant (Gemini)

The LLM assistant is **mention-based** and will only respond when the bot is explicitly mentioned in a message (e.g., `@botname ...`).

### Behavior
- Reads up to `LLM_MAX_MESSAGES` recent messages (default 25) with a `LLM_MAX_CHARS` cap (default 12,000).
- Supports **image Q&A** when the mention message (or the replied-to message) contains an image attachment.
- Uses a per-user cooldown (default 3 seconds).
- Applies safe formatting and anti-spam constraints (no `@everyone` / `@here` mentions in replies).

### Lore support
A lightweight lore system can inject server-specific inside jokes or context into the prompt **only when triggers match**.

- Lore file example: `tools/lore.x_files.json`
- Optional: set `LORE_PATH=tools/lore.x_files.json`

This avoids bloating every request while still making the assistant context-aware.

---

## Jobs ingestion and filtering

The bot supports:
- **Greenhouse** boards (`GREENHOUSE_BOARDS`)
- **Lever** sites (`LEVER_SITES`)
- **Workday** sites (`WORKDAY_SITES`)

Filtering is driven by `.env` allow/deny lists:
- `JOBS_LOCATION_ALLOWLIST`
- `JOBS_KEYWORDS_ALLOWLIST`
- `JOBS_KEYWORDS_DENYLIST`

Recommended approach:
- Keep allowlist focused on SWE/CS/CE/hardware keywords (e.g., “software engineer”, “embedded”, “firmware”, “fpga”).
- Use denylist aggressively for non-technical roles (sales, marketing, PM, HR, etc.).
- Keep short ambiguous tokens out of allowlists (e.g., plain “c”) to reduce false positives.

---

## Alerts and reporting

### Market watch
- Designed to avoid spamming: it triggers on threshold crossings/edges (not every cooldown interval while above threshold).
- Checks:
  - VIX threshold (`MARKET_WATCH_VIX_LEVEL`)
  - Large index move (`MARKET_WATCH_INDEX_MOVE_PCT`)
  - Large watched-stock move (`MARKET_WATCH_STOCK_MOVE_PCT`)

### Daily market report
- Posts at `MARKET_DAILY_REPORT_TIME` in `MARKET_DAILY_REPORT_TZ`.
- Summarizes watched tickers and includes the server’s virtual stock close.
- Also includes a **fun-market section** (weekly snapshot of active server stocks).

### TTC and GO alerts
- TTC: polls a Mastodon feed (instance/account configurable).
- GO: polls Metrolinx GO Open Data API; supports line allowlist and optional filtering of facility advisories.

### BTC address watch
- Watches a configured list of BTC addresses and posts events into the configured channel.
- Uses an Esplora-compatible endpoint (default Blockstream).

---

## Data storage (SQLite)

The bot uses SQLite for:
- Persistent bot state and checkpoints (e.g., “last seen” IDs for feeds)
- Job de-duplication (avoid reposting previously seen jobs)
- Exam schedule storage
- Blackjack bankroll/stats/leaderboards and reset counts
- Activity leaderboard counts
- Virtual stock history (`$IC`)
- Fun market stock catalog/state (prices, weekly baselines, active/inactive status)
- Fun market positions (long/short), collateral locks, and trade ledger/history

Database path is configured via `DB_PATH`.

---

## Configuration

The bot is configured via environment variables. Copy `.env.example` to `.env` and fill in values.

Minimum required:
- `DISCORD_TOKEN`
- Optional (recommended for dev): `GUILD_ID`
- Channels for the features you enable (set to 0 to disable)

### Enabling modules
Most subsystems are enabled by:
- setting the relevant channel id to a non-zero value, and/or
- setting an explicit `*_ENABLED=true` flag (where applicable)

Examples:
- Jobs: `JOB_ALERTS_CHANNEL_ID`, `GREENHOUSE_BOARDS`, `LEVER_SITES`, `WORKDAY_SITES`
- Market watch: `MARKET_WATCH_ENABLED=true`, `MARKET_WATCH_CHANNEL_ID=...`
- Daily report: `MARKET_DAILY_REPORT_ENABLED=true`, `MARKET_DAILY_REPORT_CHANNEL_ID=...`
- TTC: `TTC_ALERTS_CHANNEL_ID=...`
- GO: `GO_ALERTS_CHANNEL_ID=...`, `GO_API_KEY=...`
- LLM: `LLM_ENABLED=true`, `GEMINI_API_KEY=...`, `GEMINI_MODEL=gemini-2.5-flash`

### Fun market configuration
The fun market stock catalog is **code-defined** (not configured via slash commands):
- Edit `icbot/services/fun_market_catalog.py` to add/remove/update server stocks.
- Run `/ic invest sync` after changes to sync catalog updates into SQLite.

### Permissions
Admin-only actions are gated by role IDs:
- `ALLOWED_ROLE_IDS` (comma-separated role IDs)

---

## Setup and run

### Requirements
- Python 3.11+
- A Discord bot token (Discord Developer Portal)
- Optional API keys (GO, Gemini, etc.) depending on enabled modules

### Install
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Configure
```bash
cp .env.example .env
# edit .env with your token, guild, channels, and API keys
```

### Run
```bash
python main.py
```

---

## Project layout

```text
project-root/
├─ .env.example
├─ .gitignore
├─ LICENSE
├─ README.md
├─ main.py
├─ requirements.txt
├─ icbot/
│  ├─ __init__.py
│  ├─ app.py
│  ├─ config.py
│  ├─ db.py
│  ├─ embeds.py
│  ├─ permissions.py
│  ├─ utils.py
│  ├─ features/
│  │  ├─ __init__.py
│  │  ├─ blackjack.py
│  │  ├─ exams.py
│  │  ├─ invest.py                  
│  │  ├─ jobs.py
│  │  ├─ lc_leaderboard.py
│  │  ├─ report.py
│  │  ├─ sports.py
│  │  └─ stock.py                   
│  ├─ games/
│  │  ├─ __init__.py
│  │  └─ blackjack_engine.py
│  ├─ services/
│  │  ├─ __init__.py
│  │  ├─ btc_esplora.py
│  │  ├─ crypto_fng.py
│  │  ├─ fun_market.py              
│  │  ├─ fun_market_catalog.py      
│  │  ├─ gemini_client.py
│  │  ├─ go_client.py
│  │  ├─ jobs_ats_client.py
│  │  ├─ jobs_workday_client.py
│  │  ├─ sports_client.py
│  │  ├─ ttc_client.py
│  │  └─ yahoo_quotes.py
│  └─ tasks/
│     ├─ __init__.py
│     ├─ btc_watch.py
│     ├─ go_alerts.py
│     ├─ job_alerts.py
│     ├─ jobs_workday_alerts.py
│     ├─ llm_assistant.py
│     ├─ market_daily_report.py     
│     ├─ market_watch.py
│     └─ ttc_alerts.py
└─ tools/
   ├─ __init__.py
   ├─ lore_loader.py
   └─ purge_global_commands.py
```

---

## Notes for production use

If you run this in a public server:
- Keep the LLM assistant mention-only (default behavior).
- Set conservative caps (`LLM_MAX_MESSAGES`, `LLM_MAX_CHARS`) and a small cooldown.
- Configure budgets/alerts at your model provider and keep API keys private.
- Use `ALLOWED_ROLE_IDS` for destructive actions (resets, stock changes, exam updates, fun-market price changes).
- Be transparent with fun-market price updates (use reasons when moving/setting prices) to keep it fair/fun for server members.

---

## License
See `LICENSE`.