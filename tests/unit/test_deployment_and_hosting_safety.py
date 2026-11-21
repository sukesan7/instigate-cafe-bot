"""Unit tests for production hosting configurations, Dockerfile security, systemd, and backup safety."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tomllib
import warnings
from pathlib import Path

import pytest
import yaml

import icbot
from icbot.bot import create_bot
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database, DatabasePathError, prepare_sqlite_path
from tools.backup_db import backup_database, restore_database, verify_sqlite_integrity
from tools.smoke_staging import run_staging_smoke

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _extract_dockerfile_env(content: str) -> dict[str, str]:
    """Parse key=value pairs from Dockerfile ENV instructions."""
    joined_lines: list[str] = []
    buf = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1].strip() + " "
        else:
            buf += line
            joined_lines.append(buf.strip())
            buf = ""
    if buf:
        joined_lines.append(buf.strip())

    env_map: dict[str, str] = {}
    for stmt in joined_lines:
        if not stmt.startswith("ENV "):
            continue
        body = stmt[4:].strip()
        for match in re.finditer(r'([A-Z0-9_]+)=(?:"([^"]*)"|([^\s]+))', body):
            key = match.group(1)
            val = match.group(2) if match.group(2) is not None else match.group(3)
            env_map[key] = val
    return env_map


def test_dockerfile_security_and_non_root():
    """Verify Dockerfile enforces non-root execution, canonical DB_PATH, 0700 /data, and zero inbound ports."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    assert dockerfile_path.exists(), "Missing Dockerfile"

    content = dockerfile_path.read_text(encoding="utf-8")

    # 1. Non-root user configuration (UID/GID 10001)
    assert "useradd -u 10001 -g campusbot" in content
    assert "USER campusbot" in content, "Dockerfile runtime stage must switch to non-root user"

    # 2. Persistent /data directory pre-created with campusbot ownership and 0700 mode (not 0777)
    assert "chown -R campusbot:campusbot /data" in content
    assert "chmod 0700 /data" in content
    assert "777" not in content

    # 3. Canonical storage environment contract
    env_map = _extract_dockerfile_env(content)
    assert env_map.get("DB_PATH") == "/data/campus.db"
    assert env_map.get("GRADES_DB_PATH") == "/data/tmu_grades.db"
    assert "SQLITE_PATH" not in env_map, "Dockerfile must use canonical DB_PATH, not deprecated SQLITE_PATH"

    # 4. Honest local readiness healthcheck (no live token required, verifies DB_PATH parent)
    assert "Settings.load_validated()" in content
    assert "prepare_sqlite_path(s.db_path, ensure_parent=True)" in content

    # 5. Process signal handling
    assert "STOPSIGNAL SIGTERM" in content, "Dockerfile must define STOPSIGNAL SIGTERM"

    # 6. Outbound only (no exposed inbound ports)
    for line in content.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("EXPOSE"):
            pytest.fail(f"Gateway-only bot must not expose inbound ports: {clean_line}")

    # 7. Zero baked secrets
    assert "DISCORD_TOKEN=" not in content, "Secrets must not be hardcoded in Dockerfile"
    assert "GEMINI_API_KEY=" not in content, "API keys must not be hardcoded in Dockerfile"


def test_release_tag_single_mechanism_matches_pyproject_and_compose():
    """Verify Dockerfile, docker-compose.yml, pyproject.toml, and icbot.__version__ share the same release version."""
    pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pep440_version = pyproject_data["project"]["version"]  # e.g. "1.0.0rc1"
    app_version = icbot.__version__  # e.g. "1.0.0-rc1"
    assert app_version.replace("-", "") == pep440_version

    dockerfile_content = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f'ARG APP_VERSION="{app_version}"' in dockerfile_content
    assert 'org.opencontainers.image.version="${APP_VERSION}"' in dockerfile_content
    assert "0.2.4" not in dockerfile_content

    compose_path = REPO_ROOT / "docker-compose.yml"
    compose_raw = compose_path.read_text(encoding="utf-8")
    assert "0.2.4" not in compose_raw, "Stale 0.2.4 tag must not appear in docker-compose.yml"
    assert f"${{APP_VERSION:-{app_version}}}" in compose_raw


def test_docker_compose_configuration_and_quotas():
    """Verify docker-compose.yml defines canonical DB_PATH, resource quotas, log rotation, and no exposed ports."""
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

    # 3. Canonical DB_PATH / GRADES_DB_PATH environment variables
    env_list = bot_cfg.get("environment", [])
    env_kv = dict(item.split("=", 1) for item in env_list if "=" in item)
    assert env_kv.get("DB_PATH") == "/data/campus.db"
    assert env_kv.get("GRADES_DB_PATH") == "/data/tmu_grades.db"
    assert "SQLITE_PATH" not in env_kv

    # 4. Storage persistence
    volumes = bot_cfg.get("volumes", [])
    assert any("./data:/data" in v or "/data" in v for v in volumes), (
        "Missing persistent volume mount for /data"
    )

    # 5. Log rotation
    logging_cfg = bot_cfg.get("logging", {})
    assert logging_cfg.get("driver") == "json-file"
    assert "max-size" in logging_cfg.get("options", {})
    assert "max-file" in logging_cfg.get("options", {})

    # 6. Resource quotas
    resources = bot_cfg.get("deploy", {}).get("resources", {})
    limits = resources.get("limits", {})
    assert "memory" in limits, "Compose service must define memory limits"
    assert "cpus" in limits, "Compose service must define CPU limits"

    # 7. Process lifecycle
    assert bot_cfg.get("stop_signal") == "SIGTERM"
    assert bot_cfg.get("stop_grace_period") in ("30s", 30)

    # 8. No exposed ports (outbound only)
    assert "ports" not in bot_cfg, "Gateway-only bot must not expose inbound ports in docker-compose.yml"


