import discord
from discord import app_commands
from icbot.app import ic
from icbot.embeds import system as system_embed

# --- Help Content Data ---
HELP_CATEGORIES = [
    {
        "title": "📚 Main Utilities",
        "description": "Core commands for users of Instigate Cafe.",
        "commands": [
            ("/ic profile", "View your user profile and stats"),
            ("/ic dailyclaim", "Claim your daily $100 reward"),
            ("/ic resume", "Submit your resume for an InstigateAI review"),
            ("/ic jobs", "List the latest new grad/intern job openings"),
            ("/ic report", "Pull the latest daily market report on-demand"),
            ("/ic leaderboard", "View the interactive server balance leaderboard")
        ]
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
            ("/ic grades", "View grade distributions for a course (e.g., ele888)")
        ]
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
            ("/ic icse trades", "Show recent market trades")
        ]
    },
    {
        "title": "🎰 Casino Games",
        "description": "Try your luck and risk your IC Cash to win big.",
        "commands": [
            ("/ic casino blackjack", "Play a hand of blackjack against the dealer"),
            ("/ic casino roulette", "Spin the roulette wheel"),
            ("/ic casino slots", "Pull the lever on the slot machine"),
            ("/ic casino payouts", "Show the slot machine payout table")
        ]
    },
    {
        "title": "ℹ️ Info & Updates",
        "description": "General information and event notifications.",
        "commands": [
            ("/ic serverinfo", "View information about this server"),
            ("/ic botinfo", "View details and capabilities of the IC Bot"),
            ("/ic sports nba", "Get upcoming NBA games and latest headlines"),
            ("/ic help", "Show this help menu")
        ]
    }
]

class HelpPaginationView(discord.ui.View):
    def __init__(self, current_page: int, total_pages: int, settings):
        super().__init__(timeout=180) # 3 minute timeout
        self.current_page = current_page
        self.total_pages = total_pages
        self.settings = settings
        
        # Disable buttons if at boundaries
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.total_pages - 1

    async def update_message(self, interaction: discord.Interaction):
        embed = build_help_embed(self.current_page, self.total_pages, self.settings)
        # Update button states
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.total_pages - 1
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        await self.update_message(interaction)


def build_help_embed(page: int, total_pages: int, settings) -> discord.Embed:
    category = HELP_CATEGORIES[page]
    
    desc_lines = [str(category["description"]), ""]
    for cmd_name, cmd_desc in category["commands"]:
        desc_lines.append(f"**`{cmd_name}`**\n{cmd_desc}\n")

    embed = system_embed(
        settings,
        title=f"InstigateAI | {category['title']}",
        summary="\n".join(desc_lines),
        fields=[],
        footer_hint=f"Help Menu • Page {page + 1}/{total_pages}"
    )
    return embed


@ic.command(name="help", description="Show the paginated help menu for all IC bot commands.")
async def ic_help(interaction: discord.Interaction):
    settings = getattr(interaction.client, "settings", None)
    
    total_pages = len(HELP_CATEGORIES)
    current_page = 0
    
    embed = build_help_embed(current_page, total_pages, settings)
    view = HelpPaginationView(current_page, total_pages, settings)
    
    await interaction.response.send_message(embed=embed, view=view)
