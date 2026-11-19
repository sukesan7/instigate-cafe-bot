from __future__ import annotations

import re

from icbot.services.jobs.models import CareerLevel

# Seniority / executive patterns
_SENIORITY_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|vice\s+president|partner|architect|distinguished|fellow)\b",
    re.IGNORECASE,
)

# Compound senior titles where 'associate' is qualified by senior terms
_SENIOR_ASSOCIATE_RX = re.compile(
    r"\b(senior\s+associate|associate\s+(director|vp|vice\s+president|partner|principal|lead|manager))\b",
    re.IGNORECASE,
)

_INTERN_RX = re.compile(
    r"\b(intern(ship)?|co[- ]?op|student|summer\s+analyst|summer\s+associate)\b",
    re.IGNORECASE,
)

_NEW_GRAD_RX = re.compile(
    r"\b(new\s*grads?|graduates?|recent\s*grads?|rotational|trainee)\b",
    re.IGNORECASE,
)

_ENTRY_RX = re.compile(
    r"\b(entry[- ]?level|junior|jr\.?|associate\s+(software|engineer|developer|consultant|analyst|accountant|designer)|tax\s+associate|audit\s+associate|assurance\s+associate|staff\s+accountant)\b",
    re.IGNORECASE,
)

_FALLBACK_ENTRY_ASSOCIATE_RX = re.compile(
    r"\b(associate)\b",
    re.IGNORECASE,
)

_REMOTE_RX = re.compile(r"\b(remote|telecommute|work\s+from\s+home|wfh)\b", re.IGNORECASE)

_DISCIPLINE_PATTERNS: dict[str, re.Pattern[str]] = {
    "software": re.compile(
        r"\b(software|swe|developer|backend|front\s*end|full\s*stack|platform|infrastructure|site\s+reliability|sre|devops|distributed\s+systems)\b",
        re.IGNORECASE,
    ),
    "hardware": re.compile(
        r"\b(hardware|pcb|schematic|board\s+design|bring[- ]?up|asic|fpga)\b",
        re.IGNORECASE,
    ),
    "cs": re.compile(
        r"\b(computer\s+science|artificial\s+intelligence|machine\s+learning|ml\b|data\s+scientist|data\s+science|data\s+analyst|data\s+engineer|cybersecurity|security\s+analyst|quant|trading|quantitative\s+research|ai\s+research|ml\s+research|research\s+(scientist|engineer|developer))\b",
        re.IGNORECASE,
    ),
    "electrical": re.compile(
        r"\b(electrical|ee\b|embedded|firmware|fpga|verilog|vhdl|asic|rf\b|signal|power\s+systems)\b",
        re.IGNORECASE,
    ),
    "mechanical": re.compile(
        r"\b(mechanical|mech\b|manufacturing|mechatronics|robotics|cad|solidworks|autocad|thermal|automotive|aerospace)\b",
        re.IGNORECASE,
    ),
    "civil": re.compile(
        r"\b(civil|structural|transportation|geotechnical|surveyor|construction|environmental\s+engineer)\b",
        re.IGNORECASE,
    ),
    "chemical": re.compile(
        r"\b(chemical|process\s+engineer|materials\s+science|polymer|biochemical)\b",
        re.IGNORECASE,
    ),
    "consulting": re.compile(
        r"\b(consultant|solutions\s+consultant|implementation\s+consultant|professional\s+services|solutions\s+engineer|sales\s+engineer|management\s+consulting)\b",
        re.IGNORECASE,
    ),
    "finance": re.compile(
        r"\b(finance|financial\s+analyst|investment\s+banking|capital\s+markets|equity\s+research|private\s+equity|portfolio|asset\s+management|risk\s+analyst|wealth\s+management|trading|underwriting|treasury)\b",
        re.IGNORECASE,
    ),
    "accounting": re.compile(
        r"\b(accounting|accountant|staff\s+accountant|audit|tax|assurance|cpa|financial\s+reporting|bookkeeping|accounts\s+payable|accounts\s+receivable)\b",
        re.IGNORECASE,
    ),
    "business": re.compile(
        r"\b(business\s+analyst|operations|business\s+development|strategy|project\s+coordinator|project\s+management|operations\s+analyst|supply\s+chain|logistics)\b",
        re.IGNORECASE,
    ),
    "marketing": re.compile(
        r"\b(marketing|digital\s+marketing|growth\s+marketing|social\s+media|content|communications|brand|public\s+relations|pr\b|seo\b|copywriter)\b",
        re.IGNORECASE,
    ),
    "design": re.compile(
        r"\b(product\s+design(er)?|ui\b|ux\b|ui[\s/]*ux|user\s+experience|visual\s+design(er)?|graphic\s+design(er)?|interaction\s+design(er)?|web\s+design(er)?)\b",
        re.IGNORECASE,
    ),
    "biology": re.compile(
        r"\b(biology|biotech|biological|laboratory|lab\s+technician|clinical\s+research|bioinformatics|molecular|microbiology|pharmaceutical|pharmacology|healthcare|chemistry|chemist)\b",
        re.IGNORECASE,
    ),
    "law": re.compile(
        r"\b(legal|law\b|compliance|regulatory|paralegal|contracts|policy\s+analyst)\b",
        re.IGNORECASE,
    ),
}

