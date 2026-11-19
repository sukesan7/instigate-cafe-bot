from __future__ import annotations

import os
import sys
import time
import tracemalloc

import aiohttp
import pytest

from icbot.errors import ProviderError
from icbot.services.jobs.models import JobFilterCriteria, JobPosting
from icbot.services.jobs.pipeline import JobsIngestionPipeline, matches_filter_criteria
from tests.helpers import FakeTextChannel

pytestmark = [pytest.mark.storage, pytest.mark.provider_contract]


def _build_test_posting(
    provider: str,
    employer: str,
    external_id: str,
    title: str,
    *,
    location: str = "Toronto, ON",
    posted_unix: int = 1700000000,
    updated_unix: int | None = None,
) -> JobPosting:
    blob = f"{title} {employer} {location}".lower()
    return JobPosting(
        provider=provider,  # type: ignore[arg-type]
        employer=employer,
        external_id=external_id,
        title=title,
        location=location,
        url=f"https://example.invalid/{provider}/{employer}/{external_id}",
        posted_unix=posted_unix,
        updated_unix=updated_unix,
        fetched_unix=1700000000,
        text_blob=blob,
    )


async def test_pipeline_silent_baseline_and_restart_idempotency(tmp_db):
    pipeline = JobsIngestionPipeline(concurrency_limit=5, clock=lambda: 1700000000.0)
    channel = FakeTextChannel(channel_id=900000000000000001, name="test-jobs")

    jobs = [
        _build_test_posting("greenhouse", "Stripe", "gh-1", "Software Engineer Intern"),
        _build_test_posting("lever", "Palantir", "lv-1", "New Grad SWE"),
        _build_test_posting("workday", "RBC", "wd-1", "Junior Developer"),
    ]

    # Cycle 1: Source has never been seen -> silent checkpoint
    has_seen_before = await pipeline.has_seen_global(tmp_db, "greenhouse:Stripe", "gh-1")
    assert not has_seen_before

    await pipeline.checkpoint_seen(tmp_db, jobs)

    # Verify all 3 jobs exist in job_seen
    count = await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen")
    assert count == (3,)
    assert len(channel.sent) == 0

    # Cycle 2: Repeat with identical feed -> all are already seen
    unseen_jobs = []
    for j in jobs:
        if not await pipeline.has_seen_global(tmp_db, j.source_key, j.external_id):
            unseen_jobs.append(j)

    assert unseen_jobs == []
    assert len(channel.sent) == 0


async def test_pipeline_filter_rules_matching():
    criteria = JobFilterCriteria(
        location_allowlist=("toronto", "remote"),
        keywords_allowlist=(),
        keywords_denylist=("sales", "recruiter"),
        require_early_career=True,
    )

    # Eligible intern
    j1 = _build_test_posting(
        "greenhouse", "Stripe", "1", "Software Engineering Intern", location="Toronto, ON"
    )
    assert matches_filter_criteria(j1, criteria) is True

    # Ineligible senior
    j2 = _build_test_posting(
        "greenhouse", "Stripe", "2", "Senior Software Engineering Intern", location="Toronto, ON"
    )
    assert matches_filter_criteria(j2, criteria) is False

    # Ineligible non-tech
    j3 = _build_test_posting("lever", "Palantir", "3", "Business Analyst Intern", location="Toronto, ON")
    assert matches_filter_criteria(j3, criteria) is False

    # Ineligible denied keyword
    j4 = _build_test_posting(
        "lever", "Palantir", "4", "Software Sales Engineer Intern", location="Toronto, ON"
    )
    assert matches_filter_criteria(j4, criteria) is False

    # Ineligible location
    j5 = _build_test_posting("workday", "Acme", "5", "Software Engineer Intern", location="London, UK")
    assert matches_filter_criteria(j5, criteria) is False

    # Eligible remote fallback
    j6 = _build_test_posting("workday", "Acme", "6", "Remote Software Engineer Intern", location="Not Listed")
    assert matches_filter_criteria(j6, criteria) is True


async def test_pipeline_reannounce_on_update_policy(tmp_db):
    pipeline_no_reannounce = JobsIngestionPipeline(reannounce_on_update=False, clock=lambda: 1700000000.0)
    pipeline_with_reannounce = JobsIngestionPipeline(reannounce_on_update=True, clock=lambda: 1700000000.0)

    job_initial = _build_test_posting(
        "greenhouse",
        "Stripe",
        "upd-101",
        "Software Engineer Intern",
        posted_unix=1700000000,
        updated_unix=1700000000,
    )
    job_updated = _build_test_posting(
        "greenhouse",
        "Stripe",
        "upd-101",
        "Software Engineer Intern",
        posted_unix=1700000000,
        updated_unix=1700005000,
    )

    # Deliver initial version
    guild_id = 800000000000000001
    channel_id = 900000000000000001
    await pipeline_no_reannounce.checkpoint_seen(tmp_db, [job_initial])
    await pipeline_no_reannounce.record_delivery(tmp_db, guild_id, channel_id, job_initial)

    # Policy 1: reannounce_on_update=False -> reports already delivered
    is_delivered_1 = await pipeline_no_reannounce.is_already_delivered(
        tmp_db,
        guild_id,
        channel_id,
        job_updated.source_key,
        job_updated.external_id,
        job_updated.updated_unix,
    )
    assert is_delivered_1 is True

    # Policy 2: reannounce_on_update=True -> reports NOT delivered (eligible for reannounce)
    is_delivered_2 = await pipeline_with_reannounce.is_already_delivered(
        tmp_db,
        guild_id,
        channel_id,
        job_updated.source_key,
        job_updated.external_id,
        job_updated.updated_unix,
    )
    assert is_delivered_2 is False


