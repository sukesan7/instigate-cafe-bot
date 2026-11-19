"""Unit tests for Prompt 10B: Safe, deduplicated regional and financial alerts.

Verifies:
1. Multi-guild deduplication isolation and material update transitions.
2. Bootstrap suppression of historical dumps (checkpoint-only vs backfill).
3. Bitcoin address validation, capping at 50, and privacy masking.
4. Financial attribution, timestamps, and explicit non-financial advice disclaimers.
5. Named timezones (America/Toronto) and DST scheduling resilience without historical flood.
6. Safe alert delivery with permission checks (send_messages, embed_links), missing channels,
   and Discord HTTP 429 rate limiting.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import discord

from icbot.db import Database
from icbot.services.alert_dedupe import (
    FINANCIAL_DISCLAIMER,
    AlertDedupeManager,
    AlertLifecycleStatus,
    compute_content_hash,
    format_financial_embed_footer,
    get_local_date_str,
    is_eligible_daily_report_window,
    mask_btc_address,
    safe_send_alert,
    sanitize_btc_addresses,
    validate_btc_address,
)
from tests.helpers import FakeTextChannel, FrozenClock

# ---------------------------------------------------------------------------
# 1. Multi-Guild Isolation & Material Update Transitions
# ---------------------------------------------------------------------------


async def test_multi_guild_dedupe_isolation(tmp_db: Database):
    """Alerts delivered to Guild A (channel 101) do not suppress alerts in Guild B (channel 201)."""
    clock = FrozenClock(1700000000.0)
    mgr = AlertDedupeManager(clock=clock)

    guild_a = 1001
    ch_a = 101
    guild_b = 2002
    ch_b = 201

    alert_type = "ttc_alert"
    entity_id = "notif-501-delayed"
    content_hash = compute_content_hash("Line 1 Yonge-University delay at Bloor")

    # Guild A sees it first
    status_a = await mgr.check_alert(tmp_db, guild_a, alert_type, ch_a, entity_id, content_hash)
    assert status_a == AlertLifecycleStatus.NEW_EVENT
    await mgr.record_delivery(tmp_db, guild_a, alert_type, ch_a, entity_id, content_hash)

    # Guild A checking again sees duplicate
    dup_a = await mgr.check_alert(tmp_db, guild_a, alert_type, ch_a, entity_id, content_hash)
    assert dup_a == AlertLifecycleStatus.IDENTICAL_DUPLICATE

    # Guild B checking the exact same incident entity_id still sees NEW_EVENT (isolated)
    status_b = await mgr.check_alert(tmp_db, guild_b, alert_type, ch_b, entity_id, content_hash)
    assert status_b == AlertLifecycleStatus.NEW_EVENT
    await mgr.record_delivery(tmp_db, guild_b, alert_type, ch_b, entity_id, content_hash)

    # Both are now recorded independently
    assert (
        await mgr.check_alert(tmp_db, guild_a, alert_type, ch_a, entity_id, content_hash)
        == AlertLifecycleStatus.IDENTICAL_DUPLICATE
    )
    assert (
        await mgr.check_alert(tmp_db, guild_b, alert_type, ch_b, entity_id, content_hash)
        == AlertLifecycleStatus.IDENTICAL_DUPLICATE
    )


async def test_alert_lifecycle_material_update_transitions(tmp_db: Database):
    """Status changes (e.g. Active -> Resolved or new timestamp) trigger MATERIAL_UPDATE."""
    clock = FrozenClock(1700000000.0)
    mgr = AlertDedupeManager(clock=clock)
    guild_id = 1001
    ch_id = 101
    alert_type = "go_alert"
    entity_id = "lakeshore-west-delay"

    # Step 1: Initial alert
    hash1 = compute_content_hash("Lakeshore West: 25 min delay due to signal failure")
    st1 = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, entity_id, hash1)
    assert st1 == AlertLifecycleStatus.NEW_EVENT
    await mgr.record_delivery(tmp_db, guild_id, alert_type, ch_id, entity_id, hash1)

    # Step 2: Identical duplicate is skipped
    st2 = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, entity_id, hash1)
    assert st2 == AlertLifecycleStatus.IDENTICAL_DUPLICATE

    # Step 3: Material update (e.g. delay cleared / all-clear)
    hash2 = compute_content_hash("Lakeshore West: ALL CLEAR. Regular service has resumed.")
    st3 = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, entity_id, hash2)
    assert st3 == AlertLifecycleStatus.MATERIAL_UPDATE

    # Record updated delivery
    await mgr.record_delivery(tmp_db, guild_id, alert_type, ch_id, entity_id, hash2)

    # Step 4: Now hash2 is duplicate, and a subsequent change (hash3) triggers MATERIAL_UPDATE
    assert (
        await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, entity_id, hash2)
        == AlertLifecycleStatus.IDENTICAL_DUPLICATE
    )
    hash3 = compute_content_hash("Lakeshore West: Secondary track work scheduled tonight.")
    assert (
        await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, entity_id, hash3)
        == AlertLifecycleStatus.MATERIAL_UPDATE
    )


# ---------------------------------------------------------------------------
# 2. Bootstrap Suppression & Backfill Safeguards
# ---------------------------------------------------------------------------


async def test_bootstrap_checkpointing_prevents_historical_dump(tmp_db: Database):
    """On fresh install, existing historical items are checkpointed without sending alerts."""
    clock = FrozenClock(1700000000.0)
    mgr = AlertDedupeManager(clock=clock)
    guild_id = 1001
    ch_id = 101
    alert_type = "ttc_alert"

    # Before bootstrap
    assert not await mgr.is_bootstrapped(tmp_db, guild_id, alert_type, ch_id)

    # 10 historical alerts exist on the external endpoint
    historical_items = [f"status-{i}" for i in range(1, 11)]
    for item in historical_items:
        h = compute_content_hash(f"Historical alert body {item}")
        await mgr.checkpoint_bootstrap_item(tmp_db, guild_id, alert_type, ch_id, item, h)

    await mgr.mark_bootstrapped(tmp_db, guild_id, alert_type, ch_id)
    assert await mgr.is_bootstrapped(tmp_db, guild_id, alert_type, ch_id)

    # Verify none of the historical items trigger NEW_EVENT on subsequent checks
    for item in historical_items:
        h = compute_content_hash(f"Historical alert body {item}")
        status = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, item, h)
        assert status == AlertLifecycleStatus.IDENTICAL_DUPLICATE

    # New event arriving post-bootstrap is recognized
    new_h = compute_content_hash("Brand new incident after bot started")
    status = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, "status-new", new_h)
    assert status == AlertLifecycleStatus.NEW_EVENT


# ---------------------------------------------------------------------------
# 3. Bitcoin Address Validation, Capping & Privacy Masking
# ---------------------------------------------------------------------------


def test_btc_address_validation():
    """Validates Base58, Bech32, Bech32m, and rejects invalid strings."""
    # Valid Mainnet & Testnet addresses
    valid, _ = validate_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert valid is True
    valid, _ = validate_btc_address("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")
    assert valid is True
    valid, _ = validate_btc_address("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    assert valid is True
    valid, _ = validate_btc_address("bc1p5d7rx65aze2943edehfrimjxyvk474q95zx8o5gm6a9qzv9wuvtq94pnxw")
    assert valid is True
    valid, _ = validate_btc_address("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", chain="testnet")
    assert valid is True
    valid, _ = validate_btc_address("bc1qsyntheticdiwatch000000000000000")
    assert valid is True

    # Invalid addresses
    valid, _ = validate_btc_address("")
    assert valid is False
    valid, _ = validate_btc_address("0x71C7656EC7ab88b098defB751B7401B5f6d8976F")
    assert valid is False
    valid, _ = validate_btc_address("bc1q")
    assert valid is False
    valid, _ = validate_btc_address("bc1q" + "a" * 100)
    assert valid is False
    valid, _ = validate_btc_address("1InvalidCharacters!@#$%^")
    assert valid is False
    valid, _ = validate_btc_address("bc1q spaces in between")
    assert valid is False


def test_btc_address_sanitization_and_hard_cap():
    """Sanitizes address list, filters invalid, deduplicates, and caps at max 50."""
    raw_list = [
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
        "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",  # Duplicate
        "0xinvalidethaddress",  # Invalid
        "bad_addr",  # Invalid
    ]
    sanitized = sanitize_btc_addresses(raw_list, max_addresses=50)
    assert len(sanitized) == 2
    assert "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" in sanitized
    assert "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq" in sanitized

    # Test hard cap at 50
    over_limit = [f"bc1qtestaddressnumber{i:04d}0000000000000000" for i in range(100)]
    capped = sanitize_btc_addresses(over_limit, max_addresses=50)
    assert len(capped) == 50


def test_btc_privacy_masking():
    """Masks Bitcoin addresses to preserve privacy in embeds and logs."""
    addr = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    masked = mask_btc_address(addr)
    assert masked.startswith("bc1qar")
    assert masked.endswith("wf5mdq")
    assert "…" in masked
    assert addr not in masked
    assert len(masked) < len(addr)

    # Short address handling
    assert mask_btc_address("short") == "short"


# ---------------------------------------------------------------------------
# 4. Financial Attribution & Legal Disclaimers
# ---------------------------------------------------------------------------


def test_financial_disclaimer_and_attribution_footer():
    """All financial alert footers assert informational nature and source attribution."""
    assert "informational purposes only" in FINANCIAL_DISCLAIMER
    assert "Not financial, trading, or investment advice" in FINANCIAL_DISCLAIMER

    footer = format_financial_embed_footer(
        source="Yahoo Finance & alternative.me",
        updated_at="2026-03-30",
        extra="Market Volatility Trigger",
    )
    assert "Source: Yahoo Finance & alternative.me" in footer
    assert "Updated: 2026-03-30" in footer
    assert "Market Volatility Trigger" in footer


# ---------------------------------------------------------------------------
# 5. Named Timezones & DST Transitions (No Backlog Flood)
# ---------------------------------------------------------------------------


def test_named_timezones_and_invalid_fallback():
    """Resolves local date string across timezones with safe fallback."""
    ts = 1760000000.0  # Deterministic timestamp

    date_toronto = get_local_date_str("America/Toronto", now_unix=ts)
    assert len(date_toronto) == 10 and date_toronto.count("-") == 2

    date_utc = get_local_date_str("UTC", now_unix=ts)
    assert len(date_utc) == 10

    # Invalid timezone falls back safely without raising ZoneInfoNotFoundError
    date_fallback = get_local_date_str("Invalid/NonExistent_Zone", now_unix=ts)
    assert len(date_fallback) == 10


def test_market_daily_report_window_and_dst_eligibility():
    """Verifies eligibility window for daily market report with DST awareness."""
    # Market close is typically 16:00 ET. Allowed posting window: 16:30 - 23:59.
    # 2026-03-09 21:00:00 UTC = 17:00:00 EDT (Toronto is UTC-4 after DST spring-forward)
    edt_ts = 1773090000.0  # March 2026, 17:00 EDT
    assert is_eligible_daily_report_window("16:30", "America/Toronto", now_unix=edt_ts)

    # 15:00 EDT is before market close (not eligible)
    early_ts = edt_ts - (2 * 3600)  # 15:00 EDT
    assert not is_eligible_daily_report_window("16:30", "America/Toronto", now_unix=early_ts)


async def test_downtime_does_not_flood_backlogged_daily_reports(tmp_db: Database):
    """After days of bot downtime, daily report posts only for today, not flooded backfill."""
    clock = FrozenClock(1700000000.0)
    mgr = AlertDedupeManager(clock=clock)
    guild_id = 1001
    ch_id = 101
    alert_type = "market_daily_report"

    # Last report was 5 days ago
    last_date = "2026-03-25"
    today_date = "2026-03-30"

    # Simulate recorded delivery on last_date
    await mgr.record_delivery(tmp_db, guild_id, alert_type, ch_id, last_date, last_date)

    # Today is a new day
    status = await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, today_date, today_date)
    assert status == AlertLifecycleStatus.NEW_EVENT

    # Once recorded for today, calling again on the same day is deduped
    await mgr.record_delivery(tmp_db, guild_id, alert_type, ch_id, today_date, today_date)
    assert (
        await mgr.check_alert(tmp_db, guild_id, alert_type, ch_id, today_date, today_date)
        == AlertLifecycleStatus.IDENTICAL_DUPLICATE
    )


# ---------------------------------------------------------------------------
# 6. Channel Resolution, Permissions & Discord 429 Handling
# ---------------------------------------------------------------------------


class MockPerms:
    def __init__(self, send_messages: bool = True, embed_links: bool = True):
        self.send_messages = send_messages
        self.embed_links = embed_links


async def test_safe_send_alert_with_full_permissions():
    """Sends rich embed when send_messages and embed_links are granted."""
    channel = FakeTextChannel(channel_id=101, name="alerts")
    channel.permissions_for = lambda member: MockPerms(send_messages=True, embed_links=True)  # type: ignore[attr-defined]

    bot = SimpleNamespace(
        get_channel=lambda cid: channel if cid == 101 else None,
        fetch_channel=None,
        user=SimpleNamespace(id=999),
    )
    embed = discord.Embed(title="TTC Alert", description="Major delay")

    ok, status = await safe_send_alert(bot, 101, embed=embed, fallback_text="Fallback text")
    assert ok is True
    assert status == "delivered"
    assert len(channel.sent) == 1
    assert channel.sent[0].embed is not None


async def test_safe_send_alert_falls_back_to_plain_text_without_embed_links():
    """Falls back to plain text when channel lacks embed_links permission."""
    channel = FakeTextChannel(channel_id=101, name="alerts")
    channel.permissions_for = lambda member: MockPerms(send_messages=True, embed_links=False)  # type: ignore[attr-defined]

    bot = SimpleNamespace(
        get_channel=lambda cid: channel if cid == 101 else None,
        fetch_channel=None,
        user=SimpleNamespace(id=999),
    )
    embed = discord.Embed(title="TTC Alert", description="Major delay")

    ok, status = await safe_send_alert(bot, 101, embed=embed, fallback_text="Fallback TTC delay text")
    assert ok is True
    assert status == "delivered"
    assert len(channel.sent) == 1
    assert channel.sent[0].embed is None
    assert channel.sent[0].content == "Fallback TTC delay text"


async def test_safe_send_alert_handles_permission_denied():
    """Returns permission_denied cleanly when send_messages is False without raising."""
    channel = FakeTextChannel(channel_id=101, name="alerts")
    channel.permissions_for = lambda member: MockPerms(send_messages=False, embed_links=False)  # type: ignore[attr-defined]

    bot = SimpleNamespace(
        get_channel=lambda cid: channel if cid == 101 else None,
        fetch_channel=None,
        user=SimpleNamespace(id=999),
    )

    ok, status = await safe_send_alert(bot, 101, fallback_text="Denied text")
    assert ok is False
    assert status == "permission_denied"
    assert len(channel.sent) == 0


async def test_safe_send_alert_handles_missing_channel():
    """Handles deleted or inaccessible channel gracefully."""
    bot = SimpleNamespace(
        get_channel=lambda cid: None,
        fetch_channel=MagicMock(side_effect=discord.NotFound(MagicMock(status=404), "Channel not found")),
        user=SimpleNamespace(id=999),
    )

    ok, status = await safe_send_alert(bot, 999999, fallback_text="Some text")
    assert ok is False
    assert status == "channel_not_found"


async def test_safe_send_alert_handles_rate_limiting():
    """Returns rate_limited status on Discord HTTP 429 without unhandled exception."""
    channel = FakeTextChannel(channel_id=101, name="alerts")

    async def _failing_send(*args: Any, **kwargs: Any) -> Any:
        resp = MagicMock(status=429)
        raise discord.HTTPException(resp, "You are being rate limited.")

    channel.send = _failing_send  # type: ignore[method-assign]

    bot = SimpleNamespace(
        get_channel=lambda cid: channel if cid == 101 else None,
        fetch_channel=None,
        user=SimpleNamespace(id=999),
    )

    ok, status = await safe_send_alert(bot, 101, fallback_text="Rate limit test")
    assert ok is False
    assert status == "rate_limited"
