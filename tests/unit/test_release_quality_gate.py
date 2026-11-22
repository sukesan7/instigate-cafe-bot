"""Release quality gate regression tests spanning PRs A-D, CI safety, and missing screenshot/doc checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.check_command_docs import introspect_command_tree, validate_command_docs
from tools.check_docs import validate_markdown_files

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.unit


def test_ci_workflow_enforces_complete_non_production_quality_gate() -> None:
    """Verify .github/workflows/ci.yml runs all required offline gates across 3.11-3.13 with zero deploy/gateway."""
    ci_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {"contents": "read"}
    env = workflow["env"]
    assert env["ICBOT_ALLOW_GATEWAY"] == "false"
    assert env["DISCORD_TOKEN"] == ""
    assert env["GEMINI_API_KEY"] == ""
    assert env["LLM_ENABLED"] == "false"

    py_versions = workflow["jobs"]["quality"]["strategy"]["matrix"]["python-version"]
    assert {"3.11", "3.12", "3.13"}.issubset(set(py_versions))

    step_runs = "\n".join(str(s.get("run", "")) for s in workflow["jobs"]["quality"]["steps"])
    required_commands = (
        "uv lock --check",
        "uv sync --frozen --dev",
        "tools/scan_secrets.py --enforce-clean",
        "pip-audit",
        "ruff check .",
        "ruff format --check .",
        "mypy .",
        "tools/smoke_offline.py",
        "tools/check_syntax.py",
        "tools/smoke_staging.py",
        "tools/check_docs.py",
        "tools/check_command_docs.py",
        "pytest --collect-only",
        "pytest --cov=icbot",
    )
    for cmd in required_commands:
        assert cmd in step_runs, f".github/workflows/ci.yml missing required gate command: {cmd}"

    # Safety boundary: never start live gateway or trigger Railway deploys
    assert "--allow-gateway" not in step_runs
    assert "railway" not in step_runs.lower()
    assert "deploy" not in step_runs.lower()


def test_doc_and_image_validator_rejects_missing_screenshots_and_missing_docs(tmp_path: Path) -> None:
    """Verify tools/check_docs.py rejects missing Markdown and HTML screenshot paths and broken doc links."""
    (tmp_path / "docs").mkdir()
    sample = tmp_path / "README.md"
    sample.write_text(
        "\n".join(
            [
                "# Sample Repo",
                "![Missing Shot](assets/screenshots/01-missing.png)",
                '<img src="assets/screenshots/02-missing.png" alt="Missing HTML shot" />',
                "See [Missing Guide](docs/nonexistent.md) for details.",
            ]
        ),
        encoding="utf-8",
    )

    report = validate_markdown_files([sample], repo_root=tmp_path, check_anchors=True)
    assert report.ok is False
    img_violations = [v for v in report.violations if v.kind == "missing_image"]
    doc_violations = [v for v in report.violations if v.kind == "missing_local_target"]
    assert len(img_violations) == 2, f"Expected 2 missing screenshot violations, got {img_violations}"
    assert len(doc_violations) == 1, f"Expected 1 missing doc link violation, got {doc_violations}"


def test_pr_a_through_d_release_invariants_and_verification_ledger() -> None:
    """Verify key PR A-D deliverables and docs/release-verification.md blockers checklist are intact."""
    # PR A & B: Core hardened runtime modules & failure taxonomy / UI contracts exist
    from typing import get_args

    from icbot import config_parsers, errors, theme, ui
    from icbot.academics import exam_codecs, exams_service
    from icbot.services.http_transport import ProviderCircuitBreaker

    assert len(get_args(errors.FailureKind)) == 12
    assert hasattr(ui, "BasePaginationView") and hasattr(ui, "SHARED_INTERACTION_EXCEPTIONS")
    assert theme.DEFAULT_THEME_NAME == "instigate_cafe"
    assert ProviderCircuitBreaker is not None

    # PR D: Clean re-exports and tooling layout
    assert exams_service.parse_exams_csv is exam_codecs.parse_exams_csv
    assert config_parsers.parse_int is not None
    assert not (PROJECT_ROOT / "AGENTS.md").exists()
    assert (PROJECT_ROOT / "scripts" / "admin" / "purge_global_commands.py").is_file()
    assert (PROJECT_ROOT / "tools" / "verify_offline.py").is_file()

    # PR C: Published command routes match live registry (53/53)
    specs = introspect_command_tree()
    cmd_report = validate_command_docs(
        specs,
        features_text=(PROJECT_ROOT / "docs" / "features.md").read_text(encoding="utf-8"),
        readme_text=(PROJECT_ROOT / "README.md").read_text(encoding="utf-8"),
        config_text=(PROJECT_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"),
    )
    assert cmd_report.ok, f"Command route divergence: {cmd_report.violations}"

    # E1: Release verification ledger documents unexecuted checks and Git history sensitivity blocker
    ledger_path = PROJECT_ROOT / "docs" / "release-verification.md"
    assert ledger_path.is_file(), "docs/release-verification.md is missing"
    ledger = ledger_path.read_text(encoding="utf-8")
    for required_phrase in (
        "Not Executed Locally",
        "Release-Blockers Checklist",
        "Git History Sensitivity Review",
        "does **not** prove Git history is clean",
        "Python 3.11 & 3.13",
        "pip-audit",
        "Docker",
        "CI Run URL",
    ):
        assert required_phrase in ledger, f"docs/release-verification.md missing '{required_phrase}'"
