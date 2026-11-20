# Instigate Cafe Bot — Production Hosting, Recovery & Operations Runbook

**Version:** 1.0.0 (Phase 12 Safe Deployment & Production Hosting)  
**Target Environments:** Self-Hosted Linux VPS (Docker Compose / systemd) & Managed Staging/Production (Railway)  
**Safety & Operational Posture:** Least-privilege non-root execution, strictly outbound networking, zero baked secrets, single-instance scheduler ownership.

---

## 1. Executive Summary & Supported Deployment Paths

The Instigate Cafe Bot is a stateful Discord bot designed for small-to-medium campus communities. It provides academic tooling, job alerts, transit updates, a virtual campus economy, and optional AI-assisted resume feedback.

Because the bot relies on SQLite with Write-Ahead Logging (WAL mode) and persistent background schedulers, it must **never** be run with multiple active replicas. Two production deployment models are supported:

| Deployment Method | Primary Use Case | Process Supervision | Secret Injection | Storage |
|---|---|---|---|---|
| **Docker Compose (Recommended)** | Self-hosted Linux VPS or home lab | Docker Engine (`unless-stopped`) | Runtime `.env` via `env_file` | Bind mount `./data:/data` |
| **systemd Unit (Alternative)** | Dedicated Ubuntu/Debian Linux VPS | systemd service manager | `EnvironmentFile=/opt/campus-bot/.env` | Local directory `/opt/campus-bot/data` |
| **Railway (Managed Hosting)** | Reconciled cloud production service | Railway Container Platform | Railway Dashboard Environment Variables | Railway Persistent Volume at `/data` |

---

## 2. Public GitHub Release vs. Maintainer-Hosted Public Invite

When sharing or distributing the Instigate Cafe Bot, maintainers and community members must distinguish between two fundamentally different operational models:

### 2.1 Model A: Public GitHub Source Release (Self-Hosted Model)
- **Concept:** Contributors and other student clubs clone this repository, configure their own Discord Application in the Discord Developer Portal, and self-host the bot for their specific server.
- **Data Boundary:** Complete data isolation. The club retains its own SQLite database, user balances, and local configuration.
- **Secrets & API Limits:** The self-hoster provides their own Discord Bot Token, Google Gemini API key, and optional transit/odds tokens. No API quotas or costs impact the upstream maintainer.
- **Discord Approval:** No Discord Verification or Privileged Intent approval is needed, because each self-hosted instance serves fewer than 100 guilds.

### 2.2 Model B: Maintainer-Hosted Public Bot (Centralized Invite Model)
- **Concept:** The upstream maintainer hosts a single running bot process and publishes an OAuth2 invite link allowing arbitrary Discord servers to add the bot.
- **Architectural Requirements:**
  1. **Discord Bot Verification:** Mandatory once the bot reaches 100 guilds.
  2. **Message Content Intent Approval:** Discord strictly restricts the Message Content Intent for bots in >100 guilds. To remain viable as a public bot without privileged intent review, the bot must operate **exclusively via Slash Commands (`/ic`)**, with `@mention` assistant features disabled or restricted.
  3. **Multi-Tenant Data Isolation:** Ensure guild settings, economy balances, and admin features are strictly partitioned by `guild_id` to prevent cross-server data leakage.
  4. **Quota & Cost Alarms:** Maintainer absorbs all external API costs (Gemini token usage, transit proxies, hosting fees). Rate limits must be enforced on a per-guild basis.

---

## 3. Supported Deployment Path 1: Docker & Docker Compose (Recommended)

