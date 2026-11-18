"""Unit tests for hardened academic parsers, distribution alignment, off-loop charts, and TTL/negative cache."""

from __future__ import annotations

import matplotlib
import pytest

from icbot.academics import (
    MAX_CHART_PROFESSORS,
    AcademicProviderCache,
    CacheKey,
    ChartRenderError,
    PayloadLimitError,
    ProviderProvenance,
    ProviderResult,
    SchemaDriftError,
    build_accessible_grade_summary,
    format_freshness_line,
    parse_notes_spa_bundle,
    parse_notes_spa_html,
    parse_rmp_prof_response,
    parse_rmp_school_response,
    parse_tmu_grades_html,
    render_grade_distribution_chart,
    render_grade_distribution_chart_async,
)
from tests.helpers import load_fixture_json, load_fixture_text

pytestmark = pytest.mark.unit


def test_tmu_grades_parser_fixes_misaligned_all_professors_and_decorative_divs():
    """Regression lock: skipping 'All Professors' or decorative flex divs must NOT shift distributions."""
    html = load_fixture_text("tmu_grades_misaligned_v1.html")
    parsed = parse_tmu_grades_html(html, "coe817")
    assert parsed is not None
    assert parsed.course_title == "COE817 - Applied Cryptography"
    assert parsed.alignment_mode == "discrete_rows"
    assert len(parsed.professors) == 2

    ada = parsed.professors[0]
    turing = parsed.professors[1]
    assert ada.name == "Prof. Ada Lovelace"
    assert sum(int(item["count"]) for item in ada.distribution) == 40
    assert {item["letterGrade"]: item["count"] for item in ada.distribution} == {
        "B+": 10,
        "A": 20,
        "A+": 10,
    }

    assert turing.name == "Prof. Alan Turing"
    assert sum(int(item["count"]) for item in turing.distribution) == 50
    assert {item["letterGrade"]: item["count"] for item in turing.distribution} == {
        "C": 15,
        "B": 20,
        "B+": 15,
    }


def test_tmu_grades_parser_aligns_named_json_out_of_dom_order():
    """Named JSON distribution objects align by instructor name even when reversed from DOM order."""
    html = load_fixture_text("tmu_grades_named_json_v1.html")
    parsed = parse_tmu_grades_html(html, "cps530")
    assert parsed is not None
    assert parsed.alignment_mode == "named_json"
    assert len(parsed.professors) == 2

    hopper = parsed.professors[0]
    dijkstra = parsed.professors[1]
    assert hopper.name == "Prof. Grace Hopper"
    assert sum(int(item["count"]) for item in hopper.distribution) == 55
    assert dijkstra.name == "Prof. Edsger Dijkstra"
    assert sum(int(item["count"]) for item in dijkstra.distribution) == 65


def test_tmu_grades_parser_enforces_payload_limit_and_preserves_baseline_fixture():
    baseline_html = load_fixture_text("tmu_grades_course.html")
    parsed = parse_tmu_grades_html(baseline_html, "ele888")
    assert parsed is not None
    assert parsed.alignment_mode == "all_rows"
    assert len(parsed.professors) == 1
    assert parsed.professors[0].name == "Prof. Ada Lovelace"
    assert sum(int(item["count"]) for item in parsed.professors[0].distribution) == 45

    with pytest.raises(PayloadLimitError):
        parse_tmu_grades_html("x" * 2000, "ele888", max_bytes=1024)


def test_notes_spa_bounded_parsers_and_limits():
    html = load_fixture_text("notes_spa_index.html")
    js = load_fixture_text("notes_spa_bundle.js")

    parsed_html = parse_notes_spa_html(html, base_url="https://example.invalid/Notes/")
    assert ("CPS109", "https://example-student.notion.site/CPS109-00000000000000000000000000000003") in (
        parsed_html.links
    )
    assert parsed_html.bundle_url == "https://example.invalid/Notes/assets/index-deadbeef.js"

    pairs, truncated = parse_notes_spa_bundle(js)
    assert truncated is False
    codes = {code for code, _url in pairs}
    assert {"ELE888", "COE817"}.issubset(codes)

    # Size limit and link cap enforcement
    with pytest.raises(PayloadLimitError):
        parse_notes_spa_html("x" * 2048, base_url="https://example.invalid/", max_bytes=1024)
    with pytest.raises(PayloadLimitError):
        parse_notes_spa_bundle("x" * 2048, max_bytes=1024)

    capped_pairs, was_truncated = parse_notes_spa_bundle(js, max_links=1)
    assert len(capped_pairs) == 1
    assert was_truncated is True


