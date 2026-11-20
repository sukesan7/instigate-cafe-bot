from __future__ import annotations

import contextlib
from typing import Any

import discord

from icbot.app import ic
from icbot.config import Settings
from icbot.container import resolve_guild_settings
from icbot.embeds import Field
from icbot.embeds import system as system_embed
from icbot.permissions import can_manage_guild
from icbot.registry import is_feature_enabled

COMMAND_METADATA: dict[str, dict[str, str]] = {
    # Core & Utility
    "/ic profile": {
        "desc": "View your user profile, wallet balance, and stats.",
        "example": "/ic profile",
    },
    "/ic dailyclaim": {
        "desc": "Claim your daily virtual currency allowance.",
        "example": "/ic dailyclaim",
    },
    "/ic getting-started": {
        "desc": "Interactive step-by-step introduction for new students.",
        "example": "/ic getting-started",
    },
    "/ic help": {
        "desc": "Open this dynamic command guide and help directory.",
        "example": "/ic help",
    },
    "/ic leaderboard": {
        "desc": "View the server virtual balance and assets leaderboard.",
        "example": "/ic leaderboard",
    },
    "/ic serverinfo": {
        "desc": "Display server information, member counts, and channels.",
        "example": "/ic serverinfo",
    },
    "/ic botinfo": {
        "desc": "View bot version, uptime, and system diagnostics.",
        "example": "/ic botinfo",
    },
    "/ic status": {
        "desc": "Display operational status and actionable module health.",
        "example": "/ic status",
    },
    # Academics
    "/ic notes": {
        "desc": "Search student-contributed course notes and study guides.",
        "example": "/ic notes course:CPS109",
    },
    "/ic prof": {
        "desc": "Look up professor ratings and student feedback.",
        "example": "/ic prof name:Smith",
    },
    "/ic grades": {
        "desc": "View historical grade distribution charts for a course.",
        "example": "/ic grades course:ELE888",
    },
    "/ic exam list": {
        "desc": "List scheduled exams with optional course or term filters.",
        "example": "/ic exam list upcoming_only:True",
    },
    "/ic exam add": {
        "desc": "[Admin] Add a midterm or final exam to the server schedule.",
        "example": "/ic exam add course_code:MTH140 date:2026-12-10",
    },
    "/ic exam remove": {
        "desc": "[Admin] Remove a scheduled exam by its ID.",
        "example": "/ic exam remove exam_id:42",
    },
    "/ic exam clear": {
        "desc": "[Admin] Clear all exam records for a specific term.",
        "example": "/ic exam clear term:W2026",
    },
    "/ic exam import": {
        "desc": "[Admin] Import exam dates from a CSV or iCalendar (.ics) file.",
        "example": "/ic exam import fmt:csv file:<attachment>",
    },
    "/ic exam export": {
        "desc": "Export this server's exam schedule as CSV or iCalendar (.ics).",
        "example": "/ic exam export fmt:ics",
    },
    # Careers
    "/ic jobs": {
        "desc": "Search active internships and new grad opportunities.",
        "example": "/ic jobs query:intern location:Toronto",
    },
    "/ic job_preferences": {
        "desc": "Set your private job preferences for alerts and search.",
        "example": "/ic job_preferences locations:Toronto remote_only:True",
    },
    "/ic job_preview": {
        "desc": "Test job filter rules against a simulated sample posting.",
        "example": "/ic job_preview title:Intern preset:software",
    },
    "/ic job_subscribe": {
        "desc": "[Admin] Subscribe a channel to automated job alert feeds.",
        "example": "/ic job_subscribe action:add channel:#jobs preset:software",
    },
    # AI
    "/ic resume": {
        "desc": "Submit your resume for private, discipline-tailored AI critique.",
        "example": "/ic resume file:<resume.pdf> discipline:software",
    },
    # Market (ICSE)
    "/ic icse market": {
        "desc": "Display current stock listings and index performance.",
        "example": "/ic icse market",
    },
    "/ic icse quote": {
        "desc": "Look up real-time price and summary for a single stock.",
        "example": "/ic icse quote symbol:IC",
    },
    "/ic icse portfolio": {
        "desc": "Inspect your virtual stock holdings and cash balance.",
        "example": "/ic icse portfolio",
    },
    "/ic icse buy": {
        "desc": "Buy shares of a listed virtual stock.",
        "example": "/ic icse buy symbol:IC qty:10",
    },
    "/ic icse sell": {
        "desc": "Sell shares of a stock you currently own.",
        "example": "/ic icse sell symbol:IC qty:5",
    },
    "/ic icse short": {
        "desc": "Open or increase a short position against a stock.",
        "example": "/ic icse short symbol:IC qty:10",
    },
    "/ic icse cover": {
        "desc": "Cover (close) an existing short position.",
        "example": "/ic icse cover symbol:IC qty:10",
    },
    "/ic icse trades": {
        "desc": "Review recent market transactions across the server.",
        "example": "/ic icse trades limit:10",
    },
    "/ic icse history": {
        "desc": "View historical price adjustments and news headlines.",
        "example": "/ic icse history symbol:IC limit:5",
    },
    "/ic icse stock": {
        "desc": "Quick quote for the server's primary benchmark stock.",
        "example": "/ic icse stock",
    },
    "/ic report": {
        "desc": "Generate the daily market summary report immediately.",
        "example": "/ic report",
    },
    # Casino
    "/ic casino blackjack": {
        "desc": "Play a hand of classic blackjack against the virtual dealer.",
        "example": "/ic casino blackjack bet:25",
    },
    "/ic casino roulette": {
        "desc": "Spin the European roulette wheel on red, black, or numbers.",
        "example": "/ic casino roulette bet:50 bet_type:red",
    },
    "/ic casino slots": {
        "desc": "Pull the virtual slot machine lever for matching payouts.",
        "example": "/ic casino slots bet:20",
    },
    "/ic casino payouts": {
        "desc": "Display the slot machine symbol and payout multiplier table.",
        "example": "/ic casino payouts",
    },
    # Sports
    "/ic sports nba": {
        "desc": "Check upcoming NBA games, scores, and sports news.",
        "example": "/ic sports nba",
    },
    "/ic sports nfl": {
        "desc": "Check upcoming NFL football matchups and schedules.",
        "example": "/ic sports nfl",
    },
    "/ic sports mma": {
        "desc": "View mixed martial arts event schedules and fight cards.",
        "example": "/ic sports mma",
    },
    "/ic sports ufc": {
        "desc": "View upcoming UFC championship fight cards and odds.",
        "example": "/ic sports ufc",
    },
    "/ic sports boxing": {
        "desc": "View upcoming professional boxing bouts and headlines.",
        "example": "/ic sports boxing",
    },
    # Admin
    "/ic admin setup": {
        "desc": "[Admin] Interactive configuration wizard for channels and presets.",
        "example": "/ic admin setup",
    },
    "/ic admin config": {
        "desc": "[Admin] View or update server configuration overrides directly.",
        "example": "/ic admin config action:show",
    },
    "/ic admin features": {
        "desc": "[Admin] Enable or disable optional bot feature modules.",
        "example": "/ic admin features profile:student_core",
    },
    "/ic admin roles": {
        "desc": "[Admin] Manage authorized server roles for bot administration.",
        "example": "/ic admin roles action:list",
    },
    "/ic admin audit": {
        "desc": "[Admin] Review recent server configuration changes.",
        "example": "/ic admin audit",
    },
    "/ic admin llm_start": {
        "desc": "[Admin] Re-enable the AI mention assistant and resume tools.",
        "example": "/ic admin llm_start",
    },
    "/ic admin llm_stop": {
        "desc": "[Admin] Temporarily pause AI mention assistant and resume tools.",
        "example": "/ic admin llm_stop",
    },
    "/ic admin move": {
        "desc": "[Admin] Adjust a stock price by a delta with an audit reason.",
        "example": "/ic admin move symbol:IC change:+2 reason:Earnings",
    },
    "/ic admin set": {
        "desc": "[Admin] Set a stock to an exact price with an audit reason.",
        "example": "/ic admin set symbol:IC value:100 reason:Reset",
    },
    "/ic admin sync_catalog": {
        "desc": "[Admin] Synchronize stock catalog or reset to synthetic defaults.",
        "example": "/ic admin sync_catalog reset_to_default:True",
    },
    "/ic admin weekroll": {
        "desc": "[Admin] Reset weekly stock performance baselines.",
        "example": "/ic admin weekroll note:NewWeek",
    },
}

