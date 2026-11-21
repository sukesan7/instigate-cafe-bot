# tests/unit/test_portfolio_presentation_and_launch_assets.py
"""Unit tests verifying GitHub portfolio presentation, launch assets, and release consistency."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_launch_assets_document_structure_and_completeness() -> None:
    """Verify docs/launch-assets.md contains all required launch sections and portfolio components."""
    assets_path = PROJECT_ROOT / "docs/launch-assets.md"
    assert assets_path.is_file(), "docs/launch-assets.md is missing"

    content = assets_path.read_text(encoding="utf-8")
    assert len(content) > 3000, f"docs/launch-assets.md is unexpectedly short ({len(content)} chars)"

    # 1. Four High-Quality Staging Mockups
    assert "Academic Schedule & Exam Countdown" in content
    assert "/ic exam list" in content
    assert "Career Tools & AI Resume Consent Barrier" in content
    assert "/ic resume" in content
    assert "Submit for AI Review" in content
    assert "View No-AI Checklist" in content
    assert "Regional Transit & Commuter Advisory" in content
    assert "Metrolinx GO" in content
    assert "Virtual Equity Exchange & Financial Ledger" in content
    assert "Instigate Cafe Stock Exchange" in content or "ICSE" in content

    # 2. 45-90 Second Video Storyboard
    assert "Demo Video Storyboard" in content
    assert "0:00" in content
    assert "0:15" in content
    assert "0:35" in content
    assert "0:55" in content
    assert "0:75" in content
    assert "0:90" in content

    # 3. Professional LinkedIn Post Draft
    assert "LinkedIn Announcement Post" in content
    assert "v1.0.0-rc1" in content
    assert "Decoupled Architecture" in content or "decoupled" in content.lower()
    assert "ACID" in content
    assert "offline pytest suite" in content.lower()
    assert "Software Engineering" in content
    assert "Commerce" in content
    assert "https://github.com/sukesan7/instigate-cafe-bot" in content

    # 4. GitHub Topics & Discovery Tags
    assert "GitHub Topics" in content
    assert "discord-bot" in content
    assert "sqlite-wal" in content
    assert "clean-architecture" in content

    # 5. Owner-Editable Launch Checklist
    assert "Owner-Editable Launch Checklist" in content
    assert "Repository Visibility Decision" in content
    assert "Live Bot Hosting Decision" in content
    assert "Railway" in content

    # 6. Version 1.1 Follow-Up Roadmap Outline
    assert "Version 1.1 Follow-Up Roadmap Outline" in content
    assert "Multi-Campus Adapter Registry" in content
    assert "Web Event & Exam Dashboard" in content


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

    # Visual Showcase & Links
    assert "Staging Visual Showcase" in content
    assert "docs/launch-assets.md" in content

    # GitHub Topics
    assert "GitHub Topics & Discovery Tags" in content
    assert "discord-bot" in content
    assert "clean-architecture" in content


def test_launch_assets_contain_zero_sensitive_data() -> None:
    """Verify docs/launch-assets.md uses strictly synthetic data and zero credentials or real lore."""
    assets_path = PROJECT_ROOT / "docs/launch-assets.md"
    content = assets_path.read_text(encoding="utf-8")

    # Must only use synthetic guild IDs
    assert "900000000000000001" in content
    assert "900000000000000002" in content

    # Forbidden private lore or real tokens
    forbidden_tokens = ["MTAw", "AIzaSy", "ghp_", "sk-"]
    for tok in forbidden_tokens:
        assert tok not in content, f"Detected potential credential token '{tok}' in launch assets"
