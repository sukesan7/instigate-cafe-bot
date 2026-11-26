"""Unit tests for the offline Markdown documentation validator (`tools/check_docs.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_docs import (
    PROJECT_ROOT,
    list_tracked_markdown_files,
    validate_markdown_files,
)

pytestmark = pytest.mark.unit


def test_all_tracked_repository_markdown_files_pass_offline_validation() -> None:
    """Verify every tracked/public Markdown file passes link, image, anchor, and placeholder checks."""
    tracked = list_tracked_markdown_files(PROJECT_ROOT)
    assert len(tracked) >= 14, f"Expected at least 14 markdown files, found {len(tracked)}"

    # Verify compact flat docs/ layout (zero legacy nested subdirectories)
    docs_root = PROJECT_ROOT / "docs"
    nested_dirs = [p.name for p in docs_root.iterdir() if p.is_dir()]
    assert nested_dirs == [], f"Unexpected nested directories under docs/: {nested_dirs}"

    report = validate_markdown_files(tracked, repo_root=PROJECT_ROOT, check_anchors=True)
    assert report.ok, "Documentation validation failed:\n" + "\n".join(
        f"  {v.file}:{v.line} [{v.kind}] {v.message}" for v in report.violations
    )
    assert report.local_links_checked >= 45
    assert report.local_images_checked >= 1


def test_public_repo_contains_no_private_agent_scaffolding_or_prompt_pack_references() -> None:
    """Verify AGENTS.md is removed and public docs/templates do not reference internal agent prompt packs."""
    assert not (PROJECT_ROOT / "AGENTS.md").exists(), (
        "Private agent rules file AGENTS.md should not be tracked"
    )

    forbidden_terms = (
        "AGENTS.md",
        "START_HERE.md",
        "RELEASE_GATES.md",
        "phase_XX",
        "Phase XX",
        "prompt E3",
        "Antigravity",
    )
    public_files = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "CONTRIBUTING.md",
        PROJECT_ROOT / "SECURITY.md",
        *sorted((PROJECT_ROOT / "docs").glob("*.md")),
        *sorted((PROJECT_ROOT / ".github").rglob("*.md")),
        *sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml")),
    ]
    for path in public_files:
        content = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in content, (
                f"Internal agent/prompt reference '{term}' found in {path.relative_to(PROJECT_ROOT)}"
            )


def test_validator_rejects_file_uris_missing_links_images_anchors_placeholders_and_unknown_hosts(
    tmp_path: Path,
) -> None:
    """Verify negative cases are caught cleanly on synthetic Markdown fixtures."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text("# Valid Heading\n\nBody text.\n", encoding="utf-8")
    (tmp_path / "docs" / "bad.md").write_text(
        "\n".join(
            [
                "# Sample Doc",
                "- Workstation link: [Config](file:///home/user/repo/config.py)",
                "- Missing local code link: [Missing](../icbot/does_not_exist.py)",
                "- Missing relative image: ![Diagram](images/missing.png)",
                "- Broken heading anchor: [Bad Anchor](target.md#nonexistent-heading)",
                "- Forbidden placeholder: `git clone https://github.com/your-username/campus-bot.git`",
                "- Dead script: `tools/restore_db.py` and `icbot/storage/user.py`",
                "- Unresolved marker: TODO: finish this section",
                "- Unallowlisted external host: [Unknown](https://untrusted-host.example/foo)",
                "- Valid relative link + anchor: [Target](target.md#valid-heading)",
            ]
        ),
        encoding="utf-8",
    )

    report = validate_markdown_files(
        [tmp_path / "docs" / "bad.md"],
        repo_root=tmp_path,
        check_anchors=True,
    )
    assert report.ok is False
    kinds = {v.kind for v in report.violations}
    assert "file_uri" in kinds
    assert "missing_local_target" in kinds
    assert "missing_image" in kinds
    assert "missing_anchor" in kinds
    assert "forbidden_placeholder" in kinds
    assert "unallowlisted_external_url" in kinds


def test_non_git_archive_fallback_excludes_cache_and_build_dirs_but_catches_real_errors(
    tmp_path: Path,
) -> None:
    """Verify list_tracked_markdown_files excludes generated caches in non-.git archives while keeping real docs."""
    # Legitimate repository docs
    (tmp_path / "README.md").write_text("# Root\n\nSee [Guide](docs/guide.md).\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\nOK.\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("# PR Checklist\n", encoding="utf-8")

    # Generated cache / venv / build / coverage dirs that contain invalid/irrelevant Markdown
    for excluded_dir in (
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "htmlcov",
        "artifacts",
        "node_modules",
    ):
        d = tmp_path / excluded_dir
        d.mkdir(parents=True)
        (d / "README.md").write_text("[Broken Link](does_not_exist.md)\n", encoding="utf-8")

    discovered = list_tracked_markdown_files(tmp_path)
    rels = {p.relative_to(tmp_path).as_posix() for p in discovered}
    assert rels == {"README.md", "docs/guide.md", ".github/PULL_REQUEST_TEMPLATE.md"}

    report = validate_markdown_files(discovered, repo_root=tmp_path, check_anchors=True)
    assert report.ok is True

    # Introduce an actual documentation error in a legitimate doc -> still fails
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\n[Dead](missing.md)\n", encoding="utf-8")
    report_bad = validate_markdown_files(discovered, repo_root=tmp_path, check_anchors=True)
    assert report_bad.ok is False
    assert any(v.kind == "missing_local_target" and v.file == "docs/guide.md" for v in report_bad.violations)