def test_rmp_parsers_detect_schema_drift_cleanly():
    drifted = load_fixture_json("rmp_schema_drift_v1.json")
    with pytest.raises(SchemaDriftError, match="teachers"):
        parse_rmp_prof_response(drifted, institution="Toronto Metropolitan University")

    with pytest.raises(SchemaDriftError):
        parse_rmp_school_response({"errors": [{"message": "Rate limited"}]})

    # Valid fixtures parse cleanly into typed records
    school_id = parse_rmp_school_response(load_fixture_json("rmp_school_response.json"))
    assert school_id == "U2Nob29sLTEyMzQ="

    records = parse_rmp_prof_response(
        load_fixture_json("rmp_prof_response.json"),
        institution="Toronto Metropolitan University",
    )
    assert len(records) == 1
    assert records[0].full_name == "Ada Lovelace"
    assert records[0].avg_rating == pytest.approx(4.8)


async def test_off_loop_matplotlib_chart_renderer_no_global_mutation_and_accessible_alt():
    """Chart renders off the event loop, never mutates global rcParams, enforces bounds, and returns alt text."""
    rc_before = dict(matplotlib.rcParams)

    profs = [
        {
            "name": f"Prof. Instructor {i}",
            "term": "Fall 2025",
            "avg": "Avg: B+",
            "common": "Common: A-",
            "students": "45",
            "dist": [
                {"letterGrade": "B", "count": 15},
                {"letterGrade": "A-", "count": 20},
                {"letterGrade": "A+", "count": 10},
            ],
        }
        for i in range(1, 12)  # 11 professors -> capped to MAX_CHART_PROFESSORS (8)
    ]

    res = await render_grade_distribution_chart_async("ELE888 - Intelligent Systems", profs)
    assert res.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert res.size_bytes == len(res.png_bytes)
    assert res.plotted_count == MAX_CHART_PROFESSORS
    assert res.truncated_count == 3
    assert "ELE888 - Intelligent Systems" in res.accessible_alt_text
    assert "A: 67%" in res.accessible_alt_text

    # Global matplotlib rcParams must remain untouched (no plt.style.use side effects!)
    assert dict(matplotlib.rcParams) == rc_before

    # Enforcing tight max_bytes raises ChartRenderError cleanly
    with pytest.raises(ChartRenderError, match="attachment bound"):
        render_grade_distribution_chart("ELE888", profs[:1], max_bytes=128)

    empty_alt = build_accessible_grade_summary("EMPTY101", [])
    assert "no instructor distribution data" in empty_alt.lower()


def test_academic_provider_cache_ttl_negative_cache_and_eviction():
    cache = AcademicProviderCache(max_entries=2)
    k_ok = CacheKey.make("rmp_graphql", "tmu", "ada lovelace")
    k_nf = CacheKey.make("tmu_grades_zone", "tmu", "nope999")
    k_err = CacheKey.make("notes_spa", "tmu", "site-a")

    ok_res = ProviderResult(
        data=["ada"],
        status="ok",
        provenance=ProviderProvenance(
            provider_id="rmp_graphql",
            provider_name="RateMyProfessors",
            fetched_at=1000,
            attribution="RMP",
            cached=False,
        ),
    )
    nf_res = ProviderResult(
        data=None,
        status="not_found",
        provenance=ProviderProvenance(
            provider_id="tmu_grades_zone",
            provider_name="TMU Grades Zone",
            fetched_at=1000,
        ),
        error_code="course_not_found",
    )

    cache.set_provider_result(k_ok, ok_res, positive_ttl=600, now=1000.0)
    cache.set_provider_result(k_nf, nf_res, not_found_ttl=120, now=1000.0)

    # Fresh hits mark cached=True while preserving original fetched_at=1000
    hit_ok = cache.get_provider_result(k_ok, now=1100.0)
    assert hit_ok is not None
    assert hit_ok.provenance.cached is True
    assert hit_ok.provenance.fetched_at == 1000
    assert "Last fetched: <t:1000:R> (cached)" in format_freshness_line(hit_ok.provenance)

    # Negative cache hits within 120s and expires after 120s
    hit_nf = cache.get_provider_result(k_nf, now=1050.0)
    assert hit_nf is not None and hit_nf.status == "not_found"
    assert cache.get_provider_result(k_nf, now=1200.0) is None

    # Adding a third entry evicts oldest when at max_entries=2
    err_res = ProviderResult(
        data=[],
        status="error",
        provenance=ProviderProvenance(provider_id="notes_spa", provider_name="SPA", fetched_at=1200),
    )
    cache.set_provider_result(k_err, err_res, error_ttl=60, now=1200.0)
    assert len(cache) <= 2
