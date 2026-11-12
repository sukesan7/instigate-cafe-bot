# Instigate Cafe Bot — Bot Lifecycle, Command Registry & Gateway Safeguard

This document specifies the Phase 3A startup, command synchronization, reconnection, and shutdown architecture implemented across [`main.py`](../../main.py), [`icbot/bot.py`](../../icbot/bot.py), and [`icbot/registry.py`](../../icbot/registry.py).

## 1. Zero Import-Time Side Effects

Importing [`main.py`](../../main.py), [`icbot/bot.py`](../../icbot/bot.py), or [`icbot/registry.py`](../../icbot/registry.py) performs **zero** runtime side effects:
- No `load_dotenv()` call on import (called inside [`main()`](../../main.py) with `override=False` so explicit shell/CI variables always win over local `.env` files).
- No [`Settings`](../../icbot/config.py) instantiation, [`Database`](../../icbot/db.py) construction, `aiohttp.ClientSession` allocation, `CampusBot` creation, or eager `icbot.features.*` imports at import time.

## 2. Controlled Command Registry (`/ic`)

[`icbot/registry.py`](../../icbot/registry.py) replaces implicit `# noqa: F401` feature imports with explicit `FEATURE_MODULES` declaration and `register_ic_commands(tree, guild=..., override=True)`.
- All existing [`icbot.app.ic`](../../icbot/app.py) subgroups (`admin`, `casino`, `exam`, `icse`, `sports`) and all 41 leaf command qualified names remain unchanged.
- Commands are registered with `override=True`; destructive automatic `tree.clear_commands()` calls on startup or reconnect are forbidden so slash commands are never wiped during transient Discord disconnects.
- Because the bot has zero prefix commands, `CampusBot` initializes with `help_command=None` and `on_message()` skips `process_commands(message)` unless prefix commands (`bot.commands`) are explicitly registered.

## 3. Startup, Reconnect & Shutdown Lifecycle

```text
main()
  ├─ load_dotenv(override=False) -> Settings.from_env()
  ├─ validate_gateway_launch(settings, allow_gateway=...)   # Fail closed unless opted in
  └─ create_bot(settings) -> CampusBot
       ├─ setup_hook()  [Idempotent, once per live runtime]
       │    ├─ 1. await db.init() + load persisted llm_killed meta flag
       │    ├─ 2. Initialize bot.http_session + yahoo_quotes/btc_esplora/crypto_fng sessions
       │    ├─ 3. Log sanitized non-secret configuration summary
       │    ├─ 4. Register /ic tree (override=True; never clear_commands)
       │    ├─ 5. Execute controlled initial sync (guild or global; non-fatal on Discord sync error)
       │    ├─ 6. Spawn background poller tasks once (each calls await self.wait_until_ready())
       │    └─ On failure before completion: roll back tasks, HTTP sessions, and DB handle cleanly
       ├─ on_ready()    [Reconnect-safe: increments ready_count & logs only]
       └─ close()       [Cancels & awaits tasks, closes HTTP sessions + service sessions + db + super().close()]
```

## 4. Guild Development Sync vs Controlled Global Sync

| Mode | Condition | Behavior | Trade-off |
| :--- | :--- | :--- | :--- |
| **Guild Dev Sync** | `GUILD_ID > 0` and valid (`<= MAX_SNOWFLAKE`) | `register_ic_commands(tree, guild=discord.Object(GUILD_ID), override=True)` + `await tree.sync(guild=...)` once in `setup_hook()` | Instant propagation in a single dev guild without touching global commands. |
| **Controlled Global Sync** | `GUILD_ID == 0` or invalid snowflake fallback | Global `register_ic_commands(tree, override=True)` + `await tree.sync()` once in `setup_hook()` | Global Discord propagation can take up to ~1 hour; running once in `setup_hook()` prevents rate-limit storms on WebSocket reconnects. |

Repeated `setup_hook()` calls after completion are idempotent no-ops, and `on_ready()` never re-initializes the DB, never clears/re-registers/re-syncs commands, and never spawns duplicate background tasks. Transient `discord.DiscordException` / `aiohttp.ClientError` / `TimeoutError` sync errors retain the in-memory command tree and single task set unless `raise_on_sync_error=True`.

## 5. Runtime Gateway Safeguard & Railway Production Note

To guarantee neither local developer shells nor CI runners accidentally connect to Discord:
- [`Settings.allow_gateway`](../../icbot/config.py) (`ICBOT_ALLOW_GATEWAY`) defaults to `false`.
- [`validate_gateway_launch()`](../../icbot/bot.py) refuses to call `bot.run()` unless `ICBOT_ALLOW_GATEWAY=true` or `--allow-gateway` is passed explicitly.
- `.env.example`, `Makefile` (`SAFE_ENV`), `.github/workflows/ci.yml`, `tools/smoke_offline.py`, `tools/scan_secrets.py`, and `tests/conftest.py` all default `ICBOT_ALLOW_GATEWAY` to `false`.
- **Railway Deployment Prerequisite:** Existing Railway `python main.py` startup conventions and deployment configs are intentionally unchanged. When production deployment is separately reviewed and approved, the Railway service environment variables must explicitly include `ICBOT_ALLOW_GATEWAY=true` (or invoke `python main.py --allow-gateway`).