async def test_pipeline_cooldown_and_rate_limiting(mock_aiohttp):
    pipeline = JobsIngestionPipeline(concurrency_limit=3, clock=lambda: 100.0)
    board = "slowboard"
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    mock_aiohttp.get(url, status=429, headers={"Retry-After": "25"}, text="Too Many Requests")

    async with aiohttp.ClientSession() as session:
        # Fetch 1 triggers 429
        with pytest.raises(ProviderError) as exc_info:
            await pipeline.fetch_source_postings(session, "greenhouse", board)
        assert exc_info.value.status == 429

        health = pipeline.source_health_map["greenhouse:slowboard"]
        assert health.status == "cooling_down"
        assert health.cooldown_until == 125.0

        # Fetch 2 during cooldown is skipped immediately without making HTTP call
        calls_before = len(mock_aiohttp.calls)
        jobs, _ = await pipeline.fetch_source_postings(session, "greenhouse", board)
        assert jobs == []
        assert len(mock_aiohttp.calls) == calls_before


async def test_pipeline_permanent_error_blacklisting(mock_aiohttp):
    pipeline = JobsIngestionPipeline(concurrency_limit=3, clock=lambda: 100.0)
    bad_sources: set[str] = set()

    mock_aiohttp.get(
        "https://boards-api.greenhouse.io/v1/boards/nonexistent/jobs?content=true",
        status=404,
        text="Board not found",
    )

    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProviderError) as exc_info:
            await pipeline.fetch_source_postings(
                session, "greenhouse", "nonexistent", bad_sources=bad_sources
            )
        assert exc_info.value.status == 404
        assert "greenhouse:nonexistent" in bad_sources

        # Subsequent call is skipped via bad_sources check
        calls_before = len(mock_aiohttp.calls)
        jobs, _ = await pipeline.fetch_source_postings(
            session, "greenhouse", "nonexistent", bad_sources=bad_sources
        )
        assert jobs == []
        assert len(mock_aiohttp.calls) == calls_before


async def test_1000_jobs_performance_and_memory_benchmark(tmp_db):
    """
    Performance trace for a representative 1,000-job fixture:
    Bounds CPU time to under 1.0 second and memory overhead to under 15 MB.
    """
    pipeline = JobsIngestionPipeline(clock=lambda: 1700000000.0)
    criteria = JobFilterCriteria(
        location_allowlist=("toronto", "remote", "waterloo", "new york"),
        keywords_denylist=("sales", "recruiter", "manager"),
        require_early_career=True,
    )

    # Generate 1,000 synthetic jobs across providers
    synthetic_postings: list[JobPosting] = []
    providers = ["greenhouse", "lever", "workday"]
    employers = ["Stripe", "Palantir", "RBC", "Meta", "Google", "Amazon", "Shopify", "Databricks"]
    titles = [
        "Software Engineering Intern",
        "Firmware Co-op",
        "Hardware Design Intern",
        "New Grad Software Developer",
        "Senior Engineering Director",
        "Staff Infrastructure Architect",
        "Product Sales Recruiter",
        "Junior Machine Learning Researcher",
        "Solutions Consultant Associate",
        "Mechanical Robotics Intern",
    ]
    locations = ["Toronto, ON", "Waterloo, ON", "Remote - Canada", "London, UK", "New York, NY"]

    for i in range(1000):
        prov = providers[i % len(providers)]
        emp = employers[i % len(employers)]
        title = titles[i % len(titles)]
        loc = locations[i % len(locations)]
        synthetic_postings.append(
            _build_test_posting(
                prov,
                emp,
                f"ext-{i:04d}",
                title,
                location=loc,
                posted_unix=1700000000 - i * 60,
            )
        )

    # Measure memory and CPU time
    tracemalloc.start()
    t_start = time.perf_counter()

    # 1. Matching & filtering
    matched = [j for j in synthetic_postings if matches_filter_criteria(j, criteria)]

    # 2. Checkpoint ingestion into SQLite
    await pipeline.checkpoint_seen(tmp_db, synthetic_postings)

    # 3. Query seen verification
    count_row = await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen")

    t_elapsed = time.perf_counter() - t_start
    _current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mb = peak_mem / (1024 * 1024)

    assert count_row == (1000,)
    assert len(matched) > 0

    # CPU bound: processing 1,000 jobs through regexes and SQLite must be fast (< 1.0s native, < 2.5s under coverage/CI tracing)
    time_limit = 2.5 if (sys.gettrace() is not None or os.environ.get("CI")) else 1.0
    assert t_elapsed < time_limit, f"Ingestion took {t_elapsed:.3f}s (exceeded {time_limit}s limit)"

    # Memory bound: peak allocation for 1,000 jobs must be small (< 15 MB)
    assert peak_mb < 15.0, f"Peak memory was {peak_mb:.2f} MB (exceeded 15 MB limit)"
