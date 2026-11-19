from __future__ import annotations

from typing import Any

import pytest

from icbot.services.jobs.models import (
    JobFilterCriteria,
    JobPosting,
)
from icbot.services.jobs.rules import (
    DeterministicRuleEngine,
)
from tests.helpers import load_fixture_json

pytestmark = pytest.mark.unit


def _create_posting(
    title: str,
    employer: str = "Acme Corp",
    location: str = "Toronto, ON",
    provider: str = "greenhouse",
) -> JobPosting:
    blob = f"{title} {employer} {location}".strip()
    return JobPosting(
        provider=provider,  # type: ignore[arg-type]
        employer=employer,
        external_id=f"test-{abs(hash(title)) % 100000}",
        title=title,
        location=location,
        url=f"https://example.invalid/{employer}/{abs(hash(title))}",
        text_blob=blob,
    )


def test_explainable_trace_structure_on_match_and_rejection():
    engine = DeterministicRuleEngine()
    criteria = JobFilterCriteria(
        location_allowlist=("toronto",),
        keywords_denylist=("sales",),
        require_early_career=True,
        preset="tech_swe",
    )

    # Eligible Software Intern
    job_pass = _create_posting("Software Engineer Intern", location="Toronto, ON")
    dec_pass = engine.evaluate(job_pass, criteria)

    assert dec_pass.matched is True
    assert dec_pass.career_level == "intern"
    assert "software" in dec_pass.disciplines
    assert "MATCH" in dec_pass.explanation
    rule_names = [t.rule for t in dec_pass.trace]
    assert "seniority_exclusion" in rule_names
    assert "early_career_required" in rule_names
    assert "discipline_match" in rule_names
    assert "location_filter" in rule_names
    assert all(t.passed for t in dec_pass.trace)

    # Ineligible Senior Director
    job_fail = _create_posting("Senior Director of Engineering", location="Toronto, ON")
    dec_fail = engine.evaluate(job_fail, criteria)

    assert dec_fail.matched is False
    assert dec_fail.career_level == "experienced"
    assert "REJECT" in dec_fail.explanation
    failed_rules = [t.rule for t in dec_fail.trace if not t.passed]
    assert "seniority_exclusion" in failed_rules


def test_ambiguous_associate_and_analyst_disambiguation():
    engine = DeterministicRuleEngine()

    # Ambiguous 1: "Senior Associate" -> must be experienced and rejected by early career
    crit_early = JobFilterCriteria(require_early_career=True, preset="all")
    job_sr_assoc = _create_posting("Senior Associate Consultant")
    dec_sr_assoc = engine.evaluate(job_sr_assoc, crit_early)
    assert dec_sr_assoc.career_level == "experienced"
    assert dec_sr_assoc.matched is False
    assert any(t.rule == "seniority_exclusion" and not t.passed for t in dec_sr_assoc.trace)

    # Ambiguous 2: "Associate Director" -> must be experienced and rejected
    job_assoc_dir = _create_posting("Associate Director of Finance")
    dec_assoc_dir = engine.evaluate(job_assoc_dir, crit_early)
    assert dec_assoc_dir.career_level == "experienced"
    assert dec_assoc_dir.matched is False

    # Ambiguous 3: "Tax Associate" -> entry level, matches accounting
    crit_acct = JobFilterCriteria(require_early_career=True, preset="accounting")
    job_tax_assoc = _create_posting("Tax Associate (CPA Track)")
    dec_tax_assoc = engine.evaluate(job_tax_assoc, crit_acct)
    assert dec_tax_assoc.career_level == "entry"
    assert "accounting" in dec_tax_assoc.disciplines
    assert dec_tax_assoc.matched is True

    # Ambiguous 4: "Audit Associate" -> entry level, matches accounting
    job_audit_assoc = _create_posting("Audit Associate - Entry Level")
    dec_audit_assoc = engine.evaluate(job_audit_assoc, crit_acct)
    assert dec_audit_assoc.career_level == "entry"
    assert "accounting" in dec_audit_assoc.disciplines
    assert dec_audit_assoc.matched is True

    # Ambiguous 5: "Associate Software Engineer" -> entry level, matches tech_swe
    crit_swe = JobFilterCriteria(require_early_career=True, preset="tech_swe")
    job_assoc_swe = _create_posting("Associate Software Engineer")
    dec_assoc_swe = engine.evaluate(job_assoc_swe, crit_swe)
    assert dec_assoc_swe.career_level == "entry"
    assert "software" in dec_assoc_swe.disciplines
    assert dec_assoc_swe.matched is True

    # Ambiguous 6: "Data Analyst Intern" -> protected against legacy over-filtering in data_analytics
    crit_data = JobFilterCriteria(require_early_career=True, preset="data_analytics")
    job_data_analyst = _create_posting("Data Analyst Intern")
    dec_data = engine.evaluate(job_data_analyst, crit_data)
    assert dec_data.career_level == "intern"
    assert dec_data.matched is True


