# Instigate Cafe Bot — Offline Testing & Coverage Guide

**Phase 2: Offline Tests, Characterization, and Developer Tooling**

This repository enforces an **offline-first automated test suite**. Running `pytest` never connects to Discord, executes live HTTP scrapers, loads private `.env` secrets, or mutates production SQLite files.

---

## 1. Suite Architecture & Marker Taxonomy

Pytest configuration lives in [`pyproject.toml`](file:///home/sukesan/dev/discord/campus-bot/pyproject.toml):
- `testpaths = ["tests"]`
- `norecursedirs` excludes `scripts/`, `.venv/`, `docs/`, `assets/`, and `tools/`
- `asyncio_mode = "auto"` with per-test function event loops
- `--strict-markers -m 'not live_smoke'` prevents undeclared markers and excludes live checks by default

| Marker | Scope | Suite Directory | External I/O |
| :--- | :--- | :--- | :--- |
| `unit` | Pure logic (`Settings`, game engines, permissions, embeds, privacy spec, network guard) | [`tests/unit/`](file:///home/sukesan/dev/discord/campus-bot/tests/unit/) | None (in-memory only) |
| `storage` | SQLite persistence (`Database.init`, `UserStore`, `FunMarketStore` trades/positions/collateral) | [`tests/integration/test_storage_user_and_market.py`](file:///home/sukesan/dev/discord/campus-bot/tests/integration/test_storage_user_and_market.py) | Isolated `tmp_path` SQLite |
| `discord_mock` | Slash command callbacks via `FakeInteraction`, `FakeMember`, `FakeResponse`, `FakeFollowup` | [`tests/integration/test_discord_mock_commands.py`](file:///home/sukesan/dev/discord/campus-bot/tests/integration/test_discord_mock_commands.py) | Mocked Discord + local fixtures |
| `provider_contract` | External payload parsers (RMP GraphQL, Notes SPA, TMU Grades HTML, Greenhouse, Lever, Workday, Metrolinx GO, Gemini, Yahoo Finance) | [`tests/integration/test_provider_contracts.py`](file:///home/sukesan/dev/discord/campus-bot/tests/integration/test_provider_contracts.py) | Monkeypatched `aiohttp` + [`tests/fixtures/`](file:///home/sukesan/dev/discord/campus-bot/tests/fixtures/) |
| `live_smoke` | Opt-in manual live checks (never collected automatically) | [`scripts/manual/`](file:///home/sukesan/dev/discord/campus-bot/scripts/manual/README.md) | Requires explicit owner consent + `ICBOT_ALLOW_LIVE_SMOKE=1` |

---

## 2. Offline Safety Guarantees (`tests/conftest.py`)

[`tests/conftest.py`](file:///home/sukesan/dev/discord/campus-bot/tests/conftest.py) installs two `autouse=True` safeguards for every test:

1. **Outbound Network Guard (`_disable_outbound_network`):**
   - Blocks IPv4/IPv6 (`AF_INET`, `AF_INET6`) `socket.socket.connect` and `connect_ex` (while leaving `AF_UNIX` socketpairs intact for `asyncio` and `aiosqlite`).
   - Blocks `socket.create_connection`, external `socket.getaddrinfo`, `urllib.request.urlopen`, `http.client.HTTPConnection` / `HTTPSConnection`, and unmocked `aiohttp.ClientSession._request`.
   - Raises [`OfflineNetworkDisabledError`](file:///home/sukesan/dev/discord/campus-bot/tests/helpers.py) immediately if any code attempts unmocked outbound I/O.
2. **Environment & Cache Isolation (`_isolate_environment`):**
   - Clears `DISCORD_TOKEN`, `GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`, `GREENHOUSE_BOARDS`, `LEVER_SITES`, `WORKDAY_SITES`, `NOTES_SITES`, and channel IDs.
   - Disables pollers (`LLM_ENABLED=false`, `MARKET_WATCH_ENABLED=false`, `BTC_WATCH_ENABLED=false`, `JOBS_POLL_SECONDS=0`).
   - Stubs `dotenv.load_dotenv` so local `.env` files are never read during tests.
   - Clears cached module-level sessions, `asyncio.Lock` instances bound to previous loops (`yahoo_quotes`, `btc_esplora`, `crypto_fng`), and `notes._NOTES_CACHE`.

---

## 3. Reusable Fixtures

| Fixture | Purpose |
| :--- | :--- |
| `tmp_db` | Temporary file-backed `Database` initialized cleanly via `await database.init()`. |
| `frozen_time` | Manual Unix clock (`clock.set(...)`, `clock.advance(...)`) for cooldowns and cache TTLs. |
| `synthetic_settings` | Offline `Settings` object with empty token, synthetic snowflake IDs, and local paths. |
| `seed_guild_users` | Pre-populates two synthetic user profiles (`800000000000000001`, `800000000000000002`). |
| `make_member` / `make_interaction` | Builds fake Discord `Member` (satisfying `isinstance(..., discord.Member)`), `FakeClient`, `FakeResponse` (enforcing `defer` / `InteractionResponded`), and `FakeFollowup`. |
| `mock_aiohttp` | Routes `aiohttp.ClientSession` `GET`/`POST`/`request` calls to registered local fixture payloads and records headers/bodies for contract assertions. |

---

## 4. Sanitized Fixtures (`tests/fixtures/`)

All files in [`tests/fixtures/`](file:///home/sukesan/dev/discord/campus-bot/tests/fixtures/) are synthetically constructed and contain zero real user PII, Discord tokens, API keys, or private community lore:
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

Manual live probes formerly in `tests/test_*.py` now reside in [`scripts/manual/`](file:///home/sukesan/dev/discord/campus-bot/scripts/manual/README.md), excluded from pytest discovery and wrapped in `if __name__ == "__main__":` guards.

---

## 6. Coverage Policy & Baseline Status

- **Coverage configuration** (`[tool.coverage.run]` in `pyproject.toml`): measures line and branch coverage (`branch = true`) across `icbot/`.
- **Policy:** Never lower or bypass failing checks (`--no-cov` in CI or silently deleting assertions) to manufacture green runs. New or modified modules in subsequent phases must include deterministic offline tests in the matching marker category.
- **Measured coverage status (Python 3.12.13 offline baseline, 43 passed):**
  - Command: `.venv/bin/pytest --cov=icbot --cov-report=term-missing -v`
  - Overall `icbot/` baseline: **33%** combined statement+branch coverage across 5,879 statements and 1,884 branches (with untested pollers/UI commands scheduled for characterization in Phase 2B+).
  - Key tested modules: `slots_engine.py` (**100%**), `fun_market_catalog.py` (**93%**), `config.py` (**93%**), `permissions.py` (**92%**), `gemini_client.py` (**82%**), `user_service.py` (**81%**), `yahoo_quotes.py` (**80%**), `db.py` (**78%**), `blackjack_engine.py` (**77%**), `notes.py` (**76%**), `grades.py` (**75%**), `profile.py` (**74%**), `go_client.py` (**74%**), `fun_market.py` (**73%**), `jobs_ats_client.py` (**73%**), `embeds.py` (**71%**), `jobs_workday_client.py` (**69%**), `exams.py` (**68%**), `roulette_engine.py` (**62%**), `prof.py` (**59%**).

