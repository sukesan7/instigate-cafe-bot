<p align="left">
  <img src="assets/ic_banner.png" alt="Instigate Cafe Bot Banner"/>
</p>

# Instigate Cafe Bot

[![Version v1.0.0](https://img.shields.io/badge/version-v1.0.0-2b6cb0)](pyproject.toml)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776ab)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-2f855a)](LICENSE)
[![Quality & Security Gate](https://github.com/sukesan7/instigate-cafe-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sukesan7/instigate-cafe-bot/actions/workflows/ci.yml)

A self-hostable Discord slash-command bot (`/ic`) originally built for a student server and modernized into a configurable open-source application—combining exam calendars, course resource links, early-career job feeds, optional consent-based AI resume review, regional transit/market alerts, and a simulated virtual community economy backed by SQLite (WAL mode).

---

## Overview

Instigate Cafe Bot is distributed strictly as self-hostable source code—there is no hosted public bot invite, managed hosting platform, or commercial support operation. Every bot command lives under `/ic` (53 commands across 17 feature modules), paired with optional background alert pollers coordinated by a single `TaskSupervisor`.

All optional integrations and background watchers default to disabled, so you only enable what your server needs. Only `DISCORD_TOKEN` is required to bring a self-hosted bot online; optional provider keys (`GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`) are unused unless configured.

---

## Features

| Area | What It Does | Optional / Scope Notes |
| :--- | :--- | :--- |
| **Academics** | Shared server exam calendar with `.ics`/CSV export, RateMyProfessors ratings, course study-guide links, and grade distributions. | `/ic exam` works for any school. Default `/ic notes`, `/ic grades`, and default `RMP_UNIVERSITY` target **TMU** (see [Scope & Limitations](#scope-and-limitations)). |
| **Careers** | Searches cached Greenhouse, Lever, and Workday intern/new-grad postings, saves personal filter preferences, and posts guild job alerts. | No API key required; feeds are polled only when board lists, channel ID, and poll interval are configured. |
| **AI Resume Assistance** | Ephemeral PDF/image resume critique with a pre-transmission consent prompt (`ResumeConfirmView`) and an offline No-AI checklist. | **Optional & off by default.** Requires both `LLM_ENABLED=true` and `GEMINI_API_KEY` (a key alone never enables AI). Processed in RAM with no local disk storage; Discord CDN and Gemini API terms apply. |
| **Community & Economy** | Guild-scoped virtual tokens, daily allowance, net-worth leaderboard, simulated ICSE virtual stock exchange, and unit-tested PRNG casino games (Blackjack, Roulette, Slots). | Runs locally on SQLite WAL. **Simulated community entertainment only—no real-money wagering or financial advice.** |
| **Transit & Market Feeds** | Optional background alerts for GO Transit and TTC delays, Yahoo Finance market snapshots/volatility alerts, Bitcoin address watcher, and sports schedules/headlines. | **Optional & region/source-dependent.** GO (`GO_API_KEY`) and TTC Mastodon cover Toronto/GTA; sports odds use optional `ODDS_API_KEY`. |
| **Administration** | Guided `/ic admin setup` wizard, per-guild feature profiles/toggles, role delegation, config audit log, redacted `/ic status` reports, and `/ic admin llm_stop`. | Requires Discord `Manage Server` / `Administrator` or configured `ALLOWED_ROLE_IDS`. |

---

## Gallery

Real Discord captures using the default `instigate_cafe` theme:

<table>
  <tr>
    <td width="50%" align="center" valign="top">
      <img src="assets/screenshots/01-getting-started.png" alt="Discord embed showing the Instigate Cafe Bot /ic getting-started Step 1 of 4 onboarding walkthrough with Previous Step and Next Step buttons" width="440"/><br/>
      <sub><b>Onboarding Walkthrough</b> — <code>/ic getting-started</code> interactive 4-step guide</sub>
    </td>
    <td width="50%" align="center" valign="top">
      <img src="assets/screenshots/02-notes.png" alt="Discord embed showing /ic notes course study-guide results for MEC511 and COE528 with source attribution and live/cached freshness labels" width="440"/><br/>
      <sub><b>Academic Course Notes</b> — <code>/ic notes</code> study links with live/cached freshness</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <img src="assets/screenshots/03-jobs.png" alt="Discord embed showing the paginated /ic jobs Campus Job Board with employer locations, career levels, ATS sources, HTTPS apply links, and pagination buttons" width="440"/><br/>
      <sub><b>Career Job Board</b> — <code>/ic jobs</code> paginated ATS listings with HTTPS apply links</sub>
    </td>
    <td width="50%" align="center" valign="top">
      <img src="assets/screenshots/04-icse-market.png" alt="Discord embed displaying the virtual Instigate Cafe Stock Exchange (/ic icse market) with synthetic ticker prices, weekly baselines, and simulated-points disclaimer" width="440"/><br/>
      <sub><b>Simulated Stock Exchange</b> — <code>/ic icse market</code> virtual campus points</sub>
    </td>
  </tr>
</table>

Additional screenshots (`/ic help`, `/ic casino blackjack`, `/ic sports boxing`): **[Full Screenshot Gallery](docs/gallery.md)**

---

## Representative Commands

All commands live under `/ic`. A few common starting points:

| Command | Purpose |
| :--- | :--- |
| `/ic getting-started` | Interactive 4-step walkthrough for new members. |
| `/ic help` | Dynamic command directory filtered to modules enabled in your server. |
| `/ic exam list` | Upcoming shared guild exams (private exams automatically reply ephemerally). |
| `/ic notes` | Course study-guide lookup with alias normalization and validated HTTPS links. |
| `/ic jobs` | Search cached early-career ATS job postings with HTTPS-only apply links. |
| `/ic resume` | Ephemeral AI resume review (with explicit consent) or offline self-review checklist. |
| `/ic icse market` | View active listings and weekly changes on the simulated campus stock exchange. |
| `/ic admin setup` | Guided server setup for channels, campus preset, and enabled feature modules. |
| `/ic status` | Inspect module readiness, SQLite Schema v7 WAL health, and redacted issue reports. |

See [Features & Command Guide](docs/features.md) for all 53 slash commands and options.

---

## Self-Hosted vs. Hosted Public Bot

- **Self-Hosted Open-Source Release (Intended Use):** Clone this repository, register your own Discord Application, provide any optional API keys you choose, and store your SQLite database (`DB_PATH`) on your own machine or server. No Discord `>100`-server verification is needed, and you stay in full control of your community's data.
- **Publicly Inviteable Hosted Bot (Not Operated Here):** This repository does not provide an official shared bot invite, managed hosting service, or commercial support.

See [Deployment & Recovery Guide](docs/deployment.md) for Docker Compose, `systemd`, and Railway setup.

---

## Scope and Limitations

- **TMU & Toronto defaults:** Out-of-the-box study guides (`/ic notes`), grade distributions (`/ic grades` via `tmu.grades.zone`), default `RMP_UNIVERSITY` (`Toronto Metropolitan University`), and transit pollers (Metrolinx GO, TTC Mastodon) target TMU and the Greater Toronto Area. Other schools can override `RMP_UNIVERSITY`, `CAMPUS_ID`, `EXAMS_TZ`, and `NOTES_SITES` or add a custom adapter (see [External Providers](docs/providers.md)).
- **Privacy & storage boundaries:** Persistent data is stored in local plaintext SQLite (`DB_PATH`); host-level disk encryption is the operator's responsibility. Personal chat messages are never saved to SQLite, and **no automatic channel-history collection occurs by default** (`LLM_CHANNEL_HISTORY_ENABLED=false`). Resume files are processed in RAM without touching local disk, though attachments and opt-in AI prompts transit Discord CDN and Google Gemini under their respective terms.
- **Single active replica:** SQLite WAL and background pollers require `replicas: 1` per database.
- **Simulated community games & market:** Casino games use standard Python PRNG (`unit-tested PRNG game engines`), not a cryptographic commit-reveal protocol. ICSE trading and market alerts are purely virtual community features—never financial advice or real-money wagering.

---

## Quick Start (Self-Hosting)

Requires **Python 3.11–3.13** (`requires-python = ">=3.11,<3.14"`; **Python 3.12** is recommended). On rolling Linux distributions where system `python3` is 3.14+, use [`uv`](https://docs.astral.sh/uv/) to provision Python 3.12 automatically.

### 1. Install Runtime Dependencies

Recommended using `uv`:

```bash
git clone https://github.com/sukesan7/instigate-cafe-bot.git
cd instigate-cafe-bot

uv venv --python 3.12
uv sync --frozen --no-dev
```

*Alternative without `uv` (standard Python 3.11–3.13 + `pip`):*

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Optional hash-verified install:
# .venv/bin/pip install --require-hashes -r requirements.lock
```

### 2. Discord Developer Portal Setup

1. Create an Application and Bot in the [Discord Developer Portal](https://discord.com/developers/applications) and copy its **Bot Token**.
2. Open **OAuth2 → URL Generator**, select scopes **`bot`** and **`applications.commands`**, and choose permission integer **`277025507392`** (`Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`, `Use Slash Commands`, `Add Reactions`).
3. Leave all **Privileged Gateway Intents OFF** by default (`Server Members`, `Presence`, and `Message Content` are unnecessary for `/ic` slash commands, `/ic resume`, and direct `@mention` replies). Enable **Message Content Intent** later *only* if you deliberately set both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true`.

### 3. Configure and Run

Create a private `.env` file from [`.env.example`](.env.example). By default, optional watchers (`LLM_ENABLED=false`, `MARKET_WATCH_ENABLED=false`, `MARKET_DAILY_REPORT_ENABLED=false`, `BTC_WATCH_ENABLED=false`, `JOB_ALERTS_CHANNEL_ID=0`, `WORKDAY_SITES=`) remain disabled or unconfigured, and `WORKDAY_POLL_SECONDS=0` inherits `JOBS_POLL_SECONDS` (`600` default) only once `WORKDAY_SITES` and `JOB_ALERTS_CHANNEL_ID` are set.

**Important startup safeguard:** `python main.py` refuses to connect to Discord by default (`ICBOT_ALLOW_GATEWAY=false`) so importing modules or verifying config never opens a live bot connection by accident. Pass **`--allow-gateway`** (or set `ICBOT_ALLOW_GATEWAY=true`) when you intentionally launch the bot:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set at minimum DISCORD_TOKEN (plus optional GUILD_ID for instant slash sync)

uv run python main.py --allow-gateway
# Or, using the standard pip virtual environment:
# .venv/bin/python main.py --allow-gateway
```

Once your bot is online in Discord, run **`/ic admin setup`** to choose alert channels, campus presets, and enabled modules.

*(Looking to run tests, linting, or contribute code? See [CONTRIBUTING.md](CONTRIBUTING.md) and [Development & Testing Guide](docs/development.md) for `uv sync --frozen --dev` setup.)*

---

## Documentation

- [Documentation Index](docs/README.md) — Guide to user, operator, and contributor docs
- [Screenshot Gallery](docs/gallery.md) — Additional Discord UI captures
- [Features & 53-Command Guide](docs/features.md) — Complete `/ic` slash-command reference
- [Configuration Reference](docs/configuration.md) — Environment variables and per-guild overrides
- [Deployment, Troubleshooting & Recovery](docs/deployment.md) — Docker Compose, `systemd`, Railway, diagnostics, and SQLite WAL backups
- [Privacy & Data Model](docs/privacy.md) — Data flows, ephemeral resume handling, and user export/erasure
- [Security Policy](SECURITY.md) — Vulnerability disclosure and supported versions
- [External Providers & Campus Extensions](docs/providers.md) — Provider limits, SSRF rules, and custom university adapters
- [System Architecture](docs/architecture.md) — Service container, task supervision, SQLite migrations, and sequence flows
- [Contributing](CONTRIBUTING.md) & [Development & Testing Guide](docs/development.md) — Contributor setup, offline tests, lint/type checks, and UI conventions
- [Issues & Feature Requests](https://github.com/sukesan7/instigate-cafe-bot/issues) — Bug reports and proposals

---

## License & Attribution

- Source code is licensed under the [MIT License](LICENSE) (Copyright &copy; 2026 sukesan).
- "Instigate Cafe" branding and `assets/ic_logo.png` are reserved by the Instigate Cafe community.
- External course notes remain the copyright of their student authors; transit data follows the [Metrolinx Open Data Licence](https://www.metrolinx.com/en/about-us/open-data) and public TTC Mastodon feeds.
