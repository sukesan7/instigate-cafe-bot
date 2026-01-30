# Instigate Cafe Bot 🏰☕️

A lightweight, modular Discord bot for a university friend group server (“Instigate Cafe”).  
Built in **Python** with **discord.py** slash commands, consistent **purple-themed embeds**, and a clean “feature module” layout.

> This is a private/fun bot, but it’s structured and documented like a “real” project for consistency with my other repos.

---

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-Active-success)

---

## Core Capabilities

### 📈 Instigate Cafe “Stock”
A running server joke turned into a persistent “index” you can adjust and track.

- View current stock value
- Change stock with +/- deltas (**admin/allowed roles**)
- Set stock directly (**admin/allowed roles**)
- Stock history (who/when/reason)

All outputs are presented in consistent, clean embeds.

---

### 🃏 Blackjack (Interactive Buttons)
Start a blackjack hand and play using **buttons** (no spammy `/hit` commands).

- Button controls: **Hit / Stand / Double / Split / Forfeit**
- **Only the player who started the hand** can interact (others can view)
- **5-minute inactivity timeout** → automatic forfeit (server-safety)

Includes a basic “toy bankroll” (starting bankroll configurable).

---

### 📊 Activity Leaderboard (Interactive)
Ranks most active users by **message count**.

- **This Month** and **All Time**
- **Prev / Next** paging
- Only the command author can click buttons; everyone can view output

> Counts messages the bot can see. No message content storage required.

---

### 🚇 TTC Alerts (Auto-Posted)
No slash command required. The bot automatically polls TTC notices and posts **serious alerts** into a dedicated channel.

- Posts into `TTC_ALERTS_CHANNEL_ID`
- Filters for **major** incidents (service suspended, no service, police/emergency, injuries/fatalities, etc.)
- Avoids spam: first run sets a checkpoint; posts only *new* alerts after that
- Can be configured to post **major-only** or **all** alerts

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
Admin-style actions (stock changes / sets / resets) are gated by:

- Discord **Administrator** permission, OR
- Membership in one of the role IDs listed in `ALLOWED_ROLE_IDS`

This avoids fragile “role name” matching.

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
    embeds.py              # consistent embed styling (incl. TTC alerts)

    features/
      stock.py             # stock commands + history
      blackjack.py         # /ic blackjack + interactive buttons
      lc_leaderboard.py    # /ic leaderboard + monthly/all-time paging

    games/
      blackjack_engine.py  # blackjack rules/state engine (pure logic)

    services/
      ttc_client.py        # TTC RSS fetch + parse

    tasks/
      ttc_alerts.py        # background poller that posts TTC alerts
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
   - Attach Files (optional)
   - Read Message History (optional)

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

# Embeds (optional)
EMBED_LOGO_URL=
EMBED_ACCENT_COLOR=#A64DFF
EMBED_FOOTER_TEXT=Instigate Cafe

# Useful for test servers only. Do NOT enable on your main server.
FORCE_GUILD_RESYNC=false
```

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

### Adding a new feature
1. Create a module in `icbot/features/`
2. Register commands under `/ic` (importing the module in `main.py` is enough)
3. Use `icbot/embeds.py` helpers to keep output consistent

### Background indicators (like TTC)
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

---

## Safety / Rules
- Admin-gated actions are **role-ID based**
- Leaderboard stores counts only (no message content)
- Blackjack sessions timeout after inactivity to prevent stuck games
- TTC alerts are filtered + checkpointed to prevent spam

---

## Roadmap Ideas (Later)
- Daily Toronto forecast reports
- TMU facilities / outage alerts
- `/ic fetch` document vault (course PDFs) with admin-only upload + indexed retrieval
- More mini-games (dice, trivia, etc.) under the same embed style + UI patterns
