# Instigate Cafe Bot

Core features:
- `/ic stock` + `/ic stock_change` + `/ic stock_set` + `/ic stock_history`
- Blackjack: `/ic bj start|hit|stand|double|split|balance|leaderboard`
- Activity leaderboard (message count): `/ic lc leaderboard`

## Setup
1) Create a Discord application + bot in the Developer Portal.
2) Invite it with scopes: `bot`, `applications.commands`.
3) Create `.env` from `.env.example` and fill values.
4) Install deps and run:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Notes
- For **instant slash command availability**, set `GUILD_ID` to your server id (guild sync).
- For activity leaderboard (message count), the bot needs the **Server Messages intent** enabled (default intents usually suffice). You do NOT need Message Content intent.


## Embeds
Set `EMBED_LOGO_URL` in `.env` to show a logo thumbnail on every embed (paste a Discord CDN image URL).
Set `EMBED_ACCENT_COLOR` to change the bright purple accent (hex like `#A64DFF`).
