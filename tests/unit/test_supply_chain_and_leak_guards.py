"""Unit tests for GitHub quality gates, software supply chain security, and leak guards."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.scan_secrets import (
    SECRET_REGEXES,
    is_forbidden_path,
)
from tools.scan_secrets import (
    main as scan_secrets_main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_forbidden_file_patterns_match_dangerous_artifacts():
    """Verify that is_forbidden_path blocks databases, WAL files, private envs, and credentials."""
    dangerous_paths = [
        "production.db",
        "data/users.sqlite",
        "var/cache.sqlite3",
        "campus.db-wal",
        "campus.db-shm",
        "campus.sqlite-wal",
        "campus.sqlite-shm",
        "campus.sqlite3-wal",
        "campus.sqlite3-shm",
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.backup",
        "keys/private.pem",
        "id_rsa",
        "id_ed25519",
        "tools/lore.private.json",
        "lore.server_secrets.json",
        "config/client_secret.json",
        "google_credentials.json",
    ]
    for path in dangerous_paths:
        assert is_forbidden_path(path), f"Expected '{path}' to be recognized as a forbidden artifact."


def test_forbidden_file_patterns_exempt_safe_templates():
    """Verify that example templates and test fixtures are explicitly exempted from blocking."""
    safe_paths = [
        ".env.example",
        "tools/lore.example.json",
        "tests/fixtures/synthetic_lore.json",
        "README.md",
        "icbot/config.py",
    ]
    for path in safe_paths:
        assert not is_forbidden_path(path), f"Expected '{path}' to be permitted as safe/exempt."


def test_secret_regexes_match_simulated_tokens():
    """Verify regex patterns identify Discord tokens, Gemini keys, and API secrets."""
    # Constructed dynamically so static file scanners do not flag the test file itself
    simulated_discord_token = "MTAwMDAwMDAwMDAwMDAwMDAwMA" + ".Gxx_xx." + ("x" * 38)
    simulated_gemini_key = "AIzaSy" + ("A" * 33)
    simulated_aws_key = "AKIA" + ("B" * 16)
    simulated_gh_pat = "ghp_" + ("C" * 36)
    simulated_priv_key = "-----" + "BEGIN " + "PRIVATE KEY-----"

    assert SECRET_REGEXES["Discord Bot Token"].search(simulated_discord_token)
    assert SECRET_REGEXES["Google Gemini API Key"].search(simulated_gemini_key)
    assert SECRET_REGEXES["AWS Access Key ID"].search(simulated_aws_key)
    assert SECRET_REGEXES["GitHub Personal Access Token"].search(simulated_gh_pat)
    assert SECRET_REGEXES["Private Key Header"].search(simulated_priv_key)


def test_ci_workflow_least_privilege_and_no_gateway():
    """Verify CI workflow enforces read-only contents, matrix support, zero tokens, and no gateway."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists(), "Missing .github/workflows/ci.yml"

    with open(ci_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    # 1. Least privilege permissions
    assert workflow.get("permissions") == {"contents": "read"}

    # 2. Concurrency isolation
    assert "concurrency" in workflow

    # 3. Safe environment overrides
    env = workflow.get("env", {})
    assert env.get("ICBOT_ALLOW_GATEWAY") == "false"
    assert env.get("DISCORD_TOKEN") == ""
    assert env.get("LLM_ENABLED") == "false"

    # 4. Matrix includes supported Python versions
    matrix = workflow["jobs"]["quality"]["strategy"]["matrix"]
    py_versions = matrix.get("python-version", [])
    assert "3.11" in py_versions
    assert "3.12" in py_versions
    assert "3.13" in py_versions

    # 5. Review steps across offline quality and networked dependency-audit jobs
    steps = workflow["jobs"]["quality"]["steps"]
    step_names = [s.get("name", "") for s in steps]
    step_runs = [s.get("run", "") for s in steps]

    assert any("secret" in name.lower() for name in step_names), "Missing secret scanning step name"
    assert not any("pip-audit" in r for r in step_runs), (
        "Offline quality job must not run networked pip-audit"
    )
    assert any("scan_secrets.py --enforce-clean" in r for r in step_runs), "Missing secret scanner step"
    assert any("smoke_offline.py" in r for r in step_runs), "Missing offline smoke check step"

    audit_job = workflow["jobs"]["dependency-audit"]
    assert audit_job.get("permissions") == {"contents": "read"}
    audit_runs = [s.get("run", "") for s in audit_job["steps"]]
    assert any("pip-audit" in r for r in audit_runs), "Missing pip-audit step in dependency-audit job"

    # Assert no steps execute live bot or deploy to Railway
    for r in [*step_runs, *audit_runs]:
        assert "main.py" not in r or "smoke_offline.py" in r, "CI must not invoke live main.py"
        assert "railway up" not in r, "CI must not contain Railway deployment triggers"
        assert "deploy" not in r.lower(), "CI must be strictly CI-only without deployment actions"


def test_live_smoke_workflow_gated_and_isolated_from_pr():
    """Verify live smoke workflow is never triggered on pull_request and is gated by credentials."""
    smoke_path = REPO_ROOT / ".github" / "workflows" / "live-smoke.yml"
    assert smoke_path.exists(), "Missing .github/workflows/live-smoke.yml"

    with open(smoke_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    # 1. Must never trigger on pull_request (YAML coerces unquoted 'on' to True)
    triggers = workflow.get("on") or workflow.get(True) or {}
    assert "pull_request" not in triggers, "Live smoke checks must NEVER run on pull requests"
    assert "workflow_dispatch" in triggers or "schedule" in triggers

    # 2. Least privilege permissions
    assert workflow.get("permissions") == {"contents": "read"}

    # 3. Gateway safety env
    assert workflow.get("env", {}).get("ICBOT_ALLOW_GATEWAY") == "false"

    # 4. Zero deployment triggers
    steps = workflow["jobs"]["live-smoke"]["steps"]
    step_runs = [s.get("run", "") for s in steps]
    for r in step_runs:
        assert "railway" not in r.lower()
        assert "deploy" not in r.lower()


def test_dependabot_configuration_syntax():
    """Verify dependabot configuration for pip and github-actions."""
    dep_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert dep_path.exists(), "Missing .github/dependabot.yml"

    with open(dep_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert config.get("version") == 2
    updates = config.get("updates", [])
    ecosystems = [u.get("package-ecosystem") for u in updates]
    assert "pip" in ecosystems
    assert "github-actions" in ecosystems

    for u in updates:
        assert u.get("schedule", {}).get("interval") == "weekly"


def test_open_source_governance_documents_exist():
    """Verify all required open-source governance and release documents exist and are populated."""
    required_docs = [
        ("LICENSE", "MIT License"),
        ("SECURITY.md", "Reporting a Vulnerability"),
        ("CONTRIBUTING.md", "Contributing to Instigate Cafe Bot"),
        ("CHANGELOG.md", "[0.2.4]"),
        ("docs/deployment.md", "Pre-Publication Security"),
    ]

    for filename, expected_content in required_docs:
        doc_path = REPO_ROOT / filename
        assert doc_path.exists(), f"Missing required documentation file: {filename}"
        content = doc_path.read_text(encoding="utf-8")
        assert len(content) > 100, f"File {filename} appears unexpectedly short."
        assert expected_content in content, f"File {filename} missing expected phrase '{expected_content}'."


def test_clean_secret_scan_enforce_clean():
    """Execute scan_secrets --enforce-clean on repository worktree and assert clean status."""
    exit_code = scan_secrets_main(["--enforce-clean"])
    assert exit_code == 0, "Expected secret scanner to exit with code 0 on clean worktree."
