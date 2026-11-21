# Instigate Cafe Bot — Complete Configuration Reference

**Document Version:** 2.0.0 (Phase 12 Configuration Specification)  
**Configuration Source:** [`icbot/config.py`](../icbot/config.py)  
**Loading Precedence:** Process Environment Variables &rarr; Local `.env` file (if present) &rarr; Safe Built-in Defaults

---

## 1. Overview & Safety Invariants

The Instigate Cafe Bot uses strict runtime configuration validation via the `Settings` model. On boot:
- **Zero Secret Echo:** If a configuration error occurs, all secret tokens and API keys are redacted before error messages are emitted.
- **Gateway Safeguard:** Live Discord gateway connection is blocked unless `ICBOT_ALLOW_GATEWAY=true` (or the `--allow-gateway` CLI flag) is explicitly passed.
- **Safe Development Defaults:** All background pollers (`LLM_ENABLED`, `MARKET_WATCH_ENABLED`, `BTC_WATCH_ENABLED`, `JOBS_POLL_SECONDS=0`) default to disabled to ensure a clean checkout boots safely offline.

---

## 2. Core Discord & Safety Flags

| Environment Variable | Settings Field | Type | Default | Description & Security / Cost Risk | Example |
|---|---|---|---|---|---|
| `DISCORD_TOKEN` | `token` | string | `""` | Bot authentication token from Discord Developer Portal. **CRITICAL RISK:** Compromise allows full bot takeover. Keep permissions at `chmod 600`. | `MTAwMDAw...` |
| `GUILD_ID` | `guild_id` | integer | `0` | Primary Discord Server snowflake ID for instant command synchronization. `0` disables guild-specific sync. | `900000000000000001` |
| `ALLOWED_ROLE_IDS` | `allowed_role_ids` | list[int] | `[]` | Comma-separated list of Discord Role IDs granted admin/operator commands (`/ic admin`). | `111222333,444555666` |
| `STOCK_CHANNEL_ID` | `stock_channel_id` | integer | `0` | Discord Text Channel ID for economy announcements and stock ticker alerts. | `900000000000000002` |
| `USER_COMMAND_CHANNEL_ID` | `user_command_channel_id` | integer | `0` | Channel ID where user commands are permitted (when channel restriction is enabled). | `900000000000000003` |
| `ICBOT_ALLOW_GATEWAY` | `allow_gateway` | boolean | `false` | Explicit opt-in required to open live WebSockets to Discord Gateway. Prevents accidental live launches in CI or dev. | `true` |
| `FORCE_GUILD_RESYNC` | `force_guild_resync` | boolean | `false` | When `true`, forces Discord slash command tree re-synchronization on bot boot. | `false` |

---

## 3. Storage & Database Paths

| Environment Variable | Settings Field | Type | Default | Description & Security Risk | Example |
|---|---|---|---|---|---|
| `DB_PATH` | `db_path` | string | `ic_stock.db` | Canonical path to the persistent SQLite database file for user balances, market trades, exams, scheduler checkpoints, and audit logs. Normalized via `normalize_sqlite_path()`; parent directory is verified writable (`0700` on creation) and fails closed via `DatabasePathError` if unwritable (never falls back to another path). Uses WAL mode. | `/data/campus.db` |
| `GRADES_DB_PATH` | `grades_db_path` | string | `tmu_grades.db` | Path to local grade distributions SQLite cache (optional; when empty or unset in container runtimes, `ServiceContainer` uses the shared `Database(settings.db_path)` or falls back to `settings.db_path`). | `/data/tmu_grades.db` |
| `SQLITE_PATH` *(Deprecated)* | `db_path` | string | *(unset)* | Narrowly scoped deprecated compatibility alias for `DB_PATH`. Used **only** when `DB_PATH` is unset or empty; emits `DeprecationWarning` and is scheduled for removal in `v1.1.0`. Never touches `GRADES_DB_PATH`. | `/data/campus.db` |
| `LORE_PATH` | `lore_path` | string | `""` | Path to custom server lore file. If empty, falls back safely to example lore (`tools/lore.example.json`). | `/data/lore.json` |

---

## 4. Branding & Visual Theme Presets

