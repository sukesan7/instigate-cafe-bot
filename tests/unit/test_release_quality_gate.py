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
    audit_runs = "\n".join(str(s.get("run", "")) for s in workflow["jobs"]["dependency-audit"]["steps"])
    required_offline_commands = (
        "uv lock --check",
        "uv sync --frozen --dev",
        "tools/scan_secrets.py --enforce-clean",
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
    for cmd in required_offline_commands:
        assert cmd in step_runs, f".github/workflows/ci.yml missing required offline gate command: {cmd}"

    assert "pip-audit" not in step_runs, "Offline matrix job must not invoke networked pip-audit"
    assert "pip-audit" in audit_runs, "dependency-audit job must invoke pip-audit"

    # Safety boundary: never start live gateway or trigger Railway deploys
    all_runs = f"{step_runs}\n{audit_runs}"
    assert "--allow-gateway" not in all_runs
    assert "railway" not in all_runs.lower()
    assert "deploy" not in all_runs.lower()


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


def test_pr_a_through_d_release_invariants() -> None:
    """Verify key runtime invariants, clean layout, and command-doc parity remain intact."""
    from typing import get_args

    from icbot import config_parsers, errors, theme, ui
    from icbot.academics import exam_codecs, exams_service
    from icbot.services.http_transport import ProviderCircuitBreaker

    assert len(get_args(errors.FailureKind)) == 12
    assert hasattr(ui, "BasePaginationView") and hasattr(ui, "SHARED_INTERACTION_EXCEPTIONS")
    assert theme.DEFAULT_THEME_NAME == "instigate_cafe"
    assert ProviderCircuitBreaker is not None

    assert exams_service.parse_exams_csv is exam_codecs.parse_exams_csv
    assert config_parsers.parse_int is not None
    assert not (PROJECT_ROOT / "AGENTS.md").exists()
    assert (PROJECT_ROOT / "scripts" / "admin" / "purge_global_commands.py").is_file()
    assert (PROJECT_ROOT / "tools" / "verify_offline.py").is_file()

    # Published command routes match live registry (53/53)
    specs = introspect_command_tree()
    cmd_report = validate_command_docs(
        specs,
        features_text=(PROJECT_ROOT / "docs" / "features.md").read_text(encoding="utf-8"),
        readme_text=(PROJECT_ROOT / "README.md").read_text(encoding="utf-8"),
        config_text=(PROJECT_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"),
    )
    assert cmd_report.ok, f"Command route divergence: {cmd_report.violations}"

    # Pre-publication Git history hygiene warning remains documented in deployment & security policy
    deploy_doc = (PROJECT_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "Clean Git history before making a repo public" in deploy_doc
    assert "tools/scan_secrets.py --enforce-clean" in deploy_doc


def test_discord_slash_command_payload_within_api_limits() -> None:
    """Verify the serialized /ic command tree fits Discord's hard API limits (<=4000 chars, <=25 options/choices)."""
    import discord
    from discord import app_commands

    from icbot.registry import get_root_group, load_feature_modules

    load_feature_modules()
    root = get_root_group()
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    tree.add_command(root)
    payload = root.to_dict(tree)

    def _count_discord_chars(obj: dict[str, object]) -> int:
        total = len(str(obj.get("name", ""))) + len(str(obj.get("description", "")))
        for ch in obj.get("choices", []) or []:  # type: ignore[attr-defined]
            total += len(str(ch.get("name", "")))
            if isinstance(ch.get("value"), str):
                total += len(ch["value"])
        for opt in obj.get("options", []) or []:  # type: ignore[attr-defined]
            total += _count_discord_chars(opt)
        return total

    def _assert_node_limits(obj: dict[str, object], path: str) -> None:
        name = str(obj.get("name", ""))
        desc = str(obj.get("description", ""))
        assert 1 <= len(name) <= 32, f"{path}: invalid name length {len(name)}"
        assert 1 <= len(desc) <= 100, f"{path}: invalid description length {len(desc)}"
        opts = obj.get("options", []) or []
        choices = obj.get("choices", []) or []
        assert len(opts) <= 25, f"{path}: {len(opts)} options exceeds Discord max 25"  # type: ignore[arg-type]
        assert len(choices) <= 25, f"{path}: {len(choices)} choices exceeds Discord max 25"  # type: ignore[arg-type]
        for opt in opts:  # type: ignore[attr-defined]
            _assert_node_limits(opt, f"{path}/{opt.get('name')}")

    _assert_node_limits(payload, f"/{payload['name']}")
    total_chars = _count_discord_chars(payload)
    assert total_chars <= 4000, (
        f"/{payload['name']} serialized payload is {total_chars} chars, exceeding Discord's hard 4000-char limit."
    )
