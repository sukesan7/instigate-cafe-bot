# Instigate Cafe Bot 🏰☕️

A lightweight, modular Discord bot for a university friend group server (“Instigate Cafe”).  
Built in **Python** with **discord.py** slash commands, consistent **purple-themed embeds**, and a clean “feature module” layout.

> Private/fun bot — but structured like a “real” repo for consistency with my other projects.

---

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-Active-success)

---

## Features

### 📈 Instigate Cafe “Stock”
A running server joke turned into a persistent index you can adjust and track.

- View current stock value
- Change stock with +/- deltas (**admin/allowed roles**)
- Set stock directly (**admin/allowed roles**)
- Stock history (who/when/reason)
- Optional: broadcast stock changes to a dedicated channel (`STOCK_CHANNEL_ID`)

All outputs are presented in consistent, clean embeds.

---

### 🃏 Blackjack (Interactive Buttons)
Start a blackjack hand and play using **buttons** (no spammy `/hit` commands).

- Button controls: **Hit / Stand / Double / Split / Forfeit**
- **Only the player who started the hand** can interact (others can view)
- **5-minute inactivity timeout** → automatic forfeit (safety)

Includes a “toy bankroll” system (starting bankroll configurable).

---

### 📊 Activity Leaderboard (Interactive)
Ranks most active users by **message count**.

- **This Month** and **All Time**
- **Prev / Next** paging
- Only the command author can click buttons; everyone can view output

> Counts messages the bot can see. No message content storage required.

---

### 🚇 TTC Alerts (Auto-Posted)
No slash command required. The bot automatically polls TTC notices and posts alerts into a dedicated channel.

- Posts into `TTC_ALERTS_CHANNEL_ID`
- Configurable alert level:
  - `major` = serious incidents only (service suspended, no service, police/emergency, injuries/fatalities, etc.)
  - `all` = post every notice
- Uses a checkpoint to avoid reposting old alerts
- Optional “bootstrap” mode (test servers) to post the latest matching status once on startup

---

### 💼 Job Alerts (Auto-Posted from Public ATS Boards)
Automatically polls public job boards and posts matching roles into a `#job-alerts` channel.

- Posts into `JOB_ALERTS_CHANNEL_ID`
- Sources supported:
  - **Greenhouse** (board tokens)
  - **Lever** (site slugs)
  - **Workday** (company career sites)
- Filtered by location + keywords (configurable)
- Anti-spam safeguards:
  - Per-cycle posting cap (prevents flooding)
  - Checkpointing so old postings aren’t re-posted
- Optional “bootstrap” mode (test servers) to post a small sample on first run

---

### 📉 Market Watch (Volatility Alerts)
Automatically polls a lightweight quotes endpoint and posts alerts for:

- **Fear / volatility index** spikes (VIX)
- Big index moves (SPY/QQQ)
- Big moves in a configurable watchlist (defaults include FAANG+, PLTR, select biotech)

Configure with `MARKET_WATCH_*` environment variables.

---

### 📊 Daily Market Report (Scheduled Recap)
Posts a daily recap at a scheduled time (default **16:30 America/Toronto**) including:

- Indices snapshot
- Volatility snapshot (VIX / VIX9D / VVIX when available)
- Top gainers/losers from your watchlist
- Crypto Fear & Greed (alternative.me)
- The fictitious **Instigate Cafe Stock** close ($IC)

Configure with `MARKET_DAILY_REPORT_*`.

---

### ₿ Bitcoin Address Watch ("Satoshi" / whales)
Optional on-chain watcher that posts an alert when any watched BTC address shows new activity.

- Uses an Esplora-compatible API (defaults to Blockstream)
- You provide the addresses via `BTC_WATCH_ADDRESSES`

Configure with `BTC_WATCH_*`.

---

## Commands (User-Facing)

### Stock
- `/ic stock`
- `/ic stock_change change:+100 reason:"Midterm win"`
- `/ic stock_set value:500 reason:"Job offer"`
- `/ic stock_history`

### Games
- `/ic blackjack bet:10`

### Leaderboard
- `/ic leaderboard`

---

## Permissions & Server Cleanliness

### ✅ Role-ID Based Admin Actions
Admin-style actions (stock changes / sets) are gated by:

- Discord **Administrator** permission, OR
- Membership in one of the role IDs listed in `ALLOWED_ROLE_IDS`

This avoids fragile role-name matching.

### 🧼 Single Channel Mode (Optional)
To keep the server clean and organized:

- Regular users can be restricted to using `/ic ...` in **one channel**
- Admin/allowed roles can run `/ic` anywhere

Enable with `USER_COMMAND_CHANNEL_ID`.

---

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

---

## Setup

### 1) Discord Developer Portal
1. Create a Discord Application
2. Add a Bot
3. Invite it with scopes:
   - `bot`
   - `applications.commands`