# --- Static Baseline Help Content (Backwards-Compatible Definition) ---
HELP_CATEGORIES: list[dict[str, Any]] = [
    {
        "title": "📚 Main Utilities",
        "description": "Core commands for users of Instigate Cafe.",
        "commands": [
            ("/ic profile", "View your user profile and stats"),
            ("/ic dailyclaim", "Claim your daily $100 reward"),
            ("/ic getting-started", "Interactive step-by-step introduction for new students"),
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
            ("/ic status", "Display operational status and actionable diagnostics"),
            ("/ic sports nba", "Get upcoming NBA games and latest headlines"),
            ("/ic help", "Show this help menu"),
        ],
    },
]

GETTING_STARTED_STEPS: list[dict[str, Any]] = [
    {
        "step": 1,
        "title": "Welcome & Daily Rewards",
        "badge": "🪙 Step 1 of 4: Welcome & Campus Economy",
        "description": (
            "Welcome to **Instigate Cafe**! This bot is your everyday campus companion for "
            "academics, career discovery, and campus community life.\n\n"
            "Here is how to get your journey started:"
        ),
        "fields": [
            (
                "1. Claim your daily allowance",
                "Run **`/ic dailyclaim`** every 24 hours to collect free virtual campus funds. "
                "These are risk-free virtual tokens for fun simulations.",
                False,
            ),
            (
                "2. View your profile",
                "Run **`/ic profile`** anytime to view your virtual balance, transaction history, "
                "and server rank.",
                False,
            ),
            (
                "3. Server leaderboard",
                "Check **`/ic leaderboard`** to see top community balances and active members.",
                False,
            ),
        ],
        "tip": "No configuration is required! Run `/ic dailyclaim` right now. For privacy and data deletion policies, check `/ic botinfo`.",
    },
    {
        "step": 2,
        "title": "Academic Survival Kit",
        "badge": "🎓 Step 2 of 4: Academic Toolkit",
        "description": (
            "Whether you're in arts, science, business, or engineering, stay ahead in your courses "
            "with collaborative academic tools:"
        ),
        "fields": [
            (
                "📚 Course Notes & Study Guides",
                "Use **`/ic notes course:CPS109`** to find study materials, past exam guides, and reference sheets "
                "shared by students. Works with standard university course codes (e.g. `CPS109`, `MTH140`, `ENG101`).",
                False,
            ),
            (
                "👨‍🏫 Professor Ratings",
                "Before enrolling in classes, run **`/ic prof name:Smith`** to check instructor ratings, "
                "difficulty scores, and student reviews directly from RateMyProfessors.",
                False,
            ),
            (
                "📅 Exam Schedules & Tracking",
                "Never miss a test. Run **`/ic exam list`** to see midterms and finals for your courses. "
                "You can export your schedule to Google Calendar or Apple Calendar using **`/ic exam export`**. "
                "(Course instructors and admins add exams via `/ic exam add`).",
                False,
            ),
            (
                "📊 Grade Distributions",
                "Curious about past class averages? Check **`/ic grades course:ELE888`** to see historical "
                "grade distributions where supported.",
                False,
            ),
        ],
        "tip": "Course codes are case-insensitive. Try `/ic notes` with your hardest class!",
    },
    {
        "step": 3,
        "title": "Career & Internship Discovery",
        "badge": "💼 Step 3 of 4: Careers & Job Alerts",
        "description": (
            "Finding internships and new-grad roles shouldn't require checking 20 job boards every day. "
            "Instigate Cafe ingests fresh early-career roles directly from company ATS portals:"
        ),
        "fields": [
            (
                "🔎 Browse Fresh Postings",
                "Run **`/ic jobs`** to browse recent internship, co-op, and junior postings. "
                "Filter easily by keyword (e.g. `/ic jobs query:marketing`) or location (e.g. `/ic jobs location:Toronto`).",
                False,
            ),
            (
                "🎯 Set Your Preferences",
                "Use **`/ic job_preferences`** to choose your target disciplines, preferred cities, "
                "or remote-only options so searches highlight what matters to you.",
                False,
            ),
            (
                "📄 Private AI Resume Review",
                "Upload a draft with **`/ic resume`** to get confidential, constructive feedback. "
                "Your document is evaluated ephemerally in memory and never stored on disk or in the database.",
                False,
            ),
        ],
        "tip": "Use `/ic job_preferences` once, and future searches can automatically apply your filters.",
    },
    {
        "step": 4,
        "title": "Community Simulations & Mini-Games",
        "badge": "🏛️ Step 4 of 4: Community & Casual Games",
        "description": (
            "Take a break between study sessions with risk-free campus mini-games and simulations:"
        ),
        "fields": [
            (
                "📈 Virtual Stock Exchange (ICSE)",
                "Experience stock trading with virtual campus funds. Check **`/ic icse market`** to view listings, "
                "and **`/ic icse buy`** / **`/ic icse sell`** to trade simulated stocks without risking real money.",
                False,
            ),
            (
                "🃏 Casino Card Games",
                "Try your hand at **`/ic casino blackjack`**, spin the wheel with **`/ic casino roulette`**, "
                "or pull the lever with **`/ic casino slots`**.",
                False,
            ),
            (
                "📖 Complete Directory",
                "Whenever you need a reminder of all available commands, simply type **`/ic help`** "
                "to open the full interactive command index.",
                False,
            ),
        ],
        "tip": "All currency and trading are 100% virtual and purely for entertainment.",
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
        ("/ic profile", "View your user profile, wallet balance, and stats"),
        (
            "/ic dailyclaim",
            f"Claim your daily {settings.currency_symbol}{settings.daily_claim_amount} reward",
        ),
        ("/ic getting-started", "Interactive guided walkthrough for new students"),
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
                    ("/ic exam add", "[Admin] Add a new exam to the schedule"),
                    ("/ic exam remove", "[Admin] Remove an exam by ID"),
                    ("/ic exam clear", "[Admin] Clear exams for a specific term"),
                    ("/ic exam export", "Export scheduled exams to CSV or ICS"),
                    ("/ic exam import", "[Admin] Import exams from a CSV or ICS file"),
                    ("/ic grades", "View grade distributions for a course (e.g., ele888)"),
                ],
            }
        )

    # 3. Career Opportunities
    if is_feature_enabled("careers", settings):
        categories.append(
            {
                "title": "💼 Careers & Job Alerts",
                "description": "Discover internships and new grad opportunities from verified employer feeds.",
                "commands": [
                    ("/ic jobs", "Search through cached job postings with pagination and filters"),
                    (
                        "/ic job_preferences",
                        "View or update your private notification and search preferences",
                    ),
                    ("/ic job_preview", "Test job filter criteria against a sample posting"),
                    ("/ic job_subscribe", "[Admin] Manage server job alert channel subscriptions"),
                ],
            }
        )

    # 4. Virtual Stock Exchange
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
                    ("/ic icse history", "Show recent admin price updates for ICSE stocks"),
                    ("/ic icse stock", "Show primary benchmark stock price and stats"),
                    ("/ic report", "Generate the daily market report on-demand"),
                ],
            }
        )

    # 5. Casino Games
    if is_feature_enabled("casino", settings):
        categories.append(
            {
                "title": "🎰 Casino Games",
                "description": f"Try your luck and risk your {settings.currency_name} in casual mini-games.",
                "commands": [
                    ("/ic casino blackjack", "Play a hand of blackjack against the dealer"),
                    ("/ic casino roulette", "Spin the roulette wheel"),
                    ("/ic casino slots", "Pull the lever on the slot machine"),
                    ("/ic casino payouts", "Show the slot machine payout table"),
                ],
            }
        )

    # 6. Server Administration (Only for guild managers)
    if interaction is not None and can_manage_guild(interaction, settings):
        categories.append(
            {
                "title": "⚙️ Server Administration",
                "description": "Administrative tools to configure features, presets, channels, and audit logs.",
                "commands": [
                    ("/ic admin setup", "Interactive wizard to configure server settings and channels"),
                    ("/ic admin config", "Inspect or edit per-guild configuration overrides"),
                    ("/ic admin features", "View or toggle optional feature modules"),
                    ("/ic admin roles", "Manage administrative allowed roles for bot management"),
                    ("/ic admin audit", "View recent administrative configuration audit trail"),
                    ("/ic admin llm_start", "Re-enable AI assistant features"),
                    ("/ic admin llm_stop", "Pause AI assistant features"),
                    ("/ic admin move", "Move a stock price by a delta with an audit reason"),
                    ("/ic admin set", "Set a stock to an exact price with an audit reason"),
                    ("/ic admin sync_catalog", "Sync stock catalog or reset to synthetic defaults"),
                    ("/ic admin weekroll", "Reset weekly stock change baselines"),
                ],
            }
        )

    # 7. Community & Info
    info_cmds: list[tuple[str, str]] = [
        ("/ic serverinfo", "View information about this server"),
        ("/ic botinfo", f"View details and capabilities of {settings.bot_display_name}"),
        ("/ic status", "Display operational status and actionable diagnostics"),
    ]
    if is_feature_enabled("sports", settings):
        info_cmds.extend(
            [
                ("/ic sports nba", "Get upcoming NBA games and latest headlines"),
                ("/ic sports nfl", "Get upcoming NFL games and latest headlines"),
                ("/ic sports mma", "Get upcoming MMA events and latest headlines"),
                ("/ic sports ufc", "Get upcoming UFC events and latest headlines"),
                ("/ic sports boxing", "Get upcoming boxing bouts and latest headlines"),
            ]
        )
    info_cmds.append(("/ic help", "Show this paginated help menu"))

    categories.append(
        {
            "title": "ℹ️ Info & Updates",
            "description": "General information, diagnostics, and event notifications.",
            "commands": info_cmds,
        }
    )

    return categories


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
                cmd_name = str(item[0])
                cmd_desc = str(item[1])
                meta = COMMAND_METADATA.get(cmd_name)
                example_suffix = f"\n*Example:* `{meta['example']}`" if meta and "example" in meta else ""
                desc_lines.append(f"**`{cmd_name}`** — {cmd_desc}{example_suffix}\n")

    embed = system_embed(
        settings,
        title=f"{settings.bot_display_name} | {category['title']}",
        summary="\n".join(desc_lines),
        fields=[],
        footer_hint=f"Help Menu • Page {safe_page + 1}/{total_pages}",
    )
    return embed


