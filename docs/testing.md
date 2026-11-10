# Instigate Cafe Bot — Offline Testing & Coverage Guide

**Phase 2: Offline Tests, Characterization, and Developer Tooling**

This repository enforces an **offline-first automated test suite**. Running `pytest` never connects to Discord, executes live HTTP scrapers, loads private `.env` secrets, or mutates production SQLite files.

---

## 1. Suite Architecture & Marker Taxonomy

Pytest configuration lives in [`pyproject.toml`](../pyproject.toml):
- `testpaths = ["tests"]`
- `norecursedirs` excludes `scripts/`, `.venv/`, `docs/`, `assets/`, and `tools/`
- `asyncio_mode = "auto"` with per-test function event loops
- `--strict-markers -m 'not live_smoke'` prevents undeclared markers and excludes live checks by default

| Marker | Scope | Suite Directory / Files | External I/O |
| :--- | :--- | :--- | :--- |
| `unit` | Pure logic (`Settings`, game engines & table invariants, permissions, embed bounds, privacy spec, network guard) | [`tests/unit/`](../tests/unit/) (`test_config.py`, `test_game_engines.py`, `test_game_invariants.py`, `test_network_guard.py`, `test_permissions_and_embeds.py`, `test_privacy_model.py`) | None (in-memory only) |
| `storage` | SQLite persistence, concurrency races, short collateral, startup migrations (`Database.init`, `UserStore`, `BlackjackStore`, `FunMarketStore`) | [`test_storage_user_and_market.py`](../tests/integration/test_storage_user_and_market.py), [`test_economy_and_market_invariants.py`](../tests/integration/test_economy_and_market_invariants.py) | Isolated `tmp_path` SQLite |
| `discord_mock` | Slash commands, UI views, timeouts, RBAC & DB settlement via `FakeInteraction` / `FakeMessage` | [`test_discord_mock_commands.py`](../tests/integration/test_discord_mock_commands.py), [`test_casino_views_and_commands.py`](../tests/integration/test_casino_views_and_commands.py) | Mocked Discord + local fixtures |
| `provider_contract` | External payload parsers, background poller checkpoints, and 12-client × 6-mode HTTP error-family matrix | [`test_provider_contracts.py`](../tests/integration/test_provider_contracts.py), [`test_pollers_and_http_errors.py`](../tests/integration/test_pollers_and_http_errors.py) | Monkeypatched `aiohttp` + [`tests/fixtures/`](../tests/fixtures/) |
| `live_smoke` | Opt-in manual live checks (never collected automatically) | [`scripts/manual/`](../scripts/manual/README.md) | Requires explicit owner consent + `ICBOT_ALLOW_LIVE_SMOKE=1` |

> **Domain Invariant & Defect Matrix:** See [`docs/testing/invariant-matrix.md`](testing/invariant-matrix.md) for the complete catalog of verified invariants, HTTP error-family contracts across all 12 external clients, non-test code-audited risks, and tracked `ISSUE-2B-01` .. `ISSUE-2B-11` strict `xfail` locks.

---

## 2. Offline Safety Guarantees (`tests/conftest.py`)

[`tests/conftest.py`](../tests/conftest.py) installs two `autouse=True` safeguards for every test:

1. **Outbound Network Guard (`_disable_outbound_network`):**
   - Blocks IPv4/IPv6 (`AF_INET`, `AF_INET6`) `socket.socket.connect` and `connect_ex` (while leaving `AF_UNIX` socketpairs intact for `asyncio` and `aiosqlite`).
   - Blocks `socket.create_connection`, external `socket.getaddrinfo`, `urllib.request.urlopen`, `http.client.HTTPConnection` / `HTTPSConnection`, and unmocked `aiohttp.ClientSession._request`.
   - Raises [`OfflineNetworkDisabledError`](../tests/helpers.py) immediately if any code attempts unmocked outbound I/O.
2. **Environment & Cache Isolation (`_isolate_environment`):**
   - Clears `DISCORD_TOKEN`, `GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`, `GREENHOUSE_BOARDS`, `LEVER_SITES`, `WORKDAY_SITES`, `NOTES_SITES`, and channel IDs.
   - Disables pollers (`LLM_ENABLED=false`, `MARKET_WATCH_ENABLED=false`, `BTC_WATCH_ENABLED=false`, `JOBS_POLL_SECONDS=0`).
   - Stubs `dotenv.load_dotenv` so local `.env` files are never read during tests.
   - Clears cached module-level sessions, `asyncio.Lock` instances bound to previous loops (`yahoo_quotes`, `btc_esplora`, `crypto_fng`), `notes._NOTES_CACHE`, `icbot.features.blackjack._sessions`, and `icbot.tasks.job_alerts._MARK_SEEN_MODE`.

---

## 3. Reusable Fixtures

