"""Regression tests for self-hosted container builds, storage fail-closed errors, and Railway safety."""

from __future__ import annotations

import configparser
import os
import re
import sqlite3
from pathlib import Path

import pytest

import main as main_mod
from icbot.config import Settings
from icbot.db import Database, DatabasePathError, MigrationError, prepare_sqlite_path

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_dockerfile_pins_uv_compatible_with_uv_lock_revision() -> None:
    """Verify Dockerfile copies a uv version compatible with uv.lock (revision >= 5, no 0.5.x)."""
    lock_head = "\n".join((REPO_ROOT / "uv.lock").read_text(encoding="utf-8").splitlines()[:10])
    m_rev = re.search(r"^revision\s*=\s*(\d+)", lock_head, flags=re.MULTILINE)
    assert m_rev is not None, "Expected revision field in uv.lock header"
    assert int(m_rev.group(1)) >= 5

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    m_uv = re.search(r"ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)", dockerfile)
    assert m_uv is not None, "Dockerfile must pin an explicit ghcr.io/astral-sh/uv:X.Y.Z tag"
    major, minor, _patch = (int(part) for part in m_uv.group(1).split("."))
    assert (major, minor) >= (0, 13), (
        f"Dockerfile uv:{m_uv.group(1)} is too old for uv.lock revision {m_rev.group(1)}; pin >= 0.13.9"
    )


def test_systemd_unit_parses_cleanly_as_strict_ini() -> None:
    """Verify systemd/campus-bot.service has valid INI syntax, zero duplicate options, and safe paths."""
    parser = configparser.ConfigParser(strict=True, empty_lines_in_values=False)
    # Environment= appears multiple times intentionally in systemd units; parse non-env strictly
    raw_lines = (REPO_ROOT / "systemd" / "campus-bot.service").read_text(encoding="utf-8").splitlines()
    filtered = [line for line in raw_lines if not line.strip().startswith("Environment=")]
    parser.read_string("\n".join(filtered))

    assert list(parser.sections()) == ["Unit", "Service", "Install"]
    assert parser["Service"]["ExecStart"] == "/opt/campus-bot/.venv/bin/python main.py --allow-gateway"
    assert parser["Service"]["WorkingDirectory"] == "/opt/campus-bot"
    assert parser["Service"]["ReadWritePaths"] == "/opt/campus-bot/data"


@pytest.mark.asyncio
async def test_readable_errors_for_permissions_missing_mount_absent_secrets_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify readable fail-closed errors for storage permissions, missing mounts, absent secrets, and schemas."""
    # 1. Permission-denied DB parent directory -> DatabasePathError with UID and parent path
    locked_parent = tmp_path / "locked_data"
    locked_parent.mkdir(mode=0o700)
    if os.getuid() != 0:
        locked_parent.chmod(0o500)
        try:
            with pytest.raises(DatabasePathError, match="not writable by UID"):
                prepare_sqlite_path(str(locked_parent / "campus.db"), ensure_parent=True)
        finally:
            locked_parent.chmod(0o700)

    # 2. Missing /data mount under read-only parent -> DatabasePathError explaining creation failure
    ro_mount_root = tmp_path / "ro_root"
    ro_mount_root.mkdir(mode=0o755)
    if os.getuid() != 0:
        ro_mount_root.chmod(0o555)
        try:
            with pytest.raises(DatabasePathError, match="Cannot create database parent directory"):
                prepare_sqlite_path(str(ro_mount_root / "data" / "campus.db"), ensure_parent=True)
        finally:
            ro_mount_root.chmod(0o755)

    # 3. Absent DISCORD_TOKEN with opt-in vs default disabled gateway -> readable SystemExit
    monkeypatch.setenv("DB_PATH", str(tmp_path / "absent_secret.db"))
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("ICBOT_ALLOW_GATEWAY", "false")
    with pytest.raises(SystemExit, match="Gateway launch disabled by runtime safeguard"):
        main_mod.main([])

    with pytest.raises(SystemExit, match="Missing DISCORD_TOKEN"):
        main_mod.main(["--allow-gateway"])

    # 4. Old pre-v7 SQLite DB migrates cleanly while newer unsupported user_version fails closed
    legacy_db_path = tmp_path / "legacy_v1.db"
    conn = sqlite3.connect(str(legacy_db_path))
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);")
    conn.execute("INSERT INTO users (user_id, balance) VALUES (42, 250);")
    conn.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    upgraded_db = Database(str(legacy_db_path))
    await upgraded_db.init()
    try:
        health = await upgraded_db.get_storage_health()
        assert health.schema_version == 7
        assert health.journal_mode.lower() == "wal"
    finally:
        await upgraded_db.close()

    future_db_path = tmp_path / "future_v999.db"
    fconn = sqlite3.connect(str(future_db_path))
    fconn.execute("PRAGMA user_version = 999;")
    fconn.commit()
    fconn.close()

    future_db = Database(str(future_db_path))
    with pytest.raises(MigrationError, match=r"user_version=999.*newer than"):
        await future_db.init()
    await future_db.close()

    # Empty optional secret Settings remains valid offline
    s = Settings.load_validated(environ={"DB_PATH": ":memory:", "ICBOT_ALLOW_GATEWAY": "false"})
    assert s.token == ""
    assert s.allow_gateway is False


def test_docs_document_container_smoke_plan_error_diagnostics_and_verification_tiers() -> None:
    """Verify deployment and development docs cover container smoke, error diagnostics, and test boundaries."""
    deploy_doc = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    dev_doc = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")

    # Multi-boot tokenless throwaway volume test plan and troubleshooting in deployment.md
    assert "mktemp -d" in deploy_doc
    assert "ICBOT_ALLOW_GATEWAY=false" in deploy_doc
    assert "DatabasePathError" in deploy_doc
    assert "MigrationError" in deploy_doc
    assert "Missing DISCORD_TOKEN" in deploy_doc

    # Development doc clarifies offline mocks vs manual live Docker/Railway/Discord steps
    assert "Automated tests mock Discord interactions" in dev_doc
    assert "Quality & Security Gate" in dev_doc
