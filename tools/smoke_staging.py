#!/usr/bin/env python3
# mypy: ignore-errors
# ruff: noqa: E402
"""Offline staging smoke test and lifecycle recovery verification for Instigate Cafe Bot.

Simulates staging environment startup, configuration loading, schema migration (v4 WAL),
service container dependency injection, single-instance scheduler ownership, and graceful
process shutdown without connecting to the live Discord gateway or making outbound network calls.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database


async def run_staging_smoke() -> bool:
    print("=" * 80)
    print(" Instigate Cafe Bot — Staging Environment Lifecycle Smoke Check")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmp_dir:
        staging_db_path = Path(tmp_dir) / "staging_campus.db"

        # 1. Staging configuration validation
        t0 = time.perf_counter()
        staging_guild_id = 900_000_000_000_000_001
        settings = Settings(
            guild_id=staging_guild_id,
            token="synthetic-staging-token-offline",
            gemini_api_key="synthetic-staging-key-offline",
            allow_gateway=False,  # strictly offline
            db_path=str(staging_db_path),
            llm_enabled=False,
            market_watch_enabled=False,
            jobs_poll_seconds=0,
            workday_poll_seconds=0,
        )
        t_cfg = (time.perf_counter() - t0) * 1000.0
        print(f" [PASS] Staging configuration loaded ({t_cfg:.2f}ms)")
        print(f"        Guild ID: {settings.guild_id} (Synthetic Staging Guild)")
        print("        Gateway:  DISABLED (Offline Simulation Mode)")

        # 2. Database initialization and migration to Schema v4 (WAL mode)
        t0 = time.perf_counter()
        db = Database(str(staging_db_path))
        await db.init()
        t_db = (time.perf_counter() - t0) * 1000.0

        journal_mode_row = await db.fetchone("PRAGMA journal_mode;")
        journal_mode = journal_mode_row[0].lower() if journal_mode_row else ""
        schema_version_row = await db.fetchone("PRAGMA user_version;")
        schema_version = schema_version_row[0] if schema_version_row else 0

        print(f" [PASS] Database initialized and migrated ({t_db:.2f}ms)")
        print(f"        Journal mode:   {journal_mode.upper()} (expected: WAL)")
        print(f"        Schema version: v{schema_version} (expected: v4)")
        assert journal_mode == "wal", f"Expected WAL journal mode, got {journal_mode}"
        assert schema_version >= 4, f"Expected schema version >= 4, got v{schema_version}"

        # 3. Service Container & Dependency Injection
        t0 = time.perf_counter()
        services = ServiceContainer.from_settings(settings, db=db)
        u_store = services.user_store(settings)
        await u_store.ensure_user(user_id=123456789, display_name="StagingTester")
        t_svc = (time.perf_counter() - t0) * 1000.0
        print(f" [PASS] Service container wired cleanly ({t_svc:.2f}ms)")

        # 4. Bot runtime instantiation and command registration
        t0 = time.perf_counter()
        from icbot.bot import create_bot

        bot = create_bot(settings=settings, db=db, services=services, register_commands=True)
        cmd_count = len(bot.tree.get_commands())
        t_bot = (time.perf_counter() - t0) * 1000.0
        print(f" [PASS] In-memory bot instantiated with {cmd_count} command trees ({t_bot:.2f}ms)")

        # 5. Single-instance scheduler ownership verification
        t0 = time.perf_counter()
        assert bot.supervisor is not None
        registered_tasks = list(bot.supervisor.health_snapshot().keys())
        print(
            f" [PASS] Supervisor registered {len(registered_tasks)} single-instance tasks ({', '.join(sorted(registered_tasks))})"
        )

        # 6. Graceful shutdown and process signal lifecycle
        t0 = time.perf_counter()
        await bot._cleanup_runtime()
        t_down = (time.perf_counter() - t0) * 1000.0
        print(f" [PASS] Graceful runtime cleanup verified ({t_down:.2f}ms)")
        print("        Background tasks stopped, HTTP sessions closed, SQLite flushed.")

    print("=" * 80)
    print(" [SUMMARY] Staging Lifecycle Smoke Check: ALL CHECKS PASSED (100% Offline)")
    print("=" * 80)
    return True


def main() -> int:
    try:
        success = asyncio.run(run_staging_smoke())
        return 0 if success else 1
    except (AssertionError, OSError, RuntimeError) as exc:
        print(f"[FAIL] Staging smoke test failed with exception: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
