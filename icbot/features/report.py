from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.registry import guard_feature
from icbot.tasks.market_daily_report import (
    build_market_report_embed,
    resolve_market_report_tickers,
)


def _now_stamp(tz_name: str, *, now_unix: float | None = None) -> str:
    now_dt = datetime.fromtimestamp(now_unix, tz=UTC) if now_unix is not None else datetime.now(UTC)
    try:
        return now_dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return now_dt.strftime("%Y-%m-%d %H:%M UTC")


@ic.command(name="report", description="Market report.")
async def report_cmd(interaction: discord.Interaction):
    if not await guard_feature(interaction, "market"):
        return
    # Always public (not ephemeral) so everyone sees the same data.
    await interaction.response.defer(thinking=True)

    bot = interaction.client
    base_settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if base_settings is None or db is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings/db).")
        return

    settings = await resolve_guild_settings(bot, interaction)
    container = get_container(bot)

    tz_name = str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto")
    tickers = resolve_market_report_tickers(settings)

    embed = await build_market_report_embed(
        settings=settings,
        db=db,
        tz_name=tz_name,
        tickers=tickers,
        title=(
            f"📊 {settings.embed_footer_text} Stock Watcher Report"
            if settings.embed_footer_text != "Instigate Cafe"
            else "📊 Instigate Cafe's Stock Watcher Report"
        ),
        subtitle=f"{_now_stamp(tz_name, now_unix=container.clock())} ({tz_name})",
        debug=bool(getattr(settings, "market_daily_report_debug", False)),
        services=container,
    )

    await interaction.followup.send(embed=embed)
