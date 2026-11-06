# Instigate Cafe Bot — Pre-Publication Checklist & Attribution Register

This document defines the strict pre-publication security, privacy, licensing, and attribution requirements that must be verified and approved prior to making the Instigate Cafe Bot repository public.

---

## 1. Absolute Release Safeguards

> [!CAUTION]
> **Railway Production Autodeploy Risk:**  
> The Railway production service previously redeployed automatically on GitHub branch pushes. Prior to any repository publication or history modification:
> 1. Verify that Railway automatic deployment triggers are completely disconnected or pointed to an isolated private branch.
> 2. Never push commits or tags to `origin/main` until explicit owner sign-off is documented.
> 3. Never start a live gateway connection (`python main.py`, `bot.run()`) from development or test environments.

---

## 2. Sensitive Artifact & Secret Scrubbing Verification

Before the repository is published, the following security gates must pass:

### 2.1 Tracked File De-coupling
The following historical artifacts have been removed from the repository on owner instruction (copies safely preserved in local scratch backup):
- [x] `ic_stock.db` (REMOVED — SQLite user balances, transaction ledger, and portfolio holdings)
- [x] `tmu_grades.db` (REMOVED — student grade distribution tables)
- [x] `tools/lore.tmu_files.json` (REMOVED — private server community lore and member nicknames)
- [x] `tools/scraper.py` (REMOVED — dead legacy scraper script)

### 2.2 Git History Inspection & Rewriting
- [x] **Owner Decision (Nov 5, 2026):** Git commit history scrubbing was reviewed and explicitly waived by the owner ("dont worry about the commit history scrubbing"). Modernization will proceed with current commit history.

### 2.3 Automated Secret Scanner Execution
Run the project's secret scanner in strict publication enforcement mode:
```bash
python3 tools/scan_secrets.py --enforce-clean
```
- [ ] Scanner must return exit code `0` with zero critical findings and zero tracked legacy artifacts.

### 2.4 Configuration Hygiene
Inspect `.env.example` to confirm:
- [ ] All secret variables (`DISCORD_TOKEN`, `GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`, `ALPHAVANTAGE_API_KEY`) are blank.
- [ ] All Discord snowflake IDs (`GUILD_ID`, `*_CHANNEL_ID`, `ALLOWED_ROLE_IDS`) are set to `0` or documented with placeholder comments.
- [ ] All optional background integrations (`LLM_ENABLED`, `MARKET_WATCH_ENABLED`, `MARKET_DAILY_REPORT_ENABLED`, `BTC_WATCH_ENABLED`, `JOBS_BOOTSTRAP_POST`, `TTC_BOOTSTRAP_POST_LATEST`) default to `false`.

---

## 3. Intellectual Property, Licensing & Attribution Register

The repository contains a mixture of original software code, community assets, and external integrations. The following legal boundaries must be preserved:

| Asset Category | Location in Repository | Applicable License / Terms | Usage Boundary & Restrictions |
| :--- | :--- | :--- | :--- |
| **Bot Source Code** | `icbot/`, `tools/`, `main.py` | **MIT License** (Copyright 2026 sukesan) | Permissive open-source use, modification, and distribution with copyright notice preservation. |
| **Brand Identity & Trademarks** | "Instigate Cafe", server logos, banners | **All Rights Reserved** (Instigate Cafe) | The MIT license does **not** grant rights to use the "Instigate Cafe" trademark, server branding, or visual assets for public or commercial bot clones. Clones should adopt independent branding. |
| **Community Lore** | `tools/lore.*.json` | **Private Community Property** | Real server member names, handles, inside jokes, and server events are private to the Instigate Cafe community. Only `tools/lore.example.json` may be published. |
| **Course Notes Hub** | `/ic notes` (`NOTES_SITES`) | **Student Authors' Copyright** | Course notes hosted on Notion were authored by **Jasper Cruz** and **Krish Patel**. The bot merely provides hyperlinks to external student study guides. The notes content is not licensed under MIT. |
| **Professor Ratings** | `/ic rmp` | **RateMyProfessors / Chewse Inc.** | Retrieved via unofficial RMP GraphQL/REST endpoint for non-commercial student informational lookup. Commercial exploitation or aggressive scraping is prohibited. |
| **Grade Distributions** | `/ic grades` (`tmu_grades.db`) | **Institutional Academic Data** | Historical grade statistics are private university records. The database file must never be published in open source. Only synthetic schema fixtures may be shipped. |
| **GO Transit Feeds** | `icbot/tasks/go_alerts.py` | **Metrolinx Open Data Licence** | Accessed via the official [Metrolinx Open Data API](https://www.metrolinx.com/en/about-us/open-data). Requires compliance with Metrolinx attribution requirements. |
| **TTC Alerts** | `icbot/tasks/ttc_alerts.py` | **Public Fediverse / Mastodon** | Aggregated from public Toronto Transit Commission Mastodon feeds for real-time commuter advisory. |
| **Market Data** | `icbot/services/yahoo_quotes.py` | **Yahoo Finance Terms of Service** | Informational and virtual economy simulation only. Explicit disclaimers must state that the bot does not provide financial or investment advice. |

---

## 4. Pre-Publication Verification Commands

Execute the following offline verification suite before initiating publication:

```bash
# 1. Verify tracked files with secret scanner
python3 tools/scan_secrets.py --enforce-clean

# 2. Verify all Python files compile cleanly without syntax errors
python3 -c "
import ast, pathlib
for p in pathlib.Path('.').rglob('*.py'):
    if '.git' in p.parts: continue
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
print('AST check: ALL PYTHON FILES COMPILE CLEANLY')
"

# 3. Verify clean Settings instantiation with default/empty environment
python3 -c "
from icbot.config import Settings
s = Settings.from_env()
assert s.token == '', 'Token should be empty'
assert s.llm_enabled is False, 'LLM should be disabled'
assert s.market_watch_enabled is False, 'Market watch should be disabled'
assert s.lore_path == '', 'Lore path should be empty string'
print('Settings check: CLEAN BOOT VERIFIED')
"

# 4. Verify lore loading falls back safely to lore.example.json
python3 -c "
import tools.lore_loader as ll
lore = ll.load_lore()
assert len(lore) > 0, 'Lore should load entries from candidate paths'
print(f'Lore check: Successfully loaded {len(lore)} lore entries')
"
```

---

## 5. Owner Review & Sign-Off Register

The following sign-off checklist must be reviewed and explicitly approved by the repository owner (`sukesan`) prior to public repository launch:

- [ ] **Sign-off 1 (Railway Isolation):** Confirmation that Railway production bot does not trigger deployments from public GitHub commits.
- [x] **Sign-off 2 (Database Scrubbing):** Confirmation that `ic_stock.db` and `tmu_grades.db` are removed from the repository. *(Approved by owner Nov 5, 2026)*
- [x] **Sign-off 3 (Lore Scrubbing):** Confirmation that private member references in `tools/lore.tmu_files.json` are removed from the repository. *(Approved by owner Nov 5, 2026)*
- [ ] **Sign-off 4 (External Content Disclaimer):** Approval of the copyright notice and external attribution disclaimers in `README.md`.
- [x] **Sign-off 5 (History Review):** Git commit history scrubbing evaluated and waived by owner ("dont worry about the commit history scrubbing").
- [ ] **Sign-off 6 (Public Visibility Change):** Final authorization to change GitHub repository visibility from **Private** to **Public**.