def test_presets_matrix_coverage():
    engine = DeterministicRuleEngine()

    presets_to_test = [
        ("business_finance", "Investment Banking Summer Analyst", True),
        ("business_finance", "Software Engineering Intern", False),
        ("accounting", "Staff Accountant Intern", True),
        ("accounting", "Product Designer Co-op", False),
        ("design_ux", "Product Design Intern", True),
        ("design_ux", "Tax Associate", False),
        ("biotech_health", "Biotechnology Laboratory Intern", True),
        ("biotech_health", "Investment Banking Summer Analyst", False),
        ("marketing", "Growth Marketing Intern", True),
        ("marketing", "Hardware PCB Design Co-op", False),
        ("general_entry", "Campus Brand Ambassador Intern", True),
        ("all", "Clinical Research Assistant Co-op", True),
    ]

    for preset_key, title, should_match in presets_to_test:
        crit = JobFilterCriteria(require_early_career=True, preset=preset_key)
        job = _create_posting(title)
        decision = engine.evaluate(job, crit)
        assert decision.matched is should_match, (
            f"Expected title '{title}' with preset '{preset_key}' to match={should_match}, "
            f"got {decision.matched}. Trace: {decision.explanation}"
        )


def test_precision_and_recall_on_labeled_evaluation_dataset():
    """
    Evaluate precision and recall across labeled multi-major fixture dataset.
    Asserts >= 95% precision and recall for target presets and produces an error analysis table.
    """
    engine = DeterministicRuleEngine()
    data = load_fixture_json("labeled_job_eval_dataset.json")
    assert len(data) >= 25, "Evaluation dataset must contain at least 25 items"

    presets_to_evaluate = ["tech_swe", "business_finance", "accounting", "design_ux", "biotech_health"]
    metrics: dict[str, dict[str, int]] = {
        p: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for p in presets_to_evaluate
    }
    error_analysis_rows: list[dict[str, Any]] = []

    for item in data:
        title = item["title"]
        employer = item["employer"]
        location = item.get("location", "Toronto, ON")
        job = _create_posting(title, employer=employer, location=location)

        for preset in presets_to_evaluate:
            expected_match = item["should_match_preset"].get(preset, False)
            crit = JobFilterCriteria(require_early_career=True, preset=preset)
            decision = engine.evaluate(job, crit)
            actual_match = decision.matched

            if actual_match and expected_match:
                metrics[preset]["tp"] += 1
            elif actual_match and not expected_match:
                metrics[preset]["fp"] += 1
                error_analysis_rows.append(
                    {
                        "id": item["id"],
                        "title": title,
                        "preset": preset,
                        "type": "FALSE_POSITIVE",
                        "explanation": decision.explanation,
                    }
                )
            elif not actual_match and expected_match:
                metrics[preset]["fn"] += 1
                error_analysis_rows.append(
                    {
                        "id": item["id"],
                        "title": title,
                        "preset": preset,
                        "type": "FALSE_NEGATIVE",
                        "explanation": decision.explanation,
                    }
                )
            else:
                metrics[preset]["tn"] += 1

    # Verify precision and recall for each evaluated preset
    for preset, counts in metrics.items():
        tp = counts["tp"]
        fp = counts["fp"]
        fn = counts["fn"]

        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0

        assert precision >= 0.95, f"Preset '{preset}' precision {precision:.2f} < 0.95 (TP={tp}, FP={fp})"
        assert recall >= 0.95, f"Preset '{preset}' recall {recall:.2f} < 0.95 (TP={tp}, FN={fn})"

    # Ensure zero false positives on ambiguous titles
    assert len(error_analysis_rows) == 0, f"Error analysis found misclassifications: {error_analysis_rows}"


async def test_user_job_preferences_database_crud_and_privacy(tmp_db):
    """Verify user job search preferences storage, retrieval, and hard deletion."""
    user_id = 888000111222333444

    # 1. Non-existent returns None
    initial = await tmp_db.get_user_job_preferences(user_id)
    assert initial is None

    # 2. Insert preferences
    await tmp_db.set_user_job_preferences(
        user_id,
        preset="business_finance",
        disciplines=["finance", "accounting"],
        career_levels=["intern", "entry"],
        locations=["Toronto, ON", "Waterloo, ON"],
        keywords_allow=["risk", "capital"],
        keywords_deny=["sales"],
        remote_only=True,
    )

    prefs = await tmp_db.get_user_job_preferences(user_id)
    assert prefs is not None
    assert prefs["user_id"] == user_id
    assert prefs["preset"] == "business_finance"
    assert "finance" in prefs["disciplines"]
    assert "intern" in prefs["career_levels"]
    assert "Toronto, ON" in prefs["locations"]
    assert "risk" in prefs["keywords_allow"]
    assert prefs["remote_only"] is True

    # 3. Hard deletion ensures complete PII wipe
    deleted = await tmp_db.delete_user_job_preferences(user_id)
    assert deleted is True

    after_delete = await tmp_db.get_user_job_preferences(user_id)
    assert after_delete is None
