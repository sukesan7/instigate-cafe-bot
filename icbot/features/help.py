from __future__ import annotations

from typing import Any

import discord

from icbot.app import ic
from icbot.config import Settings
from icbot.container import resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.permissions import can_manage_guild
from icbot.registry import is_feature_enabled

# --- Static Baseline Help Content (Backwards-Compatible Definition) ---
HELP_CATEGORIES: list[dict[str, Any]] = [
    {
        "title": "📚 Main Utilities",
        "description": "Core commands for users of Instigate Cafe.",
        "commands": [
            ("/ic profile", "View your user profile and stats"),
            ("/ic dailyclaim", "Claim your daily $100 reward"),
            ("/ic resume", "Submit your resume for an InstigateAI review"),
            ("/ic jobs", "List the latest new grad/intern job openings"),
            ("/ic report", "Pull the latest daily market report on-demand"),
            ("/ic leaderboard", "View the interactive server balance leaderboard"),
        ],
    },
    {
        "title": "🎓 Academic Toolkit",
        "description": "Tools to assist with university studying and finding courses.",
        "commands": [
            ("/ic notes", "Search through available course notes repositories"),
            ("/ic prof", "Search RateMyProfessors ratings for a professor"),
            ("/ic exam list", "List your scheduled exams"),
            ("/ic exam add", "Add a new exam to your schedule"),
            ("/ic exam remove", "Remove an exam by ID"),
            ("/ic grades", "View grade distributions for a course (e.g., ele888)"),
        ],
    },
    {
        "title": "🏛️ Instigate Cafe Stock Exchange",
        "description": "Virtual market for trading $IC and managing your portfolio.",
        "commands": [
            ("/ic icse market", "Show the current Instigate Cafe Stock Exchange"),
            ("/ic icse portfolio", "View your market portfolio"),
            ("/ic icse quote", "Show a single stock quote"),
            ("/ic icse buy", "Buy shares of a listed stock"),
            ("/ic icse sell", "Sell shares of a stock you own"),
            ("/ic icse short", "Open or increase a short position"),
            ("/ic icse cover", "Cover (close) a short position"),
            ("/ic icse trades", "Show recent market trades"),
        ],
    },
    {
        "title": "🎰 Casino Games",
        "description": "Try your luck and risk your IC Cash to win big.",
        "commands": [
            ("/ic casino blackjack", "Play a hand of blackjack against the dealer"),
            ("/ic casino roulette", "Spin the roulette wheel"),
            ("/ic casino slots", "Pull the lever on the slot machine"),
            ("/ic casino payouts", "Show the slot machine payout table"),
        ],
    },
    {
        "title": "ℹ️ Info & Updates",
        "description": "General information and event notifications.",
        "commands": [
            ("/ic serverinfo", "View information about this server"),
            ("/ic botinfo", "View details and capabilities of the IC Bot"),
            ("/ic sports nba", "Get upcoming NBA games and latest headlines"),
            ("/ic help", "Show this help menu"),
        ],
    },
]


