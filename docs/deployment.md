# Instigate Cafe Bot — Deployment, Troubleshooting & Recovery

**Supported Hosting Paths:** Self-Hosted Linux VPS (Docker Compose / `systemd`) & Managed Container Hosting (Railway)  
**Operational Posture:** Non-root execution (`UID 10001`), outbound-only networking (zero inbound ports), runtime secret injection, single-instance scheduler ownership, and SQLite WAL online backups.

---

## 1. Self-Hosted Deployment vs. Shared Public Instance

| Consideration | Self-Hosted Instance (Intended Use) | Shared Hosted Instance (Not Operated Here) |
| :--- | :--- | :--- |
| **How It Works** | Clone the repository, register your own Discord Application, and run a single container or service. | One operator hosts a multi-server instance with a shared invite link. |
| **Data Ownership** | SQLite database (`DB_PATH`) stays on your own host; you are the sole administrator and data controller. | Shared database across servers; central operator handles cross-server requests and API bills. |
| **Discord Verification** | Not required (`<100` servers). | Mandatory Discord Bot Verification at `>=100` servers. |
| **Privileged Intents** | **Zero privileged intents by default** (slash commands, `/ic resume`, and direct `@mention` replies use only unprivileged `guilds` + `messages`). Message Content Intent is requested *only* when both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` are set at process startup. | Discord restricts Message Content Intent at scale; run with `LLM_CHANNEL_HISTORY_ENABLED=false`. |

---

## 2. Single Active Scheduler Process Ownership

> [!CAUTION]
> **Do Not Run Multiple Replicas (`replicas: 1`):**  
> Never run more than **one active bot process** against the same SQLite database file.

Running multiple processes against one `DB_PATH` causes duplicate channel alerts, settlement races in casino/ICSE views, and SQLite `database is locked` or WAL corruption errors (especially on network shares like NFS/SMB). Always set `replicas: 1` and mount local SSD/block storage.

---

## 3. Discord OAuth2 Scopes, Permissions & Intents

1. **Required OAuth2 Scopes:** `bot`, `applications.commands`.
2. **Minimum Permission Bitmask:** **`277025507392`**
   - `Send Messages` (`0x00000800`) — command responses and alert feeds
   - `Embed Links` (`0x00004000`) — rich UI embeds
   - `Attach Files` (`0x00008000`) — `.ics`/`.csv` exam exports, grade charts, and issue reports
   - `Read Message History` (`0x00010000`) — interaction context and message updates
   - `Use Slash Commands` (`0x80000000`) — `/ic` application commands
   - `Add Reactions` (`0x00000040`) — interactive feedback cues
3. **Gateway Intents Policy:**
   - **Default (`0` privileged intents):** `CampusBot` requests only unprivileged `guilds` and `messages` (`guild_messages` + `dm_messages`). All 53 `/ic` slash commands, `/ic resume`, and direct `@mention` AI replies work with **Message Content Intent OFF**.
   - **Message Content Intent (Optional):** Requested at startup **only** when both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` are set so `@mention` replies can read recent channel context. Enable **Message Content Intent** in the Discord Developer Portal (*Application → Bot → Privileged Gateway Intents*) and restart the bot process. Guild overrides cannot enable this intent on a running process; if disabled in the Portal or at startup, channel-history lookup degrades cleanly while direct `@mention` replies continue working.
   - **Server Members & Presence Intents:** Not used (`false`).

---

## 4. Deployment Path 1: Docker & Docker Compose (Recommended)

### 4.1 Container Layout & Healthcheck Scope
[`Dockerfile`](../Dockerfile) builds a multi-stage, non-root image:
- **Build Stage:** Installs locked runtime dependencies (`uv.lock`) into `/app/.venv` using `ghcr.io/astral-sh/uv:0.13.9`.
- **Runtime Stage:** `python:3.12-slim-bookworm` running as non-root `campusbot:campusbot` (`UID/GID 10001:10001`) with `/data` owned by `10001:10001` (`0700`).
- **Storage Env:** `DB_PATH=/data/campus.db`, `GRADES_DB_PATH=/data/tmu_grades.db`.
- **No Inbound Ports:** All traffic is outbound WSS/HTTPS.
- **Graceful Shutdown:** `STOPSIGNAL SIGTERM` runs `CampusBot.close()`, stops `TaskSupervisor` loops, closes HTTP sessions, and checkpoints SQLite WAL (`stop_grace_period: 30s`).
- **Healthcheck Scope:** Verifies `.venv`, `Settings.load_validated()`, and write access to `DB_PATH`'s parent directory without network I/O (does not check live Discord gateway authentication).

