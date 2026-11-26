# tests/unit/test_portfolio_presentation_and_launch_assets.py
"""Unit tests verifying restrained README presentation, compact docs architecture, and hygiene."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_compact_public_docs_index_and_scope_clarity() -> None:
    """Verify docs/README.md indexes the compact public layout and clarifies hosting & campus scope."""
    index_path = PROJECT_ROOT / "docs/README.md"
    assert index_path.is_file(), "docs/README.md is missing"

    content = index_path.read_text(encoding="utf-8")
    for expected_doc in (
        "features.md",
        "gallery.md",
        "configuration.md",
        "deployment.md",
        "architecture.md",
        "providers.md",
        "privacy.md",
        "development.md",
    ):
        assert expected_doc in content, f"docs/README.md does not link to {expected_doc}"

    assert "Self-Hosted Open-Source" in content
    assert "Hosted Public Bot" in content
    assert "TMU" in content

    # Consolidated/obsolete planning & release records should not remain under docs/
    for archived_path in (
        "docs/launch-assets.md",
        "docs/alpha-beta-readiness.md",
        "docs/usability_review.md",
        "docs/audit/architecture_inventory.md",
        "docs/screenshots-guide.md",
        "docs/staging-runbook.md",
        "docs/release-evidence.md",
        "docs/release-verification.md",
        "docs/testing.md",
        "docs/troubleshooting.md",
        "docs/ui-style.md",
    ):
        assert not (PROJECT_ROOT / archived_path).exists(), (
            f"Obsolete/merged document should not remain in docs/: {archived_path}"
        )


def test_readme_restrained_landing_page_and_honest_boundaries() -> None:
    """Verify README.md is concise, honest, visually restrained, and separates self-hosting from dev setup."""
    readme_path = PROJECT_ROOT / "README.md"
    content = readme_path.read_text(encoding="utf-8")
    contrib_content = (PROJECT_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    dev_content = (PROJECT_ROOT / "docs/development.md").read_text(encoding="utf-8")

    # Brand hero with real banner + attribution and compact factual badges
    assert "assets/ic_banner.png" in content
    assert "assets/ic_logo.png" in content
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject["project"]["version"]
    assert f"v{expected_version}" in content
    assert "v1.0.0-rc1" not in content
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

    # Self-hosting Quick Start uses runtime-only install + --allow-gateway; contributor setup lives in CONTRIBUTING/development
    assert "3.12" in content and "3.13" in content
    assert ".env.example" in content
    assert "uv sync --frozen --no-dev" in content
    assert "requirements.txt" in content
    assert "--allow-gateway" in content
    assert "uv sync --frozen --dev" in contrib_content and "uv sync --frozen --dev" in dev_content
    assert "tools/verify_offline.py" in contrib_content and "tools/verify_offline.py" in dev_content

    # Verified 2-column Gallery with real local optimized screenshots (zero placeholder/unredacted leaks)
    assert "## Gallery" in content
    assert "docs/gallery.md" in content
    for img_rel in (
        "assets/screenshots/01-getting-started.png",
        "assets/screenshots/02-notes.png",
        "assets/screenshots/03-jobs.png",
        "assets/screenshots/04-icse-market.png",
    ):
        assert img_rel in content, f"README.md Gallery missing verified screenshot: {img_rel}"
        img_path = PROJECT_ROOT / img_rel
        assert img_path.is_file(), f"Referenced screenshot does not exist: {img_rel}"
        raw = img_path.read_bytes()
        assert len(raw) < 500_000, f"Screenshot exceeds 500 KB budget: {img_rel}"
        assert b"eXIf" not in raw and b"tEXt" not in raw and b"iTXt" not in raw

    # Full gallery page includes additional verified screenshots and excludes unredacted captures
    gallery_content = (PROJECT_ROOT / "docs/gallery.md").read_text(encoding="utf-8")
    for extra_rel in (
        "assets/screenshots/05-help.png",
        "assets/screenshots/06-blackjack.png",
        "assets/screenshots/07-sports.png",
    ):
        assert extra_rel in gallery_content, f"docs/gallery.md missing verified screenshot: {extra_rel}"
        extra_path = PROJECT_ROOT / extra_rel
        assert extra_path.is_file(), f"Referenced gallery screenshot missing: {extra_rel}"
        raw = extra_path.read_bytes()
        assert len(raw) < 500_000
        assert b"eXIf" not in raw and b"tEXt" not in raw and b"iTXt" not in raw

    for unsafe_name in ("exam-list.png", "status.png", "resume.png"):
        assert unsafe_name not in content, f"Unredacted capture {unsafe_name} must not appear in README.md"
        assert unsafe_name not in gallery_content, (
            f"Unredacted capture {unsafe_name} must not appear in docs/gallery.md"
        )
        assert not (PROJECT_ROOT / "assets" / unsafe_name).exists(), (
            f"Unredacted capture assets/{unsafe_name} must not remain tracked in assets/"
        )

    # Key doc links
    for link_target in (
        "docs/README.md",
        "docs/gallery.md",
        "docs/features.md",
        "docs/configuration.md",
        "docs/deployment.md",
        "docs/privacy.md",
        "docs/providers.md",
        "docs/architecture.md",
        "docs/development.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert link_target in content, f"README.md missing link to {link_target}"


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
