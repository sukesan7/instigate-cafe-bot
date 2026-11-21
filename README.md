# Instigate Cafe Bot

[![Version v1.0.0-rc1](https://img.shields.io/badge/version-v1.0.0--rc1-blue.svg)](pyproject.toml)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![discord.py 2.3+](https://img.shields.io/badge/discord.py-2.3%2B%20app__commands-5865F2)](https://github.com/Rapptz/discord.py)
[![Storage SQLite WAL](https://img.shields.io/badge/Storage-SQLite%20WAL-lightgrey)](https://www.sqlite.org/)
[![Offline Test Suite](https://img.shields.io/badge/Tests-Offline%20Pytest%20Suite-blue)](tests/)
[![Offline Quality Gate](https://github.com/sukesan7/instigate-cafe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sukesan7/instigate-cafe-bot/actions/workflows/ci.yml)
[![License MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A modular, student-focused Discord bot combining academic study tools, tech job alerts, regional transit advisories, a virtual campus economy, and optional privacy-first AI resume feedback. Built with clean architecture, strict multi-guild tenancy, and atomic SQLite Write-Ahead Logging (WAL) transactions.

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
  - **Not an automated compliance tool:** While engineered for data minimization and operator-run Right-to-be-Forgotten deletion/anonymization, hosting operators remain responsible for their community's legal privacy policies, host disk encryption, and upstream third-party API agreements.
  - **Not a persistent chat logger:** The application does not record or store channel chat messages to local disk or SQLite, and **no automatic channel-history collection occurs by default** (`LLM_CHANNEL_HISTORY_ENABLED=false`). Message content is processed only when explicitly invoked via slash commands or, when the operator enables `LLM_ENABLED=true`, via direct `@mentions` (which read surrounding channel context in memory only if the operator also explicitly opts into `LLM_CHANNEL_HISTORY_ENABLED=true`).

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
| **Careers (5)** | `/ic jobs`, `/ic job_preferences`, `/ic job_preview`, `/ic job_subscribe`, `/ic resume` | ATS job matching (Greenhouse, Lever, Workday) and memory-only AI resume critique (not stored on local bot disk/SQLite; Discord and Gemini API terms apply) |
| **Economy & Casino (7)** | `/ic dailyclaim`, `/ic profile`, `/ic leaderboard`, `/ic casino blackjack`, `/ic casino roulette`, `/ic casino slots`, `/ic casino payouts` | Daily allowances, net worth leaderboards, and unit-tested PRNG game engines with deterministic payout tables |
| **Fun-Stock Market ICSE (10)** | `/ic icse market`, `/ic icse stock`, `/ic icse quote`, `/ic icse buy`, `/ic icse sell`, `/ic icse short`, `/ic icse cover`, `/ic icse portfolio`, `/ic icse trades`, `/ic icse history` | Virtual equity exchange with long/short trading and real-time market data |
| **Info & Diagnostics (6)** | `/ic botinfo`, `/ic serverinfo`, `/ic status`, `/ic help`, `/ic report`, `/ic getting-started` | System diagnostics, component readiness, redaction test, and onboarding guides |
| **Sports (5)** | `/ic sports nba`, `/ic sports nfl`, `/ic sports mma`, `/ic sports ufc`, `/ic sports boxing` | Upcoming game schedules, betting odds, and sports news headlines |

</details>

*(See full architecture details and sequence diagrams in [Architecture Documentation](docs/architecture.md)).*

### 3.1 Honest Provider Comparison & Operating Limits

To prevent unexpected hosting surprises or hidden subscription dependencies, all external provider boundaries, rate limits, and fallback mechanisms are documented honestly:

| Provider Domain | External Integration | Upstream Limits & Cost Boundaries | Local Fallback & Degradation Invariant |
|---|---|---|---|
| **RateMyProfessors** | Unofficial GraphQL API | Undocumented upstream rate limits; free access. | In-memory query caching; user guidance with direct web lookup links. |
| **Metrolinx GO** | Official Open Data REST API | Requires free Metrolinx API key; rate limits apply. | Exponential-backoff outage dampener; suppresses duplicate alerts. |
| **TTC Alerts** | Public Fediverse / Mastodon | Public RSS/REST polling; no API key required. | Circuit breaker pauses on connection failure; zero crash loops. |
| **Career ATS Feeds** | Public RSS & JSON Feeds (Greenhouse, Lever, Workday) | Scraping frequency bounded by poller interval (default 30m). | In-memory cache preserved; stale listings served with timestamp warnings. |
| **Google Gemini AI** | Google AI Studio REST API | Free tier: 15 RPM / 1M TPM; paid tier pay-as-you-go. | Mandatory opt-in barrier (`ResumeConfirmView`); offline checklist fallback. |
| **Market Quotes** | Yahoo Finance / AlphaVantage | Yahoo rate limits scrapers; AlphaVantage free key (25 req/day). | Simulated price movement generator; internal exchange catalog keeps trading active. |
| **SQLite WAL Storage** | Local filesystem (`sqlite3`) | Single-writer concurrency lock; zero cloud storage fee. | Immediate ACID write transactions; double-entry ledger prevents balance drift. |

### 3.2 Staging Visual Showcase (Synthetic Guilds)

The bot has been verified offline against synthetic staging test guilds (**Guild Alpha** `900000000000000001` and **Guild Beta** `900000000000000002`). See [`docs/features.md`](docs/features.md) and [`docs/ui-style.md`](docs/ui-style.md) for the complete command catalog and rendered embed snapshots across themes:

- **Pillar 1 — Academic Hub & Exam Countdown:** Autonomous guild setup (`/ic admin setup`) and shared exam countdowns with iCalendar (`.ics`) export.
- **Pillar 2 — Career Tools & AI Resume Consent:** Ephemeral resume critiques with mandatory third-party AI consent disclosure and no-AI offline rubric.
- **Pillar 3 — Transit & Regional Commuter Alerts:** Metrolinx GO transit line delays and TTC transit advisories with circuit breaker dampening.
- **Pillar 4 — Virtual Stock Exchange (ICSE) & Economy:** Atomic double-entry paper trading with strict multi-guild ledger isolation.

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
git clone https://github.com/sukesan7/instigate-cafe-bot.git
cd instigate-cafe-bot

# 2. Install locked development environment
uv sync --dev

# 3. Run the complete test suite offline
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

*(For production Docker Compose, `systemd`, and Railway deployment recipes, see [Deployment, Hosting & Disaster Recovery](docs/deployment.md)).*

---

## 6. Module Management & Setup

Server administrators can configure modules directly inside Discord without touching code:
- **Interactive Setup Wizard:** Run **`/ic admin setup`** to configure channel assignments and community profiles (`default`, `student_core`, `toronto_tmu`, `community_full`).
- **Feature Toggling:** Run **`/ic admin features disable:casino`** to silence gambling commands in academic channels.
- **Diagnostics & Issue Reports:** Run **`/ic status issue_report:True`** to export a sanitized diagnostic file with all tokens redacted.

---

## 7. Documentation Index

Start at the **[Documentation Index](docs/README.md)**, or jump directly to a guide:

- [Features & 53-Command Guide](docs/features.md)
- [Complete Configuration Reference](docs/configuration.md)
- [Deployment, Hosting & Disaster Recovery](docs/deployment.md)
- [Troubleshooting & Incident Runbook](docs/troubleshooting.md)
- [System Architecture & Developer Reference](docs/architecture.md)
- [External Provider Specifications & Campus Guide](docs/providers.md)
- [Privacy Architecture, Data Map & Deletion Model](docs/privacy.md)
- [Discord UI & Visual Style Contract](docs/ui-style.md)
- [Development & Tooling Guide](docs/development.md)
- [Offline Testing & Invariants Guide](docs/testing.md)
- [Release Claims & Verification Evidence Matrix](docs/release-evidence.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy & Vulnerability Disclosure](SECURITY.md)
- [Changelog](CHANGELOG.md)

---

## 8. License & Attribution

- Bot source code is licensed under the [MIT License](LICENSE) (Copyright &copy; 2026 sukesan).
- "Instigate Cafe" brand identity and server logos are reserved by the Instigate Cafe community.
- Course notes study guides are authored by **Jasper Cruz** and **Krish Patel** (Copyright &copy; respective authors).
- Transit data provided under the Metrolinx Open Data Licence and public TTC Mastodon feeds.

---

## 9. GitHub Topics & Discovery Tags

`discord-bot` • `discord-py` • `python3` • `sqlite-wal` • `asyncio` • `clean-architecture` • `career-tools` • `academic-schedule` • `transit-alerts` • `virtual-economy` • `rate-my-professor` • `privacy-by-design` • `offline-testing` • `student-community`
