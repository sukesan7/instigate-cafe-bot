# Instigate Cafe Bot

[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![discord.py 2.3+](https://img.shields.io/badge/discord.py-2.3%2B%20app__commands-5865F2)](https://github.com/Rapptz/discord.py)
[![Storage SQLite WAL](https://img.shields.io/badge/Storage-SQLite%20WAL-lightgrey)](https://www.sqlite.org/)
[![CI Offline Quality Gate](https://img.shields.io/badge/CI-Offline%20Quality%20Gate-success)](.github/workflows/ci.yml)
[![License MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A modular, student-focused Discord bot combining academic study tools, tech job alerts, regional transit advisories, a virtual campus economy, and optional privacy-first AI resume feedback.

---

## 1. What the Bot Is (and What It Is Not)

- **What it IS:**
  - An application running strictly on Discord Slash Commands (`/ic`) under a single clean command namespace.
  - An academic utility providing study guide links (Notion), RateMyProfessors ratings, and shared guild exam calendars with iCalendar (`.ics`) export.
  - A tech careers feed filtering junior software engineering internships and new grad postings from Greenhouse, Lever, and Workday.
  - A simulated campus paper-trading exchange (ICSE) and casino mini-games running on an atomic, double-entry SQLite ledger.
  - A self-hostable service executing as a non-root process with outbound-only networking and zero open inbound ports.

- **What it is NOT:**
  - **Not a universal multi-campus bot:** Default study guides and course notes focus on Toronto Metropolitan University (TMU) STEM curricula. Adding new institutions requires implementing a campus adapter (see [Provider Guide](docs/providers.md)).
  - **Not a financial or investment platform:** All stock quotes, trades, and casino bets are purely educational simulations using virtual campus tokens. Real money transactions are neither supported nor possible.
  - **Not an automated compliance tool:** While engineered for data minimization and Right-to-be-Forgotten deletion, hosting operators remain responsible for their community's privacy policies.
  - **Not an always-listening microphone:** The bot does not eavesdrop on channel chat. Message content is processed only when explicitly invoked via slash commands or direct `@mentions`.

---

## 2. High-Level Architecture

```
                       Discord Platform
                     (Outgoing WSS / HTTPS)
                              │
                              ▼
                   ┌───────────────────────┐
                   │   main.py (Gateway)   │
                   └──────────┬────────────┘
                              │
         ┌────────────────────┴────────────────────┐
         ▼                                         ▼
┌──────────────────┐                     ┌──────────────────┐
│ Command Router   │                     │ TaskSupervisor   │
│ (/ic 53 leaves)  │                     │ (8 pollers)      │
└────────┬─────────┘                     └────────┬─────────┘
         │                                        │
         ▼                                        ▼
┌───────────────────────────────────────────────────────────┐
│              ServiceContainer & Domain Engines            │
│  • Academics   • Careers (ATS)   • Economy   • Gemini AI  │
└─────────────────────────────┬─────────────────────────────┘
                              │
                              ▼
                   ┌───────────────────────┐
                   │  SQLite 3 (WAL Mode)  │
                   │  • users  • ledger    │
                   └───────────────────────┘
```

---

## 3. Feature Status & Module Matrix

All user-facing commands reside under the root group **`/ic`** across 17 modular subsystems:

| Feature Domain | Root Slash Commands | Background Loop | External API Dependency | Offline Fallback Behavior |
|---|---|---|---|---|
| **Academics** | `/ic notes`, `/ic prof`, `/ic grades`, `/ic exam ...` | None | RateMyProfessors (GraphQL) | Server manual catalog links; empty error guidance |
| **Careers & Jobs** | `/ic jobs`, `/ic job_preferences`, `/ic job_preview`, `/ic resume` | Jobs Poller (`JOBS_POLL_SECONDS`) | Greenhouse, Lever, Workday | In-memory cached postings; offline resume rubric |
| **Virtual Economy** | `/ic dailyclaim`, `/ic profile`, `/ic leaderboard` | None | None (Internal SQLite Ledger) | Fully offline, atomic ACID transactions |
| **Casino Games** | `/ic casino blackjack/roulette/slots/payouts` | None | None (Pure Python Game Engines) | Fully offline math engines |
| **Fun-Stock Market** | `/ic icse buy/sell/short/cover/market/quote...` | Market Watch & Daily Report | Yahoo Finance / AlphaVantage | Internal catalog prices; simulated price moves |
| **Transit Alerts** | Background channel alerts | TTC & GO Pollers | Metrolinx Open Data, TTC Mastodon | Circuit breakers pause requests during outages |
| **Administration** | `/ic admin setup`, `/ic admin config`, `/ic admin features`, `/ic admin roles`, `/ic admin set`, `/ic admin move`, `/ic admin audit`, `/ic admin sync_catalog`, `/ic admin weekroll`, `/ic admin llm_start`, `/ic admin llm_stop` | None | None | Instant local configuration mutation |
| **Diagnostics** | `/ic botinfo`, `/ic serverinfo`, `/ic status`, `/ic help`, `/ic report`, `/ic getting-started` | None | None | Redacted system and storage status inspection |

<details>
<summary><b>Click to expand the complete 53 slash command inventory</b></summary>

| Subsystem | Slash Commands | Description |
|---|---|---|
| **Admin (11)** | `/ic admin setup`, `/ic admin config`, `/ic admin features`, `/ic admin roles`, `/ic admin set`, `/ic admin move`, `/ic admin audit`, `/ic admin sync_catalog`, `/ic admin weekroll`, `/ic admin llm_start`, `/ic admin llm_stop` | Server configuration, feature toggles, role management, audit logs, and AI killswitch |
| **Academics (9)** | `/ic notes`, `/ic prof`, `/ic grades`, `/ic exam add`, `/ic exam remove`, `/ic exam list`, `/ic exam clear`, `/ic exam export`, `/ic exam import` | Course study notes, RateMyProfessors ratings, grade distributions, and shared exam schedules |
| **Careers (5)** | `/ic jobs`, `/ic job_preferences`, `/ic job_preview`, `/ic job_subscribe`, `/ic resume` | ATS job matching (Greenhouse, Lever, Workday) and memory-only AI resume critique |
| **Economy & Casino (7)** | `/ic dailyclaim`, `/ic profile`, `/ic leaderboard`, `/ic casino blackjack`, `/ic casino roulette`, `/ic casino slots`, `/ic casino payouts` | Daily allowances, net worth leaderboards, and provably fair math engines |
| **Fun-Stock Market ICSE (10)** | `/ic icse market`, `/ic icse stock`, `/ic icse quote`, `/ic icse buy`, `/ic icse sell`, `/ic icse short`, `/ic icse cover`, `/ic icse portfolio`, `/ic icse trades`, `/ic icse history` | Virtual equity exchange with long/short trading and real-time market data |
| **Info & Diagnostics (6)** | `/ic botinfo`, `/ic serverinfo`, `/ic status`, `/ic help`, `/ic report`, `/ic getting-started` | System diagnostics, component readiness, redaction test, and onboarding guides |
| **Sports (5)** | `/ic sports nba`, `/ic sports nfl`, `/ic sports mma`, `/ic sports ufc`, `/ic sports boxing` | Upcoming game schedules, betting odds, and sports news headlines |

</details>

*(See full architecture details and sequence diagrams in [Architecture Documentation](docs/architecture.md)).*

---

## 4. Self-Hosted Source Release vs. Hosted Public Invite

| Consideration | Model A: Self-Hosted Community Bot (Recommended) | Model B: Maintainer-Hosted Public Invite |
|---|---|---|
| **Hosting Model** | Community hosts their own container (Docker / systemd) | Upstream maintainer hosts multi-tenant instance |
| **Data Ownership** | 100% data sovereignty; SQLite DB stays on your disk | Data stored on maintainer's central infrastructure |
| **Discord Verification** | Not required (<100 servers) | Required by Discord (>100 servers) |
| **Privileged Intents** | Not required for slash commands; Message Content optional | Strictly restricted by Discord review team |
| **API Costs & Quotas** | Self-hoster supplies their own free/low-tier API keys | Maintainer absorbs all external API expenses |

---

## 5. Quick Start (Safe Offline Default)

### 5.1 Prerequisites
- Python 3.11, 3.12, or 3.13
- [`uv`](https://docs.astral.sh/uv/) package manager

### 5.2 Local Setup
```bash
# 1. Clone repository
git clone https://github.com/your-username/campus-bot.git
cd campus-bot

# 2. Install locked development environment
uv sync --dev

# 3. Run the complete test suite offline (625+ tests)
uv run pytest

# 4. Run the zero-token offline smoke check
uv run python tools/smoke_offline.py

# 5. Run the staging lifecycle smoke check
uv run python tools/smoke_staging.py
```

### 5.3 Launching the Live Bot
By default, the bot **refuses to connect to Discord** to protect developers from accidental live launches. To launch:

```bash
# 1. Copy example configuration
cp .env.example .env
chmod 600 .env

# 2. Edit .env with your DISCORD_TOKEN and GUILD_ID
# 3. Launch with explicit gateway opt-in:
uv run python main.py --allow-gateway
```

*(For production Docker Compose and systemd deployment recipes, see [Production Hosting Guide](docs/production-hosting-and-recovery.md)).*

---

## 6. Module Management & Setup

Server administrators can configure modules directly inside Discord without touching code:
- **Interactive Setup Wizard:** Run **`/ic admin setup`** to configure channel assignments and community profiles (`default`, `engineering_heavy`, `student_core`).
- **Feature Toggling:** Run **`/ic admin features disable:casino`** to silence gambling commands in academic channels.
- **Diagnostics & Issue Reports:** Run **`/ic status issue_report:True`** to export a sanitized diagnostic file with all tokens redacted.

---

## 7. Documentation Index

- [System Architecture & Sequence Flows](docs/architecture.md)
- [Complete Configuration Reference](docs/configuration.md)
- [External Provider Specifications & Integration Guide](docs/providers.md)
- [Privacy Architecture, Data Flows & Deletion Model](docs/privacy.md)
- [Operations, Backups & Incident Runbook](docs/operations.md)
- [Production Hosting & Recovery Runbook](docs/production-hosting-and-recovery.md)
- [Usability Review & Archetype Audit Report](docs/usability_review.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy & Vulnerability Disclosure](SECURITY.md)
- [Changelog](CHANGELOG.md)

---

## 8. License & Attribution

- Bot source code is licensed under the [MIT License](LICENSE) (Copyright &copy; 2026 sukesan).
- "Instigate Cafe" brand identity and server logos are reserved by the Instigate Cafe community.
- Course notes study guides are authored by **Jasper Cruz** and **Krish Patel** (Copyright &copy; respective authors).
- Transit data provided under the Metrolinx Open Data Licence and public TTC Mastodon feeds.