| Environment Variable | Settings Field | Type | Default | Description & Appearance | Example |
|---|---|---|---|---|---|
| `THEME_PRESET` | `theme_preset` | string | `instigate_cafe` | Active visual preset (`instigate_cafe`, `tmu_bold`, `emerald_mint`, `cyber_neon`, `nordic_frost`). | `instigate_cafe` |
| `BOT_DISPLAY_NAME` | `bot_display_name` | string | `InstigateAI` | Display name rendered in embed headers and notifications. | `CampusBot` |
| `EMBED_LOGO_URL` | `embed_logo_url` | string | *(preset)* | HTTPS URL to logo avatar displayed in rich embed headers. | `https://example.com/logo.png` |
| `EMBED_ACCENT_COLOR` | `embed_accent_color` | color/int | `0xA64DFF` | Hex color code (`#RRGGBB` or `0xRRGGBB`) for embed borders. | `#A64DFF` |
| `EMBED_FOOTER_TEXT` | `embed_footer_text` | string | `Instigate Cafe` | Footer string appended to informational embeds. | `Campus Community` |
| `CURRENCY_NAME` | `currency_name` | string | `IC Cash` | Name of the virtual community currency. | `Campus Coin` |
| `CURRENCY_SYMBOL` | `currency_symbol` | string | `$` | Symbol used for currency displays. | `¢` |
| `PRIMARY_STOCK_SYMBOL` | `primary_stock_symbol` | string | `IC` | Primary virtual stock market ticker symbol. | `CAMP` |
| `PRIMARY_STOCK_NAME` | `primary_stock_name` | string | `Instigate Cafe` | Friendly name of primary equity listed on ICSE. | `Campus Commons` |
| `STOCK_EXCHANGE_NAME` | `stock_exchange_name` | string | `Instigate Cafe Stock Exchange` | Full title of the virtual stock exchange subsystem. | `Student Exchange` |
| `SOURCE_ATTRIBUTION` | `source_attribution` | string | `Internal Instigate Cafe Repository` | Data provenance attribution string. | `Campus Open Data` |

---

## 5. Feature Registry & Modular Profiles

| Environment Variable | Settings Field | Type | Default | Description & Modularity | Example |
|---|---|---|---|---|---|
| `FEATURE_PROFILE` | `feature_profile` | string | `default` | Preset module configuration (`default`, `student_core`, `community_full`, `toronto_tmu`). | `student_core` |
| `ENABLED_FEATURES` | `enabled_features` | list[str] | `[]` | Comma-separated list of module IDs to explicitly enable (`core`, `academics`, `careers`, `transit`, `economy`, `casino`, `market`, `ai`, `sports`). | `academics,careers` |
| `DISABLED_FEATURES` | `disabled_features` | list[str] | `[]` | Comma-separated list of module IDs to explicitly disable. | `casino,sports` |

---

## 6. AI Assistant & Gemini Governance

| Environment Variable | Settings Field | Type | Default | Description & Security / Cost Risk | Example |
|---|---|---|---|---|---|
| `LLM_ENABLED` | `llm_enabled` | boolean | `false` | Master toggle for Gemini-powered mention assistant and AI resume review (`/ic resume`). | `true` |
| `GEMINI_API_KEY` | `gemini_api_key` | string | `""` | Google AI Studio API key. **COST RISK:** Unbounded usage incurs API costs. Set billing alerts in Google Cloud. | `AIzaSy...` |
| `GEMINI_MODEL` | `gemini_model` | string | `gemini-2.5-flash` | Gemini model name used for general chat interactions. | `gemini-2.5-flash` |
| `GEMINI_RESUME_MODEL` | `gemini_resume_model` | string | `gemini-2.5-pro` | High-reasoning model utilized for confidential PDF resume critique. | `gemini-2.5-pro` |
| `GEMINI_RESUME_PROMPT` | `gemini_resume_prompt` | string | `""` | Optional system prompt override for general software engineering resume reviews. | `Custom prompt...` |
| `GEMINI_RESUME_PROMPT_HW` | `gemini_resume_prompt_hw` | string | `""` | System prompt override for hardware engineering resumes. | `Hardware prompt...` |
| `GEMINI_RESUME_PROMPT_ELE`| `gemini_resume_prompt_ele`| string | `""` | System prompt override for electrical engineering resumes. | `Electrical prompt...` |
| `LLM_COOLDOWN_SECONDS` | `llm_cooldown_seconds` | integer | `3` | Per-user rate limit cooldown between mention assistant inquiries. | `5` |
| `LLM_MAX_MESSAGES` | `llm_max_messages` | integer | `25` | Maximum conversation history window size maintained per session. | `20` |
| `LLM_MAX_CHARS` | `llm_max_chars` | integer | `12000` | Maximum input character threshold before context truncation. | `10000` |
| `LLM_MAX_IMAGE_BYTES` | `llm_max_image_bytes` | integer | `2000000` | Maximum image upload size (2MB) accepted for multimodal vision analysis. | `2000000` |
| `LLM_MAX_OUTPUT_TOKENS` | `llm_max_output_tokens`| integer | `1536` | Maximum generation token cap per response. | `1024` |
| `LLM_LORE_ENABLED` | `llm_lore_enabled` | boolean | `false` | Injects synthetic server lore snippets into the system instructions. | `false` |
| `LLM_IMAGES_ENABLED` | `llm_images_enabled` | boolean | `true` | Enables vision understanding on image attachments. | `true` |
| `LLM_ALLOW_DM` | `llm_allow_dm` | boolean | `false` | Allows mention assistant interactions in Direct Messages. | `false` |
| `LLM_ALLOWED_CHANNEL_IDS`| `llm_allowed_channel_ids`| list[int] | `[]` | Channel ID allowlist for AI interactions (empty allows all channels). | `900000000000000004` |
| `LLM_ALLOWED_ROLE_IDS` | `llm_allowed_role_ids` | list[int] | `[]` | Role ID allowlist for AI interactions (empty allows all roles). | `111222333` |
| `LLM_CHANNEL_HISTORY_ENABLED`| `llm_channel_history_enabled`| boolean | `false` | Privacy flag: whether to read surrounding channel message history. | `false` |
| `LLM_DEBUG` | `llm_debug` | boolean | `false` | Enables verbose prompt debugging in console logs. Redaction filter still applies. | `false` |

