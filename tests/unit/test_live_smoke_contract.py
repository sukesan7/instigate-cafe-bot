"""Offline contract tests for `.github/workflows/live-smoke.yml`, `live_smoke` pytest markers, and gating."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "live-smoke.yml"
JUNIT_PASSED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" errors="0" failures="0" skipped="1" time="0.25">
<testcase classname="x" name="t1" /><testcase classname="x" name="t2"><skipped message="unconfigured" /></testcase>
</testsuite></testsuites>
"""
JUNIT_ALL_SKIPPED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" errors="0" failures="0" skipped="3" time="0.05">
<testcase classname="x" name="t1"><skipped message="missing key" /></testcase>
</testsuite></testsuites>
"""
JUNIT_FAILED_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" errors="0" failures="1" skipped="0" time="0.20">
<testcase classname="x" name="t1"><failure message="HTTP 500 token=super_secret_go_key_12345">boom</failure></testcase>
</testsuite></testsuites>
"""
JUNIT_EMPTY_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="0" errors="0" failures="0" skipped="0" time="0.01" /></testsuites>
"""


def test_live_smoke_workflow_is_manual_dispatch_only_and_inert_by_default() -> None:
    """live-smoke.yml must never run on schedule or pull_request and must stay inert when disabled."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True) or {}

    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" not in triggers, "Mere secret presence must not cause weekly scheduled network requests"
    assert "workflow_dispatch" in triggers

    dispatch_inputs = triggers["workflow_dispatch"].get("inputs", {})
    assert "confirm_live_network" in dispatch_inputs
    assert dispatch_inputs["confirm_live_network"].get("default") is False
    assert "provider" in dispatch_inputs

    assert workflow.get("permissions") == {"contents": "read"}

    env = workflow.get("env", {})
    assert env.get("ICBOT_ALLOW_GATEWAY") == "false"
    assert env.get("DISCORD_TOKEN") == ""
    assert env.get("GEMINI_API_KEY") == ""
    assert env.get("LLM_ENABLED") == "false"
    assert env.get("DB_PATH") == ":memory:"

    # Ensure workflow references our gating runner rather than raw `pytest -m live_smoke` that fails with exit 5
    steps = workflow["jobs"]["live-smoke"]["steps"]
    combined_runs = "\n".join(str(s.get("run", "")) for s in steps)
    combined_envs = {k: v for s in steps for k, v in (s.get("env") or {}).items()}
    assert "tools/run_live_smoke.py" in combined_runs
    assert "OPT_IN_LIVE_SMOKE" in combined_envs
    assert "railway" not in combined_runs.lower()
    assert "main.py" not in combined_runs


def test_live_smoke_pytest_collection_exists_and_excluded_from_default_run() -> None:
    """Default pytest excludes live_smoke tests; `-m live_smoke` discovers real marked tests offline."""
    res_live = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-markers",
            "-m",
            "live_smoke",
            "--collect-only",
            "-q",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DISCORD_TOKEN": "", "ICBOT_ALLOW_GATEWAY": "false", "DB_PATH": ":memory:"},
    )
    assert res_live.returncode == 0, res_live.stderr + res_live.stdout
    assert "test_live_provider_smoke.py::test_live_smoke_" in res_live.stdout

    # Default selection must NOT collect tests/live_smoke/
    res_default = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DISCORD_TOKEN": "", "ICBOT_ALLOW_GATEWAY": "false", "DB_PATH": ":memory:"},
    )
    assert res_default.returncode == 0
    assert "test_live_provider_smoke.py" not in res_default.stdout


def test_live_smoke_tests_skip_safely_without_opt_in() -> None:
    """Running `-m live_smoke` without explicit opt-in skips cleanly with zero network traffic."""
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-markers",
            "-m",
            "live_smoke",
            "-rs",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DISCORD_TOKEN": "",
            "ICBOT_ALLOW_GATEWAY": "false",
            "ICBOT_ALLOW_LIVE_SMOKE": "0",
            "ICBOT_LIVE_SMOKE_OPT_IN": "false",
            "DB_PATH": ":memory:",
        },
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipped" in res.stdout.lower()


