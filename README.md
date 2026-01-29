# Instigate Cafe Bot 🏰☕️

A lightweight, modular Discord bot.  
Built in **Python** with **discord.py** slash commands, consistent **embeds**, and a clean “feature module” layout.

> This is a fun private project, but it’s structured and documented like a “real” one for consistency with my other projects.

---

## Badges

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-Active-success)

---

## Core Capabilities

### 📈 Instigate Cafe “Stock”
A running server joke turned into a persistent “index” you can adjust and track.

- View current stock value
- Change stock with +/- deltas (admin/allowed roles)
- Set stock directly (admin/allowed roles)
- Stock history (who/when/reason)

All outputs are presented in **clean embeds** with optional logo and consistent accent color.

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
Admin-style actions (stock changes / resets) are gated by:

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
    embeds.py              # consistent embed styling

    features/
      stock.py             # stock commands + history
      blackjack.py         # /ic blackjack + interactive buttons
      lc_leaderboard.py    # /ic leaderboard + monthly/all-time paging

    games/
      blackjack_engine.py  # blackjack rules/state engine (pure logic)
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
   - Attach Files (optional, if you later add PDFs)
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

# Optional: stock updates also get posted here
STOCK_CHANNEL_ID=1234567890

# Role IDs allowed to run admin-style actions (comma-separated)
ALLOWED_ROLE_IDS=111111111111111111,222222222222222222

# Optional: restrict regular users to one bot-commands channel (admins bypass)
USER_COMMAND_CHANNEL_ID=333333333333333333

# Blackjack config
BJ_STARTING_BANKROLL=100
BJ_DEALER_HITS_SOFT_17=true
BJ_MAX_SPLITS=1

# Embeds (optional)
EMBED_LOGO_URL=
EMBED_ACCENT_COLOR=#A64DFF
EMBED_FOOTER_TEXT=Instigate Cafe
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

### Data persistence
Uses **SQLite** for:
- Stock value + stock history
- Blackjack bankroll/stats (if enabled)
- Message counts (monthly + all-time)

---

## Safety / Rules
- Admin-gated actions are **role-ID based**
- Leaderboard stores counts only (no message content)
- Blackjack sessions timeout after inactivity to prevent stuck games

---

## Roadmap Ideas
- `/ic fetch` document vault (course PDFs) with admin-only upload + indexed retrieval
- `/ic nba` or `/ic nfl` schedule summaries
- More mini-games (dice, trivia, etc.) under the same embed style + UI patterns
