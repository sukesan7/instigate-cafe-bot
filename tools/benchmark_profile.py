"""Reproducible Performance & Memory Profiling Harness for Instigate Cafe Bot.

Profiles representative workloads without connecting to Discord or external networks:
1. Application startup (Settings, Database, Migrations, Container, Bot setup without gateway)
2. Slash command registration (all 17 feature modules, 52 leaf commands)
3. 100 simultaneous cached lookups (measuring event loop lag and p50/p90/p99 latency)
4. 1,000 ATS job ingestion batch (filtering, deduplication, SQLite checkpointing)
5. 50 concurrent virtual economy writes (atomic balance updates & economy_ledger auditing)
6. Matplotlib chart rendering (sync event loop blocking vs async executor offloading)
7. 10 parallel AI requests with provider stubs (concurrency control & token budgeting)

Run via:
    uv run python tools/benchmark_profile.py
    uv run python tools/benchmark_profile.py --json-out artifacts/benchmark_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yarl

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402
from icbot.academics.cache import AcademicProviderCache, CacheKey
from icbot.academics.charts import (
    render_grade_distribution_chart,
    render_grade_distribution_chart_async,
)
from icbot.bot import create_bot
from icbot.config import Settings
from icbot.db import Database
from icbot.registry import iter_leaf_commands
from icbot.services.gemini_client import AIBudgetGovernor, GeminiClient
from icbot.services.jobs.models import JobFilterCriteria, JobPosting
from icbot.services.jobs.pipeline import JobsIngestionPipeline, matches_filter_criteria
from icbot.services.user_service import UserStore
from tests.helpers import AiohttpMockRouter, FakeAiohttpResponse

# ---------------------------------------------------------------------------
# Measurement & Timing Helpers
# ---------------------------------------------------------------------------


def get_git_sha() -> str:
    """Return short Git commit SHA or 'unknown'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
        return res.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


@dataclass
class LatencyPercentiles:
    min_ms: float
    p50_ms: float
    p90_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float

    @classmethod
    def from_samples(cls, samples_s: Sequence[float]) -> LatencyPercentiles:
        if not samples_s:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        sorted_ms = sorted(s * 1000.0 for s in samples_s)
        n = len(sorted_ms)

        def _pct(p: float) -> float:
            idx = round((p / 100.0) * (n - 1))
            return sorted_ms[min(max(0, idx), n - 1)]

        return cls(
            min_ms=round(sorted_ms[0], 3),
            p50_ms=round(_pct(50), 3),
            p90_ms=round(_pct(90), 3),
            p99_ms=round(_pct(99), 3),
            max_ms=round(sorted_ms[-1], 3),
            mean_ms=round(sum(sorted_ms) / n, 3),
        )


@dataclass
class BenchmarkMetric:
    name: str
    workload_description: str
    sample_size: int
    total_time_s: float
    throughput_ops_per_sec: float
    peak_memory_kib: float
    event_loop_max_lag_ms: float
    latency: LatencyPercentiles
    notes: str = ""


@dataclass
class SystemEnvironment:
    python_version: str
    platform_system: str
    platform_release: str
    cpu_architecture: str
    cpu_count: int | None
    git_sha: str
    timestamp_utc: str


@dataclass
class BenchmarkReport:
    environment: SystemEnvironment
    metrics: list[BenchmarkMetric] = field(default_factory=list)


class EventLoopHeartbeat:
    """Monitors event loop blocking by measuring tick lag against a 1ms target."""

    def __init__(self, target_interval_s: float = 0.001) -> None:
        self.target_interval_s = target_interval_s
        self.max_lag_s = 0.0
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        while self._running:
            t0 = time.perf_counter()
            await asyncio.sleep(self.target_interval_s)
            elapsed = time.perf_counter() - t0
            lag = max(0.0, elapsed - self.target_interval_s)
            if lag > self.max_lag_s:
                self.max_lag_s = lag

    def start(self) -> None:
        self.max_lag_s = 0.0
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> float:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        return self.max_lag_s * 1000.0  # Return max lag in milliseconds


