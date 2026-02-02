import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from icbot.config import Settings, MAX_SNOWFLAKE
from icbot.db import Database
from icbot.app import ic
from icbot.permissions import is_admin_or_allowed

from icbot.tasks.ttc_alerts import poll_and_post_ttc
from icbot.tasks.job_alerts import poll_and_post_jobs
from icbot.tasks.jobs_workday_alerts import poll_and_post_workday_jobs
from icbot.tasks.market_watch import poll_and_post_market_watch
from icbot.tasks.market_daily_report import post_daily_market_report
from icbot.tasks.btc_watch import poll_and_post_btc_watch

# Import features (registers commands onto /ic)
from icbot.features import stock  # noqa: F401
from icbot.features import blackjack  # noqa: F401
from icbot.features import lc_leaderboard  # noqa: F401
from icbot.features import sports  # noqa: F401
from icbot.features import jobs  # noqa: F401
from icbot.features import report  # noqa: F401
from icbot.features.lc_leaderboard import ActivityTracker


load_dotenv(override=True)
settings = Settings.from_env()
db = Database(settings.db_path)

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True  # needed for message-count leaderboard tracking

bot = commands.Bot(command_prefix="!", intents=intents)
activity_tracker = ActivityTracker(db)


def _fmt_csv(items: list[str], *, limit: int = 12) -> str:
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        return "(default)"
    if len(items) <= limit:
        return ",".join(items)
    head = ",".join(items[:limit])
    return f"{head},...(+{len(items) - limit})"


async def ic_channel_gate(interaction: discord.Interaction) -> bool:
    """
    Gate ONLY /ic slash commands for non-admin users:
      - Admin/allowed roles can use /ic anywhere
      - Regular users can only use /ic in USER_COMMAND_CHANNEL_ID (if set)
    """
    # Allow in DMs
    if not interaction.guild:
        return True

    # Only gate the /ic command group
    cmd = getattr(interaction, "command", None)
    qname = getattr(cmd, "qualified_name", "") or ""
    if not qname.startswith("ic"):
        return True

    # Admin/allowed roles can run anywhere
    if is_admin_or_allowed(interaction, settings):
        return True

    allowed_ch = int(getattr(settings, "user_command_channel_id", 0) or 0)
    if allowed_ch and interaction.channel_id != allowed_ch:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Use bot commands in <#{allowed_ch}> to keep the server clean.",
                ephemeral=True,
            )
        return False

    return True


@bot.tree.interaction_check
async def _ic_tree_interaction_check(interaction: discord.Interaction) -> bool:
    return await ic_channel_gate(interaction)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Silence check failures (we already responded in the gate)
    if isinstance(error, app_commands.CheckFailure):
        return
    raise error


async def _ttc_loop() -> None:
    """Background poller that posts TTC alerts into TTC_ALERTS_CHANNEL_ID."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await poll_and_post_ttc(bot)
        except Exception as e:
            print(f"[TTC] loop error: {e!r}")
        await asyncio.sleep(max(30, int(getattr(settings, "ttc_poll_seconds", 120) or 120)))


async def _jobs_loop() -> None:
    """Background poller that posts job alerts into JOB_ALERTS_CHANNEL_ID."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await poll_and_post_jobs(bot)
        except Exception as e:
            print(f"[JOBS] loop error: {e!r}")
        await asyncio.sleep(max(60, int(getattr(settings, "jobs_poll_seconds", 600) or 600)))


async def _workday_loop() -> None:
    """Background poller for Workday sites (posts into the same jobs channel)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await poll_and_post_workday_jobs(bot)
        except Exception as e:
            print(f"[WORKDAY] loop error: {e!r}")
        await asyncio.sleep(
            max(
                60,
                int(getattr(settings, "workday_poll_seconds", 0) or getattr(settings, "jobs_poll_seconds", 600) or 600),
            )
        )


async def _market_loop() -> None:
    """Background poller that posts real-market volatility alerts into STOCK_CHANNEL_ID (or MARKET_WATCH_CHANNEL_ID)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await poll_and_post_market_watch(bot)
        except Exception as e:
            print(f"[MARKET] loop error: {e!r}")
        await asyncio.sleep(max(10, int(getattr(settings, "market_watch_poll_seconds", 300) or 300)))


