# Instigate Cafe Bot — Configuration, Dependency Injection & Per-Guild Readiness

This document specifies the Phase 3B configuration validation, capability readiness reporting, typed error redaction, process dependency container, and per-guild settings separation implemented across [`icbot/config.py`](../../icbot/config.py), [`icbot/guild_config.py`](../../icbot/guild_config.py), [`icbot/errors.py`](../../icbot/errors.py), [`icbot/container.py`](../../icbot/container.py), and [`icbot/bot.py`](../../icbot/bot.py).

## 1. Typed Sub-Configurations & Schema Validation

[`Settings`](../../icbot/config.py) preserves flat attribute access for existing callers while organizing configuration into 9 frozen domain dataclasses accessible as read-only properties or assembled via `Settings.from_components(...)`:

| Property | Sub-config | Scope |
| :--- | :--- | :--- |
| `settings.discord` | `DiscordConfig` | Bot token, guild sync ID, channels, roles, embed branding, gateway opt-in |
| `settings.storage` | `StorageConfig` | `db_path`, `grades_db_path`, `lore_path` |
| `settings.academics` | `AcademicsConfig` | `exams_tz`, `notes_sites`, `rmp_university` |
| `settings.careers` | `CareersConfig` | Job channels, polling intervals, Greenhouse/Lever/Workday sources & filters |
| `settings.gemini` | `GeminiConfig` | Gemini API key, models, token/char/image bounds, custom resume prompts |
| `settings.economy` | `EconomyConfig` | Daily claim amount, Blackjack starting bankroll, S17 rule, splits, TTL |
| `settings.transit` | `TransitConfig` | TTC Mastodon and GO Transit API endpoints, alert levels, TLS, channels |
| `settings.financial` | `FinancialAlertsConfig` | Market watch thresholds, daily report timezone/time/tickers, BTC watch |
| `settings.sports` | `SportsConfig` | `odds_api_key` and `sports_rss_{nba,nfl,mma,ufc,boxing}` feed overrides |

### Validation Modes & Aliases

- **Forgiving Mode (`Settings.from_env(strict=False)`):** Default loader preserving safe local/test startup (invalid snowflakes clamp safely while setting `guild_id_invalid=True`; `workday_poll_seconds == 0` inherits `jobs_poll_seconds`).
- **Strict Mode (`Settings.from_env(strict=True)` / `Settings.load_validated()` / `settings.validate(strict=True)`):** Validates integers, booleans, floats, snowflakes (`0..MAX_SNOWFLAKE`), negative values, `http://`/`https://` URLs (`embed_logo_url`, `ttc_mastodon_base`, `go_api_base`, `btc_esplora_base_url`, `sports_rss_*`, `notes_sites`, `workday_sites`), 24-bit embed colors, `HH:MM` report times, IANA timezones (`exams_tz`, `market_daily_report_tz`), and `major`/`all` alert levels — raising [`ConfigValidationError`](../../icbot/errors.py) without leaking secrets.
- **Deprecated Env Aliases (emit `DeprecationWarning`):**
  - `LLM_MODEL` → `GEMINI_MODEL`
  - `TTC_CHANNEL_ID` → `TTC_ALERTS_CHANNEL_ID`
  - `JOBS_CHANNEL_ID` → `JOB_ALERTS_CHANNEL_ID`
  - `ALPHAVANTAGE_API_KEY` (unused legacy variable)
  - Properties `greenhouse_board_tokens`, `lever_site_tokens`, and `jobs_channel_id` emit `DeprecationWarning`.

## 2. Capability Readiness & Safe Startup Diagnostics

`settings.capabilities()` evaluates integration readiness as `dict[str, CapabilityStatus]` without exposing secret values:

- `discord_gateway`, `llm_assistant`, `resume_review`, `odds_api`, `ttc_alerts`, `go_alerts`, `job_alerts`, `market_watch`, `market_daily_report`, `btc_watch`
- Missing optional keys degrade gracefully rather than blocking bot startup:
  - `/ic sports` continues serving public RSS headlines when `ODDS_API_KEY` is empty or unavailable.
  - `/ic resume` and `@mention` LLM assistant check readiness dynamically and fail gracefully without crashing `/ic` startup.