---

## 7. RateMyProfessors & Campus Academics

| Environment Variable | Settings Field | Type | Default | Description & Provider Provenance | Example |
|---|---|---|---|---|---|
| `RMP_UNIVERSITY` | `rmp_university` | string | `Toronto Metropolitan University` | University name queried on RateMyProfessors GraphQL endpoint. | `University of Toronto` |
| `CAMPUS_ID` | `campus_id` | string | `""` | Identifier for local campus integration. | `tmu` |
| `CAMPUS_URL` | `campus_url` | string | `""` | Official institutional web portal URL. | `https://www.torontomu.ca` |
| `ACADEMIC_COURSE_ALIASES`| `academic_course_aliases`| list[str]| `[]` | Cross-listed course code equivalence pairs (`CPS109:CS109`). | `CPS109:CS109` |
| `ACADEMIC_ALLOWED_HOSTS`| `academic_allowed_hosts`| list[str]| `[]` | SSRF security allowlist for note downloads and syllabus links. | `torontomu.ca,github.com` |
| `ACADEMIC_ENABLED_PROVIDERS`| `academic_enabled_providers`| list[str]| `[]` | Enabled academic data source identifiers (`rmp`, `notes`, `exams`). | `rmp,notes` |
| `EXAMS_TZ` | `exams_tz` | string | `America/Toronto` | Primary timezone for exam countdown calculations and .ics generation. | `America/Toronto` |
| `EXAM_REMINDERS_ENABLED`| `exam_reminders_enabled`| boolean | `false` | Background task posting notifications prior to scheduled exams. | `true` |
| `EXAM_REMINDERS_CHANNEL_ID`| `exam_reminders_channel_id`| integer | `0` | Text channel ID for scheduled exam notifications. | `900000000000000005` |
| `EXAM_REMINDER_LEAD_HOURS`| `exam_reminder_lead_hours`| list[int]| `[24, 2]` | Notification lead times in hours prior to exam start. | `48, 24, 2` |
| `NOTES_SITES` | `notes_sites` | list[str] | `[]` | Domain allowlist for verified campus notes hosting repositories. | `github.com` |

---

## 8. Job Alerts & ATS Ingestion