def _parse_hhmm(raw: str, default_h: int = 16, default_m: int = 30) -> tuple[int, int]:
    raw = (raw or "").strip()
    if not raw:
        return default_h, default_m
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
            return int(h), int(m)
        # allow "1630"
        if len(raw) == 4 and raw.isdigit():
            return int(raw[:2]), int(raw[2:])
    except Exception:
        pass
    return default_h, default_m


def _seconds_until_next(hh: int, mm: int, tz_name: str) -> float:
    """Seconds until next (hh:mm) in the given timezone."""
    from datetime import datetime, timedelta

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
    except Exception:
        now = datetime.utcnow()
        tz = None

    if tz is not None:
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    else:
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if now >= target:
        target = target + timedelta(days=1)

    return max(1.0, (target - now).total_seconds())


async def _daily_market_report_loop() -> None:
    """Posts a daily market recap at a scheduled time (e.g. 16:30 America/Toronto)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            enabled = bool(getattr(settings, "market_daily_report_enabled", False))
            if not enabled:
                await asyncio.sleep(300)
                continue

            hh, mm = _parse_hhmm(str(getattr(settings, "market_daily_report_time", "16:30") or "16:30"))
            tz_name = str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto")

            # Sleep until the next scheduled time
            await asyncio.sleep(_seconds_until_next(hh, mm, tz_name))

            # Post (task internally dedupes per local date)
            await post_daily_market_report(bot)

            # Small backoff to avoid immediate re-run on edge cases
            await asyncio.sleep(60)
        except Exception as e:
            print(f"[DAILY] loop error: {e!r}")
            await asyncio.sleep(120)


async def _btc_watch_loop() -> None:
    """Polls watched BTC addresses and posts alerts on movement."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await poll_and_post_btc_watch(bot)
        except Exception as e:
            print(f"[BTC] loop error: {e!r}")
        await asyncio.sleep(max(30, int(getattr(settings, "btc_watch_poll_seconds", 300) or 300)))


