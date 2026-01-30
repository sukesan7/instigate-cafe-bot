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
- Filtered by location + keywords (configurable)
- Anti-spam safeguards:
  - Per-cycle posting cap (prevents flooding)
  - Checkpointing so old postings aren’t re-posted
- Optional “bootstrap” mode (test servers) to post a small sample on first run

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

    tasks/
      ttc_alerts.py        # background poller that posts TTC alerts
      job_alerts.py        # background poller that posts job alerts
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

**Leaderboard intent note:**
- Requires **Guild Messages** intent (non-privileged).
- Does **not** require Message Content Intent if only counting messages.

---

### 2) Create `.env`
Copy `.env.example` → `.env`, then fill values:

```env
DISCORD_TOKEN=YOUR_TOKEN
GUILD_ID=YOUR_TEST_GUILD_ID

# Optional: if set, stock updates also get posted here
STOCK_CHANNEL_ID=1234567890

# Role IDs allowed to run admin-style actions (comma-separated)
ALLOWED_ROLE_IDS=111111111111111111,222222222222222222

# Optional: restrict regular users to one bot-commands channel (admins bypass)
USER_COMMAND_CHANNEL_ID=333333333333333333

# Blackjack config
BJ_STARTING_BANKROLL=100
BJ_DEALER_HITS_SOFT_17=true
BJ_MAX_SPLITS=1
BJ_SESSION_TTL_MINUTES=20

# TTC Alerts (auto-posting)
TTC_ALERTS_CHANNEL_ID=444444444444444444
TTC_FEED_URL=https://mstdn.ca/@ttcnotices.rss
TTC_POLL_SECONDS=120
# "major" = serious incidents only, "all" = post every notice
TTC_ALERT_LEVEL=major
# Test servers only: post latest matching alert once on startup
TTC_BOOTSTRAP_POST_LATEST=false

# Job Alerts (auto-posting)
JOB_ALERTS_CHANNEL_ID=555555555555555555
JOBS_POLL_SECONDS=600
# Greenhouse: tokens after boards.greenhouse.io/<token>
GREENHOUSE_BOARDS=stripe,robinhood,coinbase
# Lever: slugs after jobs.lever.co/<slug>
LEVER_SITES=palantir,waabi,policyme
# Optional: cut noise
JOBS_LOCATION_ALLOWLIST=Toronto,Remote,Canada
JOBS_KEYWORDS_ALLOWLIST=Intern,Internship,Co-op,Coop,New Grad,Graduate,Junior,Entry
JOBS_KEYWORDS_DENYLIST=Senior,Staff,Principal,Lead,Manager
# Test servers only: post a small sample on first run for each new source
JOBS_BOOTSTRAP_POST=false

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
