"""Bounded, pure response parsers for academic providers (Notes SPA, RMP GraphQL, TMU Grades).

Separates HTML/JSON parsing from HTTP retrieval, caching, and Discord presentation so
every parser can be verified offline against versioned sanitized response fixtures.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from icbot.academics.models import (
    ProfessorGradeRow,
    ProfessorRecord,
    ProfessorReview,
    normalize_course_key,
)
from icbot.academics.url_safety import validate_fetch_url, validate_resource_url

# Strict parser payload & extraction caps (anti-DoS / anti-ReDoS)
MAX_SPA_HTML_BYTES = 512 * 1024  # 512 KiB
MAX_SPA_JS_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_SPA_LINKS = 500
MAX_GRADES_HTML_BYTES = 1024 * 1024  # 1 MiB
MAX_GRADE_DISTRIBUTIONS = 200

_COURSE_CODE_RE = re.compile(r"([A-Z]{2,5}[-\s]?\d{3,4}[A-Z0-9]{0,2})", re.IGNORECASE)
_NOTION_URL_RE = re.compile(r"https://[A-Za-z0-9._-]+\.notion\.site/[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+")
_SPA_BUNDLE_RE = re.compile(r"/assets/index-[a-f0-9]{4,64}\.js$", re.IGNORECASE)
_NAMED_DIST_RE = re.compile(
    r'\{[^{}]*"(?:name|professor)"\s*:\s*"([^"]{1,120})"[^{}]*"distribution"\s*:\s*(\[[^\]]{0,8192}\])'
)
_DIST_ARRAY_RE = re.compile(r'"distribution"\s*:\s*(\[[^\]]{0,8192}\])')
_INT_RE = re.compile(r"\d+")


class SchemaDriftError(ValueError):
    """Raised when an upstream provider's response structure no longer matches expected contracts."""


class PayloadLimitError(ValueError):
    """Raised when an upstream response exceeds bounded parser size limits."""


# ---------------------------------------------------------------------------
# 1. Notes SPA Bounded Parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedNotesSpaHtml:
    """Structured extraction result from a Notes SPA HTML entrypoint."""

    links: tuple[tuple[str, str], ...]
    bundle_url: str | None = None
    truncated: bool = False


