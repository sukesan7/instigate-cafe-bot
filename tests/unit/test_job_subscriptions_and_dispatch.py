from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from icbot.config import Settings
from icbot.db import Database
from icbot.features.jobs import JobPaginationView
from icbot.services.jobs.metrics import JobIngestionMetrics
from icbot.services.jobs.models import JobPosting
from icbot.services.jobs.pipeline import JobsIngestionPipeline

pytestmark = pytest.mark.unit


def _build_test_posting(
    provider: str,
    employer: str,
    external_id: str,
    title: str,
    *,
    location: str = "Toronto, ON",
    posted_unix: int = 1700000000,
    text_blob: str = "",
) -> JobPosting:
    blob = text_blob or f"{title} {employer} {location}".lower()
    return JobPosting(
        provider=provider,  # type: ignore[arg-type]
        employer=employer,
        external_id=external_id,
        title=title,
        location=location,
        url=f"https://example.invalid/{provider}/{employer}/{external_id}",
        posted_unix=posted_unix,
        text_blob=blob,
    )


class FakeChannel:
    def __init__(self, channel_id: int, *, name: str = "general", fail_on_send: Exception | None = None):
        self.id = channel_id
        self.name = name
        self.sent: list[dict[str, Any]] = []
        self.fail_on_send = fail_on_send
        self.guild = SimpleNamespace(id=999, me=SimpleNamespace())

    def permissions_for(self, member: Any) -> Any:
        return SimpleNamespace(send_messages=True, embed_links=True)

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_on_send:
            raise self.fail_on_send
        self.sent.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=len(self.sent))


