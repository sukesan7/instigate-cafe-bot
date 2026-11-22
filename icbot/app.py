import discord
from discord import app_commands

# Root command group: /ic
ic = app_commands.Group(name="ic", description="Campus bot")

# Sub-groups
casino = app_commands.Group(name="casino", description="Casino", parent=ic)
icse = app_commands.Group(name="icse", description="Stock exchange", parent=ic)
admin = app_commands.Group(
    name="admin",
    description="Admin",
    parent=ic,
    default_permissions=discord.Permissions(manage_guild=True),
)
exam = app_commands.Group(name="exam", description="Exams", parent=ic)
sports = app_commands.Group(name="sports", description="Sports", parent=ic)