### 4.2 Setup and Offline Container Smoke Verification
Deploy using [`docker-compose.yml`](../docker-compose.yml):

```bash
# 1. Create persistent storage directory owned by container UID/GID 10001:10001
mkdir -p data && chmod 700 data
sudo chown 10001:10001 data

# 2. Create private runtime environment file (NEVER commit .env)
cp .env.example .env
chmod 600 .env
# Edit .env to set DISCORD_TOKEN, GUILD_ID, and any optional provider keys

# 3. Optional tokenless throwaway-volume smoke check (verifies UID 10001 & WAL persistence)
THROW_VOL="$(mktemp -d)" && chmod 700 "$THROW_VOL" && sudo chown 10001:10001 "$THROW_VOL"
docker build --build-arg APP_VERSION=1.0.0 -t campus-bot:1.0.0 .

docker run --rm --network none \
  -e DB_PATH=/data/campus.db -e GRADES_DB_PATH=/data/tmu_grades.db \
  -e DISCORD_TOKEN="" -e ICBOT_ALLOW_GATEWAY=false -e JOBS_POLL_SECONDS=0 -e WORKDAY_POLL_SECONDS=0 \
  -v "$THROW_VOL:/data" --entrypoint python3 campus-bot:1.0.0 -c \
  "import asyncio, icbot.bot; from icbot.config import Settings; from icbot.db import Database; \
s = Settings.load_validated(); assert s.allow_gateway is False; \
db = Database(s.db_path); asyncio.run(db.init()); \
asyncio.run(db.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', ('smoke_boot', 'ok'))); \
asyncio.run(db.close())"

docker run --rm --network none \
  -e DB_PATH=/data/campus.db -e ICBOT_ALLOW_GATEWAY=false \
  -v "$THROW_VOL:/data" --entrypoint python3 campus-bot:1.0.0 -c \
  "import asyncio; from icbot.db import Database; \
db = Database('/data/campus.db'); asyncio.run(db.init()); \
row = asyncio.run(db.fetchone('SELECT value FROM meta WHERE key=?', ('smoke_boot',))); \
assert row == ('ok',), f'Persistence check failed: {row}'; asyncio.run(db.close())"
stat -c '%u:%g %a %n' "$THROW_VOL/campus.db" && rm -rf "$THROW_VOL"

# 4. Launch container for live operation
docker compose up -d --build
docker compose logs -f campus-bot
docker inspect --format='{{json .State.Health}}' campus-bot
```

---

## 5. Deployment Path 2: Native `systemd` Service (Linux VPS)

For a dedicated Ubuntu/Debian host without Docker, use [`systemd/campus-bot.service`](../systemd/campus-bot.service):

```bash
# 1. Create dedicated system user
sudo useradd -r -s /bin/false -U -m -d /opt/campus-bot campusbot

# 2. Clone repository and sync locked runtime dependencies
cd /opt/campus-bot
sudo -u campusbot git clone https://github.com/sukesan7/instigate-cafe-bot.git .
sudo -u campusbot uv venv --python 3.12
sudo -u campusbot uv sync --frozen --no-dev
sudo -u campusbot mkdir -p data && sudo chmod 700 /opt/campus-bot/data

# 3. Configure runtime secrets
sudo cp .env.example /opt/campus-bot/.env
sudo chown campusbot:campusbot /opt/campus-bot/.env
sudo chmod 600 /opt/campus-bot/.env

# 4. Install and start hardened unit
sudo cp systemd/campus-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now campus-bot.service
journalctl -u campus-bot.service -f
```

