"""Typed campus, curriculum, resource, exam, and academic provider data contracts.

Designed to be campus-agnostic: institutions with different course-code conventions
(e.g. ``ELE888``, ``CS 135``, ``COMP-2404``, ``BIO120H1``, ``HIST 2010``) are supported
via normalized course keys and configurable alias/prefix maps without hardcoding TMU.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlparse

T = TypeVar("T")

ProviderStatus = Literal[
    "ok",
    "not_found",
    "unsupported_campus",
    "disabled",
    "error",
    "unsafe_url",
]

ExamVisibility = Literal["guild", "public", "private"]

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Known canonical campus aliases for optional built-in campus-scoped plugins (e.g. TMU)
_TMU_CAMPUS_IDS: frozenset[str] = frozenset(
    {
        "tmu",
        "toronto_metropolitan_university",
        "toronto-metropolitan-university",
        "ryerson",
        "ryerson_university",
        "ryerson-university",
    }
)


def normalize_course_key(raw: str) -> str:
    """Normalize arbitrary university course codes into a canonical uppercase lookup key.

    Examples:
        ``"ele 888"`` -> ``"ELE888"``
        ``"COMP-2404"`` -> ``"COMP2404"``
        ``"CS 135"`` -> ``"CS135"``
        ``"BIO120H1"`` -> ``"BIO120H1"``
        ``"HIST 2010A"`` -> ``"HIST2010A"``
    """
    cleaned = str(raw or "").strip().upper()
    return _NON_ALNUM_RE.sub("", cleaned)


def parse_course_aliases(raw_entries: Sequence[str] | Mapping[str, str] | None) -> dict[str, str]:
    """Parse alias/prefix mappings into normalized ``{alias_or_prefix: canonical}`` entries.

    Accepts either a mapping or strings formatted as ``"ALIAS=CANONICAL"`` or
    ``"OLD_PREFIX->NEW_PREFIX"`` (e.g. ``"ECE888=ELE888"``, ``"CSCI*=CS*"``).
    """
    if not raw_entries:
        return {}
    out: dict[str, str] = {}
    if isinstance(raw_entries, Mapping):
        for k, v in raw_entries.items():
            left = str(k or "").strip().upper()
            right = str(v or "").strip().upper()
            if left and right:
                if left.endswith("*") and right.endswith("*"):
                    out[normalize_course_key(left[:-1]) + "*"] = normalize_course_key(right[:-1]) + "*"
                else:
                    out[normalize_course_key(left)] = normalize_course_key(right)
        return out

    for item in raw_entries:
        text = str(item or "").strip()
        if not text:
            continue
        sep = "->" if "->" in text else ("=" if "=" in text else "")
        if not sep:
            continue
        left_raw, right_raw = text.split(sep, 1)
        left = left_raw.strip().upper()
        right = right_raw.strip().upper()
        if not left or not right:
            continue
        if left.endswith("*") and right.endswith("*"):
            lp = normalize_course_key(left[:-1])
            rp = normalize_course_key(right[:-1])
            if lp and rp:
                out[f"{lp}*"] = f"{rp}*"
        else:
            lk = normalize_course_key(left)
            rk = normalize_course_key(right)
            if lk and rk:
                out[lk] = rk
    return out


def resolve_course_key(
    raw: str,
    aliases: Mapping[str, str] | Sequence[str] | None = None,
) -> str:
    """Normalize a course code and apply exact or prefix alias mappings."""
    key = normalize_course_key(raw)
    if not key or not aliases:
        return key
    alias_map = parse_course_aliases(aliases)
    if key in alias_map:
        return alias_map[key]
    for pattern, target in alias_map.items():
        if pattern.endswith("*") and target.endswith("*"):
            src_prefix = pattern[:-1]
            dst_prefix = target[:-1]
            if src_prefix and key.startswith(src_prefix):
                return dst_prefix + key[len(src_prefix) :]
    return key


def slugify_campus_id(name_or_id: str) -> str:
    """Generate a deterministic lowercase slug identifier for a campus."""
    raw = str(name_or_id or "").strip().lower()
    slug = _SLUG_RE.sub("_", raw).strip("_")
    return slug or "generic_campus"


@dataclass(frozen=True)
class Campus:
    """Typed representation of an academic institution / campus."""

    campus_id: str
    name: str
    canonical_url: str = ""
    domain: str = ""
    timezone: str = "America/Toronto"
    source_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        cid = slugify_campus_id(self.campus_id or self.name)
        object.__setattr__(self, "campus_id", cid)
        object.__setattr__(self, "name", str(self.name or "").strip() or cid)
        url = str(self.canonical_url or "").strip()
        object.__setattr__(self, "canonical_url", url)
        dom = str(self.domain or "").strip().lower()
        if not dom and url:
            dom = (urlparse(url).hostname or "").strip().lower()
        object.__setattr__(self, "domain", dom)
        tz = str(self.timezone or "America/Toronto").strip() or "America/Toronto"
        object.__setattr__(self, "timezone", tz)
        if not isinstance(self.source_ids, MappingProxyType):
            cleaned = {
                str(k).strip(): str(v).strip() for k, v in (self.source_ids or {}).items() if str(k).strip()
            }
            object.__setattr__(self, "source_ids", MappingProxyType(cleaned))


def is_tmu_campus(campus: Campus) -> bool:
    """Return True only when ``campus`` explicitly represents Toronto Metropolitan University."""
    if campus.campus_id in _TMU_CAMPUS_IDS:
        return True
    name_slug = slugify_campus_id(campus.name)
    if name_slug in _TMU_CAMPUS_IDS:
        return True
    return campus.domain in ("torontomu.ca", "www.torontomu.ca", "ryerson.ca", "www.ryerson.ca")


def campus_from_settings(settings: Any) -> Campus:
    """Construct a typed ``Campus`` from process or per-guild ``Settings``."""
    uni_name = (
        str(getattr(settings, "rmp_university", "Toronto Metropolitan University") or "").strip()
        or "Toronto Metropolitan University"
    )
    raw_cid = str(getattr(settings, "campus_id", "") or "").strip()
    if raw_cid:
        cid = slugify_campus_id(raw_cid)
    else:
        name_slug = slugify_campus_id(uni_name)
        cid = "tmu" if name_slug in _TMU_CAMPUS_IDS else name_slug

    canonical_url = str(getattr(settings, "campus_url", "") or "").strip()
    if not canonical_url and cid == "tmu":
        canonical_url = "https://www.torontomu.ca"

    tz_name = (
        str(getattr(settings, "exams_tz", "America/Toronto") or "America/Toronto").strip()
        or "America/Toronto"
    )
    source_ids: dict[str, str] = {"rmp_name": uni_name}
    if cid == "tmu":
        source_ids["tmu_grades_zone"] = "tmu"

    return Campus(
        campus_id=cid,
        name=uni_name,
        canonical_url=canonical_url,
        timezone=tz_name,
        source_ids=source_ids,
    )


@dataclass(frozen=True)
class Department:
    """Academic department within a campus (e.g., Electrical Engineering, History)."""

    department_id: str
    campus_id: str
    code: str
    name: str


@dataclass(frozen=True)
class Program:
    """Degree program or major within a campus/department."""

    program_id: str
    campus_id: str
    name: str
    department_id: str = ""
    degree_level: str = "undergraduate"
    course_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Course:
    """Typed course record supporting multi-campus formats and alias lookup."""

    display_code: str
    normalized_key: str
    title: str = ""
    campus_id: str = ""
    department_code: str = ""
    aliases: tuple[str, ...] = ()
    term: str = ""

    @classmethod
    def from_code(
        cls,
        display_code: str,
        *,
        title: str = "",
        campus_id: str = "",
        department_code: str = "",
        aliases: Sequence[str] = (),
        term: str = "",
    ) -> Course:
        code_clean = str(display_code or "").strip()
        norm = normalize_course_key(code_clean)
        norm_aliases = tuple(
            dict.fromkeys(normalize_course_key(a) for a in aliases if normalize_course_key(a))
        )
        return cls(
            display_code=code_clean or norm,
            normalized_key=norm,
            title=str(title or "").strip(),
            campus_id=slugify_campus_id(campus_id) if campus_id else "",
            department_code=str(department_code or "").strip().upper(),
            aliases=norm_aliases,
            term=str(term or "").strip(),
        )


@dataclass(frozen=True)
class ResourceLink:
    """External licensed course resource link & metadata (never raw scraped document blobs)."""

    resource_id: str
    guild_id: int
    course_key: str
    display_code: str
    title: str
    url: str
    resource_type: str = "notes"  # notes | syllabus | textbook | repository | guide | other
    source_attribution: str = ""
    license_note: str = "External resource — rights remain with original author/institution."
    added_by: int = 0
    created_at: int = 0


@dataclass(frozen=True)
class Exam:
    """Typed exam schedule entry scoped to a guild with optional course linkage."""

    id: int
    guild_id: int
    course_code: str
    course_name: str
    start_ts: int
    end_ts: int | None = None
    course_key: str = ""
    organizer: str = ""
    location: str = ""
    notes: str = ""
    visibility: ExamVisibility = "guild"
    created_by: int = 0
    created_at: int = 0

    def __post_init__(self) -> None:
        if not self.course_key and self.course_code:
            object.__setattr__(self, "course_key", normalize_course_key(self.course_code))


@dataclass(frozen=True)
class ProfessorReview:
    """Single review snippet returned by a professor rating provider."""

    course_code: str
    helpful_rating: str
    difficulty_rating: str
    comment: str


@dataclass(frozen=True)
class ProfessorRecord:
    """Typed professor rating record independent of upstream provider schema."""

    provider_prof_id: str
    full_name: str
    institution: str
    department: str
    avg_rating: float
    num_ratings: int
    avg_difficulty: float
    would_take_again_pct: float
    top_tags: tuple[str, ...] = ()
    recent_reviews: tuple[ProfessorReview, ...] = ()
    profile_url: str = ""
    raw_payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ProfessorGradeRow:
    """Single instructor's grade distribution row for a course."""

    name: str
    term: str
    avg: str
    common: str
    students: str
    distribution: tuple[Mapping[str, Any], ...] = ()

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "term": self.term,
            "avg": self.avg,
            "common": self.common,
            "students": self.students,
            "dist": [dict(item) for item in self.distribution],
        }


@dataclass(frozen=True)
class GradeDistribution:
    """Typed grade distribution dataset for a course at a specific campus."""

    course_key: str
    course_title: str
    campus_id: str
    professors: tuple[ProfessorGradeRow, ...] = ()
    source_url: str = ""

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "title": self.course_title,
            "professors": [p.to_legacy_dict() for p in self.professors],
        }


@dataclass(frozen=True)
class ProviderProvenance:
    """Immutable provenance & retrieval metadata for every academic provider call."""

    provider_id: str
    provider_name: str
    fetched_at: int
    source_url: str = ""
    attribution: str = ""
    cached: bool = False


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """Standardized result envelope separating provider status/errors from core bot logic."""

    data: T
    status: ProviderStatus
    provenance: ProviderProvenance
    error_code: str = ""
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"
