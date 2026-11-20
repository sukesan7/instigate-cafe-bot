# Instigate Cafe Bot — External Provider Specifications & Integration Guide

**Document Version:** 2.0.0 (Phase 12 Provider Specification)  
**Architecture Contract:** [`icbot/services/provider_protocol.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/services/provider_protocol.py)  
**Security Invariants:** Strict SSRF URL validation, circuit breaker dampening, exponential backoff with jitter, zero credential leakage in logs, and deterministic offline mocking.

---

## 1. Overview & Provenance Standards

The Instigate Cafe Bot integrates with external web services for transit alerts, academic information, job feeds, and optional generative AI. To protect stability and community privacy, all external integrations adhere to these rules:

1. **Decoupled Contracts:** External calls implement either `ProviderProtocol` or `AcademicProviderProtocol`, returning structured `ProviderResult` containers with standardized status codes (`ok`, `empty`, `error`, `circuit_open`).
2. **Circuit Breakers & Dampening:** Upstream provider outages trigger circuit breakers that pause requests and dampen log noise via `ProviderOutageTracker` (1st failure at WARNING, repeated failures at DEBUG).
3. **SSRF Defense:** All external URLs fetched by academic tools must pass host whitelist and protocol validation in [`icbot/academics/url_safety.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/academics/url_safety.py).
4. **Honest Scope Notice:** Default course notes and academic tools currently focus on Toronto Metropolitan University (TMU / Ryerson) STEM curricula. The bot does **not** claim automated coverage for all North American universities out of the box, but provides a clean extension interface for other institutions.

---

## 2. Supported External Provider Directory

| Provider | Data Domain | Protocol / Endpoint | Authentication | Quota & Rate Limits | Offline Fallback |
|---|---|---|---|---|---|
| **Google Gemini API** | AI resume review & mention assistant | REST (`generativelanguage.googleapis.com`) | API Key (`GEMINI_API_KEY`) | Capped via `LLM_BUDGET_HOURLY` (default: 100/hr) | `/ic resume` presents offline self-review rubric |
| **RateMyProfessors** | Professor ratings & student difficulty | GraphQL (`www.ratemyprofessors.com/graphql`) | Public / None | 6-hour TTL in-memory cache | Returns "Rating unavailable or provider degraded" |
| **Metrolinx Open Data** | GO Transit rail and bus advisories | REST (`api.openmetrolinx.com/OpenDataAPI/api/V1/`) | Query Key (`GO_API_KEY`) | Free tier per Metrolinx terms | Circuit breaker opens; silences alerts until recovery |
| **TTC Mastodon** | Toronto subway and bus delay alerts | REST (`mstdn.ca/api/v1/accounts/...`) | Public ActivityPub / None | 300 requests per 5 min | Jittered exponential backoff; no channel flooding |
| **Greenhouse ATS** | Tech internship and new grad jobs | REST (`boards.greenhouse.io/v1/boards/...`) | Public / None | Polled every `JOBS_POLL_SECONDS` (default: 300s) | Retains existing cached postings |
| **Lever ATS** | Tech internship and new grad jobs | REST (`api.lever.co/v0/postings/...`) | Public / None | Polled every `JOBS_POLL_SECONDS` (default: 300s) | Retains existing cached postings |
| **Workday CX** | Enterprise job postings | REST (`*.myworkdayjobs.com/wday/cxs/...`) | Public / None | Polled every `WORKDAY_POLL_SECONDS` (default: 600s) | Malformed feeds quarantined in bad sources set |
| **Yahoo Finance** | Virtual market simulation quotes | HTTP Quote Endpoints | Public / None | 10-minute cache TTL | Emits static disclaimers; falls back to catalog prices |
| **Esplora Bitcoin** | Bitcoin block and mempool statistics | REST (`blockstream.info/api/`) | Public / None | Polled on fixed intervals | Skips iteration cleanly on timeout |

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
from icbot.academics.models import Campus, CourseNotesResult, ResourceLink
from icbot.services.provider_protocol import ProviderResult

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
Before returning any hyperlinks to the user or querying external endpoints, validate them using `validate_and_sanitize_url`:

```python
from icbot.academics.url_safety import validate_and_sanitize_url

safe_url = validate_and_sanitize_url(raw_url, allowed_schemes={"https"})
if safe_url is None:
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

