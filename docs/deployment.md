# Instigate Cafe Bot — Deployment, Hosting & Disaster Recovery

**Target Environments:** Self-Hosted Linux VPS (Docker Compose / `systemd`) & Managed Hosting (Railway)  
**Operational Posture:** Non-root execution (`UID 10001`), outbound-only networking (zero inbound ports), runtime secret injection, single-instance scheduler ownership, and SQLite WAL online backups.

---

## 1. Deployment Models: Self-Hosted vs. Hosted Public Invite

Before deploying or sharing the bot, distinguish clearly between the two operating models:

| Consideration | Model A: Self-Hosted Open-Source Bot (Recommended) | Model B: Maintainer-Hosted Public Invite |
| :--- | :--- | :--- |
| **How It Works** | Each club/server clones this repo, creates its own Discord Application, and runs its own container/service. | One central operator hosts a shared multi-tenant instance and publishes an OAuth2 invite URL. |
| **Data Ownership & Controller** | 100% local sovereignty; SQLite database (`DB_PATH`) stays on the server's own disk. Self-hoster is the sole Data Controller. | Shared database across guilds; central maintainer is the Data Controller and handles cross-guild deletion requests. |
| **Discord Verification** | Not required (`<100` servers). | Mandatory Discord Bot Verification at `>=100` servers. |
| **Privileged Intents** | **Zero privileged intents by default** (slash commands, `/ic resume`, and direct `@mention` replies use only unprivileged `guilds` + `messages`). Message Content Intent is requested *only* when both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` are set at process startup. | Discord restricts Message Content Intent at scale; run with `LLM_CHANNEL_HISTORY_ENABLED=false` (zero privileged intents). |
| **API Keys & Quotas** | Self-hoster supplies optional keys (`GEMINI_API_KEY`, `GO_API_KEY`, `ODDS_API_KEY`) under their own billing caps. | Maintainer absorbs all API costs and must enforce strict per-guild rate limits. |

---

## 2. Single Active Scheduler Process Ownership

> [!CAUTION]
> **Horizontal Autoscaling Is Prohibited:**  
> Never run more than **one active replica (`replicas: 1`)** against the same SQLite database.

Running multiple replicas simultaneously causes:
1. **Duplicate channel alerts** from concurrent transit, job, and market pollers.
2. **Double-spend / settlement races** in casino views and virtual market trades.
3. **SQLite `database is locked` errors** or WAL corruption (especially across network filesystems such as NFS/SMB).

Always pin `replicas: 1` in Docker Compose and Railway, and mount local SSD/block storage rather than shared network mounts.

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
3. **Privileged Gateway Intents (Least-Privilege Startup Policy):**
   - **Default (`0` privileged intents):** `CampusBot` requests only unprivileged `guilds` and `messages` (`guild_messages` + `dm_messages`) intents at startup. All 53 `/ic` slash commands, `/ic resume`, and direct `@mention` AI replies work with **Message Content Intent OFF** (Discord delivers content on messages directly mentioning the bot or in DMs without the privileged bit).
   - **Message Content Intent (Optional):** Requested at process startup **only** when the operator explicitly sets both `LLM_ENABLED=true` and `LLM_CHANNEL_HISTORY_ENABLED=true` to read surrounding channel history. Enabling this requires toggling **Message Content Intent ON** in the Discord Developer Portal (*Application → Bot → Privileged Gateway Intents*) and **restarting the bot process**. Per-guild database overrides cannot request privileged intents at runtime; if a guild enables channel history on a process started without `MESSAGE_CONTENT`, channel history degrades cleanly while direct `@mention` replies continue working.
   - **Server Members Intent & Presence Intent:** **Not required** (always disabled by default).

---

## 4. Deployment Path 1: Docker & Docker Compose (Recommended)

### 4.1 Container Architecture & Honest Healthcheck Boundary
[`Dockerfile`](../Dockerfile) builds a multi-stage, non-root image:
- **Build Stage:** Installs locked runtime dependencies into `/app/.venv` from `uv.lock` using `ghcr.io/astral-sh/uv`.
- **Runtime Stage:** `python:3.12-slim-bookworm` running as non-root `campusbot:campusbot` (`UID/GID 10001:10001`) with `/data` owned by `10001:10001` (`mode 0700`, never `0777`).
- **Canonical Storage Env:** `DB_PATH=/data/campus.db`, `GRADES_DB_PATH=/data/tmu_grades.db`.
- **Zero Inbound Ports:** No `EXPOSE` directive; Discord gateway and API calls are strictly outbound WSS/HTTPS.
- **Graceful Shutdown:** `STOPSIGNAL SIGTERM` invokes `CampusBot.close()`, cancels all 8 `TaskSupervisor` loops, closes `aiohttp` sessions, and checkpoints SQLite WAL cleanly (`stop_grace_period: 30s`).
- **Honest Local Readiness Healthcheck:** Verifies the `.venv` runtime, strict `Settings.load_validated()` parsing, and write access to `DB_PATH`'s parent directory without requiring a live `DISCORD_TOKEN` or making outbound network requests. It proves local container configuration/storage readiness—it does *not* prove live Discord gateway authentication.

### 4.2 Setup and Offline Container Verification Commands
Deploy using [`docker-compose.yml`](../docker-compose.yml):

```bash
# 1. Create persistent storage directory owned by container UID/GID 10001:10001
mkdir -p data && chmod 700 data
sudo chown 10001:10001 data

