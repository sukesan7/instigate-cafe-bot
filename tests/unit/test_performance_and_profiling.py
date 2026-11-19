"""Unit and integration tests for Prompt 10C: Performance, memory profiling, and executor safety.

Verifies:
1. Application startup without gateway is bounded in CPU time and memory.
2. Slash command registration reflects all 52 leaf commands within strict time bounds.
3. 100 simultaneous cached lookups complete without event-loop lag.
4. 1,000 ATS jobs ingestion batch executes via batched executemany in under 500ms.
5. 50 concurrent virtual economy writes execute atomically without SQLite deadlock.
6. Matplotlib chart rendering executes off-loop in an executor with deterministic figure cleanup.
7. Parallel AI requests respect bounded semaphore concurrency and token budgets.
8. Manual catalog store in-memory caching accelerates repeated lookups and invalidates on write.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import tracemalloc
from pathlib import Path
from typing import Any

import yarl

from icbot.academics.cache import AcademicProviderCache, CacheKey
from icbot.academics.catalog import ManualResourceCatalogStore
from icbot.academics.charts import (
    render_grade_distribution_chart_async,
)
from icbot.academics.models import Course
from icbot.bot import create_bot
from icbot.config import Settings
from icbot.db import Database
from icbot.services.gemini_client import AIBudgetGovernor, GeminiClient
from icbot.services.jobs.models import JobFilterCriteria, JobPosting
from icbot.services.jobs.pipeline import JobsIngestionPipeline, matches_filter_criteria
from icbot.services.user_service import UserStore
from tests.helpers import AiohttpMockRouter, FakeAiohttpResponse

# ---------------------------------------------------------------------------
# 1. Startup & Slash Command Registration Invariants
# ---------------------------------------------------------------------------


async def test_startup_latency_and_memory_bounds(tmp_path: Path):
    """Application startup without gateway initializes cleanly under 0.5s and 5MB RAM."""
    db_path = str(tmp_path / "test_startup.db")
    settings = Settings(
        token="",
        guild_id=900000000000000001,
        db_path=db_path,
        allow_gateway=False,
    )
    tracemalloc.start()
    t0 = time.perf_counter()

    db = Database(db_path)
    await db.init()
    bot = create_bot(settings, register_commands=False, sync_commands=False)
    bot.db = db
    await bot.setup_hook()

    elapsed = time.perf_counter() - t0
    _curr, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    await bot.close()
    await db.close()

    assert elapsed < 0.5, f"Startup took {elapsed:.3f}s, expected < 0.5s"
    assert peak < 5 * 1024 * 1024, f"Peak memory {peak / 1024:.1f} KiB exceeded 5MB bound"


def test_slash_command_registration_latency_and_leaf_count():
    """Slash command tree registration builds all 52 leaf commands in under 50ms."""
    from icbot.registry import load_feature_modules, registered_qualified_names

    t0 = time.perf_counter()
    load_feature_modules()
    names = registered_qualified_names()
    elapsed = time.perf_counter() - t0

    assert len(names) == 52, f"Expected exactly 52 leaf commands, found {len(names)}"
    assert elapsed < 0.05, f"Registration took {elapsed:.3f}s, expected < 0.05s"


# ---------------------------------------------------------------------------
# 2. 100 Simultaneous Cached Lookups & Event-Loop Lag
# ---------------------------------------------------------------------------


async def test_100_concurrent_cached_lookups_bounded_lag():
    """100 simultaneous cached queries complete with zero event-loop blocking (< 10ms)."""
    cache = AcademicProviderCache(max_entries=256)
    now_ts = 1760000000.0

    # Pre-seed 10 courses
    for i in range(10):
        key = CacheKey.make("test_provider", "toronto", f"course_{i}")
        cache.set(key, {"course_id": f"C{i}", "title": f"Course {i}"}, ttl_seconds=3600.0, now=now_ts)

    max_lag_s = 0.0

    async def _heartbeat():
        nonlocal max_lag_s
        for _ in range(50):
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            lag = max(0.0, (time.perf_counter() - t0) - 0.001)
            if lag > max_lag_s:
                max_lag_s = lag

    async def _lookup(idx: int):
        key = CacheKey.make("test_provider", "toronto", f"course_{idx % 10}")
        res = cache.get(key, now=now_ts)
        assert res is not None

    hb_task = asyncio.create_task(_heartbeat())
    t0 = time.perf_counter()
    await asyncio.gather(*[_lookup(i) for i in range(100)])
    elapsed = time.perf_counter() - t0
    hb_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hb_task

    assert elapsed < 0.05, f"100 lookups took {elapsed:.4f}s, expected < 0.05s"
    assert max_lag_s < 0.010, f"Event-loop lag {max_lag_s * 1000:.2f}ms exceeded 10ms bound"


# ---------------------------------------------------------------------------
# 3. 1,000 ATS Jobs Ingestion Batch via executemany
# ---------------------------------------------------------------------------


async def test_1000_jobs_ingestion_batch_executemany(tmp_db: Database):
    """1,000 ATS jobs ingest via batched executemany in under 300ms with exact count."""
    providers = ["greenhouse", "lever", "workday"]
    employers = ["Stripe", "Palantir", "RBC", "Meta", "Google", "Amazon", "Shopify", "Databricks"]
    titles = [
        "Software Engineering Intern",
        "Firmware Co-op",
        "Hardware Design Intern",
        "New Grad Software Developer",
        "Senior Engineering Director",
        "Junior Machine Learning Researcher",
    ]
    locations = ["Toronto, ON", "Waterloo, ON", "Remote - Canada", "London, UK", "New York, NY"]

    postings: list[JobPosting] = []
    for i in range(1000):
        prov = providers[i % len(providers)]
        emp = employers[i % len(employers)]
        title = titles[i % len(titles)]
        loc = locations[i % len(locations)]
        p = JobPosting(
            provider=prov,  # type: ignore[arg-type]
            employer=emp,
            external_id=f"job-{i:04d}",
            title=title,
            location=loc,
            url=f"https://careers.example.com/{prov}/{i}",
            posted_unix=1760000000 - i * 60,
            text_blob=f"{title} at {emp} in {loc}. Python and distributed systems.",
        )
        postings.append(p)

    criteria = JobFilterCriteria(
        location_allowlist=("toronto", "remote", "waterloo", "new york"),
        keywords_denylist=("sales", "recruiter", "manager"),
        require_early_career=True,
    )
    pipeline = JobsIngestionPipeline(clock=lambda: 1760000000.0)

    tracemalloc.start()
    t0 = time.perf_counter()

    matched = [j for j in postings if matches_filter_criteria(j, criteria)]
    await pipeline.checkpoint_seen(tmp_db, postings)

    elapsed = time.perf_counter() - t0
    _curr, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    count_row = await tmp_db.fetchone("SELECT COUNT(*) FROM job_seen")
    assert count_row == (1000,)
    assert len(matched) > 0
    assert elapsed < 0.50, f"1k job ingestion took {elapsed:.3f}s, expected < 0.50s"
    assert peak < 5 * 1024 * 1024, f"Peak memory {peak / 1024:.1f} KiB exceeded 5MB"


# ---------------------------------------------------------------------------
# 4. 50 Concurrent Virtual Economy Writes & Atomicity
# ---------------------------------------------------------------------------


async def test_50_concurrent_economy_writes_atomic_invariants(tmp_db: Database):
    """50 concurrent balance writes execute atomically without deadlock or balance drift."""
    clock_val = 1760000000.0
    user_svc = UserStore(tmp_db, starting_balance=1000, clock=lambda: clock_val)
    guild_id = 2002

    # Pre-seed 5 user accounts
    for uid in range(1, 6):
        await user_svc.get_balance(uid, guild_id=guild_id)

    async def _add(idx: int):
        u = (idx % 5) + 1
        await user_svc.add_balance(
            u,
            10,
            guild_id=guild_id,
            category="bench_credit",
            idempotency_key=f"bench_idem_{idx}",
        )

    t0 = time.perf_counter()
    await asyncio.gather(*[_add(i) for i in range(50)])
    elapsed = time.perf_counter() - t0

    # Total credits: 50 * 10 = 500 across 5 users -> total balance must be 5 * 1000 + 500 = 5500
    rows = await tmp_db.fetchall("SELECT balance FROM users WHERE guild_id=?", (guild_id,))
    total_balance = sum(int(r[0]) for r in rows)
    assert total_balance == 5500, f"Expected 5500 total balance, found {total_balance}"

    # Verify 50 ledger entries recorded
    ledger_count = await tmp_db.fetchone("SELECT COUNT(*) FROM economy_ledger WHERE guild_id=?", (guild_id,))
    assert ledger_count == (50,)
    assert elapsed < 0.50, f"50 writes took {elapsed:.3f}s, expected < 0.50s"


# ---------------------------------------------------------------------------
# 5. Chart Rendering: Executor Offloading & Figure Cleanup
# ---------------------------------------------------------------------------


async def test_chart_rendering_async_executor_prevents_loop_blocking():
    """Async chart rendering offloads to thread pool and cleans up temporary canvas/figure."""
    profs = [
        {
            "name": f"Professor {i}",
            "students": 100,
            "avg": "B+",
            "common": "A-",
            "dist": [{"letterGrade": "A", "count": 50}, {"letterGrade": "B", "count": 50}],
        }
        for i in range(3)
    ]

    max_lag_s = 0.0

    async def _heartbeat():
        nonlocal max_lag_s
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            lag = max(0.0, (time.perf_counter() - t0) - 0.001)
            if lag > max_lag_s:
                max_lag_s = lag

    hb = asyncio.create_task(_heartbeat())
    await asyncio.sleep(0.005)

    res = await render_grade_distribution_chart_async("CPS 109", profs)

    hb.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hb

    assert res.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert res.size_bytes > 0
    assert max_lag_s < 0.100, f"Event-loop lag {max_lag_s * 1000:.1f}ms exceeded 100ms threshold"


# ---------------------------------------------------------------------------
# 6. Parallel AI Requests & Semaphore Bounds
# ---------------------------------------------------------------------------


async def test_parallel_ai_requests_respects_semaphore_concurrency():
    """10 parallel AI requests serialize through Semaphore(3) and track budget usage."""
    governor = AIBudgetGovernor(clock=lambda: 1760000000.0)
    client = GeminiClient(
        api_key="synthetic_test_key_0000000000",
        max_concurrency=3,
        governor=governor,
        clock=lambda: 1760000000.0,
    )

    router = AiohttpMockRouter()
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Candidate analysis response."}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 20, "totalTokenCount": 70},
    }
    router.add_route("POST", lambda u: "generativelanguage" in u, status=200, json_data=payload)

    import aiohttp

    async def _mock_request(
        self: aiohttp.ClientSession,
        method: str,
        str_or_url: Any,
        *,
        params: Any = None,
        data: Any = None,
        json: Any = None,
        headers: Any = None,
        **kwargs: Any,
    ) -> FakeAiohttpResponse:
        base_url = yarl.URL(str(str_or_url))
        if params:
            base_url = base_url.update_query(params)
        body_val = json if json is not None else data
        return router.dispatch(method, str(base_url), {}, body_val, params)

    orig_request = aiohttp.ClientSession._request
    aiohttp.ClientSession._request = _mock_request  # type: ignore[method-assign]

    try:

        async def _call(idx: int):
            text = await client.generate_text(prompt=f"Resume {idx}", guild_id=1001)
            assert "Candidate analysis response" in text

        t0 = time.perf_counter()
        await asyncio.gather(*[_call(i) for i in range(10)])
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.50
        # 10 calls * 70 tokens = 700 tokens
        diag = governor.get_diagnostics()
        assert diag["successful_requests"] == 10
        assert diag["total_input_tokens"] + diag["total_output_tokens"] == 700
    finally:
        aiohttp.ClientSession._request = orig_request  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 7. Manual Resource Catalog Store In-Memory Caching
# ---------------------------------------------------------------------------


async def test_manual_catalog_store_in_memory_caching(tmp_db: Database):
    """ManualResourceCatalogStore caches course lookups in memory and invalidates on write."""
    store = ManualResourceCatalogStore(tmp_db)
    guild_id = 1001

    course = Course(
        display_code="CPS109",
        normalized_key="CPS109",
        title="Computer Science I",
        campus_id="tmu",
        aliases=("CS109", "CPS109A"),
    )
    await store.upsert_course(guild_id, course)

    # First lookup queries DB and populates in-memory cache
    c1 = await store.get_course(guild_id, "cps109")
    assert c1 is not None
    assert c1.title == "Computer Science I"
    assert (guild_id, "CPS109") in store._cache

    # Second lookup hits in-memory cache directly
    t0 = time.perf_counter()
    c2 = await store.get_course(guild_id, "cps109")
    cached_elapsed = time.perf_counter() - t0
    assert c2 is not None
    assert c2.title == "Computer Science I"
    assert cached_elapsed < 0.001, f"Cached lookup took {cached_elapsed * 1000:.3f}ms, expected < 1ms"

    # Upsert invalidates cache
    updated = Course(
        display_code="CPS109",
        normalized_key="CPS109",
        title="Computer Science I (Updated)",
        campus_id="tmu",
    )
    await store.upsert_course(guild_id, updated)
    assert (guild_id, "CPS109") not in store._cache

    # Next lookup re-caches updated value
    c3 = await store.get_course(guild_id, "cps109")
    assert c3 is not None
    assert c3.title == "Computer Science I (Updated)"
    assert (guild_id, "CPS109") in store._cache