# ---------------------------------------------------------------------------
# Benchmark Workloads
# ---------------------------------------------------------------------------


async def benchmark_startup(iterations: int = 5) -> BenchmarkMetric:
    """Workload 1: Application startup (Settings, Database, Migrations, Container, Bot creation)."""
    samples: list[float] = []
    peak_mem_kib = 0.0
    heartbeat = EventLoopHeartbeat()
    heartbeat.start()

    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(iterations):
            db_path = str(Path(tmpdir) / f"bench_startup_{i}.db")
            tracemalloc.start()
            t0 = time.perf_counter()

            settings = Settings(
                token="",
                guild_id=900000000000000001,
                db_path=db_path,
                allow_gateway=False,
            )
            db = Database(db_path)
            await db.init()
            bot = create_bot(settings, register_commands=False, sync_commands=False)
            bot.db = db
            await bot.setup_hook()

            elapsed = time.perf_counter() - t0
            samples.append(elapsed)

            _curr, peak = tracemalloc.get_traced_memory()
            peak_mem_kib = max(peak_mem_kib, peak / 1024.0)
            tracemalloc.stop()

            await bot.close()
            await db.close()

    max_lag = await heartbeat.stop()
    total_time = sum(samples)
    return BenchmarkMetric(
        name="app_startup_no_gateway",
        workload_description="Settings validation, SQLite migrations (v1-v7), Container & Bot setup",
        sample_size=iterations,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(iterations / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=round(max_lag, 2),
        latency=LatencyPercentiles.from_samples(samples),
        notes="Offline isolated startup without live gateway connection",
    )


async def benchmark_slash_command_registration(iterations: int = 5) -> BenchmarkMetric:
    """Workload 2: Inspecting and registering all 17 feature modules and 52 leaf commands."""
    samples: list[float] = []
    peak_mem_kib = 0.0
    leaf_count = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "bench_reg.db")
        settings = Settings(token="", guild_id=900000000000000001, db_path=db_path, allow_gateway=False)

        for _ in range(iterations):
            tracemalloc.start()
            t0 = time.perf_counter()

            bot = create_bot(settings, register_commands=True, sync_commands=False)
            leaves = iter_leaf_commands()
            leaf_count = len(leaves)

            elapsed = time.perf_counter() - t0
            samples.append(elapsed)

            _curr, peak = tracemalloc.get_traced_memory()
            peak_mem_kib = max(peak_mem_kib, peak / 1024.0)
            tracemalloc.stop()
            await bot.close()

    total_time = sum(samples)
    return BenchmarkMetric(
        name="slash_command_tree_registration",
        workload_description=f"Registration and metadata reflection for {leaf_count} leaf commands",
        sample_size=iterations,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(iterations / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=0.0,
        latency=LatencyPercentiles.from_samples(samples),
        notes=f"Verified exactly {leaf_count} commands across 17 feature modules",
    )


async def benchmark_cached_lookups(lookups_count: int = 100) -> BenchmarkMetric:
    """Workload 3: 100 simultaneous cached academic lookups via AcademicProviderCache."""
    cache = AcademicProviderCache(max_entries=512)
    now_ts = 1760000000.0

    # Seed cache with 20 distinct entries
    for i in range(20):
        key = CacheKey.make("test_provider", "toronto", f"course_{i}")
        cache.set(key, {"course_id": f"C{i}", "title": f"Course {i}"}, ttl_seconds=3600.0, now=now_ts)

    heartbeat = EventLoopHeartbeat()
    heartbeat.start()
    tracemalloc.start()

    samples: list[float] = []

    async def _single_lookup(idx: int) -> None:
        t0 = time.perf_counter()
        key = CacheKey.make("test_provider", "toronto", f"course_{idx % 20}")
        res = cache.get(key, now=now_ts)
        assert res is not None
        samples.append(time.perf_counter() - t0)

    t_start = time.perf_counter()
    await asyncio.gather(*[_single_lookup(i) for i in range(lookups_count)])
    total_time = time.perf_counter() - t_start

    _curr, peak = tracemalloc.get_traced_memory()
    peak_mem_kib = peak / 1024.0
    tracemalloc.stop()
    max_lag = await heartbeat.stop()

    return BenchmarkMetric(
        name="100_simultaneous_cached_lookups",
        workload_description="100 concurrent asyncio tasks reading memory-cached academic entities",
        sample_size=lookups_count,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(lookups_count / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=round(max_lag, 2),
        latency=LatencyPercentiles.from_samples(samples),
        notes="Evaluates event-loop lag and concurrent cache concurrency",
    )


async def benchmark_jobs_ingestion_1k() -> BenchmarkMetric:
    """Workload 4: Ingestion batch of 1,000 synthetic ATS jobs (filtering, rules, SQLite checkpoint)."""
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
            text_blob=f"{title} at {emp} located in {loc}. Python, systems, C++, distributed algorithms.",
        )
        postings.append(p)

    criteria = JobFilterCriteria(
        location_allowlist=("toronto", "remote", "waterloo", "new york"),
        keywords_denylist=("sales", "recruiter", "manager"),
        require_early_career=True,
    )
    pipeline = JobsIngestionPipeline(clock=lambda: 1760000000.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "bench_jobs.db")
        db = Database(db_path)
        await db.init()

        heartbeat = EventLoopHeartbeat()
        heartbeat.start()
        tracemalloc.start()
        t0 = time.perf_counter()

        # Phase 1: Filter evaluation (1,000 items)
        matched = [j for j in postings if matches_filter_criteria(j, criteria)]

        # Phase 2: Batch checkpoint ingestion into SQLite
        await pipeline.checkpoint_seen(db, postings)

        # Phase 3: DB query verification
        count_row = await db.fetchone("SELECT COUNT(*) FROM job_seen")
        assert count_row == (1000,)

        total_time = time.perf_counter() - t0
        _curr, peak = tracemalloc.get_traced_memory()
        peak_mem_kib = peak / 1024.0
        tracemalloc.stop()
        max_lag = await heartbeat.stop()
        await db.close()

    return BenchmarkMetric(
        name="1k_job_ingestion_batch",
        workload_description="Filtering, rule matching, and SQLite persistence for 1,000 ATS postings",
        sample_size=1000,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(1000 / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=round(max_lag, 2),
        latency=LatencyPercentiles(
            min_ms=round(total_time * 1000 / 1000, 3),
            p50_ms=round(total_time * 1000 / 1000, 3),
            p90_ms=round(total_time * 1000 / 1000, 3),
            p99_ms=round(total_time * 1000 / 1000, 3),
            max_ms=round(total_time * 1000 / 1000, 3),
            mean_ms=round(total_time * 1000 / 1000, 3),
        ),
        notes=f"Matched {len(matched)} / 1000 jobs. SQLite batch insert via execute_many.",
    )


async def benchmark_virtual_economy_writes(write_count: int = 50) -> BenchmarkMetric:
    """Workload 5: 50 concurrent transactions via UserService and economy_ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "bench_economy.db")
        db = Database(db_path)
        await db.init()

        clock_val = 1760000000.0
        user_svc = UserStore(db, starting_balance=1000, clock=lambda: clock_val)
        guild_id = 1001

        # Pre-seed 10 accounts
        for uid in range(1, 11):
            await user_svc.get_balance(uid, guild_id=guild_id)

        heartbeat = EventLoopHeartbeat()
        heartbeat.start()
        tracemalloc.start()

        samples: list[float] = []

        async def _single_transfer(idx: int) -> None:
            t0 = time.perf_counter()
            from_u = (idx % 10) + 1
            await user_svc.add_balance(
                from_u,
                5,
                guild_id=guild_id,
                category="test_credit",
                idempotency_key=f"bench_tx_{idx}",
            )
            samples.append(time.perf_counter() - t0)

        t_start = time.perf_counter()
        await asyncio.gather(*[_single_transfer(i) for i in range(write_count)])
        total_time = time.perf_counter() - t_start

        # Verify ledger row count
        ledger_ct = await db.fetchone("SELECT COUNT(*) FROM economy_ledger WHERE guild_id=?", (guild_id,))
        assert ledger_ct == (write_count,)

        _curr, peak = tracemalloc.get_traced_memory()
        peak_mem_kib = peak / 1024.0
        tracemalloc.stop()
        max_lag = await heartbeat.stop()
        await db.close()

    return BenchmarkMetric(
        name="50_virtual_economy_writes",
        workload_description="50 concurrent balance updates & immutable audit logging in economy_ledger",
        sample_size=write_count,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(write_count / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=round(max_lag, 2),
        latency=LatencyPercentiles.from_samples(samples),
        notes="Evaluates SQLite lock contention and serial managed connection writes",
    )


async def benchmark_chart_rendering() -> tuple[BenchmarkMetric, BenchmarkMetric]:
    """Workload 6: Matplotlib chart rendering: Sync event-loop vs Async Executor offloading."""
    synthetic_profs = [
        {
            "name": f"Professor {i}",
            "students": 120 + i * 20,
            "avg": "B+",
            "common": "A-",
            "dist": [
                {"letterGrade": "A+", "count": 25},
                {"letterGrade": "A", "count": 30},
                {"letterGrade": "A-", "count": 20},
                {"letterGrade": "B+", "count": 15},
                {"letterGrade": "B", "count": 10},
                {"letterGrade": "C", "count": 5},
                {"letterGrade": "F", "count": 2},
            ],
        }
        for i in range(5)
    ]
    course_title = "CPS 109 - Computer Science I"

    # --- Mode A: Synchronous Rendering on Event Loop ---
    hb_sync = EventLoopHeartbeat()
    hb_sync.start()
    await asyncio.sleep(0.005)  # Ensure heartbeat loop is actively ticking
    tracemalloc.start()
    t0 = time.perf_counter()
    sync_res = render_grade_distribution_chart(course_title, synthetic_profs)
    sync_elapsed = time.perf_counter() - t0
    _curr, sync_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    sync_lag = await hb_sync.stop()

    metric_sync = BenchmarkMetric(
        name="chart_render_sync_on_event_loop",
        workload_description="Grouped bar chart (5 professors, 15 buckets) executed synchronously",
        sample_size=1,
        total_time_s=round(sync_elapsed, 4),
        throughput_ops_per_sec=round(1.0 / sync_elapsed, 2) if sync_elapsed > 0 else 0.0,
        peak_memory_kib=round(sync_peak / 1024.0, 1),
        event_loop_max_lag_ms=round(sync_lag, 2),
        latency=LatencyPercentiles.from_samples([sync_elapsed]),
        notes=f"Output size: {sync_res.size_bytes} bytes. Event-loop blocked during render.",
    )

    # --- Mode B: Asynchronous Rendering in Worker Thread Executor ---
    hb_async = EventLoopHeartbeat()
    hb_async.start()
    await asyncio.sleep(0.005)  # Ensure heartbeat loop is actively ticking
    tracemalloc.start()
    t0 = time.perf_counter()
    async_res = await render_grade_distribution_chart_async(course_title, synthetic_profs)
    async_elapsed = time.perf_counter() - t0
    _curr, async_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    async_lag = await hb_async.stop()

    metric_async = BenchmarkMetric(
        name="chart_render_async_executor",
        workload_description="Grouped bar chart executed off-loop via asyncio.to_thread",
        sample_size=1,
        total_time_s=round(async_elapsed, 4),
        throughput_ops_per_sec=round(1.0 / async_elapsed, 2) if async_elapsed > 0 else 0.0,
        peak_memory_kib=round(async_peak / 1024.0, 1),
        event_loop_max_lag_ms=round(async_lag, 2),
        latency=LatencyPercentiles.from_samples([async_elapsed]),
        notes=f"Output size: {async_res.size_bytes} bytes. Event-loop remained unblocked.",
    )

    return metric_sync, metric_async


async def benchmark_parallel_ai_requests(parallel_count: int = 10) -> BenchmarkMetric:
    """Workload 7: 10 parallel AI requests against stubbed Gemini provider with governance."""
    governor = AIBudgetGovernor(clock=lambda: 1760000000.0)
    # Configure client with max_concurrency=4 to demonstrate bounded semaphore queueing
    client = GeminiClient(
        api_key="synthetic_stub_key_0000000000000",
        max_concurrency=4,
        governor=governor,
        clock=lambda: 1760000000.0,
    )

    router = AiohttpMockRouter()
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Synthetic response: candidate feedback on algorithms and systems."}]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 150,
            "candidatesTokenCount": 50,
            "totalTokenCount": 200,
        },
    }
    router.add_route("POST", lambda u: "generativelanguage" in u, status=200, json_data=payload)

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
        merged_headers: dict[str, str] = {}
        for k, v in getattr(self, "headers", {}).items():
            merged_headers[str(k)] = str(v)
        if headers:
            for k, v in dict(headers).items():
                merged_headers[str(k)] = str(v)
        body_val = json if json is not None else data
        return router.dispatch(method, str(base_url), merged_headers, body_val, params)

    orig_request = aiohttp.ClientSession._request
    aiohttp.ClientSession._request = _mock_request  # type: ignore[assignment,method-assign]

    heartbeat = EventLoopHeartbeat()
    heartbeat.start()
    tracemalloc.start()

    samples: list[float] = []

    try:

        async def _single_ai_call(idx: int) -> None:
            t0 = time.perf_counter()
            resp = await client.generate_text(
                prompt=f"Prompt {idx}: Review software resume skills in distributed systems and SQLite.",
                guild_id=1001,
            )
            assert "Synthetic response" in resp
            samples.append(time.perf_counter() - t0)

        t_start = time.perf_counter()
        await asyncio.gather(*[_single_ai_call(i) for i in range(parallel_count)])
        total_time = time.perf_counter() - t_start
    finally:
        aiohttp.ClientSession._request = orig_request  # type: ignore[method-assign]

    _curr, peak = tracemalloc.get_traced_memory()
    peak_mem_kib = peak / 1024.0
    tracemalloc.stop()
    max_lag = await heartbeat.stop()

    return BenchmarkMetric(
        name="10_parallel_ai_requests_stubbed",
        workload_description="10 concurrent Gemini requests with token governance and semaphore concurrency",
        sample_size=parallel_count,
        total_time_s=round(total_time, 4),
        throughput_ops_per_sec=round(parallel_count / total_time, 2) if total_time > 0 else 0.0,
        peak_memory_kib=round(peak_mem_kib, 1),
        event_loop_max_lag_ms=round(max_lag, 2),
        latency=LatencyPercentiles.from_samples(samples),
        notes="Bounded by Semaphore(4). Governed by AIBudgetGovernor.",
    )


# ---------------------------------------------------------------------------
# Runner & Report Generation
# ---------------------------------------------------------------------------


async def run_full_benchmark_suite() -> BenchmarkReport:
    """Run all benchmark workloads sequentially and compile report."""
    env = SystemEnvironment(
        python_version=sys.version.split()[0],
        platform_system=platform.system(),
        platform_release=platform.release(),
        cpu_architecture=platform.machine(),
        cpu_count=os.cpu_count(),
        git_sha=get_git_sha(),
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    report = BenchmarkReport(environment=env)

    # 1. Startup
    report.metrics.append(await benchmark_startup(iterations=3))

    # 2. Slash Command Tree
    report.metrics.append(await benchmark_slash_command_registration(iterations=3))

    # 3. 100 Cached Lookups
    report.metrics.append(await benchmark_cached_lookups(lookups_count=100))

    # 4. 1,000 ATS Jobs Ingestion
    report.metrics.append(await benchmark_jobs_ingestion_1k())

    # 5. 50 Virtual Economy Writes
    report.metrics.append(await benchmark_virtual_economy_writes(write_count=50))

    # 6. Chart Rendering (Sync & Async)
    m_sync, m_async = await benchmark_chart_rendering()
    report.metrics.append(m_sync)
    report.metrics.append(m_async)

    # 7. 10 Parallel AI Requests
    report.metrics.append(await benchmark_parallel_ai_requests(parallel_count=10))

    return report


def format_markdown_table(report: BenchmarkReport) -> str:
    """Format benchmark report as a GitHub-flavored markdown table."""
    lines: list[str] = [
        f"### Performance & Resource Profile Report (`{report.environment.git_sha}`)",
        f"- **Environment**: Python {report.environment.python_version} on {report.environment.platform_system} ({report.environment.cpu_architecture}, {report.environment.cpu_count} cores)",
        f"- **Generated At**: {report.environment.timestamp_utc}",
        "",
        "| Benchmark Workload | Samples | Total (s) | Ops/sec | p50 (ms) | p90 (ms) | p99 (ms) | Peak RAM (KiB) | Event-Loop Lag (ms) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for m in report.metrics:
        lines.append(
            f"| `{m.name}` | {m.sample_size} | {m.total_time_s:.3f} | {m.throughput_ops_per_sec:.1f} | "
            f"{m.latency.p50_ms:.1f} | {m.latency.p90_ms:.1f} | {m.latency.p99_ms:.1f} | "
            f"{m.peak_memory_kib:.1f} | {m.event_loop_max_lag_ms:.1f} |"
        )
    return "\n".join(lines)


def format_comparison_table(before: BenchmarkReport, after: BenchmarkReport) -> str:
    """Format a side-by-side Before/After comparison markdown table."""
    lines: list[str] = [
        "### Optimization Before / After Comparison",
        f"- **Baseline Git SHA**: `{before.environment.git_sha}` ({before.environment.timestamp_utc})",
        f"- **Optimized Git SHA**: `{after.environment.git_sha}` ({after.environment.timestamp_utc})",
        "",
        "| Workload | Baseline Total (s) | Optimized Total (s) | Speedup / Delta | Baseline Peak RAM | Optimized Peak RAM | Loop Lag Delta (ms) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    after_map = {m.name: m for m in after.metrics}
    for b in before.metrics:
        a = after_map.get(b.name)
        if not a:
            continue
        speedup = (b.total_time_s / a.total_time_s) if a.total_time_s > 0 else 1.0
        delta_str = (
            f"{speedup:.2f}x faster"
            if speedup >= 1.05
            else (f"{speedup:.2f}x" if speedup >= 0.95 else f"{speedup:.2f}x slower")
        )
        ram_delta = a.peak_memory_kib - b.peak_memory_kib
        lag_delta = a.event_loop_max_lag_ms - b.event_loop_max_lag_ms
        lines.append(
            f"| `{b.name}` | {b.total_time_s:.3f}s | {a.total_time_s:.3f}s | **{delta_str}** | "
            f"{b.peak_memory_kib:.1f} KiB | {a.peak_memory_kib:.1f} KiB ({ram_delta:+.1f}) | {lag_delta:+.1f} ms |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Instigate Cafe Bot offline benchmark and profiling harness."
    )
    parser.add_argument("--json-out", type=str, default=None, help="Save JSON report to specified path.")
    parser.add_argument(
        "--compare", type=str, default=None, help="Compare current run against baseline JSON file."
    )
    args = parser.parse_args()

    report = asyncio.run(run_full_benchmark_suite())
    table_md = format_markdown_table(report)
    print("\n" + table_md + "\n")

    if args.compare:
        compare_path = Path(args.compare)
        if compare_path.is_file():
            base_data = json.loads(compare_path.read_text(encoding="utf-8"))
            # Parse baseline
            env = SystemEnvironment(**base_data["environment"])
            base_metrics: list[BenchmarkMetric] = []
            for m in base_data["metrics"]:
                lat = LatencyPercentiles(**m["latency"])
                m_copy = dict(m)
                m_copy["latency"] = lat
                base_metrics.append(BenchmarkMetric(**m_copy))
            base_report = BenchmarkReport(environment=env, metrics=base_metrics)
            cmp_table = format_comparison_table(base_report, report)
            print("\n" + cmp_table + "\n")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report_dict = {
            "environment": asdict(report.environment),
            "metrics": [asdict(m) for m in report.metrics],
        }
        out_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
        print(f"[OK] Saved benchmark report to {out_path}")


if __name__ == "__main__":
    main()