| Fixture | Purpose |
| :--- | :--- |
| `tmp_db` | Temporary file-backed `Database` initialized cleanly via `await database.init()`. |
| `frozen_time` | Manual Unix clock (`clock.set(...)`, `clock.advance(...)`) for cooldowns and cache TTLs. |
| `synthetic_settings` | Offline `Settings` object with empty token, synthetic snowflake IDs, and local paths. |
| `seed_guild_users` | Pre-populates two synthetic user profiles (`800000000000000001`, `800000000000000002`). |
| `make_member` / `make_interaction` | Builds fake Discord `Member` (satisfying `isinstance(..., discord.Member)`), `FakeClient`, `FakeMessage` (edit/view history + `_state._get_client()`), `FakeResponse` (enforcing `defer` / `edit_message` / `InteractionResponded`), and `FakeFollowup`. |
| `make_poller_bot` | Builds an offline `FakeClient` wired to `tmp_db` and pre-registered `FakeTextChannel` instances for poller characterization tests. |
| `mock_aiohttp` | Routes `aiohttp.ClientSession` `GET`/`POST`/`request` calls to registered local fixture payloads or injected exceptions (`exc=TimeoutError`, `CancelledError`, `ClientError`) and records headers/bodies for contract assertions. |

---

## 4. Sanitized Fixtures (`tests/fixtures/`)

All files in [`tests/fixtures/`](../tests/fixtures/) are synthetically constructed and contain zero real user PII, Discord tokens, API keys, or private community lore:
- `notes_spa_index.html`, `notes_spa_bundle.js` — Vite SPA HTML + JS bundle
- `rmp_school_response.json`, `rmp_prof_response.json` — RateMyProfessors GraphQL responses (Ada Lovelace synthetic prof)
- `tmu_grades_course.html` — Grade distribution markup including `All Professors` aggregate filter check
- `greenhouse_jobs.json`, `lever_jobs.json` — ATS job feed JSON fixtures
- `workday_careers.html`, `workday_cxs_jobs.json` — Workday CSRF warmup + CXS pagination JSON
- `metrolinx_service_alerts.json` — Metrolinx GO Transit Open Data service alert JSON
- `gemini_generate_response.json` — Google Gemini REST `generateContent` response JSON
- `yahoo_quotes_response.json` — Yahoo Finance `quoteResponse` fixture (`PRE`, `POST`, `REGULAR`)
- `synthetic_lore.json` — Synthetic lore injection fixture

---

## 5. Commands

```bash
# Run full offline suite (unit + storage + discord_mock + provider_contract)
pytest

# Collect without running to verify zero import-time network side effects
pytest --collect-only

# Run only pure unit tests
pytest -m unit

# Run with branch coverage report against icbot/
pytest --cov=icbot --cov-report=term-missing

# Verify secret hygiene across tracked files
python3 tools/scan_secrets.py --enforce-clean
```

Manual live probes formerly in `tests/test_*.py` now reside in [`scripts/manual/`](../scripts/manual/README.md), excluded from pytest discovery and wrapped in `if __name__ == "__main__":` guards.

---

## 6. Coverage Policy & Baseline Status

- **Coverage configuration** (`[tool.coverage.run]` in `pyproject.toml`): measures line and branch coverage (`branch = true`) across `icbot/`.
- **Policy:** Never lower or bypass failing checks (`--no-cov` in CI or silently deleting assertions) to manufacture green runs. New or modified modules in subsequent phases must include deterministic offline tests in the matching marker category.
- **Measured coverage status (Python 3.12.13 Phase 2B characterization baseline — `204 passed, 19 xfailed`, 223 collected):**
  - Command: `.venv/bin/pytest --cov=icbot --cov-report=term-missing -v`
  - Overall `icbot/` baseline: **55%** combined statement+branch coverage across 5,879 statements and 1,884 branches (up from 33% at Phase 2A). Remaining uncharacterized UI/task modules (`admin.py`, `info.py`, `invest.py`, `jobs.py`, `report.py`, `resume.py`, `sports.py`, `btc_watch.py`, `llm_assistant.py`, `market_daily_report.py`, `market_watch.py`) are scheduled for Phase 3/4 remediation & expansion.
  - Characterized modules: `slots_engine.py` (**100%**), `roulette_engine.py` (**99%**), `roulette.py` (**98%**), `help.py` (**95%**), `grades.py` (**93%**), `gemini_client.py` (**93%**), `config.py` (**93%**), `fun_market_catalog.py` (**93%**), `permissions.py` (**92%**), `db.py` (**90%**), `blackjack_engine.py` (**89%**), `slots.py` (**89%**), `embeds.py` (**88%**), `jobs_ats_client.py` (**86%**), `sports_client.py` (**85%**), `fun_market.py` (**83%**), `user_service.py` (**81%**), `yahoo_quotes.py` (**81%**), `notes.py` (**81%**), `ttc_client.py` (**80%**), `go_client.py` (**79%**), `exams.py` (**79%**), `jobs_workday_client.py` (**78%**), `lc_leaderboard.py` (**75%**), `profile.py` (**74%**), `prof.py` (**71%**), `jobs_workday_alerts.py` (**67%**), `ttc_alerts.py` (**64%**), `crypto_fng.py` (**63%**), `blackjack.py` (**62%**), `go_alerts.py` (**58%**), `job_alerts.py` (**53%**), `btc_esplora.py` (**49%**).
  - Tracked defect locks: **19** strict `xfail` cases across **11** registered bugs (`ISSUE-2B-01` .. `ISSUE-2B-11`); see [`docs/testing/invariant-matrix.md`](testing/invariant-matrix.md).

