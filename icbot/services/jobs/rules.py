from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from icbot.services.jobs.classification import (
    classify_career_level,
    classify_discipline_tags,
    detect_is_remote,
)
from icbot.services.jobs.models import (
    CareerLevel,
    JobFilterCriteria,
    JobPosting,
    MatchDecision,
    MatchTraceItem,
)

# Seniority terms across all faculties
_SENIORITY_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|vice\s+president|partner|architect|distinguished|fellow)\b",
    re.IGNORECASE,
)

# Compound senior titles where 'associate' is senior (e.g. Senior Associate, Associate Director)
_SENIOR_ASSOCIATE_RX = re.compile(
    r"\b(senior\s+associate|associate\s+(director|vp|vice\s+president|partner|principal|lead|manager))\b",
    re.IGNORECASE,
)

# Legacy tech-only non-tech exclusion (used ONLY in legacy 'tech_swe' preset)
_LEGACY_NON_TECH_RX = re.compile(
    r"\b(business|analyst|marketing|sales|finance|hr|recruiter|product\s+manager|project\s+manager)\b",
    re.IGNORECASE,
)

_EARLY_CAREER_RX = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new\s*grads?|graduates?|recent\s*grads?|entry[- ]?level|junior|jr\.?|student|early\s+career|trainee|rotational|campus|apprentice)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DisciplinePreset:
    """Named preset defining programs, included disciplines, and default filter rules."""

    key: str
    label: str
    description: str
    included_disciplines: tuple[str, ...]
    require_early_career: bool = False
    enforce_legacy_non_tech_rejection: bool = False


DISCIPLINE_PRESETS: dict[str, DisciplinePreset] = {
    "tech_swe": DisciplinePreset(
        key="tech_swe",
        label="Software & Tech Engineering (Legacy Preset)",
        description="Software, CS, hardware, electrical, and systems roles. Enforces legacy engineering exclusions.",
        included_disciplines=("software", "cs", "hardware", "electrical", "mechanical", "consulting"),
        enforce_legacy_non_tech_rejection=True,
    ),
    "engineering_cs": DisciplinePreset(
        key="engineering_cs",
        label="Engineering & Computer Science",
        description="All engineering disciplines (software, hardware, civil, mechanical, chemical) and computer science.",
        included_disciplines=(
            "software",
            "cs",
            "hardware",
            "electrical",
            "mechanical",
            "civil",
            "chemical",
            "consulting",
        ),
    ),
    "business_finance": DisciplinePreset(
        key="business_finance",
        label="Business & Finance",
        description="Financial analysis, investment banking, corporate finance, consulting, and business operations.",
        included_disciplines=("finance", "business", "accounting", "consulting"),
    ),
    "accounting": DisciplinePreset(
        key="accounting",
        label="Accounting & Audit",
        description="Public accounting, tax, audit, assurance, financial reporting, and CPA-track positions.",
        included_disciplines=("accounting",),
    ),
    "data_analytics": DisciplinePreset(
        key="data_analytics",
        label="Data Science & Analytics",
        description="Data analytics, business intelligence, data science, machine learning, and quantitative analysis.",
        included_disciplines=("cs", "software", "business", "finance"),
    ),
    "design_ux": DisciplinePreset(
        key="design_ux",
        label="Design & UI/UX",
        description="Product design, UI/UX, visual design, graphic design, and interaction design.",
        included_disciplines=("design",),
    ),
    "marketing": DisciplinePreset(
        key="marketing",
        label="Marketing & Communications",
        description="Digital marketing, growth marketing, social media, public relations, and brand communications.",
        included_disciplines=("marketing", "business"),
    ),
    "biotech_health": DisciplinePreset(
        key="biotech_health",
        label="Biotech & Life Sciences",
        description="Biomedical research, biotech laboratory technician, clinical research, bioinformatics, and pharmacology.",
        included_disciplines=("biology", "chemical"),
    ),
    "law_compliance": DisciplinePreset(
        key="law_compliance",
        label="Law & Regulatory Compliance",
        description="Legal assistant, paralegal, compliance analyst, regulatory affairs, and policy coordination.",
        included_disciplines=("law", "business"),
    ),
    "general_entry": DisciplinePreset(
        key="general_entry",
        label="General Early Career / All Majors",
        description="Campus student internships, co-ops, and entry-level positions across all faculties and majors.",
        included_disciplines=(),
        require_early_career=True,
    ),
    "all": DisciplinePreset(
        key="all",
        label="Unrestricted / All Disciplines",
        description="Matches all postings regardless of faculty, filtering only by seniority, keywords, and location.",
        included_disciplines=(),
    ),
}