# 2. Create runtime environment file (never commit .env)
cp .env.example .env
chmod 600 .env
# Edit .env to set DISCORD_TOKEN, GUILD_ID, and any optional provider keys

# 3. Optional offline container build & mounted-volume smoke test (zero Discord connection)
docker build --build-arg APP_VERSION=1.0.0-rc1 -t campus-bot:1.0.0-rc1 .
docker run --rm --network none \
  -e DB_PATH=/data/campus.db \
  -e GRADES_DB_PATH=/data/tmu_grades.db \
  -e ICBOT_ALLOW_GATEWAY=false \
  -v "$(pwd)/data:/data" \
  --entrypoint python3 \
  campus-bot:1.0.0-rc1 \
  tools/smoke_staging.py

# 4. Launch live container when ready for Discord gateway connection
docker compose up -d --build
docker compose logs -f campus-bot
docker inspect --format='{{json .State.Health}}' campus-bot
```

---

## 5. Deployment Path 2: Native `systemd` Service (Linux VPS)

For a dedicated Ubuntu/Debian host without Docker, use [`systemd/campus-bot.service`](../systemd/campus-bot.service):

```bash
# 1. Provision dedicated system user
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

## 6. Deployment Path 3: Railway Production Reconciliation & Cutover

Because production Railway deployments must never be modified automatically by scripts or CI, confirm these settings manually in the **Railway Web Dashboard**:

| Railway UI Setting | Required State | Purpose |
| :--- | :--- | :--- |
| **Autodeploy** | **OFF (Disabled)** — manual deploys only | Prevents unverified Git pushes from restarting production (note: disabling autodeploy does not stop an already active service). |
| **Release Trigger** | **Manual Deploy Only** | Deploy only after offline tests, secret scans, and a verified backup pass. |
| **Start Command** | `python3 main.py --allow-gateway` | Satisfies the explicit gateway opt-in guard (`ICBOT_ALLOW_GATEWAY`). |
| **Replicas** | **`1`** | Mandatory single-scheduler and SQLite WAL safety invariant. |
| **Persistent Volume** | Mounted at **`/data`** | Writable by UID `10001` (`campusbot`) for `.db`, `-wal`, and `-shm`. |
| **Canonical Variables** | `DB_PATH=/data/campus.db`<br>`GRADES_DB_PATH=/data/tmu_grades.db`<br>`ICBOT_ALLOW_GATEWAY=true` | Replaces deprecated `SQLITE_PATH` so state never lands in ephemeral `/app/ic_stock.db`. |

### 6.1 Manual Railway `DB_PATH` Cutover and Rollback
1. **Inspect existing volume files** (without launching a second replica): `ls -la /data /app/*.db 2>/dev/null || true`.
2. **Take a verified online hot backup** of the authoritative DB file before changing variables:
   ```bash
   python3 tools/backup_db.py --source <active_db_path> --dest /data/backups/pre_cutover_$(date +%Y%m%d_%H%M%S).db
   python3 tools/backup_db.py --verify /data/backups/pre_cutover_*.db
   ```
3. **Copy cleanly to `/data/campus.db`** if migrating from `/data/ic_stock.db` or `/app/ic_stock.db`:
   ```bash
   python3 tools/backup_db.py --source <active_db_path> --dest /data/campus.db
   python3 tools/backup_db.py --verify /data/campus.db
   ```
4. **Update Railway Variables** to set `DB_PATH=/data/campus.db` and `GRADES_DB_PATH=/data/tmu_grades.db`, and remove deprecated `SQLITE_PATH`.
5. **Verify post-deploy:** Confirm logs show `db=/data/campus.db`, zero `DeprecationWarning: SQLITE_PATH`, and `/ic status` reports Schema v7 (WAL).
6. **Rollback if needed:** Roll back the Railway deployment snapshot, keep `DB_PATH=/data/campus.db` set, and restore the pre-cutover backup via `tools/backup_db.py --restore`.

### 6.2 Isolated Staging Environment
Always test live changes in a separate Discord Application, separate `DISCORD_TOKEN`, private staging `GUILD_ID`, and a freshly initialized empty SQLite volume. **Never clone a production SQLite database into staging.**

