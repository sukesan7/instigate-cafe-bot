"""Unit tests for production hosting configurations, Dockerfile security, systemd, and backup safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from tools.backup_db import backup_database, restore_database, verify_sqlite_integrity
from tools.smoke_staging import run_staging_smoke

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_dockerfile_security_and_non_root():
    """Verify Dockerfile enforces non-root execution, signal handling, and zero inbound ports."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    assert dockerfile_path.exists(), "Missing Dockerfile"

    content = dockerfile_path.read_text(encoding="utf-8")

    # 1. Non-root user configuration
    assert "useradd" in content or "USER campusbot" in content, "Dockerfile must configure non-root user"
    assert "USER campusbot" in content, "Dockerfile runtime stage must switch to non-root user"

    # 2. Process signal handling
    assert "STOPSIGNAL SIGTERM" in content, "Dockerfile must define STOPSIGNAL SIGTERM"

    # 3. Outbound only (no exposed inbound ports)
    for line in content.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("EXPOSE"):
            pytest.fail(f"Gateway-only bot must not expose inbound ports: {clean_line}")

    # 4. Zero baked secrets
    assert "DISCORD_TOKEN=" not in content, "Secrets must not be hardcoded in Dockerfile"
    assert "GEMINI_API_KEY=" not in content, "API keys must not be hardcoded in Dockerfile"


def test_docker_compose_configuration_and_quotas():
    """Verify docker-compose.yml defines resource quotas, log rotation, and no exposed ports."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.exists(), "Missing docker-compose.yml"

    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "campus-bot" in services, "Missing campus-bot service in docker-compose.yml"
    bot_cfg = services["campus-bot"]

    # 1. Restart policy
    assert bot_cfg.get("restart") == "unless-stopped"

    # 2. Secret injection via .env file
    env_files = bot_cfg.get("env_file", [])
    assert ".env" in env_files, "Compose service must load secrets via env_file (.env)"

    # 3. Storage persistence
    volumes = bot_cfg.get("volumes", [])
    assert any("./data:/data" in v or "/data" in v for v in volumes), (
        "Missing persistent volume mount for /data"
    )

    # 4. Log rotation
    logging_cfg = bot_cfg.get("logging", {})
    assert logging_cfg.get("driver") == "json-file"
    assert "max-size" in logging_cfg.get("options", {})
    assert "max-file" in logging_cfg.get("options", {})

    # 5. Resource quotas
    resources = bot_cfg.get("deploy", {}).get("resources", {})
    limits = resources.get("limits", {})
    assert "memory" in limits, "Compose service must define memory limits"
    assert "cpus" in limits, "Compose service must define CPU limits"

    # 6. Process lifecycle
    assert bot_cfg.get("stop_signal") == "SIGTERM"
    assert bot_cfg.get("stop_grace_period") in ("30s", 30)

    # 7. No exposed ports (outbound only)
    assert "ports" not in bot_cfg, "Gateway-only bot must not expose inbound ports in docker-compose.yml"


def test_systemd_service_unit_sandboxing():
    """Verify systemd service unit contains Linux sandboxing, signal handling, and resource quotas."""
    unit_path = REPO_ROOT / "systemd" / "campus-bot.service"
    assert unit_path.exists(), "Missing systemd/campus-bot.service"

    content = unit_path.read_text(encoding="utf-8")

    # 1. Non-root user
    assert "User=campusbot" in content
    assert "Group=campusbot" in content

    # 2. Security hardening and sandboxing directives
    assert "NoNewPrivileges=true" in content
    assert "ProtectSystem=strict" in content
    assert "ProtectHome=true" in content
    assert "PrivateTmp=true" in content
    assert "ReadWritePaths=/opt/campus-bot/data" in content

    # 3. Process lifecycle
    assert "KillSignal=SIGTERM" in content
    assert "TimeoutStopSec=30" in content
    assert "Restart=on-failure" in content

    # 4. Resource quotas
    assert "MemoryMax=512M" in content
    assert "CPUQuota=100%" in content


def test_ci_does_not_autostart_docker_or_systemd():
    """Verify CI workflows never auto-start Docker containers or systemd services."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists()
    content = ci_path.read_text(encoding="utf-8")

    assert "docker run" not in content, "CI must not execute docker run"
    assert "docker compose up" not in content, "CI must not execute docker compose up"
    assert "systemctl" not in content, "CI must not execute systemctl"


def test_sqlite_online_hot_backup_and_restore(tmp_path: Path):
    """Verify online hot backup and restore with SQLite integrity validation in WAL mode."""
    src_db = tmp_path / "live.db"
    backup_db = tmp_path / "backup.db"
    restored_db = tmp_path / "restored.db"

    # 1. Initialize source DB with WAL mode and sample records
    conn = sqlite3.connect(str(src_db))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER);")
    conn.execute("INSERT INTO users VALUES (101, 5000), (102, 12000);")
    conn.commit()
    conn.close()

    # 2. Perform online hot backup
    assert backup_database(src_db, backup_db) is True
    assert backup_db.exists()

    # 3. Verify backup integrity
    ok, msg = verify_sqlite_integrity(backup_db)
    assert ok is True
    assert msg == "ok"

    # 4. Perform restore
    assert restore_database(backup_db, restored_db) is True
    assert restored_db.exists()

    # 5. Verify restored data matches
    res_conn = sqlite3.connect(str(restored_db))
    rows = res_conn.execute("SELECT user_id, balance FROM users ORDER BY user_id;").fetchall()
    res_conn.close()

    assert rows == [(101, 5000), (102, 12000)]


@pytest.mark.asyncio
async def test_staging_smoke_lifecycle_offline():
    """Verify that smoke_staging executes completely offline with clean shutdown and 0 gateway activity."""
    result = await run_staging_smoke()
    assert result is True, "Expected staging smoke lifecycle check to succeed"