| Environment Variable | Settings Field | Type | Default | Description & Ingestion Rules | Example |
|---|---|---|---|---|---|
| `JOB_ALERTS_CHANNEL_ID` | `job_alerts_channel_id` | integer | `0` | Discord Text Channel ID where matching job postings are broadcast. | `900000000000000006` |
| `JOBS_POLL_SECONDS` | `jobs_poll_seconds` | integer | `600` | Polling interval in seconds for Greenhouse and Lever ATS job boards (`0` disables). | `300` |
| `GREENHOUSE_BOARDS` | `greenhouse_boards` | list[str] | `[]` | List of corporate Greenhouse job board tokens to track. | `stripe,datadog` |
| `LEVER_SITES` | `lever_sites` | list[str] | `[]` | List of corporate Lever site names to track. | `spotify` |
| `WORKDAY_SITES` | `workday_sites` | list[str] | `[]` | List of Workday CX search tenant URLs to ingest. | `https://rbc.wd3.myworkdayjobs.com/...` |
| `WORKDAY_POLL_SECONDS` | `workday_poll_seconds` | integer | `0` | Polling interval for Workday boards (`0` disables by default). | `1200` |
| `WORKDAY_TIMEOUT_SECONDS`| `workday_timeout_seconds`| integer | `12` | HTTP timeout in seconds for Workday endpoint requests. | `15` |
| `JOBS_LOCATION_ALLOWLIST`| `jobs_location_allowlist`| list[str]| `[]` | Geo-filtering keywords (`Toronto`, `Remote`, `Canada`). | `Toronto, Remote, Waterloo` |
| `JOBS_KEYWORDS_ALLOWLIST`| `jobs_keywords_allowlist`| list[str]| `[]` | Title keywords required to trigger an alert (`Intern`, `New Grad`, `Junior`). | `Intern, Co-op, New Grad` |
| `JOBS_KEYWORDS_DENYLIST` | `jobs_keywords_denylist` | list[str]| `[]` | Title keywords that exclude postings (`Senior`, `Lead`, `Manager`). | `Senior, Staff, Principal, Director` |
| `JOBS_BOOTSTRAP_POST` | `jobs_bootstrap_post` | boolean | `false` | When `true`, posts existing historical jobs on first startup. | `false` |
| `JOBS_REQUIRE_EARLY_CAREER`| `jobs_require_early_career`| boolean | `false` | Strict gate requiring postings to have student/early-career markers. | `true` |

---

## 9. Transit & Regional Alerts

| Environment Variable | Settings Field | Type | Default | Description & Circuit Breakers | Example |
|---|---|---|---|---|---|
| `GO_API_KEY` | `go_api_key` | string | `""` | Metrolinx Open Data API key for GO transit service alerts. | `your_go_api_key` |
| `GO_API_BASE` | `go_api_base` | string | `https://api.openmetrolinx.com/...` | Base URL for Metrolinx Open Data API. | `https://api.openmetrolinx.com/...` |
| `GO_ALERTS_CHANNEL_ID` | `go_alerts_channel_id` | integer | `0` | Discord Channel ID for GO transit alert broadcasts. | `900000000000000007` |
| `GO_POLL_SECONDS` | `go_poll_seconds` | integer | `180` | Polling frequency in seconds for GO transit service notifications. | `180` |
| `GO_ALERT_LEVEL` | `go_alert_level` | string | `major` | Filter threshold: `major` (delays > 15m, cancellations) or `all`. | `major` |
| `GO_BOOTSTRAP_POST_LATEST`| `go_bootstrap_post_latest`| boolean| `true` | Ingests latest alert state without spamming notifications. | `true` |
| `GO_INCLUDE_BUSES` | `go_include_buses` | boolean | `false` | When `true`, includes GO Bus service alerts alongside rail corridors. | `false` |
| `GO_LINES_ALLOWLIST` | `go_lines_allowlist` | list[str] | `[]` | Specific rail line corridors to track (`Lakeshore West`, `Kitchener`). | `Lakeshore West, Lakeshore East` |
| `GO_SSL_VERIFY` | `go_ssl_verify` | boolean | `true` | Enforces TLS certificate verification on Metrolinx API connections. | `true` |
| `GO_CA_BUNDLE_PATH` | `go_ca_bundle_path` | string | `""` | Path to custom TLS CA root bundle if behind corporate proxies. | `/etc/ssl/certs/ca.pem` |
| `GO_DEBUG` | `go_debug` | boolean | `false` | Verbose HTTP logging for GO Transit requests. | `false` |
| `TTC_ALERTS_CHANNEL_ID` | `ttc_alerts_channel_id` | integer | `0` | Discord Channel ID for TTC service alerts aggregated from Mastodon. | `900000000000000008` |
| `TTC_MASTODON_BASE` | `ttc_mastodon_base` | string | `""` | Base URL of the Mastodon instance (defaults to `https://mstdn.ca`). | `https://mstdn.ca` |
| `TTC_MASTODON_ACCT` | `ttc_mastodon_acct` | string | `""` | Target Mastodon handle for TTC delay announcements. | `TTCnotices` |
| `TTC_POLL_SECONDS` | `ttc_poll_seconds` | integer | `120` | Polling interval in seconds for TTC ActivityPub/REST updates. | `120` |
| `TTC_ALERT_LEVEL` | `ttc_alert_level` | string | `major` | Filter threshold: `major` (subway closures, route detours) or `all`. | `major` |
| `TTC_BOOTSTRAP_POST_LATEST`| `ttc_bootstrap_post_latest`| boolean| `false` | Discards historical notices on boot to avoid initial spam. | `false` |
| `TTC_INCLUDE_REPLIES` | `ttc_include_replies` | boolean | `true` | Ingests thread reply updates clarifying ongoing delays. | `true` |
| `TTC_INCLUDE_REBLOGS` | `ttc_include_reblogs` | boolean | `false` | Whether to process reblogged/boosted announcements. | `false` |
| `TTC_DEBUG` | `ttc_debug` | boolean | `false` | Verbose HTTP logging for TTC Mastodon requests. | `false` |

