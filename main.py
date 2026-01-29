import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from icbot.config import Settings
from icbot.db import Database
from icbot.app import ic
from icbot.permissions import is_admin_or_allowed

# Import features (registers commands onto /ic)
from icbot.features import stock  # noqa: F401
from icbot.features import blackjack  # noqa: F401
from icbot.features import lc_leaderboard  # noqa: F401
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
        # Tell user where to go (ephemeral to avoid spam)
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
    # Fallback for older forks
    bot.tree.interaction_check = ic_channel_gate  # type: ignore[attr-defined]


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Silence check failures (we already responded in the gate)
    if isinstance(error, app_commands.CheckFailure):
        return
    raise error


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
        bot.tree.add_command(ic, guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to guild {settings.guild_id}.")
    else:
        bot.tree.add_command(ic)
        await bot.tree.sync()
        print("Synced commands globally (can take a while to appear).")

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
