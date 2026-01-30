import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from icbot.config import Settings
from icbot.db import Database
from icbot.app import ic
from icbot.permissions import is_admin_or_allowed

from icbot.tasks.ttc_alerts import poll_and_post_ttc
from icbot.tasks.job_alerts import poll_and_post_jobs  # <-- make sure this path matches your file

# Import features (registers commands onto /ic)
from icbot.features import stock  # noqa: F401
from icbot.features import blackjack  # noqa: F401
from icbot.features import lc_leaderboard  # noqa: F401
from icbot.features import sports  # noqa: F401
from icbot.features.lc_leaderboard import ActivityTracker


load_dotenv()
settings = Settings.from_env()
db = Database(settings.db_path)

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True  # needed for message-count leaderboard tracking

bot = commands.Bot(command_prefix="!", intents=intents)
activity_tracker = ActivityTracker(db)


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


# Register the check in a version-friendly way
if hasattr(bot.tree, "add_check"):
    bot.tree.add_check(ic_channel_gate)
else:
    bot.tree.interaction_check = ic_channel_gate  # type: ignore[attr-defined]


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


@bot.event
async def on_ready():
    await db.init()

    # Make shared singletons available to feature modules via interaction.client
    bot.settings = settings  # type: ignore[attr-defined]
    bot.db = db  # type: ignore[attr-defined]
    bot.activity_tracker = activity_tracker  # type: ignore[attr-defined]

    # Register the /ic root group
    if settings.guild_id:
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