def _extract_course_notion_pairs(
    candidate_urls: list[str],
    *,
    max_links: int = MAX_SPA_LINKS,
) -> tuple[list[tuple[str, str]], bool]:
    """Validate Notion URLs and extract ``(normalized_course_key, url)`` pairs with strict bounds."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    truncated = False

    for raw_url in candidate_urls:
        if len(pairs) >= max_links:
            truncated = True
            break
        cleaned = raw_url.rstrip(".,);:\"'\\>")
        check = validate_resource_url(cleaned, require_https=True)
        if not check.ok:
            continue
        match = _COURSE_CODE_RE.search(check.normalized_url)
        if not match:
            continue
        code = normalize_course_key(match.group(1))
        if not code:
            continue
        pair = (code, check.normalized_url)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs, truncated


def parse_notes_spa_html(
    html: str,
    *,
    base_url: str,
    allowed_hosts: Sequence[str] | None = None,
    max_bytes: int = MAX_SPA_HTML_BYTES,
    max_links: int = MAX_SPA_LINKS,
) -> ParsedNotesSpaHtml:
    """Parse a Notes SPA HTML document using BeautifulSoup + bounded URL extraction."""
    if len(html.encode("utf-8", errors="ignore")) > max_bytes:
        raise PayloadLimitError(f"Notes SPA HTML exceeds {max_bytes} byte parser limit.")

    soup = BeautifulSoup(html, "html.parser")
    raw_urls: list[str] = []

    # 1. Structured anchor tags
    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag.get("href") or "").strip()
        if ".notion.site/" in href and href.startswith("https://"):
            raw_urls.append(href)
            if len(raw_urls) >= max_links * 2:
                break

    # 2. Bounded scan for inline Notion URLs if anchors were sparse
    if len(raw_urls) < max_links:
        for m in _NOTION_URL_RE.finditer(html):
            raw_urls.append(m.group(0))
            if len(raw_urls) >= max_links * 2:
                break

    # 3. Locate the single Vite/SPA entry JS bundle script tag and validate its target URL
    bundle_url: str | None = None
    for script_tag in soup.find_all("script", src=True):
        src = str(script_tag.get("src") or "").strip()
        if _SPA_BUNDLE_RE.search(src):
            resolved = urllib.parse.urljoin(base_url, src)
            check = validate_fetch_url(resolved, allowed_hosts=allowed_hosts)
            if check.ok:
                bundle_url = check.normalized_url
            break

    pairs, truncated = _extract_course_notion_pairs(raw_urls, max_links=max_links)
    return ParsedNotesSpaHtml(links=tuple(pairs), bundle_url=bundle_url, truncated=truncated)


def parse_notes_spa_bundle(
    js_text: str,
    *,
    max_bytes: int = MAX_SPA_JS_BYTES,
    max_links: int = MAX_SPA_LINKS,
) -> tuple[list[tuple[str, str]], bool]:
    """Extract bounded, HTTPS-validated Notion course links from a SPA JS bundle."""
    if len(js_text.encode("utf-8", errors="ignore")) > max_bytes:
        raise PayloadLimitError(f"Notes SPA JS bundle exceeds {max_bytes} byte parser limit.")

    raw_urls: list[str] = []
    for m in _NOTION_URL_RE.finditer(js_text):
        raw_urls.append(m.group(0))
        if len(raw_urls) >= max_links * 2:
            break

    return _extract_course_notion_pairs(raw_urls, max_links=max_links)


# ---------------------------------------------------------------------------
# 2. RateMyProfessors Unofficial GraphQL Defensive Parsers
# ---------------------------------------------------------------------------


def parse_rmp_school_response(payload: Any) -> str | None:
    """Parse RMP school search payload, raising ``SchemaDriftError`` on unexpected schema drift."""
    if not isinstance(payload, dict):
        raise SchemaDriftError("RMP school response must be a JSON object.")

    if payload.get("errors") and not payload.get("data"):
        raise SchemaDriftError(f"RMP GraphQL returned errors: {payload['errors']!r}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise SchemaDriftError("RMP school response missing 'data' object.")

    new_search = data.get("newSearch")
    if not isinstance(new_search, dict) or "schools" not in new_search:
        raise SchemaDriftError("RMP school response missing 'data.newSearch.schools'.")

    schools = new_search.get("schools")
    if not isinstance(schools, dict) or not isinstance(schools.get("edges"), list):
        raise SchemaDriftError("RMP school response missing 'schools.edges' list.")

    edges = schools["edges"]
    if not edges:
        return None

    first = edges[0]
    if not isinstance(first, dict) or not isinstance(first.get("node"), dict):
        raise SchemaDriftError("RMP school edge missing 'node' object.")

    node_id = first["node"].get("id")
    return str(node_id).strip() if node_id is not None and str(node_id).strip() else None


def parse_rmp_prof_response(payload: Any, *, institution: str) -> list[ProfessorRecord]:
    """Parse RMP teacher search payload into typed ``ProfessorRecord`` list with drift protection."""
    if not isinstance(payload, dict):
        raise SchemaDriftError("RMP professor response must be a JSON object.")

    data = payload.get("data")
    if not isinstance(data, dict):
        if payload.get("errors"):
            raise SchemaDriftError(f"RMP GraphQL returned errors: {payload['errors']!r}")
        raise SchemaDriftError("RMP professor response missing 'data' object.")

    new_search = data.get("newSearch")
    if not isinstance(new_search, dict) or "teachers" not in new_search:
        raise SchemaDriftError("RMP professor response missing 'data.newSearch.teachers' (schema drift).")

    teachers = new_search.get("teachers")
    if not isinstance(teachers, dict) or not isinstance(teachers.get("edges"), list):
        raise SchemaDriftError("RMP professor response missing 'teachers.edges' list.")

    records: list[ProfessorRecord] = []
    for edge in teachers["edges"]:
        if not isinstance(edge, dict) or not isinstance(edge.get("node"), dict):
            continue
        raw = edge["node"]
        first_name = str(raw.get("firstName") or "").strip()
        last_name = str(raw.get("lastName") or "").strip()
        full_name = f"{first_name} {last_name}".strip()
        if not full_name:
            continue

        tags_raw = raw.get("teacherRatingTags")
        top_tags_list: list[str] = []
        if isinstance(tags_raw, list):
            valid_tags = [t for t in tags_raw if isinstance(t, dict) and t.get("tagName")]
            valid_tags.sort(key=lambda x: _safe_int(x.get("tagCount"), 0), reverse=True)
            top_tags_list = [str(t["tagName"]).strip() for t in valid_tags[:5]]

        reviews: list[ProfessorReview] = []
        ratings_obj = raw.get("ratings")
        if isinstance(ratings_obj, dict) and isinstance(ratings_obj.get("edges"), list):
            for r_edge in ratings_obj["edges"][:3]:
                if not isinstance(r_edge, dict) or not isinstance(r_edge.get("node"), dict):
                    continue
                node = r_edge["node"]
                reviews.append(
                    ProfessorReview(
                        course_code=str(node.get("class") or "Unknown").strip() or "Unknown",
                        helpful_rating=str(node.get("helpfulRating", "?")),
                        difficulty_rating=str(node.get("difficultyRating", "?")),
                        comment=str(node.get("comment") or "").strip(),
                    )
                )

        prof_id = str(raw.get("id") or "").strip()
        profile_url = f"https://www.ratemyprofessors.com/professor/{prof_id}" if prof_id else ""
        records.append(
            ProfessorRecord(
                provider_prof_id=prof_id,
                full_name=full_name,
                institution=institution,
                department=str(raw.get("department") or "Unknown Department").strip() or "Unknown Department",
                avg_rating=_safe_float(raw.get("avgRating"), 0.0),
                num_ratings=_safe_int(raw.get("numRatings"), 0),
                avg_difficulty=_safe_float(raw.get("avgDifficulty"), 0.0),
                would_take_again_pct=_safe_float(raw.get("wouldTakeAgainPercent"), -1.0),
                top_tags=tuple(top_tags_list),
                recent_reviews=tuple(reviews),
                profile_url=profile_url,
                raw_payload=dict(raw),
            )
        )
    return records


def _safe_float(val: Any, default: float) -> float:
    try:
        if val is None or isinstance(val, bool):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int) -> int:
    try:
        if val is None or isinstance(val, bool):
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 3. TMU Grades Zone Parser (With Exact Distribution-to-Professor Alignment Fix)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedTmuGrades:
    """Result of parsing a TMU Grades Zone course page with alignment diagnostics."""

    course_title: str
    professors: tuple[ProfessorGradeRow, ...]
    alignment_mode: str  # "named_json" | "all_rows" | "discrete_rows" | "student_count" | "empty"
    alignment_warning: str = ""

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "title": self.course_title,
            "professors": [p.to_legacy_dict() for p in self.professors],
        }


@dataclass(frozen=True)
class _RawProfBlock:
    name: str
    is_aggregate: bool
    term: str
    avg: str
    common: str
    students: str
    students_int: int | None


def _validate_distribution_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        grade = str(item.get("letterGrade") or "").strip()
        count = _safe_int(item.get("count"), -1)
        if grade and count >= 0:
            cleaned.append({"letterGrade": grade, "count": count})
    return cleaned


def _parse_student_int(text: str) -> int | None:
    m = _INT_RE.search(str(text or ""))
    return int(m.group(0)) if m else None


def parse_tmu_grades_html(
    html: str,
    course_code: str,
    *,
    max_bytes: int = MAX_GRADES_HTML_BYTES,
) -> ParsedTmuGrades | None:
    """Parse TMU Grades Zone HTML with safe bounds and deterministic professor-to-distribution alignment.

    Fixes the historic index-shift bug where skipping the ``All Professors`` block or decorative
    flex containers shifted subsequent professors onto the wrong grade distribution array.
    """
    if len(html.encode("utf-8", errors="ignore")) > max_bytes:
        raise PayloadLimitError(f"Grades HTML for {course_code} exceeds {max_bytes} byte limit.")

    clean_html = html.replace('\\"', '"')
    soup = BeautifulSoup(clean_html, "html.parser")

    title_el = soup.find("h2")
    if not title_el or not title_el.text.strip():
        return None
    course_title = title_el.text.strip()

    # 1. Extract explicitly named JSON distributions when present (order-independent)
    named_distributions: dict[str, list[dict[str, Any]]] = {}
    for m in _NAMED_DIST_RE.finditer(clean_html):
        if len(named_distributions) >= MAX_GRADE_DISTRIBUTIONS:
            break
        prof_name = m.group(1).strip().lower()
        try:
            parsed_arr = _validate_distribution_list(json.loads(m.group(2)))
        except (json.JSONDecodeError, ValueError):
            continue
        if prof_name and parsed_arr:
            named_distributions[prof_name] = parsed_arr

    # 2. Extract ordered distribution arrays (bounded)
    distributions: list[list[dict[str, Any]]] = []
    for m in _DIST_ARRAY_RE.finditer(clean_html):
        if len(distributions) >= MAX_GRADE_DISTRIBUTIONS:
            break
        try:
            parsed_arr = _validate_distribution_list(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
        if parsed_arr:
            distributions.append(parsed_arr)

    # 3. Extract ONLY genuine professor DOM blocks (filter out decorative flex divs lacking .font-bold)
    raw_blocks = soup.find_all(
        "div",
        class_="flex h-full w-full flex-wrap items-center justify-center gap-2 lg:flex-row lg:items-start",
    )
    valid_rows: list[_RawProfBlock] = []
    for block in raw_blocks:
        name_div = block.select_one(".font-bold")
        if not name_div:
            continue
        name = name_div.text.strip()
        if not name:
            continue

        term_p = block.find("p")
        term = term_p.text.strip() if term_p else ""

        badges = block.find_all("span", attrs={"data-slot": "badge"})
        avg = badges[0].text.strip() if len(badges) > 0 else "N/A"
        common = badges[1].text.strip() if len(badges) > 1 else "N/A"

        student_span = block.select_one(".bg-accent")
        students = student_span.text.replace("student", "").strip() if student_span else "N/A"

        valid_rows.append(
            _RawProfBlock(
                name=name,
                is_aggregate=(name.lower() == "all professors"),
                term=term,
                avg=avg,
                common=common,
                students=students,
                students_int=_parse_student_int(students),
            )
        )

    discrete_rows = [r for r in valid_rows if not r.is_aggregate]
    if not discrete_rows:
        return ParsedTmuGrades(
            course_title=course_title,
            professors=(),
            alignment_mode="empty",
        )

    # 4. Align distributions deterministically without index-shift corruption
    assigned: list[list[dict[str, Any]]] = [[] for _ in discrete_rows]
    alignment_mode = "empty"
    alignment_warning = ""

    # Strategy A: Explicit name-matched JSON objects
    if named_distributions and all(r.name.lower() in named_distributions for r in discrete_rows):
        alignment_mode = "named_json"
        for idx, row in enumerate(discrete_rows):
            assigned[idx] = named_distributions[row.name.lower()]
    # Strategy B: Distribution list matches ALL valid professor rows (aggregate + discrete)
    elif len(distributions) == len(valid_rows):
        alignment_mode = "all_rows"
        d_idx = 0
        for v_idx, v_row in enumerate(valid_rows):
            if v_row.is_aggregate:
                continue
            assigned[d_idx] = distributions[v_idx]
            d_idx += 1
    # Strategy C: Distribution list matches ONLY discrete professor rows (aggregate row has no distribution)
    elif len(distributions) == len(discrete_rows):
        alignment_mode = "discrete_rows"
        for idx in range(len(discrete_rows)):
            assigned[idx] = distributions[idx]
    # Strategy D: Unaligned count fallback — match strictly by unique student-count sum to prevent misassignment
    elif distributions:
        alignment_mode = "student_count"
        alignment_warning = (
            f"Upstream HTML contained {len(distributions)} distribution(s) for "
            f"{len(discrete_rows)} instructor block(s); aligned by student totals."
        )
        used_dist_indices: set[int] = set()
        dist_totals = [sum(int(item["count"]) for item in d) for d in distributions]
        for idx, row in enumerate(discrete_rows):
            if row.students_int is None:
                continue
            candidates = [
                di
                for di, tot in enumerate(dist_totals)
                if di not in used_dist_indices and tot == row.students_int
            ]
            if len(candidates) == 1:
                chosen = candidates[0]
                used_dist_indices.add(chosen)
                assigned[idx] = distributions[chosen]

    prof_rows = tuple(
        ProfessorGradeRow(
            name=row.name,
            term=row.term,
            avg=row.avg,
            common=row.common,
            students=row.students,
            distribution=tuple(assigned[idx]),
        )
        for idx, row in enumerate(discrete_rows)
    )
    return ParsedTmuGrades(
        course_title=course_title,
        professors=prof_rows,
        alignment_mode=alignment_mode,
        alignment_warning=alignment_warning,
    )
