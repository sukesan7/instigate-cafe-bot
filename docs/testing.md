# Instigate Cafe Bot — Offline Testing, Invariants & Verification Guide

**Release:** `v1.0.0-rc1`

This repository enforces an **offline-first automated test suite**. Running `uv run pytest` never connects to Discord, executes live HTTP scrapers, loads private `.env` secrets, or mutates production SQLite files.

---

## 1. Suite Architecture & Marker Taxonomy

Pytest configuration lives in [`pyproject.toml`](../pyproject.toml):
- `testpaths = ["tests"]`
- `norecursedirs` excludes `scripts/`, `.venv/`, `docs/`, `assets/`, and `tools/`
- `asyncio_mode = "auto"` with per-test function event loops
- `--strict-markers -m 'not live_smoke'` prevents undeclared markers and excludes opt-in live probes by default

| Marker | Scope | Examples | External I/O |
| :--- | :--- | :--- | :--- |
| `unit` | Pure logic (`Settings`, game engines, permissions, embeds, theme luminance, privacy export/delete, 12-family failure taxonomy, doc validation, secret scan) | [`tests/unit/`](../tests/unit/) | None (in-memory / temp dir) |
| `storage` | SQLite WAL persistence, concurrency locks, multi-guild economy ledgers, Schema v1–v7 migrations | [`tests/integration/test_storage_user_and_market.py`](../tests/integration/test_storage_user_and_market.py), [`tests/integration/test_economy_and_market_invariants.py`](../tests/integration/test_economy_and_market_invariants.py) | Isolated `tmp_path` SQLite |
| `discord_mock` | Slash commands, UI views, actor/guild guards, timeouts, RBAC, and DB settlement via `FakeInteraction` / `FakeClient` | [`tests/integration/test_discord_mock_commands.py`](../tests/integration/test_discord_mock_commands.py), [`tests/integration/test_interaction_state_and_pagination.py`](../tests/integration/test_interaction_state_and_pagination.py) | Mocked Discord + local fixtures |
| `provider_contract` | External payload parsers, ATS pipeline, circuit breakers, poller checkpoints, and HTTP error-family matrix | [`tests/integration/test_provider_contracts.py`](../tests/integration/test_provider_contracts.py), [`tests/integration/test_pollers_and_http_errors.py`](../tests/integration/test_pollers_and_http_errors.py) | Monkeypatched `aiohttp` + [`tests/fixtures/`](../tests/fixtures/) |
| `live_smoke` | Opt-in manual live checks (never collected automatically) | [`scripts/manual/`](../scripts/manual/README.md) | Requires explicit owner consent + `ICBOT_ALLOW_LIVE_SMOKE=1` |

> **Development Workflow & CI:** See [Development & Tooling Guide](development.md) for `uv` setup, `Makefile` safety tiers, pre-commit hooks, and `.github/workflows/ci.yml`.

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

## 5. Offline Verification Commands

```bash
# Run full offline pytest suite
uv run pytest

# Verify zero import-time network side effects during collection
uv run pytest --collect-only

# Run only pure unit tests or coverage report
uv run pytest -m unit
uv run pytest --cov=icbot --cov-report=term-missing

# Offline smoke, documentation, secret hygiene, and synthetic staging harnesses
uv run python tools/smoke_offline.py
uv run python tools/smoke_staging.py
uv run python tools/check_docs.py
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/run_usability_audit.py
uv run python tools/run_staging_acceptance.py
uv run python tools/verify_beta_readiness.py
```

Manual live probes reside in [`scripts/manual/`](../scripts/manual/README.md), excluded from pytest discovery and guarded behind `if __name__ == "__main__":`.

---

## 6. Verified Domain Invariants & Coverage Policy

- **Coverage configuration** (`[tool.coverage.run]` in `pyproject.toml`): measures line and branch coverage (`branch = true`) across `icbot/`. Never bypass assertions or coverage checks to force green runs.
- **Core Verified Invariant Families (Offline Automated Tests):**
  1. **Economy & Market Atomicity:** `BEGIN IMMEDIATE` transactions prevent double-claiming, negative balances, overselling, and collateral drift across concurrent invocations and multi-guild tenants.
  2. **Casino Engines & Restart Recovery:** Pure Python PRNG engines (`blackjack_engine.py`, `roulette_engine.py`, `slots_engine.py`) enforce deterministic payout bounds and persistent Blackjack crash recovery.
  3. **Privacy & Erasure Invariants:** Batched `UserStore.export_user_data()` (`format_version: 2`), atomic `UserStore.delete_user_data()` de-identification across Schema v7 tables, zero local disk writes on `/ic resume`, and `LLM_CHANNEL_HISTORY_ENABLED=false` default.
  4. **UI & Interaction Safety:** Pre-defer ephemeral audience resolution for private exams, actor-bound `BasePaginationView` / `BaseConfirmView` authorization, Discord character limits, HTTPS-only job links, and zero-width mention neutralization.
  5. **12-Family Failure Taxonomy & HTTP Error Matrix:** All 12 failure families normalize safely with secret/path/SQL/prompt redaction, `asyncio.CancelledError` propagation, `ProviderCircuitBreaker` fast-fail/recovery, and RFC `Retry-After` handling.
  6. **Offline Harness Boundary:** `run_staging_acceptance.py`, `verify_beta_readiness.py`, and `run_usability_audit.py` exercise synthetic guilds and mocked interactions only. See [Release Claims & Verification Evidence](release-evidence.md) for what still requires manual owner-run live staging.