---

## 7. SQLite Online Hot Backups & Disaster Recovery Drill

In SQLite WAL mode, copying a live `.db` file with `cp` can produce a corrupt snapshot because recent commits reside in `-wal`. Always use SQLite's online backup API via [`tools/backup_db.py`](../tools/backup_db.py) (or SQLite's `VACUUM INTO 'backup.db'` statement).

### 7.1 Online Hot Backup & Verification (Zero Downtime)
```bash
# Create consistent point-in-time snapshot (runs sqlite3.connect().backup() + PRAGMA integrity_check)
uv run python tools/backup_db.py --source data/campus.db --dest backups/campus_$(date +%Y%m%d_%H%M%S).db

# Verify any snapshot independently (must report integrity ok)
uv run python tools/backup_db.py --verify backups/campus_20261119_230000.db
```

Example daily host cron entry (`03:00` local time):
```cron
0 3 * * * /opt/campus-bot/.venv/bin/python /opt/campus-bot/tools/backup_db.py --source /opt/campus-bot/data/campus.db --dest /backups/campus_$(date +\%Y\%m\%d).db >> /var/log/campus-bot-backup.log 2>&1
```

### 7.2 Restore Procedure (Requires Service Stop)
```bash
# 1. Stop the running bot so no active writer holds the WAL
docker compose stop campus-bot
# (or: sudo systemctl stop campus-bot.service)

# 2. Restore from verified backup (archives existing DB to .bak.<ts> and clears stale -wal/-shm)
uv run python tools/backup_db.py --restore backups/campus_20261119_230000.db --dest data/campus.db

# 3. Restart the service and confirm health via /ic status
docker compose start campus-bot
# (or: sudo systemctl start campus-bot.service)
```

---

## 8. Credential Rotation & Cost Controls

### 8.1 Emergency Token and Key Rotation
1. **Revoke immediately** in the upstream portal (Discord Developer Portal → Reset Token; Google AI Studio → Delete API Key; Metrolinx / Odds API consoles).
2. **Update runtime secrets** (`.env` with `chmod 600` + `docker compose up -d --force-recreate`, `sudo systemctl restart campus-bot.service`, or Railway Variables + manual redeploy).
3. **Confirm clean boot** in logs (no `401 Unauthorized` errors; redaction filter ensures tokens never appear in output).

### 8.2 API Cost Controls
- Set billing hard caps in Google Cloud / AI Studio before enabling `LLM_ENABLED=true`.
- Use `LLM_COOLDOWN_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_ALLOWED_CHANNEL_IDS`, `LLM_ALLOWED_ROLE_IDS`, and the instant `/ic admin llm_stop` killswitch to bound usage.
- Keep `ODDS_API_KEY` empty unless you specifically need sports odds (free tier: 500 calls/month).

---

## 9. Pre-Publication Security, Leak Guard & Attribution Checklist

Before publishing a fork or triggering a production release, verify every offline gate (and note that publishing open-source code and operating a hosted public bot instance are separate maintainer decisions):

```bash
uv run python tools/scan_secrets.py --enforce-clean
uv run python tools/check_docs.py
uv run python tools/smoke_offline.py
uv run python tools/smoke_staging.py
uv run pytest
```

### 9.1 Pre-Publication Security and History Remediation
If a forbidden artifact (`.env*`, `*.db`, `*-wal`, `*-shm`, private `lore.*.json`) is ever staged or committed:
1. **Revoke and rotate** any exposed credential immediately (see Section 8.1).
2. **Untrack without deleting local data:** `git rm --cached <path>` and ensure `.gitignore` covers the pattern.
3. **Verify clean tree:** `uv run python tools/scan_secrets.py --enforce-clean` must exit `0`.
4. **Coordinate history remediation** per [`SECURITY.md`](../SECURITY.md) before making any private repository public.

### 9.2 Intellectual Property & Attribution Boundaries
- **Bot source code (`icbot/`, `tools/`, `main.py`):** [MIT License](../LICENSE) (Copyright © 2026 sukesan).
- **Brand identity ("Instigate Cafe" name, logos, banners):** Reserved by the Instigate Cafe community; self-hosters should configure custom branding via [Configuration Reference](configuration.md#4-branding-visual-theme-presets).
- **Community lore:** Only `tools/lore.example.json` is publishable; private server lore must stay out of Git.
- **Third-party content:** Course notes remain the copyright of their student authors (Jasper Cruz & Krish Patel); transit feeds follow the [Metrolinx Open Data Licence](https://www.metrolinx.com/en/about-us/open-data) and public TTC Mastodon posts; grade distributions (`tmu_grades.db`) must never be committed to open source.