- [`CampusBot._log_startup_config()`](../../icbot/bot.py) and `/ic botinfo` report non-secret readiness status (never logging raw API keys, bot tokens, or credentials).

## 3. Typed Errors & Secret Redaction

[`icbot/errors.py`](../../icbot/errors.py) defines a unified exception hierarchy inheriting `BotError` while maintaining compatibility with legacy exception types:

- `ConfigValidationError` (`MisconfigurationError`, `ValueError`)
- `UserInputError` (`ValueError`)
- `PermissionDeniedError` (`PermissionError`)
- `ProviderError` (`RuntimeError`) — subclassed by external client errors (`GeminiError`, `GOFetchError`, `TTCFetchError`, `OddsAPIError`, `FunMarketError`)
- `InsufficientBalanceError` (`RuntimeError`)

All error logs and user-facing error summaries pass through `redact_secrets()`, `safe_error_summary()`, or `format_exception_safe()` to scrub configured secrets, `?key=`/`?token=`/`?api_key=` query parameters, `Authorization`/`X-API-Key` headers, Bearer/Bot prefixes, and known token shapes.

## 4. Process Container (`ServiceContainer`)

[`ServiceContainer`](../../icbot/container.py) replaces module-level mutable state across features, tasks, and services with instance-scoped resources owned by `CampusBot.services`:

- Shared `aiohttp.ClientSession` (`http_session`, `ensure_http_session()`, closed cleanly in `close()`)
- Injectable `clock` (`time.time`) and `monotonic_clock` (`time.monotonic`)
- Scoped caches and runtime state:
  - `notes_cache` (`NotesCache`, partitioned by normalized `notes_sites`)
  - `rss_cache` (`RssCache`)
  - `yahoo_crumb` (`YahooCrumbCache`)
  - `lore_cache`, `llm_last_reply`, `llm_killed`
  - `jobs_bad_sources`, `workday_bad_sources`, `bj_sessions` (`SessionManager`, lazily created so `import main` performs zero `icbot.features.*` imports)
- Store & provider factories (`user_store`, `fun_market_store`, `blackjack_store`, `build_odds_client`, `gemini_generate`) plus optional test provider hooks (`gemini_generate_fn`, `fetch_quotes_fn`, `fetch_fear_greed_fn`, `fetch_address_txs_fn`, `odds_client_factory`).
- Direct standalone unit tests creating bare `FakeClient` instances transparently receive an attached fallback `ServiceContainer` via `get_container(client)` (using fallback caches/sessions without sharing state across full `CampusBot` instances).

## 5. Process vs. Per-Guild Settings Separation

[`icbot/guild_config.py`](../../icbot/guild_config.py) enforces strict isolation between process-wide credentials and per-guild behavioral overrides stored in the `guild_settings` SQLite table:

- **`SECRET_SETTING_KEYS`** (`token`, `gemini_api_key`, `go_api_key`, `odds_api_key`) and **`PROCESS_ONLY_KEYS`** (`allow_gateway`, `force_guild_resync`, `guild_id*`, `db_path`, `grades_db_path`, `lore_path`, `ttc_mastodon_*`, `go_api_base`, `go_ssl_verify`, `go_ca_bundle_path`, poller intervals, `btc_esplora_base_url`) are **rejected** by `validate_guild_overrides()` and `GuildSettingsStore` with `ConfigValidationError`.
- **`GUILD_OVERRIDABLE_KEYS`** permits safe per-guild customization of channels (`user_command_channel_id`, `stock_channel_id`, etc.), `allowed_role_ids`, embed branding (`embed_logo_url`, `embed_accent_color`, `embed_footer_text`), timezones, `notes_sites`, `rmp_university`, `sports_rss_*`, LLM flags/model/prompts/limits (backed by the process `GEMINI_API_KEY`), economy/blackjack parameters, job filters, and alert thresholds.
- [`_CafeCommandTree.interaction_check()`](../../icbot/bot.py), every `/ic` feature command, and [`maybe_handle_llm_mention()`](../../icbot/tasks/llm_assistant.py) resolve effective guild settings via `resolve_guild_settings(client, target)` without ever writing back into `bot.settings` or allowing cross-guild state bleed.