---

## 10. Financial Feeds, Economy & Casino

| Environment Variable | Settings Field | Type | Default | Description & Risk Governance | Example |
|---|---|---|---|---|---|
| `DAILY_CLAIM_AMOUNT` | `daily_claim_amount` | integer | `100` | Virtual currency allowance granted daily via `/ic dailyclaim`. | `100` |
| `BJ_STARTING_BANKROLL` | `bj_starting_bankroll` | integer | `1000` | Default seed bankroll for newly enrolled casino participants. | `1000` |
| `BJ_DEALER_HITS_SOFT_17`| `bj_dealer_hits_soft_17`| boolean | `true` | Casino Blackjack rule standard (true = dealer hits soft 17). | `true` |
| `BJ_MAX_SPLITS` | `bj_max_splits` | integer | `1` | Maximum hand split operations permitted per round. | `1` |
| `BJ_SESSION_TTL_MINUTES`| `bj_session_ttl_minutes`| integer | `20` | Session inactivity timeout in minutes before hands auto-surrender. | `20` |
| `MARKET_WATCH_ENABLED` | `market_watch_enabled` | boolean | `false` | Enables volatility watcher (alerts on large index moves and VIX spikes). | `true` |
| `MARKET_WATCH_POLL_SECONDS`| `market_watch_poll_seconds`| integer| `60` | Polling interval for market indices during exchange hours. | `60` |
| `MARKET_WATCH_CHANNEL_ID`| `market_watch_channel_id`| integer | `0` | Discord Channel ID for market volatility alerts. | `900000000000000009` |
| `MARKET_WATCH_VIX_LEVEL`| `market_watch_vix_level`| float | `20.0` | CBOE VIX volatility index threshold that triggers a caution alert. | `25.0` |
| `MARKET_WATCH_INDEX_MOVE_PCT`| `market_watch_index_move_pct`| float| `2.5` | Percentage intraday swing in SPY/QQQ triggering an announcement. | `3.0` |
| `MARKET_WATCH_STOCK_MOVE_PCT`| `market_watch_stock_move_pct`| float| `10.0` | Single-stock intraday percentage change triggering an alert. | `15.0` |
| `MARKET_WATCH_TICKERS` | `market_watch_tickers` | list[str] | `[]` | List of market ticker symbols monitored for high volatility. | `SPY, QQQ, AAPL, TSLA` |
| `MARKET_WATCH_COOLDOWN_MINUTES`| `market_watch_cooldown_minutes`| int| `30` | Minimum duration between repeated volatility alerts for one ticker. | `60` |
| `MARKET_WATCH_DEBUG` | `market_watch_debug` | boolean | `false` | Logs quote fetches and calculations to console. | `false` |
| `MARKET_DAILY_REPORT_ENABLED`| `market_daily_report_enabled`| bool | `false` | Enables scheduled daily market close report. | `true` |
| `MARKET_DAILY_REPORT_CHANNEL_ID`| `market_daily_report_channel_id`| int| `0` | Discord Channel ID for the daily market summary embed. | `900000000000000010` |
| `MARKET_DAILY_REPORT_TIME`| `market_daily_report_time`| string | `16:30` | Time of day (`HH:MM` 24h format) when the market summary is dispatched. | `16:30` |
| `MARKET_DAILY_REPORT_TZ`| `market_daily_report_tz`| string | `America/Toronto` | Timezone for the daily market summary dispatcher. | `America/Toronto` |
| `MARKET_DAILY_REPORT_TOP_N`| `market_daily_report_top_n`| integer| `8` | Number of top gainers and losers included in daily digest. | `10` |
| `MARKET_DAILY_REPORT_TICKERS`| `market_daily_report_tickers`| list[str]| `[]` | List of tickers summarized in the daily close report. | `SPY, QQQ, AAPL, MSFT` |
| `MARKET_DAILY_REPORT_DEBUG`| `market_daily_report_debug`| bool | `false` | Verbose debug logging for daily report generation. | `false` |
| `BTC_WATCH_ENABLED` | `btc_watch_enabled` | boolean | `false` | Enables periodic Bitcoin address activity & mempool monitoring. | `false` |
| `BTC_WATCH_POLL_SECONDS`| `btc_watch_poll_seconds`| integer | `300` | Polling frequency in seconds for Bitcoin address transaction checks. | `600` |
| `BTC_WATCH_CHANNEL_ID` | `btc_watch_channel_id` | integer | `0` | Discord Channel ID for Bitcoin transaction notifications. | `0` |
| `BTC_WATCH_LABEL` | `btc_watch_label` | string | `BTC Watch` | Embed title label for Bitcoin transaction alerts. | `Whale Tracker` |
| `BTC_WATCH_ADDRESSES` | `btc_watch_addresses` | list[str] | `[]` | Comma-separated list of Bitcoin addresses monitored for UTXO activity. | `bc1q...` |
| `BTC_ESPLORA_BASE_URL` | `btc_esplora_base_url` | string | `""` | Base URL of the Blockstream Esplora REST API instance. | `https://blockstream.info/api` |
| `BTC_WATCH_COOLDOWN_MINUTES`| `btc_watch_cooldown_minutes`| int | `30` | Alert cooldown duration to prevent repeat notifications. | `30` |
| `BTC_WATCH_DEBUG` | `btc_watch_debug` | boolean | `false` | Logs Esplora query payloads and UTXO lookups. | `false` |