def test_systemd_service_unit_sandboxing():
    """Verify systemd service unit contains canonical DB_PATH, Linux sandboxing, and resource quotas."""
    unit_path = REPO_ROOT / "systemd" / "campus-bot.service"
    assert unit_path.exists(), "Missing systemd/campus-bot.service"

    content = unit_path.read_text(encoding="utf-8")

    # 1. Non-root user
    assert "User=campusbot" in content
    assert "Group=campusbot" in content

    # 2. Canonical storage paths inside ReadWritePaths=/opt/campus-bot/data
    assert "Environment=DB_PATH=/opt/campus-bot/data/campus.db" in content
    assert "Environment=GRADES_DB_PATH=/opt/campus-bot/data/tmu_grades.service" not in content
    assert "Environment=GRADES_DB_PATH=/opt/campus-bot/data/tmu_grades.db" in content
    assert "SQLITE_PATH=" not in content

    # 3. Security hardening and sandboxing directives
    assert "NoNewPrivileges=true" in content
    assert "ProtectSystem=strict" in content
    assert "ProtectHome=true" in content
    assert "PrivateTmp=true" in content
    assert "ReadWritePaths=/opt/campus-bot/data" in content

    # 4. Process lifecycle
    assert "KillSignal=SIGTERM" in content
    assert "TimeoutStopSec=30" in content
    assert "Restart=on-failure" in content

    # 5. Resource quotas
    assert "MemoryMax=512M" in content
    assert "CPUQuota=100%" in content


