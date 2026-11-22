"""Unit tests for hardened admin maintenance scripts and manual probe hygiene."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.unit


def _load_purge_module() -> Any:
    """Import scripts/admin/purge_global_commands.py offline without side effects."""
    script_path = PROJECT_ROOT / "scripts" / "admin" / "purge_global_commands.py"
    assert script_path.is_file(), "scripts/admin/purge_global_commands.py is missing"
    spec = importlib.util.spec_from_file_location("purge_global_commands", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manual_probes_renamed_and_destructive_script_relocated() -> None:
    """Verify scripts/manual/ uses probe_*.py (not test_*.py) and purge script moved to scripts/admin/."""
    manual_dir = PROJECT_ROOT / "scripts" / "manual"
    assert list(manual_dir.glob("test_*.py")) == [], "Manual live probes must not use test_*.py naming"

    expected_probes = {
        "probe_job_boards.py",
        "probe_kh_notes.py",
        "probe_notes.py",
        "probe_rmp.py",
        "probe_tmu.py",
        "probe_yf.py",
    }
    actual_probes = {p.name for p in manual_dir.glob("probe_*.py")}
    assert actual_probes == expected_probes

    assert not (PROJECT_ROOT / "tools" / "purge_global_commands.py").exists()
    assert (PROJECT_ROOT / "scripts" / "admin" / "purge_global_commands.py").is_file()
    assert (PROJECT_ROOT / "scripts" / "admin" / "README.md").is_file()


@pytest.mark.asyncio
async def test_purge_script_defaults_to_dry_run_without_reading_tokens() -> None:
    """Verify default invocation previews target scope/endpoint without accessing DISCORD_TOKEN or network."""
    mod = _load_purge_module()
    plan = mod.parse_plan(["--scope", "guild", "--guild-id", "900000000000000001"])
    assert plan.execute is False

    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = await mod.run_purge(
        plan,
        env={"DISCORD_TOKEN": "should-never-be-read"},
        out=stdout,
        err=stderr,
    )
    assert rc == 0
    output = stdout.getvalue()
    assert "[DRY-RUN]" in output
    assert "guild 900000000000000001" in output
    assert "PURGE GUILD 900000000000000001" in output
    assert "should-never-be-read" not in output
    assert stderr.getvalue() == ""


@pytest.mark.asyncio
async def test_purge_script_execute_requires_explicit_token_env_and_confirm_phrase() -> None:
    """Verify --execute rejects implicit token usage, missing env var, and mismatched confirmation phrase."""
    mod = _load_purge_module()

    # 1. Missing --token-env refuses even if DISCORD_TOKEN is set
    plan_no_env = mod.parse_plan(["--scope", "global", "--execute", "--confirm"])
    err1 = io.StringIO()
    rc1 = await mod.run_purge(
        plan_no_env,
        env={"DISCORD_TOKEN": "implicit-token-ignored"},
        out=io.StringIO(),
        err=err1,
    )
    assert rc1 == 2
    assert "--token-env" in err1.getvalue()
    assert "implicit-token-ignored" not in err1.getvalue()

    # 2. Missing value in named env var fails safely
    plan_empty = mod.parse_plan(["--scope", "global", "--execute", "--token-env", "STAGING_TOKEN"])
    err2 = io.StringIO()
    rc2 = await mod.run_purge(plan_empty, env={}, out=io.StringIO(), err=err2)
    assert rc2 == 2
    assert "STAGING_TOKEN" in err2.getvalue()

    # 3. Wrong confirmation phrase aborts before PUT call
    put_called = False
    synthetic_token = "synthetic-staging-secret-value"

    async def fake_identity(tok: str) -> tuple[int, str]:
        assert tok == synthetic_token
        return 1234567890, "StagingApp (bot=StagingBot, app_id=1234567890)"

    async def fake_put(tok: str, endpoint: str) -> tuple[int, str]:
        nonlocal put_called
        put_called = True
        return 200, "[]"

    plan_bad_phrase = mod.parse_plan(
        [
            "--scope",
            "global",
            "--execute",
            "--token-env",
            "STAGING_TOKEN",
            "--confirm",
            "--confirm-phrase",
            "WRONG PHRASE",
        ]
    )
    out3 = io.StringIO()
    err3 = io.StringIO()
    rc3 = await mod.run_purge(
        plan_bad_phrase,
        env={"STAGING_TOKEN": synthetic_token},
        fetch_identity=fake_identity,
        put_empty_commands=fake_put,
        out=out3,
        err=err3,
    )
    assert rc3 == 2
    assert put_called is False
    assert "StagingApp (bot=StagingBot, app_id=1234567890)" in out3.getvalue()
    assert synthetic_token not in out3.getvalue()
    assert synthetic_token not in err3.getvalue()


@pytest.mark.asyncio
async def test_purge_script_execute_confirmed_and_redacts_error_tokens() -> None:
    """Verify confirmed execution hits the exact endpoint and scrubs tokens from error bodies."""
    mod = _load_purge_module()
    synthetic_token = "synthetic-staging-secret-999"
    seen_endpoints: list[str] = []

    async def fake_identity(tok: str) -> tuple[int, str]:
        return 555666777, "StagingApp (bot=StagingBot, app_id=555666777)"

    async def fake_put_ok(tok: str, endpoint: str) -> tuple[int, str]:
        seen_endpoints.append(endpoint)
        return 200, "[]"

    plan_ok = mod.parse_plan(
        [
            "--scope",
            "guild",
            "--guild-id",
            "900000000000000001",
            "--execute",
            "--token-env",
            "STAGING_TOKEN",
            "--confirm",
            "--confirm-phrase",
            "PURGE GUILD 900000000000000001",
        ]
    )
    out_ok = io.StringIO()
    err_ok = io.StringIO()
    rc_ok = await mod.run_purge(
        plan_ok,
        env={"STAGING_TOKEN": synthetic_token},
        fetch_identity=fake_identity,
        put_empty_commands=fake_put_ok,
        out=out_ok,
        err=err_ok,
    )
    assert rc_ok == 0
    assert seen_endpoints == [
        "https://discord.com/api/v10/applications/555666777/guilds/900000000000000001/commands"
    ]
    assert "[OK]" in out_ok.getvalue()
    assert synthetic_token not in out_ok.getvalue()

    # Error response containing raw token must be scrubbed before printing
    async def fake_put_err(tok: str, endpoint: str) -> tuple[int, str]:
        return 401, f"Unauthorized token={synthetic_token}"

    err_fail = io.StringIO()
    rc_fail = await mod.run_purge(
        plan_ok,
        env={"STAGING_TOKEN": synthetic_token},
        fetch_identity=fake_identity,
        put_empty_commands=fake_put_err,
        out=io.StringIO(),
        err=err_fail,
    )
    assert rc_fail == 1
    assert synthetic_token not in err_fail.getvalue()
    assert "[REDACTED]" in err_fail.getvalue()


def test_probe_job_boards_import_is_offline_safe_and_updates_only_board_keys(tmp_path: Path) -> None:
    """Verify scripts/manual/probe_job_boards.py imports offline and preserves non-board .env keys."""
    script_path = PROJECT_ROOT / "scripts" / "manual" / "probe_job_boards.py"
    assert script_path.is_file()
    spec = importlib.util.spec_from_file_location("probe_job_boards", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    total_candidates = (
        len(mod.CANDIDATE_GREENHOUSE_BOARDS)
        + len(mod.CANDIDATE_LEVER_SITES)
        + len(mod.CANDIDATE_WORKDAY_SITES)
    )
    assert total_candidates >= 90
    assert any("NVIDIA|" in s for s in mod.CANDIDATE_WORKDAY_SITES)
    assert any("AMD|" in s for s in mod.CANDIDATE_WORKDAY_SITES)
    assert "palantir" in mod.CANDIDATE_LEVER_SITES

    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "DISCORD_TOKEN=keep-secret\nGREENHOUSE_BOARDS=old1\nLEVER_SITES=old2\nWORKDAY_SITES=old3\n",
        encoding="utf-8",
    )
    mod.update_env_job_boards(
        fake_env,
        greenhouse_boards=["stripe", "databricks", "stripe"],
        lever_sites=["palantir"],
        workday_sites=["NVIDIA|https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"],
    )
    updated = fake_env.read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=keep-secret\n" in updated
    assert "GREENHOUSE_BOARDS=stripe,databricks\n" in updated
    assert "LEVER_SITES=palantir\n" in updated
    assert "WORKDAY_SITES=NVIDIA|https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite\n" in updated