# User-facing category taxonomy for embeds
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
    "Hardware & Electrical Roles": [
        r"\bhardware\b",
        r"\bpcb\b",
        r"\bschematic\b",
        r"\bboard\s+design\b",
        r"\bbring[- ]?up\b",
        r"\belectrical\b|\bee\b",
        r"\bembedded\b",
        r"\bfirmware\b",
        r"\bfpga\b",
    ],
    "Computer Science & Data Roles": [
        r"\bcomputer\s+science\b",
        r"\bresearch\s+scientist\b",
        r"\bai\b|\bartificial\s+intelligence\b",
        r"\bmachine\s+learning\b|\bml\b",
        r"\bdata\s+scientist\b|\bdata\s+science\b",
        r"\bdata\s+analyst\b|\bdata\s+analytics\b",
        r"\bsecurity\b|\bcyber\b",
        r"\bquant\b|\btrading\b",
    ],
    "Finance & Business Roles": [
        r"\bfinance\b|\bfinancial\b",
        r"\binvestment\s+banking\b",
        r"\brisk\s+analyst\b",
        r"\bbusiness\s+analyst\b",
        r"\bconsulting\b",
        r"\boperations\b",
    ],
    "Accounting & Audit Roles": [
        r"\baccounting\b|\baccountant\b",
        r"\baudit\b",
        r"\btax\b",
        r"\bassurance\b",
        r"\bcpa\b",
    ],
    "Design & Creative Roles": [
        r"\bproduct\s+design\b",
        r"\bui\b|\bux\b",
        r"\bvisual\s+design\b",
        r"\bgraphic\s+design\b",
        r"\buser\s+experience\b",
    ],
    "Marketing & Communications Roles": [
        r"\bmarketing\b",
        r"\bgrowth\b",
        r"\bcommunications\b",
        r"\bsocial\s+media\b",
        r"\bcontent\b",
    ],
    "Biotech & Life Sciences Roles": [
        r"\bbiotech\b|\bbiology\b",
        r"\blab\s+technician\b",
        r"\bclinical\s+research\b",
        r"\bbioinformatics\b",
        r"\bchemistry\b",
    ],
    "Mechanical & Civil Roles": [
        r"\bmechanical\b|\bmech\b",
        r"\bmechatronics\b",
        r"\brobotics\b",
        r"\bcad\b",
        r"\bcivil\b",
        r"\bstructural\b",
    ],
}

_TAXONOMY_RES = {k: re.compile("|".join(v), re.IGNORECASE) for k, v in TAXONOMY_CATEGORIES.items()}


def classify_career_level(text: str) -> CareerLevel:
    """
    Classify career level from title and text blob.
    Disambiguates ambiguous titles such as 'Senior Associate' vs 'Tax Associate'.
    """
    if not text:
        return "unknown"

    # 1. Check senior qualifiers first (including compound 'Senior Associate' / 'Associate Director')
    if _SENIOR_ASSOCIATE_RX.search(text):
        return "experienced"

    # Check standalone seniority terms, unless it's an intern/co-op or entry-level staff accountant
    senior_hit = _SENIORITY_RX.search(text)
    if senior_hit:
        is_intern = bool(_INTERN_RX.search(text))
        is_staff_acct = bool(re.search(r"\bstaff\s+accountant\b", text, re.IGNORECASE)) and not bool(
            re.search(r"\bsenior\b", text, re.IGNORECASE)
        )
        if not is_intern and not (senior_hit.group(0).lower() == "staff" and is_staff_acct):
            return "experienced"

    # 2. Intern / co-op / student
    if _INTERN_RX.search(text):
        return "intern"

    # 3. New grad / rotational / trainee
    if _NEW_GRAD_RX.search(text):
        return "new_grad"

    # 4. Explicit junior / entry-level / early-career associate
    if _ENTRY_RX.search(text):
        return "entry"

    # 5. General associate fallback (only entry if not qualified by senior terms)
    if _FALLBACK_ENTRY_ASSOCIATE_RX.search(text):
        return "entry"

    return "unknown"


def classify_discipline_tags(text: str) -> tuple[str, ...]:
    """Extract normalized discipline tags from text across all campus faculties."""
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
