"""Automated integration test suite for staging acceptance, fault injection, and recovery.

Exercises the complete 27-scenario matrix offline against isolated synthetic temporary databases.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tools.run_staging_acceptance import StagingAcceptanceHarness


@pytest.mark.asyncio
async def test_full_staging_acceptance_and_fault_injection_matrix() -> None:
    """Execute all 27 staging acceptance, fault-injection, and recovery scenarios offline."""
    with tempfile.TemporaryDirectory() as tmp_str:
        harness = StagingAcceptanceHarness(Path(tmp_str))
        all_passed = await harness.run_all()
        assert all_passed is True, "One or more staging acceptance/fault scenarios failed"
        assert len(harness.results) == 27, f"Expected 27 scenarios, got {len(harness.results)}"

        # Assert zero P0/P1 failures
        p0_failures = [r for r in harness.results if not r.passed and r.severity == "P0"]
        p1_failures = [r for r in harness.results if not r.passed and r.severity == "P1"]
        assert len(p0_failures) == 0, f"Critical P0 failures detected: {[r.scenario_id for r in p0_failures]}"
        assert len(p1_failures) == 0, f"High P1 failures detected: {[r.scenario_id for r in p1_failures]}"


@pytest.mark.asyncio
async def test_staging_two_guild_isolation_contract() -> None:
    """Explicitly verify that data and economies between synthetic Guild Alpha and Beta never cross-bleed."""
    with tempfile.TemporaryDirectory() as tmp_str:
        harness = StagingAcceptanceHarness(Path(tmp_str))
        await harness.test_stg_04_two_guild_isolation()
        res = harness.results[-1]
        assert res.scenario_id == "STG-04"
        assert res.passed is True


@pytest.mark.asyncio
async def test_staging_atomic_financial_race_condition() -> None:
    """Explicitly verify that 10 concurrent clicks on daily claim result in exactly 1 success."""
    with tempfile.TemporaryDirectory() as tmp_str:
        harness = StagingAcceptanceHarness(Path(tmp_str))
        await harness.test_flt_11_double_click_financial_race()
        res = harness.results[-1]
        assert res.scenario_id == "FLT-11"
        assert res.passed is True


@pytest.mark.asyncio
async def test_staging_crash_mid_transaction_wal_recovery() -> None:
    """Explicitly verify that a killed process mid-transaction rolls back cleanly on restart."""
    with tempfile.TemporaryDirectory() as tmp_str:
        harness = StagingAcceptanceHarness(Path(tmp_str))
        await harness.test_flt_07_process_killed_mid_transaction()
        res = harness.results[-1]
        assert res.scenario_id == "FLT-07"
        assert res.passed is True