class FakeBot:
    def __init__(self, channels: dict[int, Any] | None = None):
        self.channels = channels or {}
        self.settings = Settings(embed_footer_text="Test Bot")

    def get_channel(self, channel_id: int) -> Any:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        if channel_id in self.channels:
            return self.channels[channel_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Channel not found")


@pytest.mark.asyncio
async def test_multi_guild_fan_out_without_cross_guild_leakage(tmp_db: Database):
    """A single posting ingestion batch fans out to separate guild subscriptions with isolated presets."""
    pipeline = JobsIngestionPipeline(clock=lambda: 1700000000.0)

    # Configure Guild 1 on tech_swe (#swe-alerts) and Guild 2 on business_finance (#finance-alerts)
    await tmp_db.set_guild_job_subscription(101, 201, preset="tech_swe", enabled=True)
    await tmp_db.set_guild_job_subscription(102, 202, preset="business_finance", enabled=True)

    # SWE job + Finance job
    j_swe = _build_test_posting("greenhouse", "Stripe", "swe-1", "Software Engineering Intern")
    j_fin = _build_test_posting("lever", "RBC", "fin-1", "Financial Analyst Intern")

    # Ingest and enqueue
    new_jobs = await pipeline.ingest_and_enqueue_postings(tmp_db, [j_swe, j_fin])
    assert len(new_jobs) == 2

    # Check pending outbox
    pending = await tmp_db.fetch_pending_job_outbox(limit=10)
    assert len(pending) == 2

    # Verify Guild 1 only got SWE job, Guild 2 only got Finance job
    g1_items = [p for p in pending if p["guild_id"] == 101]
    g2_items = [p for p in pending if p["guild_id"] == 102]

    assert len(g1_items) == 1
    assert g1_items[0]["channel_id"] == 201
    assert g1_items[0]["job_title"] == "Software Engineering Intern"

    assert len(g2_items) == 1
    assert g2_items[0]["channel_id"] == 202
    assert g2_items[0]["job_title"] == "Financial Analyst Intern"

    # Dispatch to channels
    ch_swe = FakeChannel(201, name="swe-alerts")
    ch_fin = FakeChannel(202, name="finance-alerts")
    bot = FakeBot({201: ch_swe, 202: ch_fin})

    delivered = await pipeline.dispatch_outbox(bot, tmp_db)
    assert delivered == 2
    assert len(ch_swe.sent) == 1
    assert len(ch_fin.sent) == 1

    # Outbox is now empty (all delivered)
    assert await tmp_db.fetch_pending_job_outbox(limit=10) == []


@pytest.mark.asyncio
async def test_transactional_outbox_deduplication_and_duplicate_suppression(tmp_db: Database):
    """Re-ingesting the same posting does not produce duplicate outbox records or deliveries."""
    pipeline = JobsIngestionPipeline(clock=lambda: 1700000000.0)
    await tmp_db.set_guild_job_subscription(101, 201, preset="tech_swe", enabled=True)

    j = _build_test_posting("greenhouse", "Stripe", "swe-1", "Software Engineering Intern")

    # Cycle 1
    await pipeline.ingest_and_enqueue_postings(tmp_db, [j])
    ch = FakeChannel(201)
    bot = FakeBot({201: ch})
    assert await pipeline.dispatch_outbox(bot, tmp_db) == 1
    assert len(ch.sent) == 1

    # Cycle 2 with identical posting
    new_jobs = await pipeline.ingest_and_enqueue_postings(tmp_db, [j])
    assert new_jobs == []  # Not treated as new
    assert await pipeline.dispatch_outbox(bot, tmp_db) == 0
    assert len(ch.sent) == 1  # Zero duplicate messages sent


@pytest.mark.asyncio
async def test_stale_outbox_recovery_and_at_least_once_bounding(tmp_db: Database):
    """Crash recovery reconciles in-flight 'sending' items against alert_delivery_dedupes."""
    pipeline = JobsIngestionPipeline(clock=lambda: 1700001000.0)
    await tmp_db.set_guild_job_subscription(101, 201, preset="tech_swe", enabled=True)

    # Insert an outbox item that was marked 'sending' at t=1700000000 (1000s ago)
    await tmp_db.enqueue_job_outbox(
        [
            {
                "guild_id": 101,
                "channel_id": 201,
                "source_key": "greenhouse:stripe",
                "external_id": "swe-crash",
                "job_title": "Software Engineering Intern",
                "job_company": "Stripe",
                "job_url": "https://example.invalid/swe",
                "career_level": "intern",
                "discipline_tags": ["software"],
                "preset": "tech_swe",
                "enqueued_at": 1700000000,
            }
        ]
    )
    pending = await tmp_db.fetch_pending_job_outbox()
    outbox_id = pending[0]["id"]
    await tmp_db.mark_job_outbox_sending([outbox_id])

    # Case A: Delivery WAS recorded in alert_delivery_dedupes before crash
    dedupe_key = "201:greenhouse:stripe:swe-crash"
    await tmp_db.execute(
        "INSERT INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at) VALUES (?, 'job_alert', ?, ?)",
        (101, dedupe_key, 1700000050),
    )

    recovered = await pipeline.recover_stale_outbox(tmp_db, lease_seconds=300)
    assert recovered == 1

    # Item was reconciled to 'delivered', NOT re-sent
    row = await tmp_db.fetchone("SELECT status, delivered_at FROM job_alert_outbox WHERE id=?", (outbox_id,))
    assert row == ("delivered", 1700000050)

    # Case B: Delivery was NOT recorded before crash -> safely reset to 'pending' with attempt incremented
    await tmp_db.enqueue_job_outbox(
        [
            {
                "guild_id": 101,
                "channel_id": 201,
                "source_key": "greenhouse:stripe",
                "external_id": "swe-crash-2",
                "job_title": "Backend Engineering Intern",
                "job_company": "Stripe",
                "job_url": "https://example.invalid/swe-2",
                "career_level": "intern",
                "discipline_tags": ["software"],
                "preset": "tech_swe",
                "enqueued_at": 1700000000,
            }
        ]
    )
    pending2 = await tmp_db.fetch_pending_job_outbox()
    outbox_id2 = pending2[0]["id"]
    await tmp_db.mark_job_outbox_sending([outbox_id2])

    recovered2 = await pipeline.recover_stale_outbox(tmp_db, lease_seconds=300)
    assert recovered2 == 1

    row2 = await tmp_db.fetchone("SELECT status, attempts FROM job_alert_outbox WHERE id=?", (outbox_id2,))
    assert row2 == ("pending", 1)


@pytest.mark.asyncio
async def test_discord_channel_errors_permissions_and_429_handling(tmp_db: Database):
    """Pipeline categorizes Discord 429 rate limits, missing permissions, and missing channels."""
    pipeline = JobsIngestionPipeline(clock=lambda: 1700000000.0)

    # 1. Missing channel (None)
    await tmp_db.set_guild_job_subscription(101, 999999, preset="tech_swe", enabled=True)
    j1 = _build_test_posting("greenhouse", "Stripe", "err-1", "Software Engineering Intern")
    await pipeline.ingest_and_enqueue_postings(tmp_db, [j1])

    bot_empty = FakeBot({})
    delivered = await pipeline.dispatch_outbox(bot_empty, tmp_db)
    assert delivered == 0
    assert pipeline.metrics.discord_delivery_failures["channel_not_found"] == 1

    # 2. Channel missing permissions
    class NoPermChannel(FakeChannel):
        def permissions_for(self, member: Any) -> Any:
            return SimpleNamespace(send_messages=False, embed_links=False)

    await tmp_db.set_guild_job_subscription(101, 203, preset="tech_swe", enabled=True)
    j2 = _build_test_posting("greenhouse", "Stripe", "err-2", "Software Engineering Intern")
    await pipeline.ingest_and_enqueue_postings(tmp_db, [j2])

    bot_noperm = FakeBot({203: NoPermChannel(203)})
    delivered_noperm = await pipeline.dispatch_outbox(bot_noperm, tmp_db)
    assert delivered_noperm == 0
    assert pipeline.metrics.discord_delivery_failures["missing_permissions"] == 1

    # 3. Discord 429 Rate Limit
    rate_limit_exc = discord.RateLimited(retry_after=5.0)
    await tmp_db.set_guild_job_subscription(101, 204, preset="tech_swe", enabled=True)
    j3 = _build_test_posting("greenhouse", "Stripe", "err-3", "Software Engineering Intern")
    await pipeline.ingest_and_enqueue_postings(tmp_db, [j3])

    bot_429 = FakeBot({204: FakeChannel(204, fail_on_send=rate_limit_exc)})
    delivered_429 = await pipeline.dispatch_outbox(bot_429, tmp_db)
    assert delivered_429 == 0
    assert pipeline.metrics.discord_delivery_failures["rate_limited_429"] == 1


def test_job_pagination_view_navigation_and_degraded_state():
    """JobPaginationView renders pages, handles buttons, and shows warnings when sources are degraded."""
    settings = Settings(embed_footer_text="Instigate Cafe Bot")
    postings = [
        _build_test_posting("greenhouse", f"Company {i}", f"id-{i}", f"Software Intern {i}")
        for i in range(12)
    ]

    view = JobPaginationView(
        settings=settings,
        postings=postings,
        preset_name="tech_swe",
        total_matched=12,
        last_updated_str="5m ago",
        is_degraded=True,
        query="python",
        location_filter="Toronto",
    )

    # Initial page (page 0 of 3)
    assert view.current_page == 0
    assert view.total_pages == 3
    assert view.prev_button.disabled is True
    assert view.next_button.disabled is False

    embed0 = view.build_current_embed()
    assert "Page 1/3" in embed0.title
    assert "⚠️ *ATS provider feeds are cooling down or unreachable" in embed0.description
    assert len(embed0.fields) == 5  # 5 per page

    # Advance to page 1
    view.current_page = 1
    view._sync_buttons()
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is False

    embed1 = view.build_current_embed()
    assert "Page 2/3" in embed1.title
    assert len(embed1.fields) == 5

    # Advance to last page (page 2)
    view.current_page = 2
    view._sync_buttons()
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is True

    embed2 = view.build_current_embed()
    assert "Page 3/3" in embed2.title
    assert len(embed2.fields) == 2  # remaining 2 items


def test_job_metrics_structured_snapshot():
    """JobIngestionMetrics captures fetch, match, duplicate, and delivery metrics cleanly."""
    m = JobIngestionMetrics()
    m.record_source_success("greenhouse:stripe", 0.452)
    m.record_matching_jobs(5)
    m.record_suppressed_duplicates(3)
    m.record_http_failure("lever:bad", "404 Not Found")
    m.record_discord_delivery_success(4)
    m.record_discord_delivery_failure("rate_limited_429", 1)

    snap = m.to_dict()
    assert snap["source_success_count"] == 1
    assert snap["source_latency_seconds"]["greenhouse:stripe"] == 0.452
    assert snap["new_matching_jobs"] == 5
    assert snap["suppressed_duplicates"] == 3
    assert snap["http_failures"] == 1
    assert snap["discord_delivery_successes"] == 4
    assert snap["discord_delivery_failures"]["rate_limited_429"] == 1