---

## 11. Sports Headlines & Odds (Optional)

| Environment Variable | Settings Field | Type | Default | Description & Quota Safeguards | Example |
|---|---|---|---|---|---|
| `ODDS_API_KEY` | `odds_api_key` | string | `""` | The-Odds-API token for upcoming game schedules (NBA, NFL, UFC). **QUOTA RISK:** Free tier is 500 calls/mo. | `your_odds_api_key` |
| `SPORTS_RSS_NBA` | `sports_rss_nba` | string | `""` | Custom RSS feed URL for NBA basketball headlines. | `https://example.com/nba.xml` |
| `SPORTS_RSS_NFL` | `sports_rss_nfl` | string | `""` | Custom RSS feed URL for NFL football headlines. | `https://example.com/nfl.xml` |
| `SPORTS_RSS_MMA` | `sports_rss_mma` | string | `""` | Custom RSS feed URL for MMA news. | `https://example.com/mma.xml` |
| `SPORTS_RSS_UFC` | `sports_rss_ufc` | string | `""` | Custom RSS feed URL for UFC fight cards. | `https://example.com/ufc.xml` |
| `SPORTS_RSS_BOXING` | `sports_rss_boxing` | string | `""` | Custom RSS feed URL for professional boxing updates. | `https://example.com/boxing.xml` |

---

## 12. Logging, Observability & Task Supervisor

| Environment Variable | Settings Field | Type | Default | Description & Operational Health | Example |
|---|---|---|---|---|---|
| `LOG_FILE` | `log_file` | string | `""` | File path for disk logging. If empty, logs are written to stdout/stderr. | `/data/logs/bot.log` |
| `LOG_MAX_BYTES` | `log_max_bytes` | integer | `5000000` | Maximum log file size (5MB) before rotating. | `10000000` |
| `LOG_BACKUP_COUNT` | `log_backup_count` | integer | `3` | Number of rotated historical log files retained on disk. | `5` |
| `ICBOT_TELEMETRY` | `telemetry_enabled` | boolean | `false` | Enables internal performance latency timers and health counters. | `true` |
| `TASK_STARTUP_JITTER_SECONDS`| `task_startup_jitter_seconds`| float| `15.0` | Maximum random startup delay applied to background tasks to stagger boot load. | `10.0` |
| `TASK_MAX_BACKOFF_SECONDS`| `task_max_backoff_seconds`| float | `1800.0`| Exponential backoff ceiling (30 minutes) for failing background tasks. | `1800.0` |
