from typing import Any, cast

import discord

from icbot.app import admin
from icbot.container import get_container, resolve_guild_settings
from icbot.permissions import is_admin_or_allowed


@admin.command(name="llm_stop", description="Stop the LLM from functioning (pauseschatbot and resume tools).")
async def llm_stop_cmd(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
        return

    # Store state in bot & container memory for fast lookups
    container = get_container(interaction.client)
    container.llm_killed = True
    cast(Any, interaction.client)._llm_killed = True

    # Store state in DB for persistence across restarts
    db = container.db or getattr(interaction.client, "db", None)
    if db:
        await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("llm_killed", "1"))

    await interaction.response.send_message(
        "🛑 **LLM Features Paused**: Chatbot and resume review have been disabled.", ephemeral=True
    )


@admin.command(name="llm_start", description="Re-enable the LLM features.")
async def llm_start_cmd(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
        return

    # Clear state in memory
    container = get_container(interaction.client)
    container.llm_killed = False
    cast(Any, interaction.client)._llm_killed = False

    # Clear state in DB
    db = container.db or getattr(interaction.client, "db", None)
    if db:
        await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("llm_killed", "0"))

    await interaction.response.send_message(
        "✅ **LLM Features Enabled**: Chatbot and resume review are active.", ephemeral=True
    )
