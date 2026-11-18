"""Unit tests for campus/curriculum domain models, multi-campus course keys, and URL SSRF safety."""

from __future__ import annotations

import pytest

from icbot.academics import (
    Campus,
    Course,
    Department,
    Exam,
    Program,
    ProviderProvenance,
    ProviderResult,
    ResourceLink,
    campus_from_settings,
    is_tmu_campus,
    normalize_course_key,
    parse_course_aliases,
    resolve_course_key,
    validate_fetch_url,
    validate_resource_url,
)
from icbot.config import Settings

pytestmark = pytest.mark.unit


def test_campus_and_curriculum_domain_models():
    campus = Campus(
        campus_id="maple_leaf_u",
        name="Maple Leaf University",
        canonical_url="https://www.mapleleaf.edu.invalid",
        timezone="America/Vancouver",
        source_ids={"rmp_name": "Maple Leaf University"},
    )
    assert campus.campus_id == "maple_leaf_u"
    assert campus.domain == "www.mapleleaf.edu.invalid"
    assert campus.timezone == "America/Vancouver"
    assert not is_tmu_campus(campus)

    dept = Department(
        department_id="mlu_hist",
        campus_id=campus.campus_id,
        code="HIST",
        name="History",
    )
    prog = Program(
        program_id="mlu_ba_hist",
        campus_id=campus.campus_id,
        department_id=dept.department_id,
        name="B.A. History",
        degree_level="undergraduate",
        course_prefixes=("HIST", "HSTM"),
    )
    course = Course.from_code(
        "HIST-2010",
        title="Modern Canadian History",
        campus_id=campus.campus_id,
        department_code=dept.code,
        aliases=("HST 2010", "HIST2010A"),
        term="Fall 2026",
    )
    assert course.normalized_key == "HIST2010"
    assert course.aliases == ("HST2010", "HIST2010A")
    assert prog.course_prefixes == ("HIST", "HSTM")

    res = ResourceLink(
        resource_id="manual:1",
        guild_id=100,
        course_key=course.normalized_key,
        display_code=course.display_code,
        title="Open Syllabus & Reading List",
        url="https://ocw.mapleleaf.edu.invalid/hist-2010",
        resource_type="syllabus",
        source_attribution="History Student Society",
    )
    assert res.course_key == "HIST2010"
    assert "rights remain" in res.license_note.lower()

    ex = Exam(
        id=1,
        guild_id=100,
        course_code="HIST-2010",
        course_name="Modern Canadian History",
        start_ts=1797000000,
        organizer="History Department",
        visibility="guild",
    )
    assert ex.course_key == "HIST2010"
    assert ex.organizer == "History Department"
    assert ex.visibility == "guild"


def test_campus_from_settings_detects_tmu_vs_generic():
    tmu_settings = Settings(rmp_university="Toronto Metropolitan University", exams_tz="America/Toronto")
    tmu_campus = campus_from_settings(tmu_settings)
    assert tmu_campus.campus_id == "tmu"
    assert is_tmu_campus(tmu_campus)
    assert tmu_campus.canonical_url == "https://www.torontomu.ca"

    other_settings = Settings(
        rmp_university="University of British Columbia",
        campus_id="ubc",
        campus_url="https://www.ubc.ca",
        exams_tz="America/Vancouver",
    )
    ubc_campus = campus_from_settings(other_settings)
    assert ubc_campus.campus_id == "ubc"
    assert ubc_campus.domain == "www.ubc.ca"
    assert ubc_campus.timezone == "America/Vancouver"
    assert not is_tmu_campus(ubc_campus)


def test_multi_campus_course_normalization_and_alias_mapping():
    assert normalize_course_key("ele 888") == "ELE888"
    assert normalize_course_key("COMP-2404") == "COMP2404"
    assert normalize_course_key("CS 135") == "CS135"
    assert normalize_course_key("BIO120H1") == "BIO120H1"
    assert normalize_course_key("HIST 2010A") == "HIST2010A"

    aliases = parse_course_aliases(["ECE888=ELE888", "CSCI*->CS*", "COMP 2404 = COMP2404"])
    assert resolve_course_key("ece 888", aliases) == "ELE888"
    assert resolve_course_key("CSCI 135", aliases) == "CS135"
    assert resolve_course_key("BIO120H1", aliases) == "BIO120H1"


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "ftp://files.example.invalid/notes.pdf",
        "https://user:secret@example.invalid/notes",
        "http://localhost:8000/admin",
        "https://127.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal",
        "https://192.168.1.10/router",
        "https://[::1]/loopback",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://db.internal/dump",
        "https://service.local/status",
        "https://redis/info",
        "https://example.invalid/has space",
    ],
)
def test_validate_resource_url_blocks_unsafe_targets(bad_url: str):
    res = validate_resource_url(bad_url, require_https=False)
    assert res.ok is False
    assert res.reason


def test_validate_fetch_url_requires_https_blocks_ips_and_enforces_allowlist():
    # Plain HTTP is rejected for server-side fetching
    assert validate_fetch_url("http://notes.example.invalid/index.html").ok is False

    # Direct public IP addresses are rejected for server-side scraping
    assert validate_fetch_url("https://93.184.216.34/notes").ok is False

    # Valid HTTPS URL passes without allowlist
    ok_res = validate_fetch_url("https://notes.example.invalid/courses/")
    assert ok_res.ok is True
    assert ok_res.host == "notes.example.invalid"

    # Allowlist enforcement: exact + subdomain allowed, others blocked
    allow = ("example.invalid", "tmu.grades.zone")
    assert validate_fetch_url("https://notes.example.invalid/a", allowed_hosts=allow).ok is True
    assert validate_fetch_url("https://tmu.grades.zone/course/ele888", allowed_hosts=allow).ok is True
    blocked = validate_fetch_url("https://untrusted.other.invalid/a", allowed_hosts=allow)
    assert blocked.ok is False
    assert "allowlist" in blocked.reason.lower()


def test_provider_result_provenance_contract():
    prov = ProviderProvenance(
        provider_id="manual_catalog",
        provider_name="Guild Resource Catalog",
        fetched_at=1760000000,
        attribution="Engineering Society",
        cached=True,
    )
    res: ProviderResult[list[str]] = ProviderResult(data=["item"], status="ok", provenance=prov)
    assert res.ok is True
    assert res.provenance.provider_id == "manual_catalog"
    assert res.provenance.fetched_at == 1760000000
