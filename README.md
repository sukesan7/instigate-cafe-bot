# Instigate Cafe Bot (v0.2.16)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-app__commands-5865F2)
![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)
![Status](https://img.shields.io/badge/Project-active-a)

A modular Discord bot built with Python and `discord.py` that combines:
- Slash-command utilities under a single root command group (`/ic`)
- Background pollers for ATS Job Alerts, Transit advisories, and Market reporting
- Persistent storage using SQLite for state, checkpoints, leaderboards, schedules, and a unified game/economy bankroll
- An optional mention-based LLM assistant powered by the Gemini API (text + image)

This codebase is structured to be easy to extend: **features** (slash commands), **tasks** (polling loops), **games** (engines), and **services** (API clients) are cleanly separated.

THIS IS A TEST MESSAGE

---

## Highlights

### Command Features (`icbot/features/`)
- **Jobs Engine**: `/ic jobs` discovers and filters new SWE/CS/hardware Intern and New Grad roles ingested directly from Workday, Greenhouse, and Lever ATS boards.
- **University Toolkit**: `/ic notes <course>` pulls live, up-to-date Notion links from web-scraped engineering repositories (Jasper Cruz & Krish Patel). `/ic prof <name>` fetches real-time RateMyProfessors ratings.
- **Market Report**: `/ic report` generates a real-time market snapshot and report.
- **Fun Market / ICSE**: `/ic icse ...` commands for server-specific stocks (e.g., capstone teams, meme stocks), long/short positions, portfolios, trade history, and leaderboards.
- **Casino Games**: Includes fully playable table games (`/ic casino blackjack`, `/ic casino roulette`, `/ic casino slots`) powered by independent `engine` backends, seamlessly integrated into a persistent user economy bankroll.
- **Profile & Leaderboard**: `/ic profile` handles unified bankroll limits, investments, and cash balances, while `/ic leaderboard` surfaces top server net-worth rankings.
- **System Information**: `/ic botinfo` and `/ic serverinfo` provide deep-dive diagnostics on the LLM powering the bot, runtime environment, and Discord Server settings.
- **Sports Utility**: `/ic sports nba`, `/ic sports nfl`, `/ic sports ufc`, `/ic sports mma`, `/ic sports boxing` to show upcoming events and related headlines.

### Background Tasks (`icbot/tasks/`)
- **TTC & GO Alerts**: Polls Metrolinx GO Open Data API and TTC Mastodon for critical network service advisories.
- **Jobs Poller**: Crawls Greenhouse, Lever, and Workday endpoints, aggressively parsing and filtering for strict CS/SWE Junior profiles before alerting Discord.
- **Market Watch**: Volatility watcher (VIX threshold and large index/stock moves) with anti-spam latching protocols.
- **Daily Market Report**: Scheduled daily summary at configured local time (default: 16:30 America/Toronto), summarizing virtual stock closes and fun-market rollovers.
- **LLM Mention Assistant**: Triggers entirely ambient responses whenever `@Instigator` is called, utilizing context memory windows and specific lore parameters to maintain unique character traits.

### External Integrations (`icbot/services/`)
- **Gemini API** (LLM): Text and Image understanding endpoints.
- **RateMyProfessors** GraphQL Client: Retrieves direct ratings, difficulty thresholds, and class identifiers.
- **Workday, Lever, Greenhouse**: Native parsing pipelines resolving unindexed JSON structures and API graphs from modern ATS software.
- **Yahoo Finance & Crypto**: Lightweight HTTP clients for indexes/quotes and Fear & Greed indices.
- **Metrolinx & Mastodon**: Active network pollers for dynamic alerts.

---

## Command Reference

All slash commands fall under the highly-structured `/ic` root group to avoid root-level namespace pollution in large Discord Servers.

### Utilities and Tools
- `/ic report` — Generate the current market report.
- `/ic jobs` — Show the most recent matching SWE/CS/CE jobs scraped.
- `/ic exams` — Track configured course exams.
- `/ic notes <course>` — Search through and fetch Notion archives from predefined student markdown sites.
- `/ic prof <name>` — Retrieves direct Professor ratings, difficulty bounds, and top tags from RateMyProfessors.
- `/ic grades <course>` — Scrapes and displays university grade distributions per professor for a specific course.
- `/ic resume` — Uses the Gemini AI to provide a technical review and ATS scan of your uploaded resume.

### Economy, Games, & Assets
- `/ic profile [user]` — Check centralized net worth, cash holdings, and total locked equity.
- `/ic leaderboard` — Ranks entire server by Net Worth (Cash + Portfolios).
- `/ic casino blackjack` — Play an interactive visual blackjack hand inside Discord against the dealer (buttons).
- `/ic casino roulette` — Wager on specific colors, odds, or numerical sets.
- `/ic casino slots` — Test your luck across dozens of high-value slot machine payouts.

### Fun Market / Investing (`/ic icse ...`)
A highly mature, paper-trading simulated market running on the **centralized economy bankroll**.
- `/ic icse market` — Show all active fun stocks (price + weekly move snapshot).
- `/ic icse quote <symbol>` — Show a single stock quote.
- `/ic icse portfolio [user]` — Show open positions, margins, and marked equity.
- `/ic icse buy/sell <symbol> <qty>` — Execute long position closures or scaling.
- `/ic icse short/cover <symbol> <qty>` — Take short execution routes against server stocks using strict collateral logic.

#### Admin / Market Controls (Admin/Allowed Roles)
- `/ic admin move <symbol> <delta> [reason]` — Move a stock price by a delta increment.
- `/ic admin set <symbol> <value> [reason]` — Hard-reset an equity price.
- `/ic admin llm_stop` — Pauses the LLM functionality (chatbot and resume tool).
- `/ic admin llm_start` — Re-enables the LLM functionality.

### System & Info Diagnostics
- `/ic botinfo` — View LLM runtime bounds, active tech stack, and internal modules.
- `/ic serverinfo` — View meta-data on the current discord server (guild ids, roles, structure).

---

## LLM Assistant (Gemini)

The LLM assistant is absolutely **mention-based** and designed seamlessly to respond identically to a user (`@Instigator ...`).

### Behavior
- Auto-caches recent histories up to `LLM_MAX_MESSAGES` messages.
- Transparently handles image Q/A parsing if an attachment is detected inline.
- Defends against user pings (`@everyone`) inside synthesized messages safely.

### Lore Injection
Unique character-behaviors or "server inside jokes" are loaded silently and implicitly through prompt-injection.
`tools/lore.x_files.json` structures these boundaries safely so the bot maintains extreme character adherence without bloating overhead.

---

## Jobs Ingestion & Security Strictness

The Job Board engine tracks endpoints strictly injected through `.env`:
`GREENHOUSE_BOARDS` | `LEVER_SITES` | `WORKDAY_SITES`

All jobs are run through ultra-aggressive filters dictating:
1. Allowlisted Keywords (`SWE`, `Software`, `Hardware`, `Electrical`).
2. Hard Denies against Mid-Senior configurations (`Analyst`, `Senior`, `Manager`).
3. Position Bounds optionally limiting roles using `JOBS_REQUIRE_EARLY_CAREER=true` (forces `Intern`, `Co-op`, or `New Grad` nomenclature). If `false` (default), standard mid-level engineering roles are accepted.

---

## Data Storage (SQLite)

The bot operates entirely on an AIO-synchronized SQLite cache (`icbot/db.py`) utilizing unique stores across:
- Checkpoints & Event Deduping (Jobs, Feeds, Transit).
- Leaderboard/Count/Stats tracking.
- Casino state logic.
- Investment logic (Positions, Lock collateral, Ledgers, Portfolios).

---

## Project Layout

```text
project-root/
├─ .env.example
├─ .gitignore
├─ README.md
├─ main.py
├─ icbot/
│  ├─ app.py
│  ├─ config.py
│  ├─ db.py
│  ├─ embeds.py
│  ├─ permissions.py
│  ├─ features/
│  │  ├─ admin.py
│  │  ├─ blackjack.py
│  │  ├─ exams.py
│  │  ├─ grades.py                  
│  │  ├─ info.py                  
│  │  ├─ invest.py                  
│  │  ├─ jobs.py
│  │  ├─ lc_leaderboard.py
│  │  ├─ notes.py                   
│  │  ├─ prof.py                    
│  │  ├─ profile.py                 
│  │  ├─ report.py
│  │  ├─ resume.py                  
│  │  ├─ roulette.py                
│  │  ├─ slots.py                   
│  │  └─ sports.py                   
│  ├─ games/
│  │  ├─ blackjack_engine.py
│  │  ├─ roulette_engine.py
│  │  └─ slots_engine.py
│  ├─ services/
│  │  ├─ btc_esplora.py
│  │  ├─ crypto_fng.py
│  │  ├─ fun_market.py              
│  │  ├─ fun_market_catalog.py      
│  │  ├─ gemini_client.py
│  │  ├─ go_client.py
│  │  ├─ jobs_ats_client.py
│  │  ├─ jobs_workday_client.py
│  │  ├─ prof_client.py             
│  │  ├─ sports_client.py
│  │  ├─ ttc_client.py
│  │  ├─ user_service.py            
│  │  └─ yahoo_quotes.py
│  └─ tasks/
│     ├─ btc_watch.py
│     ├─ go_alerts.py
│     ├─ job_alerts.py
│     ├─ jobs_workday_alerts.py
│     ├─ llm_assistant.py
│     ├─ market_daily_report.py     
│     ├─ market_watch.py
│     └─ ttc_alerts.py
└─ tools/
   ├─ lore.example.json
   ├─ lore_loader.py
   ├─ purge_global_commands.py
   └─ scan_secrets.py
```

---

## License & Attribution

### Software Code License
The source code of this repository is licensed under the [MIT License](LICENSE):

```text
MIT License

Copyright (c) 2026 sukesan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Community Branding and Trademark
The "Instigate Cafe" name, server logos, graphic assets, and private community lore packs (`tools/lore.*.json`) are the property of Instigate Cafe and its community maintainers. The MIT license grants permissions only to the software source code; it does not grant rights to use server trademarks, community identities, or proprietary member lore in external commercial or public deployments.

### Third-Party Data & External Asset Attribution
This bot integrates with external data services and educational materials. Contributors and operators should review [docs/publishing-checklist.md](docs/publishing-checklist.md) for full licensing boundaries:
- **Course Notes:** Notion-hosted engineering and computer science course notes (authored by Jasper Cruz and Krish Patel) are student-authored educational materials linked as third-party resources. They are not covered by the MIT software license.
- **RateMyProfessors:** Professor ratings and search results are retrieved from RateMyProfessors / Chewse Inc. for non-commercial student informational lookup.
- **Transit Data:** GO Transit information is powered by the [Metrolinx Open Data API](https://www.metrolinx.com/en/about-us/open-data) under Metrolinx Open Data terms. TTC alerts are syndicated from public transit feeds.
- **Financial Quotes:** Stock and market data queries are provided for informational and entertainment purposes only and do not constitute financial advice.