def location_matches(location: str, text_blob: str, allowlist: Sequence[str] | None) -> bool:
    """Determine whether location matches allowlist, checking explicit location and remote mentions."""
    if not allowlist:
        return True

    loc = (location or "").strip().lower()
    blob = (text_blob or "").lower()

    if not loc or "not listed" in loc:
        if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
            return True
        return any(tok.lower() in blob for tok in allowlist if tok)

    if any(tok.lower() in loc for tok in allowlist if tok):
        return True

    if any(a.strip().lower() == "remote" for a in allowlist if a) and "remote" in blob:
        return True

    return any(tok.lower() in blob for tok in allowlist if tok)


class DeterministicRuleEngine:
    """
    Deterministic, explainable rule engine for job alerts across multi-major campus disciplines.
    Evaluates postings against criteria and produces structured match traces for auditing and preview.
    """

    def __init__(self, presets: dict[str, DisciplinePreset] | None = None) -> None:
        self.presets = presets if presets is not None else DISCIPLINE_PRESETS

    def get_preset(self, name: str | None) -> DisciplinePreset:
        if not name or name not in self.presets:
            return self.presets.get("tech_swe", DISCIPLINE_PRESETS["tech_swe"])
        return self.presets[name]

    def evaluate(self, job: JobPosting, criteria: JobFilterCriteria) -> MatchDecision:
        """
        Evaluate a JobPosting against JobFilterCriteria.
        Returns a MatchDecision containing boolean verdict, matched metadata, and trace list.
        """
        trace: list[MatchTraceItem] = []
        preset = self.get_preset(criteria.preset)

        blob = (job.text_blob or f"{job.title} {job.employer} {job.location}").strip()
        blob_lower = blob.lower()
        title_lower = job.title.lower()

        # 1. Seniority Check
        is_staff_acct = bool(re.search(r"\bstaff\s+accountant\b", title_lower)) and not bool(
            re.search(r"\bsenior\b", title_lower)
        )

        senior_match = _SENIOR_ASSOCIATE_RX.search(title_lower)
        if not senior_match:
            m = _SENIORITY_RX.search(title_lower)
            if m and not (m.group(0).lower() == "staff" and is_staff_acct):
                senior_match = m

        if senior_match:
            trace.append(
                MatchTraceItem(
                    rule="seniority_exclusion",
                    passed=False,
                    reason=f"Rejected: matches senior term '{senior_match.group(0)}'",
                )
            )
        else:
            trace.append(
                MatchTraceItem(
                    rule="seniority_exclusion",
                    passed=True,
                    reason="Passed: no senior/executive terms detected in title",
                )
            )

        # 1b. Legacy Non-Tech Rejection (Applied ONLY when preset is explicitly 'tech_swe')
        if preset.enforce_legacy_non_tech_rejection:
            non_tech_match = _LEGACY_NON_TECH_RX.search(blob_lower)
            if non_tech_match:
                trace.append(
                    MatchTraceItem(
                        rule="legacy_non_tech_exclusion",
                        passed=False,
                        reason=f"Rejected: preset '{preset.key}' excludes term '{non_tech_match.group(0)}'",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="legacy_non_tech_exclusion",
                        passed=True,
                        reason=f"Passed: no excluded non-tech terms for preset '{preset.key}'",
                    )
                )

        # 2. Career Level & Early Career Check
        career_lvl: CareerLevel = (
            job.career_level if job.career_level != "unknown" else classify_career_level(blob)
        )
        early_career_required = criteria.require_early_career or preset.require_early_career
        has_early_hint = bool(_EARLY_CAREER_RX.search(blob_lower)) or career_lvl in (
            "intern",
            "new_grad",
            "entry",
        )

        if criteria.career_levels:
            if career_lvl in criteria.career_levels:
                trace.append(
                    MatchTraceItem(
                        rule="career_level_filter",
                        passed=True,
                        reason=f"Passed: career level '{career_lvl}' matches allowed levels {criteria.career_levels}",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="career_level_filter",
                        passed=False,
                        reason=f"Rejected: career level '{career_lvl}' not in allowed levels {criteria.career_levels}",
                    )
                )
        elif early_career_required:
            if has_early_hint:
                trace.append(
                    MatchTraceItem(
                        rule="early_career_required",
                        passed=True,
                        reason=f"Passed: early-career signal confirmed (level='{career_lvl}')",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="early_career_required",
                        passed=False,
                        reason="Rejected: role requires early-career hint (intern/co-op/new grad/entry-level)",
                    )
                )
        else:
            trace.append(
                MatchTraceItem(
                    rule="early_career_check",
                    passed=True,
                    reason=f"Passed: early-career gate not required (level='{career_lvl}')",
                )
            )

        # 3. Discipline Matching
        extracted_tags = job.discipline_tags if job.discipline_tags else classify_discipline_tags(blob)
        allowed_disciplines = (
            set(criteria.disciplines) if criteria.disciplines else set(preset.included_disciplines)
        )

        if not allowed_disciplines:
            # Preset 'all' or 'general_entry' accepts any discipline
            trace.append(
                MatchTraceItem(
                    rule="discipline_match",
                    passed=True,
                    reason=f"Passed: preset '{preset.key}' accepts all disciplines (detected: {extracted_tags or ('general',)})",
                )
            )
        else:
            overlap = set(extracted_tags).intersection(allowed_disciplines)
            if overlap:
                trace.append(
                    MatchTraceItem(
                        rule="discipline_match",
                        passed=True,
                        reason=f"Passed: matched disciplines {sorted(overlap)} (target: {sorted(allowed_disciplines)})",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="discipline_match",
                        passed=False,
                        reason=f"Rejected: detected disciplines {extracted_tags or ('none',)} do not match target {sorted(allowed_disciplines)}",
                    )
                )

        # 4. Keyword Denylist Check
        if criteria.keywords_denylist:
            denied_hit: str | None = None
            for d in criteria.keywords_denylist:
                if not d:
                    continue
                pattern = rf"\b{re.escape(d)}\b"
                if re.search(pattern, blob_lower, re.IGNORECASE):
                    denied_hit = d
                    break
            if denied_hit:
                trace.append(
                    MatchTraceItem(
                        rule="keyword_denylist",
                        passed=False,
                        reason=f"Rejected: matched denied keyword '{denied_hit}'",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="keyword_denylist",
                        passed=True,
                        reason="Passed: no denied keywords found",
                    )
                )
        else:
            trace.append(
                MatchTraceItem(
                    rule="keyword_denylist", passed=True, reason="Passed: no keyword denylist configured"
                )
            )

        # 5. Keyword Allowlist Check
        if criteria.keywords_allowlist:
            allowed_hit = any(a.lower() in blob_lower for a in criteria.keywords_allowlist if a)
            if allowed_hit:
                trace.append(
                    MatchTraceItem(
                        rule="keyword_allowlist",
                        passed=True,
                        reason="Passed: matched at least one required keyword",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="keyword_allowlist",
                        passed=False,
                        reason=f"Rejected: none of allowlist keywords {criteria.keywords_allowlist} found in text",
                    )
                )
        else:
            trace.append(
                MatchTraceItem(
                    rule="keyword_allowlist", passed=True, reason="Passed: no keyword allowlist configured"
                )
            )

        # 6. Location & Remote Preferences Check
        if criteria.remote_only and not (job.is_remote or detect_is_remote(job.location, blob)):
            trace.append(
                MatchTraceItem(
                    rule="remote_only_filter",
                    passed=False,
                    reason=f"Rejected: posting is not remote (location: '{job.location}')",
                )
            )
        else:
            loc_ok = location_matches(job.location, blob, criteria.location_allowlist)
            if loc_ok:
                trace.append(
                    MatchTraceItem(
                        rule="location_filter",
                        passed=True,
                        reason=f"Passed: location '{job.location or 'Not Listed'}' matches location criteria",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="location_filter",
                        passed=False,
                        reason=f"Rejected: location '{job.location}' does not match allowlist {criteria.location_allowlist}",
                    )
                )

        # 7. Employer Allowlist Check
        if criteria.employer_allowlist:
            emp_lower = job.employer.strip().lower()
            if any(ea.strip().lower() == emp_lower for ea in criteria.employer_allowlist if ea):
                trace.append(
                    MatchTraceItem(
                        rule="employer_allowlist",
                        passed=True,
                        reason=f"Passed: employer '{job.employer}' in allowed employers",
                    )
                )
            else:
                trace.append(
                    MatchTraceItem(
                        rule="employer_allowlist",
                        passed=False,
                        reason=f"Rejected: employer '{job.employer}' not in allowed employers {criteria.employer_allowlist}",
                    )
                )

        # Compile overall verdict and explanation
        all_passed = all(t.passed for t in trace)
        reasons = [t.reason for t in trace]
        if all_passed:
            explanation = (
                f"MATCH | preset='{preset.key}' | level='{career_lvl}' | "
                f"disciplines={extracted_tags or ('general',)} | loc='{job.location or 'Not Listed'}' | "
                + "; ".join(r for r in reasons if "Passed" in r)
            )
        else:
            failed_reasons = [t.reason for t in trace if not t.passed]
            explanation = f"REJECT | preset='{preset.key}' | " + "; ".join(failed_reasons)

        return MatchDecision(
            matched=all_passed,
            preset=preset.key,
            career_level=career_lvl,
            disciplines=extracted_tags,
            trace=tuple(trace),
            explanation=explanation,
        )