### 3.1 Dockerfile Architecture
The project provides a multi-stage, non-root [`Dockerfile`](file:///home/sukesan/dev/discord/campus-bot/Dockerfile):
- **Build Stage:** Uses `ghcr.io/astral-sh/uv` to install dependencies into an isolated virtual environment (`/app/.venv`) directly from `uv.lock`.
- **Runtime Stage:** Runs on `python:3.12-slim-bookworm` with a dedicated non-root user and group (`campusbot:campusbot`, UID/GID `10001:10001`).
- **Zero Inbound Ports:** No `EXPOSE` instruction is used. Discord bots connect outbound over secure WebSockets (WSS) and HTTPS; opening inbound ports increases the attack surface with zero operational benefit.
- **Signal Handling:** Configured with `STOPSIGNAL SIGTERM` to invoke `CampusBot.close()` and flush SQLite WAL pages.

### 3.2 Docker Compose Recipe
Deploy using [`docker-compose.yml`](file:///home/sukesan/dev/discord/campus-bot/docker-compose.yml):

```yaml
services:
  campus-bot:
    build:
      context: .
      dockerfile: Dockerfile
    image: campus-bot:0.2.4
    container_name: campus-bot
    restart: unless-stopped
    env_file:
      - .env
    environment:
      - SQLITE_PATH=/data/campus.db
      - ICBOT_ALLOW_GATEWAY=true
      - PYTHONUNBUFFERED=1
    volumes:
      - ./data:/data
    deploy:
      resources:
        limits:
          cpus: "1.00"
          memory: 512M
        reservations:
          cpus: "0.20"
          memory: 128M
    stop_signal: SIGTERM
    stop_grace_period: 30s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### 3.3 Quickstart Commands
```bash
# 1. Create persistent storage directory
mkdir -p data && chmod 700 data

# 2. Configure environment file (chmod 600)
cp .env.example .env
chmod 600 .env
# Edit .env with your DISCORD_TOKEN and GUILD_ID

# 3. Build and launch container in detached mode
docker compose up -d --build

# 4. View container startup logs
docker compose logs -f campus-bot

# 5. Check container health status
docker inspect --format='{{json .State.Health}}' campus-bot
```

---

## 4. Supported Deployment Path 2: Native systemd Service (Linux VPS)

For operators preferring a direct system service without Docker:

### 4.1 Service Unit Setup
1. Create a dedicated system user:
   ```bash
   sudo useradd -r -s /bin/false -U -m -d /opt/campus-bot campusbot
   ```
2. Clone repository and install dependencies into `/opt/campus-bot`:
   ```bash
   cd /opt/campus-bot
   sudo -u campusbot git clone <repo_url> .
   sudo -u campusbot uv sync --frozen --no-dev
   sudo -u campusbot mkdir -p data
   ```
3. Secure the environment file:
   ```bash
   sudo cp .env.example /opt/campus-bot/.env
   sudo chown campusbot:campusbot /opt/campus-bot/.env
   sudo chmod 600 /opt/campus-bot/.env
   ```
4. Copy and enable the systemd unit:
   ```bash
   sudo cp systemd/campus-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now campus-bot.service
   ```
5. Monitor logs via journald:
   ```bash
   journalctl -u campus-bot.service -f
   ```

### 4.2 Security Hardening Directives in Unit
[`systemd/campus-bot.service`](file:///home/sukesan/dev/discord/campus-bot/systemd/campus-bot.service) enforces Linux kernel security sandboxing:
- `NoNewPrivileges=true` prevents privilege escalation.
- `ProtectSystem=strict` makes the root filesystem read-only.
- `ProtectHome=true` hides `/home` and `/root`.
- `PrivateTmp=true` isolates `/tmp`.
- `ReadWritePaths=/opt/campus-bot/data` restricts disk writes exclusively to the SQLite directory.
- `MemoryMax=512M` and `CPUQuota=100%` prevent CPU or memory starvation of host services.

---

## 5. Railway Production Service Reconciliation & Staging Architecture

### 5.1 Discovered Railway Production Baseline
The pre-existing Railway deployment operates under the following baseline:
- **Repository Branch:** `main`
- **Autodeploy Toggle:** **MUST REMAIN DISABLED.** Historically, every push to `main` triggered an immediate rebuild and restart of the live bot. During modernization, autodeploy was disconnected to prevent unintended execution of unverified code.
- **Start Command:** `python main.py --allow-gateway` (or Nixpacks auto-detected Python start).
- **Persistence Mount:** Railway Persistent Volume attached at `/data` (configured via `SQLITE_PATH=/data/campus.db`).
- **Single Scheduler Invariant:** Railway service must be configured with **Replica Count: 1**. Never increase replica count.

### 5.2 Isolated Staging Environment
To test new features without risking production state or disturbing community members:
1. **Separate Discord Application:** Create an independent application in the Discord Developer Portal (e.g. `Instigate Cafe Staging`).
2. **Dedicated Staging Token:** Never reuse the production `DISCORD_TOKEN`.
3. **Dedicated Staging Guild:** Install the staging bot exclusively in a private test guild (`GUILD_ID=...`).
4. **Independent Database Volume:** Use a clean, freshly migrated SQLite database. **Never clone the production SQLite database** (which contains real user balances, economy transactions, and server lore) into staging.
5. **Separate Provider Credentials:** Use free/sandbox API keys for external services where available.

### 5.3 Release Approval Checklist for Railway Deployments
Before triggering a manual deployment on Railway:
- [ ] 1. All unit, integration, and benchmark tests pass offline (`uv run pytest`).
- [ ] 2. Secret scanner returns clean exit code (`uv run python tools/scan_secrets.py --enforce-clean`).
- [ ] 3. No `.db`, `.env`, or `.sqlite-wal` files are tracked in git index.
- [ ] 4. Staging lifecycle smoke check passes (`uv run python tools/smoke_staging.py`).
- [ ] 5. Existing production SQLite database has been backed up using [`tools/backup_db.py`](file:///home/sukesan/dev/discord/campus-bot/tools/backup_db.py).
- [ ] 6. Explicit written approval from the repository owner (`sukesan`) is documented.

---

## 6. Discord OAuth2 Installation Scopes & Privileged Intents

### 6.1 OAuth2 Scopes
When generating the bot invite URL in the Discord Developer Portal:
- `bot` (Mandatory: connects the bot user to the guild).
- `applications.commands` (Mandatory: registers `/ic` slash commands).

### 6.2 Permission Integer Bitmask
The minimum required permissions bitmask is **`277025507392`**:
- **Send Messages** (`0x00000800`): Required for command responses and alert feeds.
- **Embed Links** (`0x00004000`): Required for rich UI embeds.
- **Attach Files** (`0x00008000`): Required for iCalendar exam schedules, charts, and issue reports.
- **Read Message History** (`0x00010000`): Required for command interaction tracking and cleanup.
- **Use Slash Commands** (`0x80000000`): Required for modern application command execution.
- **Add Reactions** (`0x00000040`): Required for interactive pagination and confirmation cues.

### 6.3 Privileged Gateway Intents Analysis
- **Message Content Intent:**
  - **Slash Commands (`/ic`):** Do **NOT** require Message Content Intent. Discord delivers command arguments directly to the application.
  - **Mention Assistant (`@CampusBot`):** Requires Message Content Intent to read text content when mentioned in a channel.
  - **Recommendation:** Self-hosters who do not use `@CampusBot` mention assistance should leave the Message Content Intent **disabled** in the Developer Portal for maximum privacy and security.
- **Server Members Intent:** **NOT REQUIRED.** The bot resolves user info on-demand through interactions.
- **Presence Intent:** **NOT REQUIRED.** The bot does not track user online/offline status.

---

## 7. Operational Procedures & Runbooks

### 7.1 Graceful Shutdown & Process Signals
The bot handles process signals (`SIGTERM`, `SIGINT`) gracefully:
1. `CampusBot.close()` is invoked via Discord.py signal listeners.
2. The `TaskSupervisor` cancels all 8 background scheduler loops (`supervisor.stop_all()`).
3. Outbound HTTP client sessions (`aiohttp.ClientSession`) are cleanly closed.
4. SQLite WAL journal pages are flushed to the main `.db` file and the database connection is closed.
5. In Docker, `stop_grace_period: 30s` guarantees that in-flight requests finish before `SIGKILL` is issued.

### 7.2 SQLite Online Hot Backup & Restore Runbook
SQLite databases in WAL mode must not be copied with standard `cp` while running, as active writes reside in `.sqlite-wal`. Use the project's native hot backup tool [`tools/backup_db.py`](file:///home/sukesan/dev/discord/campus-bot/tools/backup_db.py):

#### Performing a Backup:
```bash
# Online hot backup (zero downtime, non-blocking)
uv run python tools/backup_db.py --source data/campus.db --dest backups/campus_$(date +%Y%m%d_%H%M%S).db
```

#### Verifying Backup Integrity:
```bash
uv run python tools/backup_db.py --verify backups/campus_20261119_230000.db
```

#### Restoring from Backup:
```bash
# 1. Stop the bot service
docker compose stop campus-bot
# (or sudo systemctl stop campus-bot)

# 2. Restore database (automatically archives existing DB to .bak)
uv run python tools/backup_db.py --restore backups/campus_20261119_230000.db --dest data/campus.db

# 3. Restart the bot service
docker compose start campus-bot
```

### 7.3 Emergency Credential & Token Rotation
If a Discord bot token, Gemini API key, or third-party token is compromised:

1. **Invalidate Credential Immediately:**
   - Discord Token: Visit Discord Developer Portal &rarr; Bot &rarr; Click **"Reset Token"**.
   - Gemini Key: Visit Google AI Studio &rarr; API Keys &rarr; Click **"Delete"** and generate a new key.
   - Metrolinx / Odds / AlphaVantage: Revoke keys in their respective provider consoles.
2. **Update Secrets in Hosting Environment:**
   - **Docker:** Update `.env`, run `chmod 600 .env`, and restart:
     ```bash
     docker compose up -d --force-recreate
     ```
   - **systemd:** Update `/opt/campus-bot/.env` and restart:
     ```bash
     sudo systemctl restart campus-bot.service
     ```
   - **Railway:** Update variables in the Railway Web Dashboard and trigger a manual redeploy.
3. **Verify Clean Startup:**
   - Inspect startup logs to ensure the bot connects with the new token and reports no `401 Unauthorized` errors.

### 7.4 Rollback Procedure
If a newly deployed version introduces regressions:

1. **Docker Compose Rollback:**
   ```bash
   # Revert code to the previous stable release tag
   git checkout v0.2.3
   # Rebuild and restart
   docker compose up -d --build
   ```
2. **Database Rollback:**
   - If schema migrations took place, restore the pre-deployment database backup using `python tools/backup_db.py --restore <backup_path> --dest data/campus.db`.

### 7.5 API Cost Controls & Rate Limits
- **Gemini API:** Configure a spending cap in Google Cloud Console. Set `LLM_BUDGET_HOURLY` in bot configuration to throttle AI requests when community usage spikes.
- **AlphaVantage / Yahoo Finance:** Stock quotes are cached in-memory with configurable TTL to avoid exceeding free-tier call budgets (5 calls/min).
- **Transit APIs:** Mastodon TTC and Metrolinx alerts poll on fixed, jittered intervals (e.g. 60–120s) with circuit breakers to prevent request flooding during upstream outages.

---

## 8. Verification & Staging Smoke Check

Before deploying to staging or production, execute the offline staging smoke check:

```bash
uv run python tools/smoke_staging.py
```

### Verified vs. Unverified Boundaries

| Verification Boundary | Verification Status | Details |
|---|---|---|
| **Non-root Container Execution** | :white_check_mark: Verified | Dockerfile specifies `USER campusbot` (UID 10001). |
| **Outbound-Only Networking** | :white_check_mark: Verified | Zero inbound ports exposed; no open listening sockets. |
| **Runtime Secret Injection** | :white_check_mark: Verified | Secrets injected via `.env` file; excluded via `.dockerignore`. |
| **SQLite WAL Mode & Migration** | :white_check_mark: Verified | Verified via `smoke_staging.py` (v7 WAL). |
| **Graceful Shutdown & Signal Handling** | :white_check_mark: Verified | `_cleanup_runtime()` cancels tasks, closes HTTP and DB cleanly. |
| **Online Hot Backup / Restore** | :white_check_mark: Verified | `tools/backup_db.py` uses SQLite online backup API with integrity checks. |
| **Live Discord Gateway Connection** | :lock: Explicitly Unverified | Prohibited by `AGENTS.md` safeguard. Must be tested on staging by owner. |
| **Third-Party Live API Scraping** | :lock: Explicitly Unverified | Mocked offline to prevent unauthorized upstream traffic. |