@bot.event
async def on_ready():
    await db.init()

    # Make shared singletons available to feature modules via interaction.client
    bot.settings = settings  # type: ignore[attr-defined]
    bot.db = db  # type: ignore[attr-defined]
    bot.activity_tracker = activity_tracker  # type: ignore[attr-defined]

    # ---- Startup config logs (helps debug .env issues) ----
    try:
        mw_enabled = bool(getattr(settings, "market_watch_enabled", False))
        mw_poll = int(getattr(settings, "market_watch_poll_seconds", 0) or 0)
        mw_channel = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
            getattr(settings, "stock_channel_id", 0) or 0
        )
        mw_vix_level = float(getattr(settings, "market_watch_vix_level", 0.0) or 0.0)
        mw_index_thr = float(getattr(settings, "market_watch_index_move_pct", 0.0) or 0.0)
        mw_stock_thr = float(getattr(settings, "market_watch_stock_move_pct", 0.0) or 0.0)
        mw_cooldown = int(getattr(settings, "market_watch_cooldown_minutes", 0) or 0)
        mw_tickers = getattr(settings, "market_watch_tickers", None) or []
        print(
            "[MARKET] config: "
            f"enabled={mw_enabled} "
            f"channel_id={mw_channel} "
            f"poll={mw_poll}s "
            f"vix_level>={mw_vix_level:g} "
            f"idx_move>={mw_index_thr:g}% "
            f"stk_move>={mw_stock_thr:g}% "
            f"cooldown={mw_cooldown}m "
            f"tickers={_fmt_csv(list(mw_tickers))} "
            f"db={settings.db_path}"
        )
        if mw_enabled and not mw_channel:
            print("[MARKET] WARNING: enabled but no channel id set (MARKET_WATCH_CHANNEL_ID or STOCK_CHANNEL_ID).")
    except Exception as e:
        print(f"[MARKET] config log error: {e!r}")

    # Jobs config log (Greenhouse/Lever/Workday)
    try:
        j_poll = int(getattr(settings, "jobs_poll_seconds", 0) or 0)
        gh = list(getattr(settings, "greenhouse_boards", None) or getattr(settings, "greenhouse_board_tokens", None) or [])
        lv = list(getattr(settings, "lever_sites", None) or getattr(settings, "lever_site_tokens", None) or [])
        wd = list(getattr(settings, "workday_sites", None) or [])

        def _preview(items: list[str], max_n: int = 12) -> str:
            items = [str(x) for x in items if str(x).strip() and str(x).strip() not in ("\\", "\\\\")]
            if not items:
                return "(none)"
            head = ",".join(items[:max_n])
            extra = len(items) - max_n
            return f"{head},...(+{extra})" if extra > 0 else head

        print(
            "[JOBS] config: "
            f"channel_id={int(getattr(settings, 'job_alerts_channel_id', 0) or 0)} "
            f"poll={j_poll}s "
            f"greenhouse={_preview(gh)} "
            f"lever={_preview(lv)} "
            f"workday={_preview(wd)}"
        )
    except Exception as e:
        print(f"[JOBS] config log error: {e!r}")

    # Register the /ic root group
    if getattr(settings, "guild_id_invalid", False):
        raw = getattr(settings, "guild_id_raw", "")
        raw_val = None
        try:
            raw_val = int(raw)
        except Exception:
            raw_val = raw
        print(
            f"[BOOT] ERROR: GUILD_ID parsed as {raw_val} (> {MAX_SNOWFLAKE}). "
            f"This usually means your .env isn't being used (system env overrides) or the value is corrupted. "
            f"Raw env GUILD_ID='{raw}'. Falling back to GLOBAL command sync."
        )

    if settings.guild_id and not getattr(settings, "guild_id_invalid", False):
        guild = discord.Object(id=settings.guild_id)
        if settings.force_guild_resync:
            bot.tree.clear_commands(guild=guild)
        bot.tree.add_command(ic, guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to guild {settings.guild_id}.")
    else:
        bot.tree.add_command(ic)
        await bot.tree.sync()
        print("Synced commands globally (can take a while to appear).")

    # Start TTC poller once
    if not getattr(bot, "_ttc_started", False):
        bot._ttc_started = True
        if getattr(settings, "ttc_alerts_channel_id", 0):
            bot.loop.create_task(_ttc_loop())

    # Start Jobs poller once
    if not getattr(bot, "_jobs_started", False):
        bot._jobs_started = True
        if getattr(settings, "job_alerts_channel_id", 0):
            bot.loop.create_task(_jobs_loop())

    # Start Workday poller once (separate task; posts into same jobs channel)
    if not getattr(bot, "_workday_started", False):
        bot._workday_started = True
        if getattr(settings, "job_alerts_channel_id", 0) and (getattr(settings, "workday_sites", None) or []):
            bot.loop.create_task(_workday_loop())

    # Start Market watch poller once
    if not getattr(bot, "_market_started", False):
        bot._market_started = True
        mw_enabled = bool(getattr(settings, "market_watch_enabled", False))
        mw_channel = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
            getattr(settings, "stock_channel_id", 0) or 0
        )

        if mw_enabled and mw_channel:
            bot.loop.create_task(_market_loop())
        elif mw_enabled and not mw_channel:
            print("[MARKET] enabled=true but no channel_id configured (set MARKET_WATCH_CHANNEL_ID or STOCK_CHANNEL_ID)")

    # Start Daily market report loop once
    if not getattr(bot, "_daily_report_started", False):
        bot._daily_report_started = True
        dr_enabled = bool(getattr(settings, "market_daily_report_enabled", False))
        if dr_enabled:
            bot.loop.create_task(_daily_market_report_loop())

    # Start BTC address watch loop once
    if not getattr(bot, "_btc_watch_started", False):
        bot._btc_watch_started = True
        bw_enabled = bool(getattr(settings, "btc_watch_enabled", False))
        if bw_enabled:
            bot.loop.create_task(_btc_watch_loop())

    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # Track activity for leaderboard
    try:
        await activity_tracker.on_message(message)
    finally:
        await bot.process_commands(message)


def main():
    if not settings.token:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(settings.token)


if __name__ == "__main__":
    main()
