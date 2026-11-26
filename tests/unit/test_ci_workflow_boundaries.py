"""Regression tests for honest CI workflow boundaries, fork safety, and offline verification invariants."""

from __future__ import annotations

import http.client
import re
import socket
import urllib.request
from pathlib import Path

import aiohttp
import pytest
import yaml

from tests.helpers import OfflineNetworkDisabledError
from tools import verify_offline

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"Expected YAML mapping in {path}"
    return loaded


def test_ci_workflow_splits_offline_quality_from_networked_dependency_audit() -> None:
    """Verify ci.yml uses honest naming and isolates offline matrix checks from networked pip-audit."""
    ci_path = WORKFLOWS_DIR / "ci.yml"
    raw = ci_path.read_text(encoding="utf-8")
    workflow = _load_yaml(ci_path)

    assert workflow.get("name") == "Quality & Security Gate"
    assert "Offline Quality Gate" not in raw
    assert not re.search(r"\bsecrets\.[A-Za-z0-9_]+", raw), (
        "PR CI workflow must never reference repository secrets"
    )

    assert workflow.get("permissions") == {"contents": "read"}
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    assert {"quality", "dependency-audit"}.issubset(jobs.keys())

    quality_job = jobs["quality"]
    audit_job = jobs["dependency-audit"]
    assert isinstance(quality_job, dict)
    assert isinstance(audit_job, dict)

    assert quality_job.get("permissions") == {"contents": "read"}
    assert audit_job.get("permissions") == {"contents": "read"}

    quality_runs = "\n".join(str(s.get("run", "")) for s in quality_job.get("steps", []))
    audit_runs = "\n".join(str(s.get("run", "")) for s in audit_job.get("steps", []))

    assert "pip-audit" not in quality_runs, "Offline matrix job must not invoke networked pip-audit"
    assert "uv lock --check" in quality_runs
    assert "uv sync --frozen --dev" in quality_runs
    assert "tools/smoke_offline.py" in quality_runs
    assert "tools/smoke_staging.py" in quality_runs

    assert "uv lock --check" in audit_runs
    assert "uv sync --frozen --dev" in audit_runs
    assert "pip-audit" in audit_runs
    assert "network" in str(audit_job.get("name", "")).lower()


def test_all_workflows_pin_actions_limit_cache_and_forbid_gateway_or_schedules() -> None:
    """Verify every workflow pins third-party actions, isolates artifacts/cache, and avoids gateway/deploys."""
    pinned_semver_or_sha = re.compile(r"^[^@]+@(?:v\d+\.\d+\.\d+|[0-9a-f]{40})$")
    forbidden_artifact_tokens = (".db", ".sqlite", ".env", "lore.", "docs/")

    for wf_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        raw = wf_path.read_text(encoding="utf-8")
        workflow = _load_yaml(wf_path)
        triggers = workflow.get("on") or workflow.get(True) or {}
        assert isinstance(triggers, dict)
        assert "schedule" not in triggers, f"{wf_path.name} must not define cron schedules"
        assert workflow.get("permissions") == {"contents": "read"}

        assert "--allow-gateway" not in raw
        assert "railway up" not in raw.lower()

        jobs = workflow.get("jobs", {})
        assert isinstance(jobs, dict)
        for job_name, job_cfg in jobs.items():
            assert isinstance(job_cfg, dict)
            for step in job_cfg.get("steps", []):
                uses = step.get("uses")
                if uses:
                    assert pinned_semver_or_sha.match(uses), (
                        f"{wf_path.name}:{job_name} action '{uses}' must pin vX.Y.Z or full commit SHA"
                    )
                    if uses.startswith("astral-sh/setup-uv@"):
                        with_cfg = step.get("with", {})
                        assert with_cfg.get("cache-dependency-glob") == "uv.lock", (
                            f"{wf_path.name}:{job_name} setup-uv must scope cache key to uv.lock"
                        )
                    if uses.startswith("actions/upload-artifact@"):
                        artifact_path = str(step.get("with", {}).get("path", ""))
                        assert artifact_path == "coverage.xml"
                        for bad in forbidden_artifact_tokens:
                            assert bad not in artifact_path.lower()


def test_docs_and_pr_template_distinguish_ci_gate_from_offline_checks() -> None:
    """Verify badge labels and contributor docs no longer mislabel networked ci.yml as offline-only."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    dev_doc = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    contrib_doc = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    pr_template = (REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")

    assert "[![Quality & Security Gate](" in readme
    assert "Offline Quality Gate" not in readme
    assert "(Quality & Security Gate)" in dev_doc
    assert "`Quality & Security Gate`" in contrib_doc
    assert "Offline Quality Gate" not in dev_doc
    assert "Offline Verification Checklist" in pr_template


@pytest.mark.asyncio
async def test_offline_test_harness_blocks_all_external_network_apis() -> None:
    """Verify socket, urllib, http.client, and aiohttp fail closed against external targets in pytest."""
    with pytest.raises(OfflineNetworkDisabledError):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("93.184.216.34", 443))
        finally:
            sock.close()

    with pytest.raises(OfflineNetworkDisabledError):
        socket.create_connection(("example.com", 443), timeout=1)

    with pytest.raises(OfflineNetworkDisabledError):
        urllib.request.urlopen("https://example.com/", timeout=1)

    with pytest.raises(OfflineNetworkDisabledError):
        conn = http.client.HTTPSConnection("example.com", timeout=1)
        conn.connect()

    async with aiohttp.ClientSession() as session:
        with pytest.raises(OfflineNetworkDisabledError):
            await session.get("https://example.com/")


def test_verify_offline_reports_actionable_remediation_on_stage_failure(tmp_path: Path) -> None:
    """Verify tools/verify_offline.py surfaces failing command snippets and remediation hints."""
    failed = verify_offline._run_stage(
        "Sample failing gate",
        [verify_offline.sys.executable, "-c", "import sys; print('missing marker'); sys.exit(2)"],
        env={"PATH": ""},
        remediation="Run the documented repair command.",
        repo_root=tmp_path,
    )
    assert failed.ok is False
    assert "exit 2" in failed.detail
    assert "missing marker" in failed.detail
    assert failed.remediation == "Run the documented repair command."