def test_effective_settings_from_dockerfile_compose_and_systemd_environments():
    """Parse real Dockerfile, docker-compose.yml, and systemd/campus-bot.service envs into Settings.load_validated()."""
    # 1. Dockerfile environment
    dockerfile_env = _extract_dockerfile_env((REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"))
    with warnings.catch_warnings(record=True) as caught_docker:
        warnings.simplefilter("always", DeprecationWarning)
        s_docker = Settings.load_validated(environ=dockerfile_env)
    assert caught_docker == []
    assert s_docker.db_path == "/data/campus.db"
    assert s_docker.grades_db_path == "/data/tmu_grades.db"
    assert s_docker.allow_gateway is True

    # 2. docker-compose.yml environment
    compose_data = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_env_list = compose_data["services"]["campus-bot"]["environment"]
    compose_env = dict(item.split("=", 1) for item in compose_env_list if "=" in item)
    with warnings.catch_warnings(record=True) as caught_compose:
        warnings.simplefilter("always", DeprecationWarning)
        s_compose = Settings.load_validated(environ=compose_env)
    assert caught_compose == []
    assert s_compose.db_path == "/data/campus.db"
    assert s_compose.grades_db_path == "/data/tmu_grades.db"
    assert s_compose.allow_gateway is True

    # 3. systemd/campus-bot.service Environment= directives
    systemd_content = (REPO_ROOT / "systemd" / "campus-bot.service").read_text(encoding="utf-8")
    systemd_env: dict[str, str] = {}
    for line in systemd_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("Environment="):
            k, v = stripped[len("Environment=") :].split("=", 1)
            systemd_env[k.strip()] = v.strip()
    with warnings.catch_warnings(record=True) as caught_systemd:
        warnings.simplefilter("always", DeprecationWarning)
        s_systemd = Settings.load_validated(environ=systemd_env)
    assert caught_systemd == []
    assert s_systemd.db_path == "/opt/campus-bot/data/campus.db"
    assert s_systemd.grades_db_path == "/opt/campus-bot/data/tmu_grades.db"
    assert s_systemd.allow_gateway is True


@pytest.mark.asyncio
async def test_non_root_writable_volume_fail_closed_and_restart_state_survival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Simulate a non-root container deployment with read-only /app and writable /data volume across a restart."""
    app_workdir = tmp_path / "app"
    data_volume = tmp_path / "data"
    app_workdir.mkdir(mode=0o755)
    data_volume.mkdir(mode=0o700)
    monkeypatch.chdir(app_workdir)

    # Verify /data volume has strict owner-only permissions (0700, no group/world write)
    data_mode = stat.S_IMODE(data_volume.stat().st_mode)
    assert data_mode == 0o700

    # Lock down /app working directory as read-only (simulating non-root container or systemd ProtectSystem=strict)
    app_workdir.chmod(0o555)
    try:
        # 1. Fail-closed verification: attempting to write a fallback DB in read-only /app must raise DatabasePathError
        unwritable_fallback = app_workdir / "ic_stock.db"
        with pytest.raises(DatabasePathError, match="not writable"):
            prepare_sqlite_path(str(unwritable_fallback), ensure_parent=True)
        assert not unwritable_fallback.exists()

        # 2. First container boot with canonical /data paths and absent optional GRADES_DB_PATH
        container_env = {
            "DB_PATH": str(data_volume / "campus.db"),
            "GRADES_DB_PATH": "",
            "ICBOT_ALLOW_GATEWAY": "false",
            "JOBS_POLL_SECONDS": "0",
            "WORKDAY_POLL_SECONDS": "0",
        }
        settings_boot1 = Settings.load_validated(environ=container_env)
        assert settings_boot1.db_path == str(data_volume / "campus.db")
        assert settings_boot1.grades_db_path == ""

        # Execute the Dockerfile HEALTHCHECK readiness contract against the mounted volume
        checked_path = prepare_sqlite_path(settings_boot1.db_path, ensure_parent=True)
        assert checked_path == str(data_volume / "campus.db")

        bot1 = create_bot(settings=settings_boot1, register_commands=False, sync_commands=False)
        await bot1.setup_hook()
        try:
            # Verify grades_cache and academic_registry share the primary DB when GRADES_DB_PATH is empty
            assert bot1.services.grades_cache.db is bot1.db

            # Persist synthetic user economy state, grades cache, and scheduler/killswitch checkpoint
            user_store1 = bot1.services.user_store(settings_boot1)
            await user_store1.ensure_user(9001, display_name="PersistentStudent")
            await user_store1.add_balance(9001, 750, category="stipend", reference_id="boot1_tx")
            await bot1.services.grades_cache.set_course(
                "ele888",
                "Intelligent Systems",
                [{"name": "Dr. Synth", "avg": "A-", "common": "A", "students": "45", "dist": []}],
                fetched_at=1_760_000_000,
            )
            await bot1.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("llm_killed", "1")
            )
        finally:
            await bot1.close()

        # Verify ONLY the intended persistent database in /data was created; zero fallback DBs in /app
        assert (data_volume / "campus.db").exists()
        assert list(app_workdir.glob("*.db")) == []
        assert not (data_volume / "tmu_grades.db").exists()

        # 3. Simulate container restart against the same mounted /data volume
        settings_boot2 = Settings.load_validated(environ=container_env)
        bot2 = create_bot(settings=settings_boot2, register_commands=False, sync_commands=False)
        await bot2.setup_hook()
        try:
            # Verify scheduler/killswitch state, user balance, ledger, and grades cache survived restart
            assert bot2.services.llm_killed is True
            user_store2 = bot2.services.user_store(settings_boot2)
            assert await user_store2.get_balance(9001) == 1750  # 1000 starting + 750 stipend
            ledger = await user_store2.get_ledger(9001)
            assert any(entry.reference_id == "boot1_tx" for entry in ledger)

            cached_course = await bot2.services.grades_cache.get_course("ele888")
            assert cached_course is not None
            assert cached_course["title"] == "Intelligent Systems"
        finally:
            await bot2.close()

        assert list(app_workdir.glob("*.db")) == []
    finally:
        app_workdir.chmod(0o755)


def test_prepare_sqlite_path_creates_0700_parent_and_rejects_invalid_targets(tmp_path: Path):
    """Verify prepare_sqlite_path normalizes paths, creates missing parents with 0700, and fails closed."""
    nested_db = tmp_path / "new_volume" / "subdir" / "campus.db"
    normed = prepare_sqlite_path(str(nested_db), ensure_parent=True)
    assert normed == str(nested_db)
    assert nested_db.parent.exists()
    assert stat.S_IMODE(nested_db.parent.stat().st_mode) == 0o700

    # Directory target must fail closed with DatabasePathError
    with pytest.raises(DatabasePathError, match="directory"):
        prepare_sqlite_path(str(tmp_path), ensure_parent=True)

    # Parent path that is actually a regular file must fail closed
    file_as_parent = tmp_path / "regular_file"
    file_as_parent.write_text("not a dir", encoding="utf-8")
    with pytest.raises(DatabasePathError, match="not a directory"):
        prepare_sqlite_path(str(file_as_parent / "campus.db"), ensure_parent=True)

    # Unwritable database file must fail closed
    ro_db = tmp_path / "readonly.db"
    ro_db.write_bytes(b"")
    ro_db.chmod(0o400)
    try:
        if os.getuid() != 0:
            with pytest.raises(DatabasePathError, match="not readable and writable"):
                prepare_sqlite_path(str(ro_db), ensure_parent=True)
    finally:
        ro_db.chmod(0o600)

    # ServiceContainer with db=None and empty grades_db_path falls back to settings.db_path
    s = Settings(db_path=str(nested_db), grades_db_path="")
    bare_container = ServiceContainer(settings=s, db=None)
    assert bare_container.grades_cache.db.path == str(nested_db)
    assert Database(":memory:").path == ":memory:"


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
