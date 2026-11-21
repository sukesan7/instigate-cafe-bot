"""Unified alert deduplication, lifecycle management, and safe delivery contracts.

Provides:
- Alert lifecycle evaluation (NEW_EVENT, MATERIAL_UPDATE, IDENTICAL_DUPLICATE, BOOTSTRAP_CHECKPOINT).
- Per-guild and per-channel destination isolation using SQLite alert_delivery_dedupes.
- Bootstrap and downtime catch-up guards to prevent historical flooding.
- Bitcoin address format validation, unbounded set capping, and privacy masking.
- Financial feed source attribution, timestamping, and legal/informational disclaimers.
- Safe Discord channel resolution and destination permission verification (send_messages, embed_links).
- Named time zone and DST transition helpers for scheduled reports.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

log = logging.getLogger(__name__)

# Standard disclaimer for all market volatility and report feeds
FINANCIAL_DISCLAIMER = (
    "Market data is for informational purposes only (delayed or best-effort). "
    "Not financial, trading, or investment advice."
)

MAX_WATCHED_BTC_ADDRESSES = 50

_BTC_PREFIXES = ("bc1", "1", "3", "tb1", "m", "n", "2")


# ---------------------------------------------------------------------------
# Bitcoin Address Validation and Privacy Protection
# ---------------------------------------------------------------------------


def validate_btc_address(address: str, *, chain: str = "mainnet") -> tuple[bool, str]:
    """Validate a Bitcoin address against standard cryptographic address formats.

    Returns (is_valid: bool, reason: str).
    """
    raw = (address or "").strip()
    if not raw:
        return False, "Address must not be empty."
    if len(raw) < 14 or len(raw) > 90:
        return False, f"Address length ({len(raw)}) outside valid Bitcoin range [14, 90]."

    chain_norm = chain.strip().lower()
    if chain_norm in ("testnet", "signet", "regtest"):
        if raw.startswith(("tb1", "m", "n", "2")) and re.match(r"^[A-Za-z0-9]+$", raw):
            return True, "Valid testnet Bitcoin address."
        return False, f"Invalid testnet Bitcoin address format: {mask_btc_address(raw)!r}."

    if not any(raw.startswith(prefix) for prefix in _BTC_PREFIXES):
        return False, f"Address {mask_btc_address(raw)!r} does not match any valid Bitcoin address format."
    if not re.match(r"^[A-Za-z0-9]+$", raw):
        return False, f"Address {mask_btc_address(raw)!r} contains invalid characters."

    return True, "Valid Bitcoin address."


def mask_btc_address(addr: str) -> str:
    """Mask a BTC address for privacy in logs and user-facing notifications."""
    a = (addr or "").strip()
    if len(a) <= 12:
        return a
    return f"{a[:6]}…{a[-6:]}"


def sanitize_btc_addresses(
    addresses: Sequence[str] | str | None,
    *,
    max_addresses: int = MAX_WATCHED_BTC_ADDRESSES,
    chain: str = "mainnet",
) -> list[str]:
    """Parse, validate, deduplicate, and bound a list of Bitcoin watch addresses.

    Rejects invalid formats, enforces the maximum address cap, and guards privacy.
    """
    if addresses is None:
        return []
    if isinstance(addresses, str):
        candidates = [t.strip() for t in addresses.split(",")]
    else:
        candidates = [str(x).strip() for x in addresses]

    cleaned: list[str] = []
    seen: set[str] = set()

    for raw in candidates:
        if not raw:
            continue
        valid, reason = validate_btc_address(raw, chain=chain)
        if not valid:
            log.warning("Skipping invalid Bitcoin watch address: %s", reason)
            continue
        if raw in seen:
            continue
        seen.add(raw)
        cleaned.append(raw)
        if len(cleaned) >= max_addresses:
            log.warning(
                "Watched Bitcoin address set exceeded safety bound of %d; remaining addresses ignored",
                max_addresses,
            )
            break

    return cleaned


# ---------------------------------------------------------------------------
# Content Hashing and Alert Lifecycle
# ---------------------------------------------------------------------------


def compute_content_hash(content: str) -> str:
    """Compute a deterministic 16-character hex hash of content for material change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class AlertLifecycleStatus(StrEnum):
    """Lifecycle classification for an inbound alert candidate."""

    NEW_EVENT = "new_event"
    MATERIAL_UPDATE = "material_update"
    IDENTICAL_DUPLICATE = "identical_duplicate"
    BOOTSTRAP_CHECKPOINT = "bootstrap_checkpoint"


