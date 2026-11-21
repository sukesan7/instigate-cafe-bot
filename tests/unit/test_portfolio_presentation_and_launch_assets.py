# tests/unit/test_portfolio_presentation_and_launch_assets.py
"""Unit tests verifying restrained README presentation, compact docs architecture, and hygiene."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_compact_public_docs_index_and_scope_clarity() -> None:
    """Verify docs/README.md indexes the compact public layout and clarifies hosting & campus scope."""
    index_path = PROJECT_ROOT / "docs/README.md"
    assert index_path.is_file(), "docs/README.md is missing"

    content = index_path.read_text(encoding="utf-8")
    for expected_doc in (
        "features.md",
        "configuration.md",
        "deployment.md",
        "troubleshooting.md",
        "architecture.md",
        "providers.md",
        "privacy.md",
        "ui-style.md",
        "development.md",
        "testing.md",
        "release-evidence.md",
        "screenshots-guide.md",
    ):
        assert expected_doc in content, f"docs/README.md does not link to {expected_doc}"

    assert "Self-Hosted Open-Source Bot" in content
    assert "Publicly Inviteable Hosted Bot" in content
    assert "TMU" in content

    # Historical planning/launch reports should not live in public docs/
    for archived_path in (
        "docs/launch-assets.md",
        "docs/alpha-beta-readiness.md",
        "docs/usability_review.md",
        "docs/audit/architecture_inventory.md",
    ):
        assert not (PROJECT_ROOT / archived_path).exists(), (
            f"Historical/planning document should be archived outside docs/: {archived_path}"
        )


def test_readme_restrained_landing_page_and_honest_boundaries() -> None:
    """Verify README.md is concise, honest, visually restrained, and free of fake screenshot links."""
    readme_path = PROJECT_ROOT / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    # Brand hero with real local logo and compact factual badges (no rainbow badge wall or stale counts)
    assert "assets/ic_logo.png" in content
    assert "v1.0.0-rc1" in content
    assert "sukesan7/instigate-cafe-bot/actions/workflows/ci.yml/badge.svg" in content
    assert "646 Passing" not in content
    assert "<details>" not in content, "README should not embed a giant accordion command inventory"
    assert "┌───" not in content, "README should not embed a giant ASCII-art architecture chart"

    # 6 core feature areas
    for domain in (
        "Academics",
        "Careers",
        "AI Resume Assistance",
        "Community & Economy",
        "Transit & Market Feeds",
        "Administration",
    ):
        assert domain in content, f"README.md missing feature domain: {domain}"

    # Self-hosted vs hosted distinction and honest TMU/campus scope
    assert "Self-Hosted" in content
    assert "Publicly Inviteable Hosted Bot" in content
    assert "Toronto Metropolitan University" in content

    # Fresh-checkout Quick Start with Python 3.12/3.13, .env.example, and offline test command
    assert "3.12" in content and "3.13" in content
    assert ".env.example" in content
    assert "uv sync --dev" in content
    assert "uv run pytest" in content
    assert "--allow-gateway" in content

    # Reserved Screenshots / Gallery section without fake image URLs
    assert "Screenshots / Gallery" in content
    assert "docs/screenshots-guide.md" in content
    assert "assets/screenshots/" not in content, "README must not link nonexistent screenshots yet"

    # Key doc links
    for link_target in (
        "docs/README.md",
        "docs/features.md",
        "docs/configuration.md",
        "docs/deployment.md",
        "docs/privacy.md",
        "docs/providers.md",
        "docs/troubleshooting.md",
        "docs/release-evidence.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert link_target in content, f"README.md missing link to {link_target}"


def test_screenshots_guide_defines_redaction_and_owner_only_capture() -> None:
    """Verify docs/screenshots-guide.md specifies command states, redaction rules, and owner-only boundary."""
    guide_path = PROJECT_ROOT / "docs/screenshots-guide.md"
    assert guide_path.is_file(), "docs/screenshots-guide.md is missing"
    content = guide_path.read_text(encoding="utf-8")

    assert "manual owner-run" in content.lower()
    assert "redaction" in content.lower()
    assert "DISCORD_TOKEN" in content
    assert "/ic resume" in content
    assert "/ic exam list" in content
    assert "/ic admin setup" in content


def test_public_docs_contain_zero_sensitive_tokens() -> None:
    """Verify README.md and all public docs/*.md contain zero real credential token prefixes."""
    forbidden_tokens = ["AIzaSy", "ghp_", "sk-"]
    for md_file in [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md"))]:
        content = md_file.read_text(encoding="utf-8")
        for tok in forbidden_tokens:
            if md_file.name == "configuration.md" and tok == "AIzaSy":
                # configuration.md shows placeholder example `AIzaSy...` (6 chars, not a key)
                assert "AIzaSy..." in content
                continue
            assert tok not in content, f"Detected potential credential token '{tok}' in {md_file.name}"