def build_getting_started_embed(
    step_index: int,
    settings: Settings,
) -> discord.Embed:
    safe_idx = max(0, min(step_index, len(GETTING_STARTED_STEPS) - 1))
    step = GETTING_STARTED_STEPS[safe_idx]

    fields: list[Field] = list(step.get("fields", []))
    if step.get("tip"):
        fields.append(("💡 Pro-Tip", str(step["tip"]), False))

    embed = system_embed(
        settings,
        title=f"{settings.bot_display_name} | {step['title']}",
        subtitle=step["badge"],
        summary=step["description"],
        fields=fields,
        footer_hint=f"Getting Started Guide • Step {safe_idx + 1}/{len(GETTING_STARTED_STEPS)}",
    )
    return embed


class HelpPaginationView(discord.ui.View):
    def __init__(
        self,
        current_page: int,
        total_pages: int,
        settings: Settings,
        categories: list[dict[str, Any]] | None = None,
        author_id: int | None = None,
    ):
        super().__init__(timeout=180)  # 3 minute timeout
        self.current_page = current_page
        self.total_pages = total_pages
        self.settings = settings
        self.categories = categories
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "ℹ️ Only the person who requested this help menu can navigate it.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def update_message(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        embed = build_help_embed(
            self.current_page,
            self.total_pages,
            self.settings,
            categories=self.categories,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous Page", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = max(0, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="Next Page ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        await self.update_message(interaction)