def test_run_live_smoke_classification_disabled_skip_empty_fail_pass(tmp_path: Path) -> None:
    """tools/run_live_smoke.py accurately distinguishes DISABLED, NO_TESTS_SELECTED, SKIPPED, FAILED, PASSED."""
    from tools.run_live_smoke import SmokeOutcome, classify_smoke_result, format_smoke_summary

    # 1. Exit code 5 or zero tests -> NO_TESTS_SELECTED (non-zero failure when enabled)
    junit_empty = tmp_path / "empty.xml"
    junit_empty.write_text(JUNIT_EMPTY_XML, encoding="utf-8")
    res_empty = classify_smoke_result(returncode=5, junit_path=junit_empty, provider="go_transit")
    assert res_empty.status == "NO_TESTS_SELECTED"
    assert res_empty.exit_code != 0

    # 2. All tests skipped (unconfigured credentials) -> SKIPPED_UNCONFIGURED (never masked as green!)
    junit_skip = tmp_path / "skip.xml"
    junit_skip.write_text(JUNIT_ALL_SKIPPED_XML, encoding="utf-8")
    res_skip = classify_smoke_result(returncode=0, junit_path=junit_skip, provider="go_transit")
    assert res_skip.status == "SKIPPED_UNCONFIGURED"
    assert res_skip.passed == 0
    assert res_skip.skipped == 3
    assert res_skip.exit_code != 0

    # 3. Genuine test failure -> FAILED (never masked; secrets scrubbed in summary)
    junit_fail = tmp_path / "fail.xml"
    junit_fail.write_text(JUNIT_FAILED_XML, encoding="utf-8")
    res_fail = classify_smoke_result(returncode=1, junit_path=junit_fail, provider="go_transit")
    assert res_fail.status == "FAILED"
    assert res_fail.failures == 1
    assert res_fail.exit_code == 1
    summary_fail = format_smoke_summary(res_fail, secrets=("super_secret_go_key_12345",))
    assert "super_secret_go_key_12345" not in summary_fail
    assert "FAILED" in summary_fail

    # 4. At least 1 passed and 0 failures/errors -> PASSED
    junit_ok = tmp_path / "ok.xml"
    junit_ok.write_text(JUNIT_PASSED_XML, encoding="utf-8")
    res_ok = classify_smoke_result(returncode=0, junit_path=junit_ok, provider="all")
    assert res_ok.status == "PASSED"
    assert res_ok.passed == 1
    assert res_ok.skipped == 1
    assert res_ok.exit_code == 0

    # 5. Disabled outcome -> DISABLED (inert exit 0)
    disabled = SmokeOutcome(
        status="DISABLED",
        provider="all",
        total=0,
        passed=0,
        skipped=0,
        failures=0,
        errors=0,
        duration_seconds=0.0,
        exit_code=0,
        reason="Manual opt-in or OPT_IN_LIVE_SMOKE secret not enabled.",
    )
    summary_dis = format_smoke_summary(disabled)
    assert "DISABLED" in summary_dis
    assert disabled.exit_code == 0


def test_run_live_smoke_inert_when_disabled_with_only_go_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mere presence of GO_API_KEY without deliberate opt-in keeps runner inert (exit 0, zero subprocesses)."""
    from tools.run_live_smoke import main as smoke_main

    monkeypatch.setenv("GO_API_KEY", "dummy_go_key_should_not_trigger")
    monkeypatch.delenv("OPT_IN_LIVE_SMOKE", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    rc = smoke_main(["--provider", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DISABLED" in out
    assert "dummy_go_key_should_not_trigger" not in out
    summary_text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "DISABLED" in summary_text
    assert "dummy_go_key_should_not_trigger" not in summary_text


async def test_live_smoke_bounded_http_helper_handles_429_and_oversized_payload(mock_aiohttp) -> None:
    """Verify bounded live-smoke fetch helper enforces byte caps, Retry-After on 429, and secret redaction."""
    from tests.live_smoke.test_live_provider_smoke import bounded_get_json

    mock_aiohttp.get(
        "https://api.example.invalid/v1/limited?key=secret_token_xyz",
        status=429,
        text="Too Many Requests for secret_token_xyz",
        headers={"Retry-After": "42"},
    )
    mock_aiohttp.get(
        "https://api.example.invalid/v1/huge",
        status=200,
        text="x" * 2048,
    )

    async with aiohttp.ClientSession() as session:
        status_429, _data, retry_after, note_429 = await bounded_get_json(
            session,
            "https://api.example.invalid/v1/limited?key=secret_token_xyz",
            secrets=("secret_token_xyz",),
            max_bytes=1024,
        )
        assert status_429 == 429
        assert retry_after == 42.0
        assert "secret_token_xyz" not in note_429

        with pytest.raises(ValueError, match="exceeded max_bytes"):
            await bounded_get_json(
                session,
                "https://api.example.invalid/v1/huge",
                max_bytes=512,
            )


def test_testing_doc_documents_disabled_schedule_and_live_smoke_contract() -> None:
    """docs/testing.md must document why scheduled live smoke is disabled and how provider_contract/live_smoke work."""
    doc = (REPO_ROOT / "docs/testing.md").read_text(encoding="utf-8")
    assert "provider_contract" in doc
    assert "live_smoke" in doc
    assert "workflow_dispatch" in doc
    assert "run_live_smoke.py" in doc
