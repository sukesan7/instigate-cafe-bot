"""External provider contract tests using sanitized fixtures and monkeypatched aiohttp."""

from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest

from icbot.config import Settings
from icbot.features.notes import fetch_notes_from_site
from icbot.features.prof import RMP_GRAPHQL_URL, build_prof_embed, get_school_id, search_prof
from icbot.services.gemini_client import GeminiError, gemini_generate_text
from icbot.services.go_client import GOFetchError, fetch_service_alerts
from icbot.services.jobs_ats_client import fetch_greenhouse_jobs, fetch_lever_jobs
from icbot.services.jobs_workday_client import fetch_workday_jobs, parse_workday_sites
from icbot.services.yahoo_quotes import YF_CRUMB_URL, YF_FC_URL, fetch_quotes
from tests.helpers import FakeAiohttpResponse, load_fixture_json, load_fixture_text

pytestmark = pytest.mark.provider_contract


async def test_notes_spa_parser_extracts_html_and_bundle_courses(mock_aiohttp):
    mock_aiohttp.get(
        "https://example.invalid/Notes/",
        text=load_fixture_text("notes_spa_index.html"),
    )
    mock_aiohttp.get(
        "https://example.invalid/Notes/assets/index-deadbeef.js",
        text=load_fixture_text("notes_spa_bundle.js"),
    )

    async with aiohttp.ClientSession() as session:
        results = await fetch_notes_from_site(session, "Synthetic Notes", "https://example.invalid/Notes/")

    mapped = dict(results)
    assert mapped["CPS109"] == "https://example-student.notion.site/CPS109-00000000000000000000000000000003"
    assert mapped["ELE888"] == "https://example-student.notion.site/ELE888-00000000000000000000000000000001"
    assert mapped["COE817"] == "https://example-student.notion.site/COE-817-00000000000000000000000000000002"


