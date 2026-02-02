from __future__ import annotations

import discord

from icbot.app import ic
from icbot.tasks.market_daily_report import (
    build_market_report_embed,
    resolve_market_report_tickers,
)


def _now_stamp(tz_name: str) -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


@ic.command(name="report", description="Generate the market report right now.")
async def report_cmd(interaction: discord.Interaction):
    # Always public (not ephemeral) so everyone sees the same data.
    await interaction.response.defer(thinking=True)

    bot = interaction.client
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings/db).")
        return

    tz_name = str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto")
    tickers = resolve_market_report_tickers(settings)

    embed = await build_market_report_embed(
        settings=settings,
        db=db,
        tz_name=tz_name,
        tickers=tickers,
        title="📊 Instigate Stock Watcher Report",
        subtitle=f"{_now_stamp(tz_name)} ({tz_name})",
        debug=bool(getattr(settings, "market_daily_report_debug", False)),
    )

    await interaction.followup.send(embed=embed)