4. Recommended bot permissions:
   - Send Messages
   - Embed Links
   - Read Message History (optional)
   - Attach Files (optional; useful later if you add a document vault)

**Intents note:**
- The Activity Leaderboard requires **Guild Messages** intent (non-privileged).
- The LLM mention assistant (Gemini) requires **Message Content Intent**.

---

### 2) Create `.env`
Copy `.env.example` → `.env`, then fill values:

```env
DISCORD_TOKEN=YOUR_TOKEN
GUILD_ID=YOUR_TEST_GUILD_ID

# Channels
STOCK_CHANNEL_ID=1234567890
TTC_ALERTS_CHANNEL_ID=444444444444444444
JOB_ALERTS_CHANNEL_ID=555555555555555555
GO_ALERTS_CHANNEL_ID=666666666666666666
MARKET_WATCH_CHANNEL_ID=777777777777777777
MARKET_DAILY_REPORT_CHANNEL_ID=888888888888888888
BTC_WATCH_CHANNEL_ID=999999999999999999

# Role IDs allowed to run admin-style actions (comma-separated)
ALLOWED_ROLE_IDS=111111111111111111,222222222222222222

# Optional: restrict regular users to using /ic in one channel (admins bypass)
USER_COMMAND_CHANNEL_ID=333333333333333333

# TTC Alerts (polls TTC Notices on Mastodon)
TTC_MASTODON_BASE=https://mstdn.ca
TTC_MASTODON_ACCT=ttcnotices
TTC_POLL_SECONDS=120
TTC_ALERT_LEVEL=major
TTC_BOOTSTRAP_POST_LATEST=false

# GO Transit Alerts (official Metrolinx Open Data)
GO_API_KEY=
GO_POLL_SECONDS=180
GO_ALERT_LEVEL=major
GO_LINES_ALLOWLIST=Lakeshore West, Lakeshore East, Stouffville

# Job Alerts
JOBS_POLL_SECONDS=600
GREENHOUSE_BOARDS=stripe,robinhood,coinbase
LEVER_SITES=palantir,wealthsimple
WORKDAY_SITES=RBC|https://rbc.wd3.myworkdayjobs.com/en-US/RBC
JOBS_LOCATION_ALLOWLIST=toronto,canada,united states,remote,hybrid
JOBS_KEYWORDS_ALLOWLIST=software,engineer,developer,c++,python,new grad,intern
JOBS_KEYWORDS_DENYLIST=sales,marketing,recruiter,hr,product manager
JOBS_BOOTSTRAP_POST=false

# LLM Mention Assistant (Gemini)
LLM_ENABLED=false
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
LLM_COOLDOWN_SECONDS=3
LLM_MAX_MESSAGES=25
LLM_MAX_CHARS=12000

# Embeds (optional)
EMBED_LOGO_URL=
EMBED_ACCENT_COLOR=#A64DFF
EMBED_FOOTER_TEXT=Instigate Cafe

# Useful for test servers only. Do NOT enable on your main server.
FORCE_GUILD_RESYNC=false
```

> **Prod tip:** use a separate `DB_PATH` per server (dev vs main) so test data doesn’t carry over.

---

### 3) Install & Run (Windows / VSCode)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -r requirements.txt
py main.py
```

> Windows timezone note: if your leaderboard uses `zoneinfo`, ensure `tzdata` is installed.  
> If it’s not already in `requirements.txt`, install it via `pip install tzdata`.

---

## Development Notes

### Adding a new slash-command feature
1. Create a module in `icbot/features/`
2. Register commands under `/ic` (importing the module in `main.py` is enough)
3. Use `icbot/embeds.py` helpers to keep output consistent

### Adding a background indicator (like TTC or Jobs)
Background “indicator” features live under:
- `icbot/services/` for data fetching/parsing
- `icbot/tasks/` for scheduled polling + posting

---

## Data Persistence
Uses **SQLite** for:
- Stock value + stock history
- Blackjack bankroll/stats (if enabled)
- Message counts (monthly + all-time)
- TTC alert checkpoint (prevents reposting old alerts)
- Job alert checkpoint (prevents reposting old postings)

---

## Safety / Rules
- Admin-gated actions are **role-ID based**
- Leaderboard stores counts only (no message content)
- Blackjack sessions timeout after inactivity to prevent stuck games
- TTC alerts are filtered + checkpointed to prevent spam
- Job alerts are capped per cycle + checkpointed to prevent flooding

---

## Roadmap Ideas (Later)
- Daily Toronto forecast reports (auto-posted)
- TMU facilities / outage alerts
- `/ic fetch` document vault (course PDFs) with admin-only upload + indexed retrieval
- More mini-games (roulette, dice, trivia, etc.) under the same embed + UI patterns
