# Instigate Cafe Bot — External Provider Specifications & Integration Guide

**Release:** `v1.0.0-rc1`  
**Architecture Contract:** [`icbot/academics/providers.py`](../icbot/academics/providers.py) & [`icbot/services/http_transport.py`](../icbot/services/http_transport.py)  
**Security Invariants:** Strict SSRF URL validation, circuit breaker dampening, exponential backoff with jitter, zero credential leakage in logs, and deterministic offline mocking.

---

## 1. Overview, Campus Scope & Provenance Standards

The Instigate Cafe Bot integrates with external web services for academics, job feeds, regional transit alerts, market snapshots, sports headlines, and optional generative AI.

1. **Decoupled Contracts:** External calls implement `ProviderProtocol` or `AcademicProviderProtocol`, returning structured `ProviderResult` containers (`ok`, `empty`, `error`, `circuit_open`).
2. **Circuit Breakers & Outage Dampening:** Repeated upstream errors open `ProviderCircuitBreaker` (fast-fail cooldown) and dampen log noise via `ProviderOutageTracker`.
3. **SSRF Defense:** External academic URLs pass host/scheme/private-IP validation in [`icbot/academics/url_safety.py`](../icbot/academics/url_safety.py) (`validate_fetch_url`), bounded by `ACADEMIC_ALLOWED_HOSTS` and `NOTES_SITES`.
4. **Honest Campus & Regional Scope:**
   - **TMU / Toronto-specific providers:** `/ic grades` (`tmu.grades.zone`), default `/ic notes` (`NOTES_SITES` Notion/SPA study guides), default `RMP_UNIVERSITY` (`Toronto Metropolitan University`), Metrolinx GO Transit, and TTC Mastodon alerts.
   - **Institution-agnostic providers:** Greenhouse, Lever, Workday ATS, Google Gemini AI, Yahoo Finance, Alternative.me Fear & Greed, Blockstream Esplora, ESPN RSS, and The Odds API.
   - Other universities can override `RMP_UNIVERSITY`, `CAMPUS_ID`, `ACADEMIC_COURSE_ALIASES`, and `NOTES_SITES` or contribute a dedicated adapter (Section 4).

---

## 2. Supported External Provider Directory

| Provider | Scope | Data Domain | Authentication (Required vs. Optional) | Quota & Rate Limits | Offline Fallback |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Google Gemini API** | Generic | AI resume review & `@mention` assistant | **Optional key** (`GEMINI_API_KEY` + `LLM_ENABLED=true`) | Free tier 15 RPM / 1M TPM; paid tier pay-as-you-go; local cooldowns & token caps | `/ic resume` offers offline **No-AI Checklist**; `@mention` disabled when unconfigured |
| **RateMyProfessors** | Configurable (**TMU default**) | Professor ratings & difficulty (`/ic prof`) | None (Public GraphQL) | Undocumented upstream limits; 6-hour in-memory cache | Returns degraded notice with manual web lookup guidance |
| **TMU Grades (`tmu.grades.zone`)** | **TMU-specific** | Course grade histograms (`/ic grades`) | None (Public HTML) | Cached locally in SQLite (`grades_course_cache`) | Serves cached data or accessible text summary fallback |
| **Campus Notes (`NOTES_SITES`)** | **TMU default** (configurable) | Student study guides (`/ic notes`) | None (Public SPA/Notion links) | 6-hour in-memory cache + per-guild SQLite catalog | Serves guild manual catalog (`academic_resources`) |
| **Metrolinx Open Data** | **Toronto / GTA (TMU)** | GO Transit rail & bus advisories | **Optional key** (`GO_API_KEY` — free registration) | Metrolinx API rate limits; polled every `GO_POLL_SECONDS` (default `180s`) | Circuit breaker opens; suppresses duplicate/noisy alerts |
| **TTC Mastodon** | **Toronto (TMU)** | TTC subway, streetcar & bus alerts | None (Public Mastodon REST) | 300 req / 5 min; polled every `TTC_POLL_SECONDS` (default `120s`) | Jittered exponential backoff; zero crash loops |
| **Greenhouse / Lever ATS** | Generic | Junior tech internship & new-grad jobs | None (Public JSON APIs) | Polled every `JOBS_POLL_SECONDS` (default `600s`; `0` disables) | Serves cached SQLite postings (`job_seen`) with timestamp |
| **Workday CX** | Generic | Enterprise job postings | None (Public CXS REST) | Polled every `WORKDAY_POLL_SECONDS` (`0` disables by default) | Permanent 404/config errors quarantined in `SourceHealth` |
| **Yahoo Finance / Fear & Greed** | Generic | Market watch & `/ic report` snapshots | None (Public HTTP) | 10-minute cache TTL; upstream scraper limits apply | Falls back to internal ICSE catalog prices |
| **Blockstream Esplora** | Generic | Optional Bitcoin address watcher | None (Public REST) | Polled every `BTC_WATCH_POLL_SECONDS` (default `300s`, disabled by default) | Skips iteration cleanly on timeout |
| **The Odds API & ESPN RSS** | Generic | `/ic sports` schedules, odds & news | **Optional key** (`ODDS_API_KEY`; free tier 500 calls/mo); RSS needs none | Free-tier monthly quota on Odds API; cached RSS | Renders ESPN RSS headlines without odds when key is omitted |

