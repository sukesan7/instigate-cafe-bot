"""Automated unit test suite for the 4 fictional archetype usability audit."""

from __future__ import annotations

import pytest

from icbot.config import Settings
from icbot.db import Database
from tools.run_usability_audit import UsabilityAuditRunner

pytestmark = pytest.mark.unit


@pytest.fixture
async def audit_runner(tmp_db: Database):
    settings = Settings(
        guild_id=900_000_000_000_000_001,
        token="synth-token-usability-test",
        gemini_api_key="synth-gemini-key-test",
        allow_gateway=False,
    )
    runner = UsabilityAuditRunner(tmp_db, settings)
    runner.start_intercept()
    try:
        yield runner
    finally:
        runner.stop_intercept()


async def test_archetype_1_first_year_arts_student(audit_runner: UsabilityAuditRunner):
    """Archetype 1: Maya (First-year Arts) completes academic discovery and exam export."""
    metrics = await audit_runner.run_archetype_1_arts_student()

    assert len(metrics) == 4
    for m in metrics:
        assert m.status == "PASS", f"Task {m.task_name} failed: {m.output_summary}"
        assert m.error_ambiguity == "CLEAR"
        assert m.duration_ms < 500.0  # sub-second response

    # Verify documented friction points are identified
    exam_metric = next(m for m in metrics if "Exam" in m.task_name)
    assert any("personal planner" in c for c in exam_metric.confusion_points)


async def test_archetype_2_graduating_engineer(audit_runner: UsabilityAuditRunner):
    """Archetype 2: Alex (Senior Engineer) configures career preferences and submits resume with privacy notice."""
    metrics = await audit_runner.run_archetype_2_graduating_engineer()

    assert len(metrics) == 4
    for m in metrics:
        assert m.status == "PASS", f"Task {m.task_name} failed: {m.output_summary}"
        assert m.error_ambiguity == "CLEAR"
        assert m.duration_ms < 500.0

    # Verify job preferences were persisted
    pref_metric = next(m for m in metrics if "Preferences" in m.task_name)
    assert "preferences" in pref_metric.output_summary.lower()


async def test_archetype_3_nontechnical_server_admin(audit_runner: UsabilityAuditRunner):
    """Archetype 3: Jordan (Server Admin) configures features, schedules exam, and exports diagnostics without CLI."""
    metrics = await audit_runner.run_archetype_3_nontechnical_admin()

    assert len(metrics) == 6
    for m in metrics:
        assert m.status == "PASS", f"Task {m.task_name} failed: {m.output_summary}"
        assert m.error_ambiguity == "CLEAR"
        assert m.duration_ms < 500.0


async def test_archetype_4_privacy_conscious_member(audit_runner: UsabilityAuditRunner):
    """Archetype 4: Sam (Privacy Advocate) inspects policy, chooses offline checklist, and executes Right-to-be-Forgotten."""
    metrics = await audit_runner.run_archetype_4_privacy_conscious()

    assert len(metrics) == 4
    for m in metrics:
        assert m.status == "PASS", f"Task {m.task_name} failed: {m.output_summary}"
        assert m.error_ambiguity == "CLEAR"
        assert m.duration_ms < 500.0

    # Verify Right-to-be-Forgotten data deletion executed
    delete_metric = next(m for m in metrics if "Right-to-be-Forgotten" in m.task_name)
    assert "deleted atomically" in delete_metric.output_summary


async def test_usability_rubric_and_task_metrics_benchmark(audit_runner: UsabilityAuditRunner):
    """Full usability audit benchmark satisfies 100% completion rate and latency thresholds."""
    all_metrics = await audit_runner.run_full_audit()

    assert len(all_metrics) == 18
    passed = [m for m in all_metrics if m.status == "PASS"]
    assert len(passed) == 18, f"Expected 18 passed, got {len(passed)}"

    # Benchmark: Average duration per task must be under 50ms for offline operations
    avg_latency = sum(m.duration_ms for m in all_metrics) / len(all_metrics)
    assert avg_latency < 50.0, f"Average task latency too high: {avg_latency:.2f}ms"
