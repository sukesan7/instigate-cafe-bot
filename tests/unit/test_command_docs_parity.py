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


def test_screenshots_guide_covers_prioritized_shot_list_and_safety_rules() -> None:
    """Verify docs/screenshots-guide.md defines all 5 priority tiers, safety rules, and leaves README unlinked."""
    guide_text = (PROJECT_ROOT / "docs/screenshots-guide.md").read_text(encoding="utf-8")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    # 5 prioritized tiers
    for marker in (
        "Priority 1",
        "Priority 2",
        "Priority 3",
        "Priority 4",
        "Priority 5",
        "Expected Embed Title",
        "Visual Theme",
        "Elements to Capture",
        "Safe Test Data",
        "Crop Guidelines",
        "Alt Text Draft",
        "Suggested Filename",
        "assets/screenshots/",
        "non-production token",
        "Never run two bot copies",
        "Never upload actual student resumes",
        "instigate_cafe",
    ):
        assert marker in guide_text, f"docs/screenshots-guide.md missing required section/rule: {marker}"

    # README must keep gallery assets unreferenced until supplied and approved by owner
    assert "assets/screenshots/" not in readme_text
