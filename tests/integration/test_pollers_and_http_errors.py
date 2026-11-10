"""Poller idempotency, checkpoint persistence, and HTTP error-family characterization tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from icbot.db import Database
from icbot.features import grades as grades_mod
from icbot.features.notes import fetch_notes_from_site
from icbot.features.prof import RMP_GRAPHQL_URL, fetch_graphql, get_school_id, search_prof
from icbot.services.btc_esplora import DEFAULT_BASE_URL as ESPLORA_BASE
from icbot.services.btc_esplora import fetch_address_txs
from icbot.services.crypto_fng import FNG_URL, fetch_fear_greed
from icbot.services.gemini_client import GeminiError, gemini_generate_text
from icbot.services.go_client import GOFetchError, fetch_service_alerts
from icbot.services.jobs_ats_client import fetch_greenhouse_jobs, fetch_lever_jobs
from icbot.services.jobs_workday_client import WorkdaySite, fetch_workday_jobs, parse_workday_sites
from icbot.services.sports_client import ODDS_BASE_URL, OddsAPIClient, OddsAPIError
from icbot.services.ttc_client import TTCFetchError, fetch_account_status_items, resolve_account_id
from icbot.services.yahoo_quotes import YF_CRUMB_URL, YF_FC_URL, fetch_quotes
from icbot.tasks.go_alerts import poll_and_post_go_alerts
from icbot.tasks.job_alerts import poll_and_post_jobs
from icbot.tasks.jobs_workday_alerts import poll_and_post_workday_jobs
from icbot.tasks.ttc_alerts import poll_and_post_ttc
from tests.helpers import load_fixture_json, load_fixture_text

pytestmark = pytest.mark.provider_contract


async def _instant_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Poller checkpoint & idempotency characterization
# ---------------------------------------------------------------------------


async def test_jobs_poller_checkpoint_dedupe_filtering_and_bad_source_cache(
    make_poller_bot,
    synthetic_settings,
    mock_aiohttp,
    tmp_db: Database,
):
    cfg = replace(
        synthetic_settings,
        greenhouse_boards=["acme", "missingboard"],
        lever_sites=["leverco"],
        jobs_bootstrap_post=False,
        jobs_location_allowlist=["Toronto", "Remote"],
    )
    bot, channels = make_poller_bot(settings=cfg)
    jobs_ch = channels[cfg.job_alerts_channel_id]

    gh_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    gh_bad_url = "https://boards-api.greenhouse.io/v1/boards/missingboard/jobs?content=true"
    lv_url = "https://api.lever.co/v0/postings/leverco?mode=json"

    gh_feed = load_fixture_json("greenhouse_jobs.json")
    lv_feed = load_fixture_json("lever_jobs.json")

    mock_aiohttp.get(gh_url, json_data=gh_feed)
    mock_aiohttp.get(gh_bad_url, status=404, text="Job not found")
    mock_aiohttp.get(lv_url, json_data=lv_feed)

    # Cycle 1: bootstrap_post=False checkpoints all jobs without spamming; missingboard cached as bad
    await poll_and_post_jobs(bot)
    assert len(jobs_ch.sent) == 0
    assert "greenhouse:missingboard" in bot._jobs_bad_sources
    seen_ct = await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen")
    assert seen_ct == (4,)

    # Cycle 2: identical feed produces 0 posts and skips cached bad source
    calls_before = len(mock_aiohttp.calls)
    await poll_and_post_jobs(bot)
    assert len(jobs_ch.sent) == 0
    bad_calls = [c for c in mock_aiohttp.calls[calls_before:] if "missingboard" in c.url]
    assert bad_calls == []

    # Cycle 3: add 1 eligible intern role + 1 Senior Director role -> only intern posted, both checkpointed
    updated_gh = {
        "jobs": gh_feed["jobs"]
        + [
            {
                "id": 900999,
                "title": "Software Engineering Intern",
                "location": {"name": "Toronto, ON"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/900999",
                "departments": [{"name": "Engineering"}],
                "updated_at": "2026-10-22T12:00:00Z",
            },
            {
                "id": 901000,
                "title": "Senior Director of Finance",
                "location": {"name": "Toronto, ON"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/901000",
                "departments": [{"name": "Finance"}],
                "updated_at": "2026-10-22T12:05:00Z",
            },
        ]
    }
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(gh_url, json_data=updated_gh)
    mock_aiohttp.get(lv_url, json_data=lv_feed)

    await poll_and_post_jobs(bot)
    assert len(jobs_ch.sent) == 1
    assert "Software Engineering Intern" in (jobs_ch.sent[0].embed.title or "")
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen") == (6,)


async def test_workday_poller_cxs_cache_ttl_and_idempotency(
    make_poller_bot,
    synthetic_settings,
    mock_aiohttp,
    tmp_db: Database,
    frozen_time,
):
    careers_url = "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"
    cxs_url = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs?locale=en-US"
    cfg = replace(
        synthetic_settings,
        workday_sites=[f"Acme|{careers_url}"],
        jobs_bootstrap_post=False,
    )
    bot, channels = make_poller_bot(settings=cfg)
    jobs_ch = channels[cfg.job_alerts_channel_id]

    wd_html = load_fixture_text("workday_careers.html")
    wd_jobs = load_fixture_json("workday_cxs_jobs.json")
    mock_aiohttp.get(careers_url, text=wd_html)
    mock_aiohttp.post(cxs_url, json_data=wd_jobs)

    # Cycle 1: discovers CXS URL, caches in workday_cxs_cache, checkpoints 2 jobs
    await poll_and_post_workday_jobs(bot)
    assert len(jobs_ch.sent) == 0
    assert await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen WHERE source='workday:Acme'") == (2,)
    cached_row = await tmp_db.fetchone(
        "SELECT api_url, last_updated_unix FROM workday_cxs_cache WHERE careers_url=?",
        (careers_url,),
    )
    assert cached_row == (cxs_url, frozen_time.unix)

    # Cycle 2 within 7-day TTL: posts 0 duplicates; adding 1 new eligible role posts exactly 1
    frozen_time.advance(6 * 24 * 3600)
    updated_wd = {
        "total": 3,
        "jobPostings": wd_jobs["jobPostings"]
        + [
            {
                "title": "New Grad Software Engineer",
                "externalPath": "/job/Toronto-ON/New-Grad-Software-Engineer_R1003",
                "locationsText": "Toronto, ON",
            }
        ],
    }
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(careers_url, text=wd_html)
    mock_aiohttp.post(cxs_url, json_data=updated_wd)

    await poll_and_post_workday_jobs(bot)
    assert len(jobs_ch.sent) == 1
    assert "New Grad Software Engineer" in (jobs_ch.sent[0].embed.title or "")


async def test_go_alerts_poller_checkpoint_advance_retention_reset_and_allowlist(
    make_poller_bot,
    synthetic_settings,
    mock_aiohttp,
    tmp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    api_base = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    cfg = replace(
        synthetic_settings,
        go_api_base=api_base,
        go_api_key="synthetic-go-key",
        go_bootstrap_post_latest=False,
        go_alert_level="major",
        go_include_buses=False,
        go_lines_allowlist=["Lakeshore West"],
    )
    bot, channels = make_poller_bot(settings=cfg)
    go_ch = channels[cfg.go_alerts_channel_id]

    endpoint = f"{api_base}/ServiceUpdate/ServiceAlert/All?key=synthetic-go-key"
    base_payload = load_fixture_json("metrolinx_service_alerts.json")
    mock_aiohttp.get(endpoint, json_data=base_payload)

    # Cycle 1: bootstrap checkpoint only
    await poll_and_post_go_alerts(bot)
    assert len(go_ch.sent) == 0
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='go_last_servicealert_code'") == (
        "SA-0001",
    )

    # Cycle 2: repeat feed posts 0
    await poll_and_post_go_alerts(bot)
    assert len(go_ch.sent) == 0

    # Cycle 3: SA-0002 (major on LW), SA-0003 (minor info on LW), SA-BUS (no train line -> filtered)
    feed3 = {
        "Messages": {
            "Message": base_payload["Messages"]["Message"]
            + [
                {
                    "Code": "SA-0002",
                    "Status": "INIT",
                    "PostedDateTime": "2026-11-01 09:00:00",
                    "SubjectEnglish": "Signal failure near Exhibition GO",
                    "BodyEnglish": "Delays on Lakeshore West due to signal problem.",
                    "Category": "Service Disruption",
                    "SubCategory": "Signal",
                    "Lines": [{"Code": "LW"}],
                },
                {
                    "Code": "SA-0003",
                    "Status": "INIT",
                    "PostedDateTime": "2026-11-01 09:10:00",
                    "SubjectEnglish": "Elevator notice at Oakville GO",
                    "BodyEnglish": "Routine elevator maintenance.",
                    "Category": "Information",
                    "SubCategory": "Station",
                    "Lines": [{"Code": "LW"}],
                },
                {
                    "Code": "SA-BUS1",
                    "Status": "INIT",
                    "PostedDateTime": "2026-11-01 09:20:00",
                    "SubjectEnglish": "Bus route 12 emergency detour",
                    "BodyEnglish": "Bus detour only.",
                    "Category": "Service Disruption",
                    "SubCategory": "Detour",
                    "Lines": [],
                },
            ]
        }
    }
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(endpoint, json_data=feed3)
    await poll_and_post_go_alerts(bot)

    assert len(go_ch.sent) == 1
    assert "Signal failure" in (go_ch.sent[0].embed.description or "")
    # Checkpoint MUST advance past trailing minor SA-0003
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='go_last_servicealert_code'") == (
        "SA-0003",
    )

    # Cycle 4: retention loss (SA-0003 missing from feed) re-baselines cleanly to newest without crash
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        endpoint,
        json_data={
            "Messages": {
                "Message": [
                    {
                        "Code": "SA-9000",
                        "PostedDateTime": "2026-11-02 08:00:00",
                        "SubjectEnglish": "Next-day signal alert",
                        "BodyEnglish": "Switch issue.",
                        "Category": "Service Disruption",
                        "Lines": [{"Code": "LW"}],
                    }
                ]
            }
        },
    )
    await poll_and_post_go_alerts(bot)
    assert len(go_ch.sent) == 1
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='go_last_servicealert_code'") == (
        "SA-9000",
    )


async def test_ttc_poller_idempotency_chronological_order_and_retry_vs_429(
    make_poller_bot,
    synthetic_settings,
    mock_aiohttp,
    tmp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    base = "https://mastodon.example.invalid"
    cfg = replace(
        synthetic_settings,
        ttc_mastodon_base=base,
        ttc_mastodon_acct="TTCnotices",
        ttc_bootstrap_post_latest=False,
        ttc_alert_level="major",
    )
    bot, channels = make_poller_bot(settings=cfg)
    ttc_ch = channels[cfg.ttc_alerts_channel_id]

    mock_aiohttp.get(
        f"{base}/api/v2/search?q=ttcnotices&type=accounts&limit=5",
        json_data={"accounts": [{"id": "42001", "acct": "ttcnotices"}]},
    )
    statuses_url = f"{base}/api/v1/accounts/42001/statuses"
    mock_aiohttp.get(
        statuses_url,
        prefix=True,
        json_data=[
            {"id": "1002", "content": "<p>Normal service resumed.</p>", "url": "https://example.invalid/1002"}
        ],
    )

    # Cycle 1: resolves account id and checkpoints 1002 without posting
    await poll_and_post_ttc(bot)
    assert len(ttc_ch.sent) == 0
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='ttc_account_id'") == ("42001",)
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='ttc_last_status_id'") == ("1002",)

    # Cycle 2: cached account_id avoids search API; since_id=1002 sent; empty -> 0 posts
    calls_before = len(mock_aiohttp.calls)
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(statuses_url, prefix=True, json_data=[])
    await poll_and_post_ttc(bot)
    assert not any("/api/v2/search" in c.url for c in mock_aiohttp.calls[calls_before:])
    assert "since_id=1002" in mock_aiohttp.calls[-1].url
    assert len(ttc_ch.sent) == 0

    # Cycle 3: transient TimeoutError once followed by newest-first items [1004 INFO, 1003 MAJOR]
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(statuses_url, prefix=True, repeat=False, exc=TimeoutError())
    mock_aiohttp.get(
        statuses_url,
        prefix=True,
        json_data=[
            {"id": "1004", "content": "<p>Good afternoon riders.</p>", "url": "https://example.invalid/1004"},
            {
                "id": "1003",
                "content": "<p>Line 1: No service due to signal problem.</p>",
                "url": "https://example.invalid/1003",
            },
        ],
    )
    await poll_and_post_ttc(bot)
    assert len(ttc_ch.sent) == 1
    assert "signal problem" in (ttc_ch.sent[0].embed.description or "").lower()
    assert await tmp_db.fetchone("SELECT value FROM meta WHERE key='ttc_last_status_id'") == ("1004",)

    # Cycle 4: HTTP 429 (TTCFetchError) does not tight-retry 3 times; single attempt exits cleanly
    mock_aiohttp._routes.clear()
    calls_429_start = len(mock_aiohttp.calls)
    mock_aiohttp.get(statuses_url, prefix=True, status=429, headers={"Retry-After": "15"}, text="slow down")
    await poll_and_post_ttc(bot)
    assert len(mock_aiohttp.calls) - calls_429_start == 1

    # Cancellation propagates immediately without being swallowed
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(statuses_url, prefix=True, exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await poll_and_post_ttc(bot)


# ---------------------------------------------------------------------------
# HTTP provider error-family matrix across all external clients
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "route_kwargs"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}),
        ("cancelled", {"exc": asyncio.CancelledError}),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "12"}, "text": '{"error":"quota"}'}),
        ("5xx", {"status": 503, "text": "Service Unavailable"}),
        (
            "malformed_json",
            {"status": 200, "body": b"{broken-json", "headers": {"Content-Type": "application/json"}},
        ),
        ("empty_or_missing", {"status": 200, "json_data": {"candidates": [{"finishReason": "SAFETY"}]}}),
    ],
)
async def test_gemini_error_family_matrix(mock_aiohttp, mode: str, route_kwargs: dict):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=k"
    mock_aiohttp.post(url, **route_kwargs)
    expected_exc = (
        asyncio.TimeoutError
        if mode == "timeout"
        else asyncio.CancelledError
        if mode == "cancelled"
        else GeminiError
    )
    with pytest.raises(expected_exc) as exc_info:
        await gemini_generate_text(api_key="k", model="gemini-2.5-flash", prompt="test")
    if expected_exc is GeminiError and mode in ("429_retry_after", "5xx"):
        assert exc_info.value.status == route_kwargs["status"]


@pytest.mark.parametrize(
    ("mode", "route_kwargs"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}),
        ("cancelled", {"exc": asyncio.CancelledError}),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "10"}, "text": "Too Many Requests"}),
        ("5xx", {"status": 502, "text": "Bad Gateway"}),
        ("malformed_json", {"status": 200, "text": "{not-json"}),
        ("empty_or_missing", {"status": 200, "json_data": {"Messages": {"Message": [{"Code": ""}]}}}),
    ],
)
async def test_go_client_error_family_matrix(mock_aiohttp, mode: str, route_kwargs: dict):
    base = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    mock_aiohttp.get(f"{base}/ServiceUpdate/ServiceAlert/All?key=k", **route_kwargs)
    async with aiohttp.ClientSession() as session:
        if mode == "empty_or_missing":
            assert await fetch_service_alerts(session, base, "k") == []
        elif mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await fetch_service_alerts(session, base, "k")
        else:
            # go_client wraps TimeoutError and ClientError into GOFetchError
            with pytest.raises(GOFetchError):
                await fetch_service_alerts(session, base, "k")


@pytest.mark.parametrize(
    ("mode", "route_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, asyncio.CancelledError),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "30"}, "text": "rate"}, TTCFetchError),
        ("5xx", {"status": 500, "text": "server err"}, aiohttp.ClientResponseError),
        ("malformed_json", {"status": 200, "text": "<html>bad</html>"}, json.JSONDecodeError),
        ("empty_or_missing", {"status": 200, "json_data": [{"id": ""}, "invalid-entry"]}, None),
    ],
)
async def test_ttc_client_error_family_matrix(mock_aiohttp, mode: str, route_kwargs: dict, expected_exc):
    base = "https://mastodon.example.invalid"
    mock_aiohttp.get(f"{base}/api/v1/accounts/77/statuses", prefix=True, **route_kwargs)
    async with aiohttp.ClientSession() as session:
        if expected_exc is None:
            assert await fetch_account_status_items(session, base, "77") == []
        else:
            with pytest.raises(expected_exc):
                await fetch_account_status_items(session, base, "77")

    # Also verify resolve_account_id 429 + missing accounts
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        f"{base}/api/v2/search", prefix=True, status=429, headers={"Retry-After": "5"}, text="429"
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TTCFetchError):
            await resolve_account_id(session, base, "TTCnotices")


@pytest.mark.parametrize(
    ("mode", "route_kwargs"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}),
        ("cancelled", {"exc": asyncio.CancelledError}),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "12"}, "text": "rate limit"}),
        ("5xx", {"status": 502, "text": "bad gateway"}),
        ("malformed_json", {"status": 200, "text": "not json"}),
        ("empty_or_missing", {"status": 200, "json_data": {"jobs": [{"title": "Missing ID"}]}}),
    ],
)
async def test_ats_greenhouse_and_lever_error_family_matrix(mock_aiohttp, mode: str, route_kwargs: dict):
    gh_url = "https://boards-api.greenhouse.io/v1/boards/demo/jobs?content=true"
    lv_url = "https://api.lever.co/v0/postings/demo?mode=json"
    mock_aiohttp.get(gh_url, **route_kwargs)
    lv_kwargs = (
        {"status": 200, "json_data": [{"text": "No ID"}]} if mode == "empty_or_missing" else route_kwargs
    )
    mock_aiohttp.get(lv_url, **lv_kwargs)

    async with aiohttp.ClientSession() as session:
        if mode == "empty_or_missing":
            assert await fetch_greenhouse_jobs(session, "demo") == []
            assert await fetch_lever_jobs(session, "demo") == []
        elif mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await fetch_greenhouse_jobs(session, "demo")
            with pytest.raises(asyncio.CancelledError):
                await fetch_lever_jobs(session, "demo")
        else:
            with pytest.raises(RuntimeError):
                await fetch_greenhouse_jobs(session, "demo")
            with pytest.raises(RuntimeError):
                await fetch_lever_jobs(session, "demo")


@pytest.mark.parametrize(
    ("mode", "post_kwargs", "get_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, {}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, {}, asyncio.CancelledError),
        (
            "429_retry_after",
            {"status": 429, "headers": {"Retry-After": "9"}, "text": "limit"},
            {"status": 429, "headers": {"Retry-After": "9"}, "text": "limit"},
            RuntimeError,
        ),
        ("5xx", {"status": 500, "text": "err"}, {"status": 503, "text": "err"}, RuntimeError),
        ("malformed_json", {"status": 200, "text": "{bad-json"}, {}, None),
        ("empty_or_missing", {"status": 200, "json_data": {"total": 0, "jobPostings": []}}, {}, None),
    ],
)
async def test_workday_client_error_family_matrix(
    mock_aiohttp,
    mode: str,
    post_kwargs: dict,
    get_kwargs: dict,
    expected_exc,
):
    site: WorkdaySite = parse_workday_sites(["Acme|https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"])[0]
    api_url = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"
    mock_aiohttp.get(site.careers_url, text="<html></html>")
    mock_aiohttp.post(api_url, prefix=True, **post_kwargs)
    if get_kwargs:
        mock_aiohttp.get(api_url, prefix=True, **get_kwargs)

    async with aiohttp.ClientSession() as session:
        if expected_exc is None:
            jobs, _info = await fetch_workday_jobs(session, site)
            assert jobs == []
        else:
            with pytest.raises(expected_exc):
                await fetch_workday_jobs(session, site)


@pytest.mark.parametrize(
    ("mode", "quote_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, asyncio.CancelledError),
        (
            "429_retry_after",
            {"status": 429, "headers": {"Retry-After": "60"}, "text": "Too Many"},
            RuntimeError,
        ),
        ("5xx", {"status": 503, "text": "Upstream down"}, RuntimeError),
        ("malformed_json", {"status": 200, "text": "not-json"}, json.JSONDecodeError),
        (
            "empty_or_missing",
            {"status": 200, "json_data": {"quoteResponse": {"result": [{"symbol": ""}]}}},
            None,
        ),
    ],
)
async def test_yahoo_quotes_error_family_matrix(mock_aiohttp, mode: str, quote_kwargs: dict, expected_exc):
    mock_aiohttp.get(YF_FC_URL, text="ok")
    mock_aiohttp.get(YF_CRUMB_URL, text="crumb-ok")
    mock_aiohttp.get("https://query2.finance.yahoo.com/v7/finance/quote", prefix=True, **quote_kwargs)
    mock_aiohttp.get("https://query1.finance.yahoo.com/v7/finance/quote", prefix=True, **quote_kwargs)

    if expected_exc is None:
        assert await fetch_quotes(["SPY"]) == {}
    else:
        with pytest.raises(expected_exc):
            await fetch_quotes(["SPY"])


@pytest.mark.parametrize(
    ("mode", "route_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, asyncio.CancelledError),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "8"}, "text": "slow"}, RuntimeError),
        ("5xx", {"status": 500, "text": "err"}, RuntimeError),
        ("malformed_json", {"status": 200, "text": "{broken"}, json.JSONDecodeError),
        ("empty_or_missing", {"status": 200, "json_data": {}}, None),
    ],
)
async def test_esplora_and_fng_error_family_matrix(mock_aiohttp, mode: str, route_kwargs: dict, expected_exc):
    btc_url = f"{ESPLORA_BASE}/address/bc1qexample/txs"
    btc_kwargs = {"status": 200, "json_data": []} if mode == "empty_or_missing" else route_kwargs
    mock_aiohttp.get(btc_url, **btc_kwargs)
    mock_aiohttp.get(FNG_URL, **route_kwargs)

    if expected_exc is None:
        assert await fetch_address_txs("bc1qexample") == []
        assert await fetch_fear_greed() is None
    else:
        with pytest.raises(expected_exc):
            await fetch_address_txs("bc1qexample")
        with pytest.raises(expected_exc):
            await fetch_fear_greed()


@pytest.mark.parametrize(
    ("mode", "route_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, asyncio.CancelledError),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "20"}, "text": "quota"}, OddsAPIError),
        ("5xx", {"status": 502, "text": "bad gateway"}, OddsAPIError),
        ("malformed_json", {"status": 200, "text": "{bad"}, json.JSONDecodeError),
        (
            "empty_or_missing",
            {"status": 200, "json_data": [{"id": "1", "commence_time": "invalid-dt"}]},
            None,
        ),
    ],
)
async def test_odds_api_client_error_family_and_cache(
    mock_aiohttp,
    frozen_time,
    mode: str,
    route_kwargs: dict,
    expected_exc,
):
    # Missing API key fails fast before network I/O
    no_key_client = OddsAPIClient("")
    with pytest.raises(OddsAPIError, match="Missing ODDS_API_KEY"):
        await no_key_client.get_events("basketball_nba")

    client = OddsAPIClient("synthetic-odds-key", cache_ttl_seconds=600)
    mock_aiohttp.get(f"{ODDS_BASE_URL}/sports/basketball_nba/events", prefix=True, **route_kwargs)

    if expected_exc is None:
        assert await client.get_events("basketball_nba") == []
        # Second call within 600s hits in-memory TTL cache without issuing new HTTP request
        assert len(mock_aiohttp.calls) == 1
        frozen_time.advance(100)
        assert await client.get_events("basketball_nba") == []
        assert len(mock_aiohttp.calls) == 1
    else:
        with pytest.raises(expected_exc):
            await client.get_events("basketball_nba")


@pytest.mark.parametrize(
    ("mode", "route_kwargs"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}),
        ("cancelled", {"exc": asyncio.CancelledError}),
        ("429_retry_after", {"status": 429, "headers": {"Retry-After": "10"}, "text": "rate"}),
        ("5xx", {"status": 500, "text": "err"}),
        ("malformed_or_empty", {"status": 200, "text": "<html>not an spa bundle</html>"}),
    ],
)
async def test_notes_and_prof_error_resilience_and_cancellation(mock_aiohttp, mode: str, route_kwargs: dict):
    notes_url = "https://example.invalid/Notes/"
    mock_aiohttp.get(notes_url, **route_kwargs)
    prof_kwargs = (
        {"status": 200, "json_data": {"data": {"newSearch": {}}}}
        if mode == "malformed_or_empty"
        else ({"status": 200, "text": "{bad-json"} if mode == "malformed_json" else route_kwargs)
    )
    mock_aiohttp.post(RMP_GRAPHQL_URL, **prof_kwargs)

    async with aiohttp.ClientSession() as session:
        if mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await fetch_notes_from_site(session, "Synthetic", notes_url)
            with pytest.raises(asyncio.CancelledError):
                await fetch_graphql(session, "query", {})
        else:
            # Scrapers degrade gracefully to [] / None instead of throwing
            assert await fetch_notes_from_site(session, "Synthetic", notes_url) == []
            assert await get_school_id(session, "Toronto Metropolitan University") is None
            assert await search_prof(session, "Lovelace", "U2Nob29sLTEyMzQ=") == []


@pytest.mark.parametrize(
    ("mode", "route_kwargs", "expected_exc"),
    [
        ("timeout", {"exc": asyncio.TimeoutError}, asyncio.TimeoutError),
        ("cancelled", {"exc": asyncio.CancelledError}, asyncio.CancelledError),
        (
            "429_retry_after",
            {"status": 429, "headers": {"Retry-After": "5"}, "text": "slow"},
            aiohttp.ClientResponseError,
        ),
        ("5xx", {"status": 500, "text": "err"}, aiohttp.ClientResponseError),
        ("empty_or_missing", {"status": 200, "text": "<html><body>No title heading</body></html>"}, None),
        (
            "malformed_dist_json",
            {"status": 200, "text": '<html><h2>CPS109</h2><script>"distribution":[{bad]</script></html>'},
            None,
        ),
    ],
)
async def test_grades_client_error_family_and_init_idempotency(
    mock_aiohttp,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    route_kwargs: dict,
    expected_exc,
):
    monkeypatch.setattr(grades_mod, "DB_PATH", str(tmp_path / "grades_err.db"))
    await grades_mod._init_db()
    await grades_mod._init_db()  # idempotent startup init

    mock_aiohttp.get("https://tmu.grades.zone/course/cps109", **route_kwargs)
    if expected_exc is None:
        res = await grades_mod.fetch_course_data("CPS109")
        if mode == "empty_or_missing":
            assert res is None
        else:
            assert res is not None and res["title"] == "CPS109" and res["professors"] == []
    else:
        with pytest.raises(expected_exc):
            await grades_mod.fetch_course_data("CPS109")