def build_dynamic_help_categories(
    interaction: discord.Interaction | None,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Generate dynamic, theme-aware help categories filtering disabled modules and restricted commands."""
    categories: list[dict[str, Any]] = []

    # 1. Main / Core Utilities
    core_cmds: list[tuple[str, str]] = [
        ("/ic profile", "View your user profile and stats"),
        (
            "/ic dailyclaim",
            f"Claim your daily {settings.currency_symbol}{settings.daily_claim_amount} reward",
        ),
    ]
    if is_feature_enabled("ai", settings):
        core_cmds.append(("/ic resume", f"Submit your resume for an {settings.bot_display_name} review"))
    if is_feature_enabled("careers", settings):
        core_cmds.append(("/ic jobs", "List the latest new grad/intern job openings"))
    if is_feature_enabled("market", settings):
        core_cmds.append(("/ic report", "Pull the latest daily market report on-demand"))
    core_cmds.append(("/ic leaderboard", "View the interactive server balance leaderboard"))

    categories.append(
        {
            "title": "📚 Main Utilities",
            "description": f"Core commands for users of {settings.bot_display_name}.",
            "commands": core_cmds,
        }
    )

    # 2. Academic Toolkit
    if is_feature_enabled("academics", settings):
        categories.append(
            {
                "title": "🎓 Academic Toolkit",
                "description": f"Tools to assist with university studying and finding courses at {settings.rmp_university}.",
                "commands": [
                    ("/ic notes", "Search through available course notes repositories"),
                    ("/ic prof", "Search RateMyProfessors ratings for a professor"),
                    ("/ic exam list", "List scheduled exams (supports course/term filters)"),
                    ("/ic exam add", "Add a new exam to the schedule"),
                    ("/ic exam remove", "Remove an exam by ID"),
                    ("/ic exam export", "Export scheduled exams to CSV or ICS"),
                    ("/ic exam import", "Import exams from a CSV or ICS file"),
                    ("/ic grades", "View grade distributions for a course (e.g., ele888)"),
                ],
            }
        )

    # 3. Virtual Stock Exchange
    if is_feature_enabled("market", settings):
        categories.append(
            {
                "title": f"🏛️ {settings.stock_exchange_name}",
                "description": f"Virtual market for trading {settings.currency_symbol}{settings.primary_stock_symbol} and managing your portfolio.",
                "commands": [
                    ("/ic icse market", f"Show the current {settings.stock_exchange_name}"),
                    ("/ic icse portfolio", "View your market portfolio"),
                    ("/ic icse quote", "Show a single stock quote"),
                    ("/ic icse buy", "Buy shares of a listed stock"),
                    ("/ic icse sell", "Sell shares of a stock you own"),
                    ("/ic icse short", "Open or increase a short position"),
                    ("/ic icse cover", "Cover (close) a short position"),
                    ("/ic icse trades", "Show recent market trades"),
                ],
            }
        )

    # 4. Casino Games
    if is_feature_enabled("casino", settings):
        categories.append(
            {
                "title": "🎰 Casino Games",
                "description": f"Try your luck and risk your {settings.currency_name} to win big.",
                "commands": [
                    ("/ic casino blackjack", "Play a hand of blackjack against the dealer"),
                    ("/ic casino roulette", "Spin the roulette wheel"),
                    ("/ic casino slots", "Pull the lever on the slot machine"),
                    ("/ic casino payouts", "Show the slot machine payout table"),
                ],
            }
        )

    # 5. Server Administration (Only for guild managers)
    if interaction is not None and can_manage_guild(interaction, settings):
        categories.append(
            {
                "title": "⚙️ Server Administration",
                "description": "Administrative tools to configure features, presets, and audit logs.",
                "commands": [
                    ("/ic admin config", "Inspect or edit per-guild configuration"),
                    ("/ic admin features", "View or toggle optional feature modules"),
                    ("/ic admin audit", "View recent administrative configuration audit trail"),
                ],
            }
        )

    # 6. Info & Updates
    info_cmds: list[tuple[str, str]] = [
        ("/ic serverinfo", "View information about this server"),
        ("/ic botinfo", f"View details and capabilities of {settings.bot_display_name}"),
    ]
    if is_feature_enabled("sports", settings):
        info_cmds.append(("/ic sports nba", "Get upcoming NBA games and latest headlines"))
    info_cmds.append(("/ic help", "Show this help menu"))

    categories.append(
        {
            "title": "ℹ️ Info & Updates",
            "description": "General information and event notifications.",
            "commands": info_cmds,
        }
    )

    return categories


class HelpPaginationView(discord.ui.View):
    def __init__(
        self,
        current_page: int,
        total_pages: int,
        settings: Settings,
        categories: list[dict[str, Any]] | None = None,
    ):
        super().__init__(timeout=180)  # 3 minute timeout
        self.current_page = current_page
        self.total_pages = total_pages
        self.settings = settings
        self.categories = categories

        # Disable buttons if at boundaries
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def update_message(self, interaction: discord.Interaction):
        embed = build_help_embed(
            self.current_page,
            self.total_pages,
            self.settings,
            categories=self.categories,
        )
        # Update button states
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        await self.update_message(interaction)


def build_help_embed(
    page: int,
    total_pages: int,
    settings: Settings,
    categories: list[dict[str, Any]] | None = None,
) -> discord.Embed:
    cat_list = categories if categories is not None else HELP_CATEGORIES
    safe_page = max(0, min(page, len(cat_list) - 1)) if cat_list else 0
    category = cat_list[safe_page] if cat_list else {"title": "Help", "description": "", "commands": []}

    desc_lines = [str(category["description"]), ""]
    raw_cmds = category.get("commands", [])
    if isinstance(raw_cmds, (list, tuple)):
        for item in raw_cmds:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                cmd_name, cmd_desc = str(item[0]), str(item[1])
                desc_lines.append(f"**`{cmd_name}`**\n{cmd_desc}\n")

    embed = system_embed(
        settings,
        title=f"{settings.bot_display_name} | {category['title']}",
        summary="\n".join(desc_lines),
        fields=[],
        footer_hint=f"Help Menu • Page {safe_page + 1}/{total_pages}",
    )
    return embed


@ic.command(name="help", description="Show the paginated help menu for all IC bot commands.")
async def ic_help(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)

    categories = build_dynamic_help_categories(interaction, settings)
    total_pages = max(1, len(categories))
    current_page = 0

    embed = build_help_embed(current_page, total_pages, settings, categories=categories)
    view = HelpPaginationView(current_page, total_pages, settings, categories=categories)

    await interaction.response.send_message(embed=embed, view=view)
