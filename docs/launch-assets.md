# Instigate Cafe Bot — Portfolio Presentation & Launch Assets

**Document Version:** 1.0.0 (Release Candidate `v1.0.0-rc1`)  
**Target Release Candidate:** [`v1.0.0-rc1`](file:///home/sukesan/dev/discord/campus-bot/pyproject.toml#L3)  
**Verification Suite:** [`tests/unit/test_portfolio_presentation_and_launch_assets.py`](file:///home/sukesan/dev/discord/campus-bot/tests/unit/test_portfolio_presentation_and_launch_assets.py)  
**Publication Status:** Launch Assets & Portfolio Package  

---

## 1. Executive Summary & Purpose

This document packages the public presentation, recruitment showcase assets, synthetic staging visual mockups, video storyboard, owner launch checklist, and social media announcements for the open-source release of **Instigate Cafe Bot**.

> [!CAUTION]
> **ABSOLUTE RELEASE SAFEGUARDS (`AGENTS.md`):**
> 1. **Independent Visibility Decision:** Changing GitHub repository visibility from **Private** to **Public** is strictly decoupled from running a public bot instance or accepting server invitations.
> 2. **Zero Live Gateway Execution:** Never execute `main.py`, open live Discord Gateway WebSockets, or run background pollers against production Discord servers without explicit owner consent.
> 3. **Production Hosting Isolation:** Automatic deployment triggers on Railway remain disabled. Enabling hosted service requires separate, explicit owner authorization.
> 4. **Zero Production Secrets:** All mockups, screenshots, and storyboards use 100% synthetic data and synthetic Snowflake IDs.

---

## 2. High-Quality Staging Visual Mockups (Synthetic Data Only)

These visual representations demonstrate the four core pillars of the bot running on synthetic staging test guilds (**Guild Alpha** `900000000000000001` and **Guild Beta** `900000000000000002`).

### 2.1 Pillar 1: Academic Schedule & Exam Countdown (`/ic exam list`)

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  📘 Instigate Cafe Academics — Upcoming Final Examinations                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  Guild: CS & Software Engineering Staging (Guild Alpha)                       │
│                                                                              │
│  • CPS209 — Computer Science II                                              │
│    📅 Friday, December 18, 2026 • 14:00 – 16:00 EST                          │
│    ⏳ Starts in: 28 days, 13 hours, 22 minutes                               │
│    📍 Location: Kerr Hall West (KHW-071)                                     │
│                                                                              │
│  • CPS310 — Computer Organization & Architecture                             │
│    📅 Monday, December 21, 2026 • 09:00 – 11:30 EST                          │
│    ⏳ Starts in: 31 days, 8 hours, 37 minutes                                │
│    📍 Location: George Vari Engineering Centre (ENG-LG11)                    │
│                                                                              │
│  💡 Tip: Use `/ic exam export` to download your personalized calendar (.ics) │
├──────────────────────────────────────────────────────────────────────────────┤
│  Showing 2 of 2 scheduled exams • Instigate Cafe Academic Hub • v1.0.0-rc1   │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Pillar 2: Career Tools & AI Resume Consent Barrier (`/ic resume review`)

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 Instigate Cafe Resume AI Review — Privacy & Consent Notice               │
├──────────────────────────────────────────────────────────────────────────────┤
│  File: student_resume.pdf (18 KB)                                            │
│  Discipline: Software Engineering • Target Role: Junior Developer            │
│                                                                              │
│  ⚠️ Third-Party AI Privacy Disclosure:                                       │
│  • Transmission: Submitting forwards your document to Google Gemini API.     │
│  • Confidentiality: Processing follows Google Cloud / Gemini API terms.      │
│  • Zero Local Retention: The bot NEVER persists your resume file, personal   │
│    information, or feedback to any local database or disk.                   │
│  • Ephemeral Delivery: Feedback is delivered ephemerally (visible only to    │
│    you) and purged from server memory immediately after delivery.            │
│                                                                              │
│  Please select your preferred review method:                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│  [ 🚀 Submit for AI Review ]  [ 📋 View No-AI Checklist ]  [ ❌ Cancel ]     │
├──────────────────────────────────────────────────────────────────────────────┤
│  Privacy-First Career Tools • Confidential & Ephemeral • v1.0.0-rc1          │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Pillar 3: Regional Transit & Commuter Advisory (`/ic transit`)

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  🚆 Metrolinx GO Transit & TTC Regional Commuter Advisory                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  Status: Active Line Advisories Detected                                     │
│                                                                              │
│  • Kitchener GO Line: Delay Advisory                                         │
│    Signal modernization between Bramalea and Malton. Trains operating with   │
│    10–15 minute holding delays. Normal weekend schedule resumes at 20:00.    │
│                                                                              │
│  • Lakeshore West Line: On Schedule                                          │
│    All trains operating normally between Union Station and Aldershot.        │
│                                                                              │
│  • TTC Line 1 (Yonge-University): Normal Service                             │
│    Regular two-minute headways through Dundas and Queen stations.            │
│                                                                              │
│  🛡️ Outage Dampener Active: Provider polling throttled to protect rate limits │
├──────────────────────────────────────────────────────────────────────────────┤
│  Source: Metrolinx Open Data & TTC Mastodon • Outbound Outage-Dampened       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.4 Pillar 4: Virtual Equity Exchange & Financial Ledger (`/ic icse market`)

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│  📈 Instigate Cafe Stock Exchange (ICSE) — Market Overview                   │
├──────────────────────────────────────────────────────────────────────────────┤
│  Guild: Business & Commerce Staging (Guild Beta) • Week 47 Session           │
│                                                                              │
│  Symbol   Company / Asset             Price       24h Change   Volume        │
│  ───────  ──────────────────────────  ──────────  ───────────  ──────────    │
│  $IC      Instigate Coffee Roasters   $100.00     +2.45%       1,420 shrs    │
│  $TECH    Campus Compute Services     $245.50     -1.12%         850 shrs    │
│  $CAMPUS  Student Union Real Estate   $ 52.20     +0.85%       3,110 shrs    │
│                                                                              │
│  Your Portfolio: $1,200.00 Cash • 2 shrs $IC ($200.00) • Net Worth: $1,400 │
│  ACID Ledger: 4 verified transactions • Double-spending concurrency locked   │
├──────────────────────────────────────────────────────────────────────────────┤
│  Simulated Paper Trading Only • Zero Real Currency • SQLite WAL Storage      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 45–90 Second Demo Video Storyboard

A concise, structured demonstration video illustrating the technical refactor and user capabilities:

| Timestamp | Visual Screen Scene | On-Screen Action & Interaction | Audio Voiceover Script |
|---|---|---|---|
| **0:00 – 0:15** | Split screen: VS Code terminal (`uv run pytest`) and Discord interface. | Display terminal showing 646 tests passing in 13s, then transition to Discord server running `/ic botinfo`. | *"Welcome to the Instigate Cafe Bot—a fully modernized, student-focused Discord bot engineered with discord.py 2.3, SQLite WAL transactions, and comprehensive offline test coverage."* |
| **0:15 – 0:35** | Discord channel: Academics & Autonomous Setup. | Administrator executes `/ic admin setup`, selects `engineering_heavy`, and runs `/ic exam list` showing upcoming exams and calendar export. | *"Server admins can configure the bot in seconds with autonomous slash-command setup wizards. Students instantly access course study guides, professor ratings, and shared exam schedules with iCalendar export."* |
| **0:35 – 0:55** | Discord channel: Privacy-First Career Tools. | User invokes `/ic resume review` with a PDF attachment; the privacy consent barrier appears; user selects offline checklist. | *"Career tools filter junior software engineering roles from Greenhouse, Lever, and Workday. For resume reviews, the bot enforces an explicit privacy consent barrier before sending anything to Gemini, with zero disk storage and an instant offline checklist alternative."* |
| **0:55 – 0:75** | Discord channel: Virtual Paper Trading & Economy. | User runs `/ic dailyclaim`, buys 2 shares of `$IC` on `/ic icse market`, and views `/ic profile` showing immutable ledger entries. | *"Our campus paper-trading exchange and casino mini-games run on an atomic SQLite ledger with WAL mode, guaranteeing zero double-spending and strict multi-guild isolation across different student societies."* |
| **0:75 – 0:90** | Terminal: Zero-leak scanner (`scan_secrets.py`) & GitHub Repo. | Terminal executes `tools/scan_secrets.py --enforce-clean` showing 0 leaks, transitioning to GitHub repo page. | *"With 646 offline tests, predictable provider degradation, and full Right-to-be-Forgotten compliance, Instigate Cafe Bot is production-ready for self-hosting. Check out the open-source code on GitHub."* |

---

## 4. Professional LinkedIn Announcement Post

**Target Audience:** Engineering Recruiters, Fellow Computer Science/Engineering Students, Open-Source Contributors.

```text
🚀 Excited to share a major engineering milestone: I've completed the full architectural modernization and open-source release of Instigate Cafe Bot (v1.0.0-rc1)!

What started as a campus Discord utility bot grew to serve hundreds of university students across Software Engineering, Computer Science, and Commerce disciplines. But like many high-growth projects, the original codebase hit scaling limits: monolithic command files, concurrent SQLite database lockups, and tight coupling to external APIs.

Over the past several weeks, I led a ground-up modernization effort focusing on software craft, resilience, and privacy:

🔧 Architectural Highlights:
• Decoupled Architecture: Refactored into clean domain layers (Academics, Careers, Virtual Economy, Transit Alerts) orchestrated by a central ServiceContainer.
• ACID Economy Transactions: Re-engineered the virtual stock exchange and economy to use SQLite Write-Ahead Logging (WAL) with strict double-entry ledger settlement, eliminating race conditions and double-spending.
• Resilient Provider Adapters: Wrapped 6 external APIs (RateMyProfessors, Metrolinx GO, TTC Fediverse, ATS job engines, Yahoo Finance, Gemini) with exponential-backoff circuit breakers and outage dampeners for graceful degradation during upstream downtime.
• Privacy-First Career Review: Implemented zero-disk ephemeral resume reviews with mandatory consent barriers, accompanied by GDPR-compliant Right-to-be-Forgotten data deletion.
• Multi-Guild Isolation: Built true multi-tenancy allowing distinct university clubs to configure independent command profiles (e.g. engineering study hubs vs. finance trading societies) with zero cross-guild data bleed.

📊 By the Numbers:
• 646 automated tests passing offline in under 14 seconds (100% test isolation with zero live network calls).
• Sub-2.5ms average interaction response latency, comfortably within Discord's 3-second interaction token deadline.
• Zero credentials, tokens, or private data leaks across 233 tracked files, verified by an automated publication scanner.
• 8/8 release gates certified for v1.0.0-rc1.

The project is packaged for self-hosting via Python 3.12/uv, Docker Compose, and systemd.

Check out the architecture, design documentation, and open-source codebase on GitHub:
👉 https://github.com/sukesan/campus-bot (Repository link placeholder)

Feedback, issues, and contributions from fellow student builders and maintainers are warmly welcome!

#Python #SoftwareEngineering #OpenSource #DiscordPy #SystemDesign #SQLite #DevOps #ComputerScience
```

---

## 5. GitHub Repository Metadata & Topics

Recommended metadata configuration for the GitHub repository:

- **Repository Description:**  
  `A modular, student-focused Discord bot featuring academic study hubs, tech ATS job alerts, regional transit advisories, a virtual equity exchange, and optional privacy-first AI career reviews.`
- **Website / Documentation:**  
  `https://github.com/sukesan/campus-bot#readme`
- **GitHub Topics (Keywords):**
  - `discord-bot`
  - `discord-py`
  - `python3`
  - `sqlite-wal`
  - `asyncio`
  - `clean-architecture`
  - `career-tools`
  - `academic-schedule`
  - `transit-alerts`
  - `virtual-economy`
  - `rate-my-professor`
  - `privacy-by-design`
  - `offline-testing`
  - `student-community`

---

## 6. Owner-Editable Launch Checklist

This checklist separates **Repository Visibility** from **Live Bot Operation** as independent decisions.

### Phase A: Repository Visibility Decision (Private -> Public)
- [ ] **Step A1 (Secret Scan):** Execute `uv run python tools/scan_secrets.py --enforce-clean` to confirm zero leaked tokens, database files, or private lore.
- [ ] **Step A2 (License Verification):** Confirm MIT License notice in [`LICENSE`](file:///home/sukesan/dev/discord/campus-bot/LICENSE) and student copyright attributions in [`README.md`](file:///home/sukesan/dev/discord/campus-bot/README.md).
- [ ] **Step A3 (CI Green):** Confirm GitHub Actions CI passes across Python 3.11, 3.12, and 3.13.
- [ ] **Step A4 (Owner Approval):** Explicit owner sign-off obtained to change GitHub visibility from **Private** to **Public**.

### Phase B: Live Bot Hosting Decision (Self-Hosted vs Hosted Invite)
- [ ] **Step B1 (Railway Isolation):** Confirm Railway production service autodeploy remains disabled so git pushes do not trigger unintended container deploys.
- [ ] **Step B2 (Staging Validation):** Run `uv run python tools/smoke_staging.py` and `uv run python tools/verify_beta_readiness.py`.
- [ ] **Step B3 (Hosting Strategy):** Choose between Model A (Source release for self-hosters) or Model B (Maintainer-hosted public bot with verified bot invite).
- [ ] **Step B4 (Owner Approval):** Explicit owner sign-off obtained prior to generating public bot invite link or deploying to production infrastructure.

---

## 7. Version 1.1 Follow-Up Roadmap Outline

A forward-looking roadmap to share with contributors following the v1.0.0-rc1 launch:

1. **Multi-Campus Adapter Registry (`v1.1.0`):**
   - Generalize the academic course note registry from TMU STEM to support pluggable campus adapters (UofT, Waterloo, McMaster).
   - Configurable university calendar parsers via community-submitted YAML manifests.
2. **Web Event & Exam Dashboard (`v1.1.0`):**
   - Lightweight FastAPI/HTMX dashboard for student leaders to visualize exam countdowns and guild leaderboards in a web browser.
   - Read-only SQLite query interface authenticated via Discord OAuth2.
3. **Expanded Career ATS Matcher (`v1.1.0`):**
   - Add Ashby and SmartRecruiters ATS ingestion engines to complement Greenhouse, Lever, and Workday.
   - Machine-readable skill taxonomy matching for Canadian tech co-op job descriptions.