async def test_ratemyprofessors_graphql_contract(mock_aiohttp, synthetic_settings: Settings):
    school_payload = load_fixture_json("rmp_school_response.json")
    prof_payload = load_fixture_json("rmp_prof_response.json")

    def _rmp_handler(call):
        query_text = (call.json_body or {}).get("query", "")
        if "SchoolSearchQuery" in query_text:
            return FakeAiohttpResponse(
                url=call.url,
                method=call.method,
                status=200,
                body=json.dumps(school_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        return FakeAiohttpResponse(
            url=call.url,
            method=call.method,
            status=200,
            body=json.dumps(prof_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    mock_aiohttp.post(RMP_GRAPHQL_URL, handler=_rmp_handler)

    async with aiohttp.ClientSession() as session:
        school_id = await get_school_id(session, "Toronto Metropolitan University")
        assert school_id == "U2Nob29sLTEyMzQ="

        profs = await search_prof(session, "Lovelace", school_id)
        assert len(profs) == 1

    embed = build_prof_embed(synthetic_settings, profs[0], "Toronto Metropolitan University")
    assert "Ada Lovelace" in embed.title
    assert "4.8/5.0" in embed.title
    assert embed.url == "https://www.ratemyprofessors.com/professor/VGVhY2hlci0xMDAx"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["🏷️ Top Tags"].startswith("CLEAR GRADING CRITERIA")
    assert "**ELE888**" in fields["💬 Recent Reviews"]


async def test_tmu_grades_html_parser_and_sqlite_cache(
    mock_aiohttp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from icbot.features import grades as grades_mod

    monkeypatch.setattr(grades_mod, "DB_PATH", str(tmp_path / "tmu_grades_test.db"))
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/ele888",
        text=load_fixture_text("tmu_grades_course.html"),
    )

    first = await grades_mod.fetch_course_data("ELE888")
    assert first is not None
    assert first["title"] == "ELE888 - Intelligent Systems"
    # Aggregate "All Professors" row must be excluded
    assert len(first["professors"]) == 1
    prof = first["professors"][0]
    assert prof["name"] == "Prof. Ada Lovelace"
    assert prof["avg"] == "Avg: B+"
    assert prof["common"] == "Common: A-"
    assert prof["students"] == "45"
    assert len(prof["dist"]) == 3
    assert len(mock_aiohttp.calls) == 1

    # Chart generation returns valid PNG binary bytes
    chart_buf = grades_mod.generate_chart(first["title"], first["professors"])
    assert chart_buf.getvalue().startswith(b"\x89PNG\r\n\x1a\n")

    # Second lookup hits local SQLite cache with zero additional HTTP calls
    second = await grades_mod.fetch_course_data("ele888")
    assert second == first
    assert len(mock_aiohttp.calls) == 1


async def test_greenhouse_and_lever_ats_contracts(mock_aiohttp):
    mock_aiohttp.get(
        "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true",
        json_data=load_fixture_json("greenhouse_jobs.json"),
    )
    mock_aiohttp.get(
        "https://api.lever.co/v0/postings/examplesite?mode=json",
        json_data=load_fixture_json("lever_jobs.json"),
    )

    async with aiohttp.ClientSession() as session:
        gh_jobs = await fetch_greenhouse_jobs(session, "examplecorp")
        lever_jobs = await fetch_lever_jobs(session, "examplesite")

    assert len(gh_jobs) == 2
    assert gh_jobs[0].external_id == "900101"
    assert gh_jobs[0].source == "greenhouse:examplecorp"
    assert gh_jobs[1].location == "(location not listed)"

    assert len(lever_jobs) == 2
    assert lever_jobs[0].external_id == "lever-job-1001"
    assert lever_jobs[0].posted_unix == 1760530200  # ms epoch normalized to seconds


async def test_workday_warmup_csrf_and_cxs_normalization(mock_aiohttp):
    sites = parse_workday_sites(["Acme|https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"])
    assert len(sites) == 1

    mock_aiohttp.get(
        "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers",
        text=load_fixture_text("workday_careers.html"),
    )
    mock_aiohttp.post(
        "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs?locale=en-US",
        json_data=load_fixture_json("workday_cxs_jobs.json"),
    )

    async with aiohttp.ClientSession() as session:
        jobs, cxs_info = await fetch_workday_jobs(session, sites[0])

    assert cxs_info.tenant == "acme"
    assert cxs_info.site == "AcmeCareers"
    assert len(jobs) == 2
    assert jobs[0].source == "workday:Acme"
    assert (
        jobs[0].url
        == "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers/job/Toronto-ON/Software-Engineering-Co-op_R1001"
    )
    assert (
        jobs[1].url
        == "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers/job/Ottawa-ON/Firmware-Engineer-Intern_R1002"
    )

    post_call = next(c for c in mock_aiohttp.calls if c.method == "POST")
    assert post_call.headers.get("X-Calypso-CSRF-Token") == "wd-csrf-synthetic-token-0001"


async def test_metrolinx_go_service_alerts_contract(mock_aiohttp):
    api_base = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    mock_aiohttp.get(
        f"{api_base}/ServiceUpdate/ServiceAlert/All?key=synthetic-go-key",
        json_data=load_fixture_json("metrolinx_service_alerts.json"),
    )

    async with aiohttp.ClientSession() as session:
        alerts = await fetch_service_alerts(session, api_base, "synthetic-go-key")

    assert len(alerts) == 1
    assert alerts[0].code == "SA-0001"
    assert alerts[0].lines == ["LW"]
    assert alerts[0].stops == ["Union Station"]
    assert alerts[0].trips == ["101"]

    mock_aiohttp.get(
        f"{api_base}/ServiceUpdate/ServiceAlert/All?key=bad-key",
        status=401,
        text="Unauthorized",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(GOFetchError):
            await fetch_service_alerts(session, api_base, "bad-key")


async def test_gemini_rest_contract_and_error_handling(mock_aiohttp):
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=synthetic-gemini"
    mock_aiohttp.post(endpoint, json_data=load_fixture_json("gemini_generate_response.json"))

    text = await gemini_generate_text(
        api_key="synthetic-gemini",
        model="gemini-2.5-flash",
        prompt="Summarize test status.",
        system_instruction="Be concise.",
        inline_images=[("image/png", b"\x89PNG\r\n\x1a\n")],
        temperature=0.2,
        max_output_tokens=128,
    )
    assert text == "Synthetic model response for unit verification."
    posted_body = mock_aiohttp.calls[-1].json_body
    assert posted_body["systemInstruction"]["parts"][0]["text"] == "Be concise."
    assert posted_body["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/png"

    with pytest.raises(GeminiError):
        await gemini_generate_text(api_key="", model="gemini-2.5-flash", prompt="Hello")

    err_endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=rate-limited"
    mock_aiohttp.post(err_endpoint, status=429, text='{"error": "quota"}')
    with pytest.raises(GeminiError) as exc_info:
        await gemini_generate_text(api_key="rate-limited", model="gemini-2.5-flash", prompt="Hello")
    assert exc_info.value.status == 429


async def test_yahoo_quotes_crumb_and_session_contract(mock_aiohttp):
    from icbot.services import yahoo_quotes as yq

    mock_aiohttp.get(YF_FC_URL, text="ok")
    mock_aiohttp.get(YF_CRUMB_URL, text="synthetic.crumb.123")
    mock_aiohttp.get(
        "https://query2.finance.yahoo.com/v7/finance/quote",
        prefix=True,
        json_data=load_fixture_json("yahoo_quotes_response.json"),
    )

    quotes = await fetch_quotes(["SPY", "QQQ", "^VIX", "spy"])
    assert set(quotes.keys()) == {"SPY", "QQQ", "^VIX"}

    # PRE session selection + calculated pct checks
    assert quotes["SPY"].best_session() == ("PRE", 505.0, 1.0, 5.0)
    assert quotes["QQQ"].best_session()[0] == "POST"
    assert round(quotes["QQQ"].post_change_pct or 0.0, 3) == round((430.0 - 425.0) / 425.0 * 100.0, 3)
    assert quotes["^VIX"].best_session()[0] == "REG"
    assert round(quotes["^VIX"].regular_change_pct or 0.0, 1) == 20.0

    # If Yahoo responds 401/403 on quotes, crumb cache resets safely
    yq._CRUMB = "stale-crumb"
    mock_aiohttp._routes.clear()
    mock_aiohttp.get("https://query2.finance.yahoo.com/v7/finance/quote", prefix=True, status=401, text="Unauthorized")
    mock_aiohttp.get("https://query1.finance.yahoo.com/v7/finance/quote", prefix=True, status=401, text="Unauthorized")
    with pytest.raises(RuntimeError, match="Yahoo quote fetch failed"):
        await fetch_quotes(["SPY"])
    assert yq._CRUMB is None