---

## 3. Legal Cautions, Terms of Service & Attribution

1. **Transit Data Attribution:**
   - Metrolinx data is accessed under the [Metrolinx Open Data Licence](https://www.metrolinx.com/en/about-us/open-data).
   - TTC advisories are derived from public Mastodon posts authored by the Toronto Transit Commission.
2. **Academic Notes Copyright:**
   - Course notes hosted on Notion were compiled and authored by student community members (**Jasper Cruz** and **Krish Patel**). The bot indexes links to these study materials but does not claim ownership or license their content under MIT.
3. **Financial Disclaimer:**
   - Virtual stock trading (`/ic icse`) and market snapshots are purely educational simulations and fun-market games. They do not constitute financial advice, and actual trades or financial transfers cannot be executed through the bot.

---

## 4. Contributor Guide: Adding a New Campus Provider

Contributors can add academic or campus resource providers for new universities without accessing production credentials or real student records:

### Step 1: Implement the Academic Provider Interface
Create a new provider module in `icbot/academics/providers/` implementing `AcademicProviderProtocol`:

```python
from __future__ import annotations
import aiohttp
from icbot.academics.models import Campus, ProviderResult, ResourceLink

class UniversityNotesProvider:
    name: str = "uwaterloo_cs"
    campus: Campus = Campus(id="uwaterloo", name="University of Waterloo")

    async def search_notes(
        self,
        course_code: str,
        *,
        session: aiohttp.ClientSession,
    ) -> ProviderResult[list[ResourceLink]]:
        # 1. Strict input normalization
        norm = course_code.strip().upper()
        
        # 2. Query endpoint using bounded timeout
        # 3. Return structured ProviderResult
        ...
```

### Step 2: Enforce URL Safety & SSRF Whitelisting
Before returning any hyperlinks to the user or querying external endpoints, validate them using `validate_fetch_url`:

```python
from icbot.academics.url_safety import validate_fetch_url

val = validate_fetch_url(raw_url)
if not val.ok:
    return ProviderResult.empty(reason="URL rejected by SSRF guard")
```

### Step 3: Write Offline Unit Tests with Synthetic Fixtures
Create a test file in `tests/unit/` using `AiohttpMockRouter` and sanitized local fixtures (never real student data or private server lore):

```python
import pytest
from tests.helpers import AiohttpMockRouter

pytestmark = pytest.mark.provider_contract

async def test_new_provider_offline_query():
    router = AiohttpMockRouter()
    router.add_route(
        "GET",
        lambda url: "university.example.edu" in url,
        status=200,
        json_data={"course": "CS135", "links": [{"title": "Lecture Notes", "url": "https://example.edu/cs135.pdf"}]},
    )
    # Execute query and assert structured results
```

### Step 4: Register in Academic Registry & Run CI
Register your provider in `icbot/container.py` under `AcademicRegistry`, and verify that all offline checks pass:
```bash
uv run pytest -m provider_contract -v
uv run python tools/scan_secrets.py --enforce-clean
```