class GettingStartedView(discord.ui.View):
    def __init__(
        self,
        current_step: int,
        settings: Settings,
        author_id: int | None = None,
    ):
        super().__init__(timeout=180)  # 3 minute timeout
        self.current_step = current_step
        self.total_steps = len(GETTING_STARTED_STEPS)
        self.settings = settings
        self.author_id = author_id
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.current_step <= 0
        self.next_button.disabled = self.current_step >= self.total_steps - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "ℹ️ Only the person who opened this guide can navigate it.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def update_message(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        embed = build_getting_started_embed(self.current_step, self.settings)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous Step", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_step = max(0, self.current_step - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="Next Step ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_step = min(self.total_steps - 1, self.current_step + 1)
        await self.update_message(interaction)


@ic.command(name="help", description="Show the paginated help menu for all IC bot commands.")
async def ic_help(interaction: discord.Interaction) -> None:
    settings = await resolve_guild_settings(interaction.client, interaction)

    categories = build_dynamic_help_categories(interaction, settings)
    total_pages = max(1, len(categories))
    current_page = 0

    embed = build_help_embed(current_page, total_pages, settings, categories=categories)
    author_id = interaction.user.id if interaction.user else None
    view = HelpPaginationView(
        current_page,
        total_pages,
        settings,
        categories=categories,
        author_id=author_id,
    )

    await interaction.response.send_message(embed=embed, view=view)


@ic.command(
    name="getting-started",
    description="Interactive guided walkthrough for new students and community members.",
)
async def ic_getting_started(interaction: discord.Interaction) -> None:
    settings = await resolve_guild_settings(interaction.client, interaction)
    current_step = 0
    embed = build_getting_started_embed(current_step, settings)
    author_id = interaction.user.id if interaction.user else None
    view = GettingStartedView(current_step, settings, author_id=author_id)
    await interaction.response.send_message(embed=embed, view=view)
