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
    """Verify every tracked Markdown file has zero file:/// URIs, broken links/images/anchors, or placeholders."""
    tracked = list_tracked_markdown_files(PROJECT_ROOT)
    assert len(tracked) >= 35, f"Expected at least 35 tracked markdown files, found {len(tracked)}"

    report = validate_markdown_files(tracked, repo_root=PROJECT_ROOT, check_anchors=True)
    assert report.ok, "Documentation validation failed:\n" + "\n".join(
        f"  {v.file}:{v.line} [{v.kind}] {v.message}" for v in report.violations
    )
    assert report.local_links_checked > 100


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
