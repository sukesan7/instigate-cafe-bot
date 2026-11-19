from __future__ import annotations

import re

from icbot.services.jobs.models import CareerLevel

_SENIORITY_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|architect|distinguished)\b",
    re.IGNORECASE,
)

_INTERN_RX = re.compile(
    r"\b(intern(ship)?|co[- ]?op|student)\b",
    re.IGNORECASE,
)

_NEW_GRAD_RX = re.compile(
    r"\b(new\s*grads?|graduates?|recent\s*grads?)\b",
    re.IGNORECASE,
)

_ENTRY_RX = re.compile(
    r"\b(entry[- ]?level|junior|jr\.?|associate)\b",
    re.IGNORECASE,
)

_NON_TECH_RX = re.compile(
    r"\b(business|analyst|marketing|sales|finance|hr|recruiter|product\s+manager|project\s+manager)\b",
    re.IGNORECASE,
)

_REMOTE_RX = re.compile(r"\b(remote|telecommute|work\s+from\s+home|wfh)\b", re.IGNORECASE)

_DISCIPLINE_PATTERNS: dict[str, re.Pattern[str]] = {
    "software": re.compile(
        r"\b(software|swe|developer|backend|front\s*end|full\s*stack|platform|infrastructure|site\s+reliability|sre|devops|distributed\s+systems)\b",
        re.IGNORECASE,
    ),
    "hardware": re.compile(
        r"\b(hardware|pcb|schematic|board\s+design|bring[- ]?up)\b",
        re.IGNORECASE,
    ),
    "cs": re.compile(
        r"\b(computer\s+science|research|ai|artificial\s+intelligence|machine\s+learning|ml|data\s+scientist|data\s+science|security|cyber|quant|trading)\b",
        re.IGNORECASE,
    ),
    "electrical": re.compile(
        r"\b(electrical|ee|embedded|firmware|fpga|verilog|vhdl|asic|rf|signal|power)\b",
        re.IGNORECASE,
    ),
    "mechanical": re.compile(
        r"\b(mechanical|mech|manufacturing|mechatronics|robotics|cad|solidworks|autocad|thermal)\b",
        re.IGNORECASE,
    ),
    "consulting": re.compile(
        r"\b(consultant|solutions\s+consultant|implementation\s+consultant|professional\s+services|solutions\s+engineer|sales\s+engineer)\b",
        re.IGNORECASE,
    ),
}

# Legacy category taxonomy for embed display
TAXONOMY_CATEGORIES: dict[str, list[str]] = {
    "Software Engineering Roles": [
        r"\bsoftware\s+engineer\b",
        r"\bsoftware\s+developer\b",
        r"\bswe\b",
        r"\bbackend\b",
        r"\bfront\s*end\b",
        r"\bfull\s*stack\b",
        r"\bplatform\b",
        r"\binfrastructure\b",
        r"\bsite\s+reliability\b|\bsre\b",
        r"\bdevops\b",
        r"\bdistributed\s+systems\b",
    ],
    "Hardware Engineering Roles": [
        r"\bhardware\b",
        r"\bpcb\b",
        r"\bschematic\b",
        r"\bboard\s+design\b",
        r"\bbring[- ]?up\b",
    ],
    "Computer Science Roles": [
        r"\bcomputer\s+science\b",
        r"\bresearch\b",
        r"\bai\b|\bartificial\s+intelligence\b",
        r"\bmachine\s+learning\b|\bml\b",
        r"\bdata\s+scientist\b|\bdata\s+science\b",
        r"\bsecurity\b|\bcyber\b",
        r"\bquant\b|\btrading\b",
    ],
    "Electrical Engineering Roles": [
        r"\belectrical\b|\bee\b",
        r"\bembedded\b",
        r"\bfirmware\b",
        r"\bfpga\b|\bverilog\b|\bvhdl\b",
        r"\basic\b",
        r"\brf\b|\bsignal\b|\bpower\b",
    ],
    "Mechanical Engineering Roles": [
        r"\bmechanical\b|\bmech\b",
        r"\bmanufacturing\b",
        r"\bmechatronics\b",
        r"\brobotics\b",
        r"\bcad\b|\bsolidworks\b|\bautocad\b",
        r"\bthermal\b",
    ],
    "Tech Consultant roles": [
        r"\bconsultant\b",
        r"\bsolutions\s+consultant\b",
        r"\bimplementation\s+consultant\b",
        r"\bprofessional\s+services\b",
        r"\bsolutions\s+engineer\b",
        r"\bsales\s+engineer\b",
    ],
}

_TAXONOMY_RES = {k: re.compile("|".join(v), re.IGNORECASE) for k, v in TAXONOMY_CATEGORIES.items()}


def classify_career_level(text: str) -> CareerLevel:
    """Classify career level from title and description text."""
    if not text:
        return "unknown"
    if _INTERN_RX.search(text):
        return "intern"
    if _NEW_GRAD_RX.search(text):
        return "new_grad"
    if _ENTRY_RX.search(text):
        return "entry"
    if _SENIORITY_RX.search(text):
        return "experienced"
    return "unknown"


def classify_discipline_tags(text: str) -> tuple[str, ...]:
    """Extract normalized discipline tags from text."""
    if not text:
        return ()
    tags: list[str] = []
    for tag, rx in _DISCIPLINE_PATTERNS.items():
        if rx.search(text):
            tags.append(tag)
    return tuple(tags)


def classify_taxonomy_categories(text: str) -> list[str]:
    """Map text into user-facing display categories."""
    t = (text or "").strip()
    if not t:
        return []
    return [name for name, rx in _TAXONOMY_RES.items() if rx.search(t)]


def detect_is_remote(location: str, text_blob: str = "") -> bool:
    """Determine whether a posting is remote based on location and text."""
    loc = (location or "").lower()
    if _REMOTE_RX.search(loc):
        return True
    blob = (text_blob or "").lower()
    return bool(_REMOTE_RX.search(blob))


def canonicalize_company_name(name: str) -> str:
    """Normalize employer name for display and deduplication."""
    clean = (name or "").strip()
    if not clean:
        return "Unknown"
    # Replace common slug separators with spaces if all lower/slug
    if clean.islower():
        clean = clean.replace("-", " ").replace("_", " ").title()
    elif "-" in clean and " " not in clean:
        clean = clean.replace("-", " ")
    elif "_" in clean and " " not in clean:
        clean = clean.replace("_", " ")
    return clean


def canonicalize_title(title: str) -> str:
    """Normalize whitespace and formatting in job titles."""
    t = (title or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t or "Untitled Role"
