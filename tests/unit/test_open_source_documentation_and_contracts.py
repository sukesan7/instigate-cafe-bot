"""Unit tests verifying open-source documentation accuracy, command parity, and privacy safeguards."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from icbot.config import Settings
from icbot.registry import load_feature_modules, registered_qualified_names

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_required_documentation_files_exist_and_non_empty() -> None:
    """Verify all canonical public documentation, operational, policy, and community files exist."""
    required_files = [
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "docs/README.md",
        "docs/features.md",
        "docs/architecture.md",
        "docs/configuration.md",
        "docs/deployment.md",
        "docs/development.md",
        "docs/privacy.md",
        "docs/providers.md",
        "docs/testing.md",
        "docs/troubleshooting.md",
        "docs/ui-style.md",
        "docs/release-evidence.md",
        "docs/release-verification.md",
        "docs/screenshots-guide.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]

    for rel_path in required_files:
        full_path = PROJECT_ROOT / rel_path
        assert full_path.is_file(), f"Expected documentation file missing: {rel_path}"
        content = full_path.read_text(encoding="utf-8").strip()
        assert len(content) > 100, (
            f"Documentation file {rel_path} appears empty or truncated ({len(content)} chars)"
        )


def test_readme_documents_all_registered_leaf_commands() -> None:
    """Verify docs/features.md documents all 53 leaf commands and README.md highlights verified commands."""
    readme_content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    features_content = (PROJECT_ROOT / "docs/features.md").read_text(encoding="utf-8")

    load_feature_modules()
    registered = registered_qualified_names()
    assert len(registered) == 53, f"Expected exactly 53 registered leaf commands, got {len(registered)}"

    for name in registered:
        # A command name like '/ic academics course' or leaf token 'course'
        tokens = name.split()
        leaf = tokens[-1]
        assert leaf in features_content, (
            f"Leaf command '{name}' ({leaf}) is missing from docs/features.md documentation"
        )

    # README stays concise with 6-10 verified representative commands and links to docs/features.md
    assert "docs/features.md" in readme_content
    rep_section = readme_content.split("## Representative Commands", 1)[1].split("\n---", 1)[0]
    mentioned_verified = [name for name in registered if f"`/{name}`" in rep_section]
    assert 6 <= len(mentioned_verified) <= 10, (
        f"Expected 6-10 verified slash commands highlighted in README.md Representative Commands, "
        f"found {len(mentioned_verified)}: {mentioned_verified}"
    )


def test_configuration_doc_covers_settings_fields() -> None:
    """Verify docs/configuration.md documents all Settings fields."""
    config_doc = (PROJECT_ROOT / "docs/configuration.md").read_text(encoding="utf-8")

    # Fields that are internal parsing state or derived and don't need direct env configuration
    internal_fields = {"guild_id_raw", "guild_id_invalid"}

    fields = [f.name for f in dataclasses.fields(Settings) if f.name not in internal_fields]

    for field_name in fields:
        assert field_name in config_doc, (
            f"Settings field '{field_name}' is not documented in docs/configuration.md"
        )


def test_architecture_doc_contains_required_mermaid_diagrams() -> None:
    """Verify docs/architecture.md includes sequence diagrams for interactions, background tasks, and economy."""
    arch_doc = (PROJECT_ROOT / "docs/architecture.md").read_text(encoding="utf-8")

    assert "```mermaid" in arch_doc
    assert "sequenceDiagram" in arch_doc
    # Diagram 1: Slash command interaction routing
    assert "check_command_gate" in arch_doc
    # Diagram 2: Periodic task poller & outage dampening
    assert "ProviderOutageTracker" in arch_doc or "CircuitBreaker" in arch_doc
    # Diagram 3: Double-entry economy transaction
    assert "BEGIN IMMEDIATE" in arch_doc or "execute_transaction" in arch_doc


def test_privacy_doc_contains_data_handling_invariants() -> None:
    """Verify docs/privacy.md specifies zero-disk resume handling, data minimization, and deletion."""
    privacy_doc = (PROJECT_ROOT / "docs/privacy.md").read_text(encoding="utf-8")

    assert "ephemeral" in privacy_doc.lower()
    assert "zero disk" in privacy_doc.lower()
    assert "delete_user_data" in privacy_doc
    assert "Right-to-be-Forgotten" in privacy_doc or "right to be forgotten" in privacy_doc.lower()


def test_operations_doc_contains_backup_and_scheduler_invariants() -> None:
    """Verify docs/deployment.md and docs/troubleshooting.md cover single scheduler, diagnostics, and hot backups."""
    deploy_doc = (PROJECT_ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    troubleshoot_doc = (PROJECT_ROOT / "docs/troubleshooting.md").read_text(encoding="utf-8")

    assert "VACUUM INTO" in deploy_doc
    assert "single" in deploy_doc.lower() and "replicas: 1" in deploy_doc.lower()
    assert "/ic status" in troubleshoot_doc
    assert "ProviderCircuitBreaker" in troubleshoot_doc


def test_providers_doc_contains_contributor_protocol_and_mocking() -> None:
    """Verify docs/providers.md contains step-by-step guidance for safe campus provider contributions."""
    prov_doc = (PROJECT_ROOT / "docs/providers.md").read_text(encoding="utf-8")

    assert "AcademicProviderProtocol" in prov_doc
    assert "mock" in prov_doc.lower() or "synthetic" in prov_doc.lower()
    assert "SSRF" in prov_doc or "validate_url" in prov_doc


def test_issue_and_pr_templates_contain_safety_and_privacy_checks() -> None:
    """Verify GitHub templates contain privacy warnings against pasting tokens or private server data."""
    issue_tmpl = (PROJECT_ROOT / ".github/ISSUE_TEMPLATE/feature_request.md").read_text(encoding="utf-8")
    pr_tmpl = (PROJECT_ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    coc = (PROJECT_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")

    assert "token" in issue_tmpl.lower() or "secret" in issue_tmpl.lower() or "privacy" in issue_tmpl.lower()
    assert "offline" in pr_tmpl.lower()
    assert "secret" in pr_tmpl.lower() or "credentials" in pr_tmpl.lower()
    assert "imsukesan@gmail.com" in coc or "reporting" in coc.lower()
