# Modular Discord Bot (v0.2.12)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-Active-success)

A modular Discord bot built with Python and `discord.py`. The bot combines slash-command utilities with background pollers for alerts, reporting, and tracking. The codebase is organized into feature modules (commands), service clients (API integrations), and tasks (scheduled loops).

## Features

### Slash Commands
- **Jobs**
  - `/ic jobs`: Show the latest matching job posts.
- **Market report**
  - `/ic report`: Generate a real-time market snapshot and report.
- **Additional utilities**
  - Feature modules are designed to be added/removed cleanly without affecting unrelated components.

### Background Pollers
- **Job alerts**
  - Polls and posts matching roles from:
    - Workday career sites
    - Greenhouse boards
    - Lever sites
  - Uses allow/deny keyword filters and location allowlists.
  - Checkpointing prevents reposting the same job repeatedly.
- **TTC alerts**
  - Polls the configured TTC feed and posts new incidents to a designated channel.
  - Supports severity filtering and checkpointing.
- **GO Transit alerts (Metrolinx Open Data API)**
  - Polls GO service alerts and posts new items to a designated channel.
  - Supports line allowlisting (e.g., Lakeshore West, Lakeshore East, Stouffville).
  - Checkpointing prevents reposting.
- **Market watch and daily market report**
  - Periodic volatility monitoring and watchlist movement alerts.
  - Scheduled daily report in a configurable timezone.
- **Optional BTC address watch**
  - Polls an Esplora-compatible API for activity on configured addresses.

### Mention Assistant (Gemini)
When a user mentions the bot in any channel (e.g., `@botname <question>`), the bot:
- collects up to `LLM_MAX_MESSAGES` prior messages (default: 25)
- caps context to `LLM_MAX_CHARS` (default: 12,000)
- calls Gemini to generate a response
- replies in the same channel

Notes:
- The assistant does not browse the internet.
- Context is kept in-memory only; no persistent storage of message content is required for this mode.

### Games and Activities
- The bot also consists of an ingame leaderboard system dependant on message count by user. To access this the user must type `/ic leaderboard`. 

- The bot includes a fun casino game of blackjack as well, that keeps track of bankroll per user as well as reset count. `/ic blackjack` to play and `/ic bj leaderboard` to see the leaderboard of users.

- There is a fictional stock system as well, where the user can set the stock, change the stock, and add reasoning as to why as well.

## Architecture

The project follows a layered structure:

- **Features**: slash command handlers (Discord-facing)
- **Services**: API clients and data fetch/parse utilities
- **Tasks**: scheduled pollers that run continuously in the background
- **DB**: SQLite persistence for checkpoints, histories, and lightweight state

A typical workflow:
1. A task poller fetches data via a service client.
2. Data is filtered, deduplicated (checkpointed), and formatted.
3. The bot posts to a configured channel and updates the checkpoint.

## Requirements
- Python 3.11+ recommended
- `discord.py` 2.x
- A Discord bot token
- API keys for optional integrations (Gemini, Metrolinx GO, etc.)

## Repo Layout

```text
instigate-cafe-bot/
  main.py
  requirements.txt
  .env.example

  icbot/
    app.py                 # /ic root command group
    config.py              # Settings + .env parsing
    db.py                  # SQLite wrapper (async)
    permissions.py         # role-ID permission checks
    embeds.py              # consistent embed styling (stock/games/alerts/jobs)

    features/
      stock.py             # stock commands + history
      blackjack.py         # /ic blackjack + interactive buttons
      lc_leaderboard.py    # /ic leaderboard + monthly/all-time paging

    games/
      blackjack_engine.py  # blackjack rules/state engine (pure logic)

    services/
      ttc_client.py        # TTC RSS fetch + parse
      jobs_ats_client.py   # Greenhouse + Lever fetch + normalize
      yahoo_quotes.py      # quote fetcher (used by market watch + daily report)
      crypto_fng.py        # crypto fear & greed (alternative.me)
      btc_esplora.py       # BTC address watcher (Esplora API)

    tasks/
      ttc_alerts.py        # background poller that posts TTC alerts
      job_alerts.py        # background poller that posts job alerts
      market_watch.py      # background poller that posts volatility alerts
      market_daily_report.py  # scheduled daily recap
      btc_watch.py         # BTC address movement alerts
```

## Setup

### 1) Discord Developer Portal Configuration
Create a Discord application and bot, then invite it to your server with:
- `bot`
- `applications.commands`

Required intents depend on enabled features:
- The mention assistant requires **Message Content Intent** to read message text for context.

### 2) Environment Configuration
Copy `.env.example` to `.env`, then fill in values for the features you enable.

### 3) Install and Run

#### Windows (PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -r requirements.txt
py main.py
```

#### macOS/Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

## Commands
Commands are grouped under `/ic`. Exact availability depends on which feature modules are included and synced.

Example:
- `/ic jobs`
- `/ic report`

## Configuration Overview

### Channels
Most background services require a channel ID. If a channel ID is `0` or unset, that service should be considered disabled (unless your code falls back to a default channel).

### Poll Intervals
- Jobs and alerts run on configurable intervals.
- Use conservative polling intervals in public servers to reduce spam and API usage.

### GO Line Allowlisting
Limit GO alerts to specific corridors by listing either codes or names.

Examples:
- `GO_LINES_ALLOWLIST=LW,LE,ST`
- `GO_LINES_ALLOWLIST=Lakeshore West,Lakeshore East,Stouffville`

## Operational Safety (Public Server Guidance)

If deploying to a large public server:
- Add per-user and per-server rate limits for the mention assistant.
- Add concurrency caps for LLM calls.
- Enforce provider-side budgets and billing protections.
- Restrict API keys where possible.

## Data Persistence
SQLite is used for:
- job checkpoints and posting state
- alert checkpoints (TTC, GO)
- market report state (if enabled)
- other lightweight bot state

## License
Refer to the MIT License. 

> All data provided by this bot does not constitute any financial advice.
