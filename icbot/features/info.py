import discord
from discord import app_commands
import os

from icbot.app import ic
from icbot.embeds import system as system_embed

@ic.command(name="serverinfo", description="Display server information.")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return
        
    settings = getattr(interaction.client, "settings", None)
    
    # Safely get dates and counts
    created_at = discord.utils.format_dt(guild.created_at, style="R")
    member_count = guild.member_count or len(guild.members)
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    role_count = len(guild.roles)
    
    owner = guild.owner
    owner_str = owner.mention if owner else "Unknown"
    
    summary = (
        f"**Server Name:** {guild.name}\n"
        f"**Server ID:** {guild.id}\n"
        f"**Owner:** {owner_str}\n"
        f"**Created:** {created_at}\n"
    )
    
    fields = [
        ("👥 Members", f"**{member_count}** Members", True),
        ("💬 Channels", f"**{text_channels}** Text | **{voice_channels}** Voice", True),
        ("🎭 Roles", f"**{role_count}** Roles", True)
    ]
    
    embed = system_embed(
        settings,
        title=f"ℹ️ {guild.name} Server Info",
        summary=summary,
        fields=fields,
        footer_hint="Server Information"
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    await interaction.response.send_message(embed=embed)


@ic.command(name="botinfo", description="Display bot capabilities and information.")
async def botinfo(interaction: discord.Interaction):
    settings = getattr(interaction.client, "settings", None)
    
    llm_model = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite-preview")
    
    summary = (
        "**Instigate Cafe Bot** is a multi-purpose utility, game, and infrastructure engine custom-built for the Instigate Cafe server.\n\n"
        "**Developer:** unkown\n"
        "**Tech Stack:** Python 3, discord.py, aiosqlite, Google Gemini API, GraphQL\n\n"
        "**Capabilities & Features:**\n"
        "• **ICSE (Instigate Cafe Stock Exchange):** A real-time virtual market tracking $IC and user positions.\n"
        "• **Job Board:** Scrapes and filters technical New Grad/Intern roles from ATS endpoints (Lever, Greenhouse, Workday).\n"
        "• **Intelligent LLM:** Mentions are powered dynamically by Google's Gemini LLM model.\n"
        "• **Casino & Games:** Economy system fully integrated with Casino table games (Blackjack, Roulette, Slots).\n"
        "• **University Toolkit:** Pulls dynamic Notion links via `/ic notes` and fetches latest RateMyProfessors data via `/ic prof`.\n"
        "• **System Watchdogs:** Runs routine alerts for TTC service notices, crypto watches, and daily ICSE summaries."
    )
    
    fields = [
        ("LLM Engine", f"Powered by **{llm_model}**", False),
        ("Source", "Internal Instigate Cafe Repository", False)
    ]
    
    embed = system_embed(
        settings,
        title="Bot Information",
        summary=summary,
        fields=fields,
        footer_hint="Bot Information"
    )
    
    # Try attaching local logo if possible (using standard static URL fallback for simplicity if host differs)
    # The prompt noted "the logo from assets/logo.png". 
    logo_file = discord.File("assets/ic_logo.png", filename="logo.png")
    embed.set_thumbnail(url="attachment://logo.png")
    
    try:
        await interaction.response.send_message(embed=embed, file=logo_file)
    except FileNotFoundError:
        embed.set_thumbnail(url=None)
        await interaction.response.send_message(embed=embed)
