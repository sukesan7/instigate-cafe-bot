# tests/unit/test_portfolio_presentation_and_launch_assets.py
"""Unit tests verifying README presentation, compact docs architecture, and hygiene."""

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


def test_readme_portfolio_presentation_and_limits() -> None:
    """Verify README.md contains honest provider limits, staging previews, and discovery tags."""
    readme_path = PROJECT_ROOT / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    # Badges (must use real workflow badge and avoid hardcoded stale test counts)
    assert "v1.0.0-rc1" in content
    assert "sukesan7/instigate-cafe-bot/actions/workflows/ci.yml/badge.svg" in content
    assert "646 Passing" not in content

    # Honest Provider Limits Table
    assert "Honest Provider Comparison & Operating Limits" in content
    assert "RateMyProfessors" in content
    assert "Metrolinx GO" in content
    assert "TTC Alerts" in content
    assert "Career ATS Feeds" in content
    assert "Google Gemini AI" in content
    assert "Market Quotes" in content
    assert "SQLite WAL Storage" in content

    # Visual Showcase & Canonical Docs Index Links
    assert "Staging Visual Showcase" in content
    assert "docs/README.md" in content
    assert "docs/features.md" in content
    assert "docs/deployment.md" in content

    # GitHub Topics
    assert "GitHub Topics & Discovery Tags" in content
    assert "discord-bot" in content
    assert "clean-architecture" in content


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
