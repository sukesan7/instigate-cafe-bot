import discord
from discord.ext import commands
from dotenv import load_dotenv

from icbot.config import Settings
from icbot.db import Database
from icbot.app import ic

# Import features (registers commands onto /ic)
from icbot.features import stock  # noqa: F401
from icbot.features import blackjack  # noqa: F401
from icbot.features import lc_leaderboard  # noqa: F401
from icbot.features.lc_leaderboard import ActivityTracker


load_dotenv()
settings = Settings.from_env()
db = Database(settings.db_path)

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
activity_tracker = ActivityTracker(db)


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
    # Track activity for /ic lc leaderboard
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