@dataclass(frozen=True)
class AlertDestination:
    """Channel destination for alert delivery."""

    guild_id: int
    channel_id: int
    filter_config: dict[str, Any]


class AlertDedupeManager:
    """Manages alert deduplication, material updates, bootstrap checkpoints, and outbox delivery."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock()) if self._clock is not None else int(datetime.now(UTC).timestamp())

    async def is_bootstrapped(
        self,
        db: Any,
        guild_id: int,
        alert_type: str,
        channel_id: int,
    ) -> bool:
        """Return True if initial bootstrap has been performed for this guild channel."""
        key = f"{channel_id}:__bootstrap__"
        row = await db.fetchone(
            "SELECT 1 FROM alert_delivery_dedupes WHERE guild_id = ? AND alert_type = ? AND dedupe_key = ? LIMIT 1",
            (guild_id, alert_type, key),
        )
        return row is not None

    async def mark_bootstrapped(
        self,
        db: Any,
        guild_id: int,
        alert_type: str,
        channel_id: int,
    ) -> None:
        """Mark a guild channel as having completed initial bootstrap."""
        key = f"{channel_id}:__bootstrap__"
        await db.execute(
            "INSERT OR REPLACE INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at) VALUES (?, ?, ?, ?)",
            (guild_id, alert_type, key, self._now()),
        )

    async def check_alert(
        self,
        db: Any,
        guild_id: int,
        alert_type: str,
        channel_id: int,
        entity_id: str,
        content_hash: str,
    ) -> AlertLifecycleStatus:
        """Evaluate whether an alert is a new event, material update, or identical duplicate."""
        exact_key = f"{channel_id}:{entity_id}:{content_hash}"
        # 1. Exact match -> identical duplicate
        exact_row = await db.fetchone(
            "SELECT 1 FROM alert_delivery_dedupes WHERE guild_id = ? AND alert_type = ? AND dedupe_key = ? LIMIT 1",
            (guild_id, alert_type, exact_key),
        )
        if exact_row is not None:
            return AlertLifecycleStatus.IDENTICAL_DUPLICATE

        # 2. Check if prior version exists for this entity in this channel
        prefix = f"{channel_id}:{entity_id}:%"
        prior_row = await db.fetchone(
            "SELECT dedupe_key FROM alert_delivery_dedupes WHERE guild_id = ? AND alert_type = ? AND dedupe_key LIKE ? LIMIT 1",
            (guild_id, alert_type, prefix),
        )
        if prior_row is not None:
            return AlertLifecycleStatus.MATERIAL_UPDATE

        return AlertLifecycleStatus.NEW_EVENT

    async def record_delivery(
        self,
        db: Any,
        guild_id: int,
        alert_type: str,
        channel_id: int,
        entity_id: str,
        content_hash: str,
    ) -> None:
        """Persist alert delivery checkpoint into alert_delivery_dedupes."""
        dedupe_key = f"{channel_id}:{entity_id}:{content_hash}"
        await db.execute(
            "INSERT OR REPLACE INTO alert_delivery_dedupes (guild_id, alert_type, dedupe_key, delivered_at) VALUES (?, ?, ?, ?)",
            (guild_id, alert_type, dedupe_key, self._now()),
        )

    async def checkpoint_bootstrap_item(
        self,
        db: Any,
        guild_id: int,
        alert_type: str,
        channel_id: int,
        entity_id: str,
        content_hash: str,
    ) -> None:
        """Record an item as seen during bootstrap without posting."""
        await self.record_delivery(db, guild_id, alert_type, channel_id, entity_id, content_hash)


# ---------------------------------------------------------------------------
# Destination Resolution and Permission Verification
# ---------------------------------------------------------------------------


async def resolve_channel_safe(bot: discord.Client, channel_id: int) -> discord.abc.Messageable | None:
    """Resolve a Discord channel safely by ID via cache or fetch."""
    if not channel_id:
        return None
    ch = bot.get_channel(channel_id)
    if ch is not None:
        return ch  # type: ignore[return-value]
    try:
        return await bot.fetch_channel(channel_id)  # type: ignore[return-value]
    except (discord.DiscordException, LookupError, ValueError):
        return None


async def safe_send_alert(
    bot: discord.Client,
    channel_id: int,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    fallback_text: str | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> tuple[bool, str]:
    """Safely send an alert to a Discord channel with permission checks and error handling.

    Defaults to `discord.AllowedMentions.none()` unless an explicit `allowed_mentions`
    exception (such as an admin-configured alert role) is passed.
    Returns (success: bool, status: str) where status indicates the outcome.
    """
    channel = await resolve_channel_safe(bot, channel_id)
    if channel is None:
        return False, "channel_not_found"

    eff_mentions = allowed_mentions if allowed_mentions is not None else discord.AllowedMentions.none()
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None) or getattr(bot, "user", None)
    if me is not None and hasattr(channel, "permissions_for"):
        perms = channel.permissions_for(me)
        if not getattr(perms, "send_messages", True):
            return False, "permission_denied"
        if embed is not None and not getattr(perms, "embed_links", True):
            orig_embed = embed
            embed = None
            if not content:
                content = fallback_text or (orig_embed.description if orig_embed else "")

    try:
        if embed is not None:
            await channel.send(content=content, embed=embed, allowed_mentions=eff_mentions)  # type: ignore[call-arg]
        else:
            await channel.send(
                content=content or fallback_text or "",
                allowed_mentions=eff_mentions,
            )  # type: ignore[call-arg]
        return True, "delivered"
    except discord.Forbidden:
        if embed is not None and fallback_text:
            try:
                await channel.send(content=fallback_text, allowed_mentions=eff_mentions)  # type: ignore[call-arg]
                return True, "delivered_plain"
            except discord.DiscordException:
                return False, "permission_denied"
        return False, "permission_denied"
    except discord.HTTPException as exc:
        if exc.status == 429:
            return False, "rate_limited"
        return False, f"http_{exc.status}"
    except (RuntimeError, OSError, ValueError) as exc:
        return False, f"error_{type(exc).__name__}"


# ---------------------------------------------------------------------------
# Timezone, Scheduling, and Financial Attribution Helpers
# ---------------------------------------------------------------------------


def get_local_date_str(tz_name: str, *, now_unix: float | None = None) -> str:
    """Format the current local date YYYY-MM-DD in a named time zone."""
    now_dt = datetime.fromtimestamp(now_unix, tz=UTC) if now_unix is not None else datetime.now(UTC)
    try:
        tz = ZoneInfo(tz_name)
        return now_dt.astimezone(tz).strftime("%Y-%m-%d")
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return now_dt.strftime("%Y-%m-%d")


def is_eligible_daily_report_window(
    scheduled_time_str: str,
    tz_name: str,
    *,
    now_unix: float | None = None,
) -> bool:
    """Return True if the current local time in named timezone is at or past the scheduled report time."""
    now_dt = datetime.fromtimestamp(now_unix, tz=UTC) if now_unix is not None else datetime.now(UTC)
    try:
        tz = ZoneInfo(tz_name)
        local_now = now_dt.astimezone(tz)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        local_now = now_dt

    try:
        parts = [int(p) for p in scheduled_time_str.strip().split(":")]
        target_hour = parts[0]
        target_min = parts[1] if len(parts) > 1 else 0
    except (ValueError, IndexError):
        target_hour, target_min = 16, 30  # Default 4:30 PM

    current_minutes = local_now.hour * 60 + local_now.minute
    target_minutes = target_hour * 60 + target_min
    return current_minutes >= target_minutes


def format_financial_embed_footer(
    source: str,
    updated_at: str | float | None = None,
    *,
    extra: str | None = None,
) -> str:
    """Build a standardized footer line showing source attribution and last-updated time."""
    parts = [f"Source: {source}"]
    if updated_at is not None:
        if isinstance(updated_at, (int, float)):
            dt = datetime.fromtimestamp(float(updated_at), tz=UTC)
            parts.append(f"Updated: {dt.strftime('%H:%M:%S UTC')}")
        else:
            parts.append(f"Updated: {updated_at}")
    if extra:
        parts.append(extra)
    return " • ".join(parts)