`systemd/campus-bot.service` enforces `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `ReadWritePaths=/opt/campus-bot/data`, `MemoryMax=512M`, and `CPUQuota=100%`.

---

## 6. Deployment Path 3: Railway Hosting & Manual `DB_PATH` Cutover

Check these settings manually in the **Railway Web Dashboard** when hosting on Railway:

| Railway UI Setting | Required State | Purpose |
| :--- | :--- | :--- |
| **Autodeploy** | **OFF** (manual deploys recommended) | Prevents unreviewed pushes from restarting an active bot. |
| **Start Command** | `python3 main.py --allow-gateway` | Satisfies the explicit gateway opt-in guard (`ICBOT_ALLOW_GATEWAY`). |
| **Replicas** | **`1`** | Mandatory single-scheduler and SQLite WAL lock invariant. |
| **Persistent Volume** | Mounted at **`/data`** | Writable by UID `10001` (`campusbot`) for `.db`, `-wal`, and `-shm`. |
| **Canonical Variables** | `DB_PATH=/data/campus.db`<br>`GRADES_DB_PATH=/data/tmu_grades.db`<br>`ICBOT_ALLOW_GATEWAY=true` | Replaces deprecated `SQLITE_PATH` so state never lands in ephemeral `/app`. |

### 6.1 Manual `DB_PATH` Cutover and Rollback
1. **Inspect existing volume files:** `ls -la /data /app/*.db 2>/dev/null || true`.
2. **Create a verified online hot backup before modifying variables:**
   ```bash
   python3 tools/backup_db.py --source <active_db_path> --dest /data/backups/pre_cutover_$(date +%Y%m%d_%H%M%S).db
   python3 tools/backup_db.py --verify /data/backups/pre_cutover_*.db
   ```
3. **Copy cleanly to `/data/campus.db`** if migrating from `/data/ic_stock.db` or `/app/ic_stock.db`:
   ```bash
   python3 tools/backup_db.py --source <active_db_path> --dest /data/campus.db
   python3 tools/backup_db.py --verify /data/campus.db
   ```
4. **Update Variables:** Set `DB_PATH=/data/campus.db` and `GRADES_DB_PATH=/data/tmu_grades.db`, and remove deprecated `SQLITE_PATH`.
5. **Verify post-deploy:** Confirm logs show `db=/data/campus.db`, zero `DeprecationWarning: SQLITE_PATH`, and `/ic status` reports Schema v7 (WAL).
6. **Rollback if needed:** Roll back the deployment snapshot, keep `DB_PATH=/data/campus.db`, and restore the pre-cutover backup via `tools/backup_db.py --restore`.

### 6.2 Safe Staging Isolation
When testing live Discord changes before updating your main server, use a separate staging Discord Application, a distinct non-production `DISCORD_TOKEN`, a private test guild/channel, and an isolated temporary `DB_PATH` outside Git (`ICBOT_ALLOW_GATEWAY=false` in `.env`, passing `--allow-gateway` only when running intentionally). Never run two bot processes simultaneously with the same `DISCORD_TOKEN`, and never copy production user databases into staging.

---

## 7. SQLite Online Hot Backups & Restore

In SQLite WAL mode, copying a live `.db` file with `cp` risks missing uncommitted `-wal` frames. Always use [`tools/backup_db.py`](../tools/backup_db.py) (which uses SQLite's online backup API / `VACUUM INTO` semantics + `PRAGMA integrity_check`):

### 7.1 Online Hot Backup (Zero Downtime)
```bash
# Snapshot while the bot is running
uv run python tools/backup_db.py --source data/campus.db --dest backups/campus_$(date +%Y%m%d_%H%M%S).db

# Verify snapshot integrity
uv run python tools/backup_db.py --verify backups/campus_20261119_230000.db
```

Example daily cron (`03:00` local time):
```cron
0 3 * * * /opt/campus-bot/.venv/bin/python /opt/campus-bot/tools/backup_db.py --source /opt/campus-bot/data/campus.db --dest /backups/campus_$(date +\%Y\%m\%d).db >> /var/log/campus-bot-backup.log 2>&1
```

### 7.2 Restore Procedure (Stop Service First)
```bash
# 1. Stop the running bot so no writer holds the WAL
docker compose stop campus-bot
# (or: sudo systemctl stop campus-bot.service)

# 2. Restore from verified backup (archives existing DB and removes stale -wal/-shm)
uv run python tools/backup_db.py --restore backups/campus_20261119_230000.db --dest data/campus.db

# 3. Restart and confirm health via /ic status
docker compose start campus-bot
# (or: sudo systemctl start campus-bot.service)
```

---

## 8. Credential Rotation & API Cost Controls

### 8.1 Rotating a Compromised Token or Key
1. **Revoke immediately** at the upstream portal (Discord Developer Portal → Reset Token; Google AI Studio → Delete API Key; Metrolinx / Odds API consoles).
2. **Update runtime secrets** (`.env` with `chmod 600` + `docker compose up -d --force-recreate`, `sudo systemctl restart campus-bot.service`, or Railway Variables + manual redeploy).
3. **Confirm clean boot** in logs (`RedactingLogFilter` ensures tokens never appear in logs).

### 8.2 API Cost Controls
- Set hard billing caps in Google Cloud / AI Studio before enabling `LLM_ENABLED=true`.
- Use `LLM_COOLDOWN_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_ALLOWED_CHANNEL_IDS`, `LLM_ALLOWED_ROLE_IDS`, and `/ic admin llm_stop` to control or immediately pause usage.
- Leave `ODDS_API_KEY` empty unless you want sports odds (free tier: 500 calls/month).

---

## 9. Pre-Publication Security, Leak Guard & Attribution

Before sharing a fork publicly, verify no secrets, SQLite files, or private lore are tracked:

```bash
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py
uv run python tools/check_command_docs.py
```

### 9.1 Pre-Publication Security and History Remediation
1. **Revoke and rotate** any exposed credential immediately (Section 8.1).
2. **Untrack local artifacts:** `git rm --cached <path>` and confirm `.gitignore` matches it.
3. **Verify clean tree:** `uv run python tools/scan_secrets.py --enforce-clean` exits `0`.
4. **Clean Git history before making a repo public:** A clean working tree or source archive does not scrub earlier commits; remove any historical `.db`, `.env`, or private `tools/lore.*.json` blobs (for example with `git filter-repo`) before changing a private repo to public.

### 9.2 Attribution Boundaries
- **Bot source code (`icbot/`, `tools/`, `main.py`):** [MIT License](../LICENSE) (Copyright © 2026 sukesan).
- **Brand identity ("Instigate Cafe" name, logos, banners):** Reserved by the Instigate Cafe community; self-hosters can customize branding in [Configuration Reference](configuration.md#4-branding-visual-theme-presets).
- **Community lore:** Only `tools/lore.example.json` is safe for public repos; private server lore stays out of Git.
- **Third-party content:** Student course notes belong to their authors (Jasper Cruz & Krish Patel); transit data follows the [Metrolinx Open Data Licence](https://www.metrolinx.com/en/about-us/open-data) and public TTC Mastodon posts.

---

## 10. Troubleshooting

### 10.1 Built-In Diagnostics (`/ic status`)
- **`/ic status`:** Shows bot version, Schema v7 WAL status, module readiness (`ready`, `disabled`, `degraded`, `missing_config`), task supervisor states, and channel permissions.
- **`/ic status issue_report:True`** *(requires Manage Server / Allowed Role)*: Attaches a redacted **`status_issue_report.md`** report safe to paste into bug reports (tokens, keys, filesystem paths, SQL params, and prompts are stripped automatically via [`icbot/errors.py`](../icbot/errors.py) and [`RedactingLogFilter`](../icbot/logging.py)).
- **`/ic botinfo`:** Displays active LLM configuration and privacy summaries.

### 10.2 Startup & Storage Errors
| Error / Symptom | Cause | Fix |
| :--- | :--- | :--- |
| **`Gateway launch disabled by runtime safeguard...`** | Expected default safety guard (`ICBOT_ALLOW_GATEWAY=false`). | Run `python main.py --allow-gateway` or set `ICBOT_ALLOW_GATEWAY=true` in `.env` / host environment. |
| **`Missing DISCORD_TOKEN in environment or .env`** | Gateway opt-in enabled without `DISCORD_TOKEN`. | Set `DISCORD_TOKEN` in `.env` (`chmod 600 .env`) or host secrets. Optional keys stay valid when blank. |
| **`DatabasePathError`** (`not writable by UID` or `Cannot create database parent directory`) | `prepare_sqlite_path()` refuses to silently fall back to `/app/ic_stock.db` when `DB_PATH` parent isn't writable (`UID 10001` in Docker, `campusbot` in systemd). | Docker: `mkdir -p data && chmod 700 data && sudo chown 10001:10001 data`. systemd: confirm `ReadWritePaths=/opt/campus-bot/data` and ownership. Railway: mount `/data` and set `DB_PATH=/data/campus.db`. |
| **`DeprecationWarning: SQLITE_PATH`** | Legacy alias still configured. | Replace `SQLITE_PATH` with `DB_PATH` (scheduled for removal in `v1.1.0`). |
| **`ConfigValidationError`** | Invalid int/channel/timezone/poll interval. | Check the named variable against [Configuration Reference](configuration.md). |
| **`MigrationError`** (`user_version` newer than `7`) | Newer SQLite file opened by older code (`Schema v7` is highest supported). | Upgrade runtime code or restore a matching backup with `tools/backup_db.py --restore`. |
| **`sqlite3.OperationalError: database is locked`** | Multiple processes sharing one DB, or DB placed on NFS/SMB. | Enforce `replicas: 1`, confirm local storage, and never copy live WAL files with `cp`. |

### 10.3 Discord Permissions, Commands & AI Context
| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `/ic` commands missing | Missing `applications.commands` scope or stale global cache | Re-invite with scopes `bot` + `applications.commands` (bitmask `277025507392`). For instant server updates, set `GUILD_ID` and boot once with `FORCE_GUILD_RESYNC=true`, then reset to `false`. |
| Alert channel stays silent | Missing `Send Messages` / `Embed Links`, or channel ID is `0` | Inspect channels via `/ic admin config` or `/ic status`. |
| `"This feature is disabled in this server"` | Disabled via `FEATURE_PROFILE` / `DISABLED_FEATURES` / guild setting | Enable the module via `/ic admin features` or `/ic admin config`. |
| `"Only the user who ran this command can interact..."` | Actor-bound button protection | Expected guard; run your own command for a personal view. |
| `@mention` skips channel history or raises `PrivilegedIntentsRequired` | `LLM_ENABLED=false`, `/ic admin llm_stop`, or `LLM_CHANNEL_HISTORY_ENABLED=true` without Message Content Intent in Developer Portal | Direct `@mention` replies need zero privileged intents. To enable `LLM_CHANNEL_HISTORY_ENABLED=true`, toggle Message Content Intent ON in the Portal and restart the bot. |

### 10.4 Failure Taxonomy, Circuit Breakers & Emergency Stop
All command handlers and pollers normalize failures into 12 typed families in [`icbot/errors.py`](../icbot/errors.py) (`validation`, `permission`, `setup_required`, `feature_disabled`, `missing_api_key`, `rate_limit`, `timeout`, `network`, `upstream_http`, `malformed_payload`, `storage`, `internal`). During sustained upstream outages:
- **`ProviderOutageTracker`** logs the first failure at `WARNING`, dampens repeated errors to `DEBUG`, and logs recovery at `INFO`.
- **`ProviderCircuitBreaker`** ([`icbot/services/http_transport.py`](../icbot/services/http_transport.py)) opens after consecutive failures for a cooldown window (`60s` default) and probes recovery in `HALF_OPEN` state.
- **Emergency AI Pause:** Run **`/ic admin llm_stop`** in Discord at any time to immediately halt all Gemini chat and resume calls without restarting (`meta.llm_killed=1`).
