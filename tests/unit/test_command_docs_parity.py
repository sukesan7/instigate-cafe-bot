"""Regression tests verifying live command-tree parity with docs/features.md and screenshots guide."""

from __future__ import annotations

from pathlib import Path

import pytest

from icbot.theme import DEFAULT_THEME_NAME, THEME_PRESETS
from tools.check_command_docs import (
    CommandSpec,
    introspect_command_tree,
    validate_command_docs,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.unit


def test_live_command_tree_matches_published_features_and_config_docs() -> None:
    """Verify every registered /ic command, option, and theme preset matches docs without drift."""
    specs = introspect_command_tree()
    assert len(specs) == 53, f"Expected 53 live leaf commands, got {len(specs)}"

    features_text = (PROJECT_ROOT / "docs/features.md").read_text(encoding="utf-8")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    config_text = (PROJECT_ROOT / "docs/configuration.md").read_text(encoding="utf-8")

    all_docs: dict[str, str] = {"README.md": readme_text}
    for md_path in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        all_docs[f"docs/{md_path.name}"] = md_path.read_text(encoding="utf-8")

    report = validate_command_docs(
        specs,
        features_text=features_text,
        readme_text=readme_text,
        config_text=config_text,
        all_docs_texts=all_docs,
    )
    assert report.ok, "Command documentation drift detected:\n" + "\n".join(
        f"  - {v}" for v in report.violations
    )
    assert report.registered_count == 53
    assert report.documented_count == 53

    # Verify default theme and every THEME_PRESETS key appear accurately in docs
    assert DEFAULT_THEME_NAME == "instigate_cafe"
    for preset_key in THEME_PRESETS:
        assert preset_key in features_text
        assert preset_key in config_text


def test_command_docs_validator_catches_missing_commands_typos_and_option_drift() -> None:
    """Negative test ensuring drift validator fails on omitted commands, /ic exams typo, and bad options."""
    sample_specs = [
        CommandSpec(
            qualified_name="ic exam list",
            module_id="academics",
            description="Show exams.",
            parameters=("course", "term"),
            required_parameters=(),
        ),
        CommandSpec(
            qualified_name="ic dailyclaim",
            module_id="economy",
            description="Claim daily reward.",
            parameters=(),
            required_parameters=(),
        ),
    ]
    bad_features = "\n".join(
        [
            "# Features",
            "Use `/ic exams` or `/ic daily` for quick checks.",
            "| Command | Options | Audience |",
            "| :--- | :--- | :--- |",
            "| `/ic exam list` | `course`, `wrong_option` | Public |",
            "| `/ic phantom` | None | Public |",
        ]
    )

    report = validate_command_docs(
        sample_specs,
        features_text=bad_features,
        readme_text="See `/ic exams` for details.",
    )
    assert report.ok is False
    joined = "\n".join(report.violations)
    assert "missing registered command '/ic dailyclaim'" in joined
    assert "documented command '/ic phantom' does not exist" in joined
    assert "option mismatch for '/ic exam list'" in joined
    assert "forbidden command typo /ic exams" in joined
    assert "forbidden command typo /ic daily" in joined


def test_gallery_screenshots_referenced_in_readme_and_gallery_exist() -> None:
    """Verify every gallery screenshot referenced in README.md and docs/gallery.md exists on disk."""
    import re

    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    gallery_text = (PROJECT_ROOT / "docs/gallery.md").read_text(encoding="utf-8")

    readme_refs = re.findall(r"assets/screenshots/[A-Za-z0-9_.-]+\.(?:png|webp)", readme_text)
    gallery_refs = re.findall(r"assets/screenshots/[A-Za-z0-9_.-]+\.(?:png|webp)", gallery_text)
    assert len(readme_refs) >= 3, "README.md Gallery should reference at least 3 screenshots"
    assert len(gallery_refs) > len(readme_refs), (
        "docs/gallery.md should provide additional captures beyond README.md"
    )
    for rel in (*readme_refs, *gallery_refs):
        assert (PROJECT_ROOT / rel).is_file(), f"Missing referenced screenshot: {rel}"
