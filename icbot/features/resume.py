from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

import aiohttp
import discord
from discord import app_commands

from icbot.app import ic
from icbot.config import Settings
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import safe_error_summary
from icbot.registry import guard_feature
from icbot.services.gemini_client import GeminiError, ModelRole

log = logging.getLogger(__name__)

# File boundaries
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_PDF_PAGES = 5  # Maximum page limit for student resumes

# Magic byte signatures
_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# Prohibited active / script tags in PDF documents
_FORBIDDEN_PDF_ACTIONS = (
    b"/JavaScript",
    b"/JS",
    b"/Launch",
    b"/EmbeddedFiles",
    b"/SubmitForm",
    b"/ImportData",
    b"/RichMedia",
)

# Canonical discipline presets
DISCIPLINE_PRESETS: dict[str, dict[str, str]] = {
    "swe": {
        "title": "Software Engineering & Tech",
        "focus": "Full-stack development, systems, algorithms, distributed systems, cloud infrastructure, and technical projects.",
        "skills_hint": "Languages (Python, Java, TypeScript, C++), frameworks, databases, Docker/K8s, CI/CD, testing, system architecture.",
    },
    "hw": {
        "title": "Hardware & Embedded Systems",
        "focus": "Digital/analog design, microcontrollers, embedded C/C++, FPGA, ASIC, PCB layout, computer architecture, and hardware verification.",
        "skills_hint": "VHDL/Verilog, ARM, RTOS, Altium/KiCad, oscilloscopes, logic analyzers, bus protocols (SPI, I2C, CAN, PCIe), board bring-up.",
    },
    "ele": {
        "title": "Electrical Systems & Power",
        "focus": "Power electronics, circuits, RF/wireless, control systems, signal processing, analog/digital design, and energy systems.",
        "skills_hint": "MATLAB/Simulink, SPICE, LabVIEW, power distribution, signal integrity, electromagnetic compatibility, PLC programming.",
    },
    "data": {
        "title": "Data Science, Analytics & AI",
        "focus": "Statistical modeling, machine learning, data engineering, exploratory data analysis, pipeline orchestration, and business intelligence.",
        "skills_hint": "Python (pandas, NumPy, scikit-learn, PyTorch/TensorFlow), SQL, ETL/ELT pipelines, Tableau/PowerBI, A/B testing, cloud warehouses.",
    },
    "business": {
        "title": "Business, Consulting & Finance",
        "focus": "Financial modeling, market research, business operations, strategy consulting, product management, and stakeholder leadership.",
        "skills_hint": "Financial valuation (DCF, LBO), Excel/VBA, Bloomberg/CapIQ, market sizing, KPI dashboards, project management, client presentations.",
    },
    "accounting": {
        "title": "Accounting & Auditing",
        "focus": "Financial reporting, taxation, audit compliance, ledger reconciliation, variance analysis, and internal controls.",
        "skills_hint": "GAAP, IFRS, ERP systems (SAP, Oracle, NetSuite), Excel (pivot tables, lookup formulas), audit sampling, tax preparation, CPA candidacy.",
    },
    "law": {
        "title": "Legal, Compliance & Policy",
        "focus": "Legal research, case preparation, statutory analysis, regulatory compliance, contract drafting, and policy advocacy.",
        "skills_hint": "Legal research (Westlaw, LexisNexis), brief drafting, bluebook citations, regulatory filings, contract review, compliance audits.",
    },
    "marketing": {
        "title": "Marketing, Communications & Design",
        "focus": "Growth marketing, brand positioning, content creation, UI/UX design, campaign execution, and audience analytics.",
        "skills_hint": "SEO/SEM, conversion rate optimization (CRO), Google Analytics, Figma/Adobe Creative Suite, copy editing, user research, wireframing.",
    },
    "science": {
        "title": "Science, Healthcare & Research",
        "focus": "Laboratory experimentation, clinical protocols, life/physical sciences, empirical research, and academic publications.",
        "skills_hint": "Experimental protocols, laboratory instrumentation (PCR, chromatography, spectroscopy), statistical software (R, SPSS), literature review.",
    },
    "general": {
        "title": "General Campus & New-Graduate",
        "focus": "Transferable professional competencies, university coursework, academic leadership, and co-op/internship achievements across all faculties.",
        "skills_hint": "Communication, quantitative problem solving, team leadership, cross-functional collaboration, project management.",
    },
}

DISCIPLINE_CHOICES = [
    app_commands.Choice(name="Software Engineering & Tech (SWE)", value="swe"),
    app_commands.Choice(name="Hardware & Embedded Systems (HW)", value="hw"),
    app_commands.Choice(name="Electrical & Systems Engineering (ELE)", value="ele"),
    app_commands.Choice(name="Data Science, Analytics & AI", value="data"),
    app_commands.Choice(name="Business, Consulting & Finance", value="business"),
    app_commands.Choice(name="Accounting & Auditing", value="accounting"),
    app_commands.Choice(name="Legal, Compliance & Policy", value="law"),
    app_commands.Choice(name="Marketing, Communications & Design", value="marketing"),
    app_commands.Choice(name="Science, Healthcare & Research", value="science"),
    app_commands.Choice(name="General Campus & New-Graduate", value="general"),
]


@dataclass(frozen=True)
class ResumeValidationResult:
    """Outcome of offline document and content signature verification."""

    valid: bool
    mime_type: str
    error: str | None = None
    page_count: int = 1
    is_scanned: bool = False
    warning: str | None = None


def validate_resume_bytes(
    file_bytes: bytes,
    filename: str,
    declared_content_type: str | None = None,
) -> ResumeValidationResult:
    """
    Perform deep validation of resume attachment bytes without executing any embedded actions.

    Verifies:
    1. Size within MAX_FILE_SIZE (5 MB) and non-zero bytes.
    2. Magic byte content signature matches declared type (PDF, PNG, JPEG).
    3. Filename extension matches content signature.
    4. Safe PDF inspection: bounds page ceiling (5 pages) and detects forbidden script/launch actions.
    5. Flags scanned PDFs lacking selectable text streams.
    """
    if len(file_bytes) > MAX_FILE_SIZE:
        return ResumeValidationResult(
            valid=False,
            mime_type="",
            error="File is too large. Please upload a file smaller than 5MB.",
        )
    if len(file_bytes) == 0:
        return ResumeValidationResult(
            valid=False,
            mime_type="",
            error="The uploaded file is empty (0 bytes).",
        )

    # Magic byte inspection
    detected_mime = ""
    # PDF magic signature %PDF-
    if file_bytes.startswith(_PDF_MAGIC) or (
        len(file_bytes) > 4 and file_bytes[:1024].find(_PDF_MAGIC) != -1
    ):
        detected_mime = "application/pdf"
    elif file_bytes.startswith(_PNG_MAGIC):
        detected_mime = "image/png"
    elif file_bytes.startswith(_JPEG_MAGIC):
        detected_mime = "image/jpeg"
    else:
        return ResumeValidationResult(
            valid=False,
            mime_type="",
            error="Unsupported file format or invalid content signature. Please upload an authentic PDF, PNG, or JPG.",
        )

    fn_lower = filename.lower().strip()
    if detected_mime == "application/pdf" and not fn_lower.endswith(".pdf"):
        return ResumeValidationResult(
            valid=False,
            mime_type="",
            error="File extension does not match detected PDF content signature.",
        )
    if detected_mime.startswith("image/") and not any(
        fn_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg")
    ):
        return ResumeValidationResult(
            valid=False,
            mime_type="",
            error=f"File extension does not match detected {detected_mime} image format.",
        )

    page_count = 1
    is_scanned = False
    warning: str | None = None

    if detected_mime == "application/pdf":
        # Prohibit executable / script tags in PDF
        for forbidden in _FORBIDDEN_PDF_ACTIONS:
            if forbidden in file_bytes:
                tag_name = forbidden.decode("ascii", errors="ignore")
                return ResumeValidationResult(
                    valid=False,
                    mime_type=detected_mime,
                    error=f"Security rejection: PDF contains forbidden active content or script execution tags ({tag_name}).",
                )

        # Count pages via object catalog
        page_matches = len(re.findall(rb"/Type\s*/Page\b", file_bytes))
        count_match = re.search(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", file_bytes)
        if count_match:
            try:
                page_count = max(page_matches, int(count_match.group(1)))
            except ValueError:
                page_count = max(1, page_matches)
        else:
            page_count = max(1, page_matches)

        if page_count > MAX_PDF_PAGES:
            return ResumeValidationResult(
                valid=False,
                mime_type=detected_mime,
                error=f"PDF exceeds page limit ({page_count} pages detected; student resumes must be {MAX_PDF_PAGES} pages or fewer).",
            )

        # Scanned PDF detection: check whether fonts or text streams exist
        has_font_or_text = bool(re.search(rb"/Font\b|BT\s|/Text\b", file_bytes))
        has_images = bool(re.search(rb"/Subtype\s*/Image\b", file_bytes))
        if has_images and not has_font_or_text:
            is_scanned = True
            warning = (
                "⚠️ **Scanned Image Warning**: Your PDF appears to be a scanned image without a selectable text layer. "
                "Employer ATS systems often reject scanned files. We recommend exporting directly from Word, Google Docs, or LaTeX."
            )

    return ResumeValidationResult(
        valid=True,
        mime_type=detected_mime,
        page_count=page_count,
        is_scanned=is_scanned,
        warning=warning,
    )


def normalize_discipline(val: str) -> tuple[str, str]:
    """
    Resolve input string to a canonical discipline key and friendly display title.
    Preserves backwards compatibility for 'SWE', 'HW', 'ELE' while supporting all faculties.
    """
    v = (val or "").strip().lower()
    if v in ("swe", "software", "cs", "computer science", "tech", "developer"):
        return "swe", DISCIPLINE_PRESETS["swe"]["title"]
    if v in ("hw", "hardware", "ce", "computer engineering", "embedded"):
        return "hw", DISCIPLINE_PRESETS["hw"]["title"]
    if v in ("ele", "electrical", "ee", "power", "electronics"):
        return "ele", DISCIPLINE_PRESETS["ele"]["title"]
    if v in ("data", "ai", "ml", "data science", "analytics", "bi"):
        return "data", DISCIPLINE_PRESETS["data"]["title"]
    if v in ("business", "finance", "fin", "consulting", "management", "commerce"):
        return "business", DISCIPLINE_PRESETS["business"]["title"]
    if v in ("accounting", "acct", "audit", "tax", "cpa"):
        return "accounting", DISCIPLINE_PRESETS["accounting"]["title"]
    if v in ("law", "legal", "policy", "paralegal", "compliance"):
        return "law", DISCIPLINE_PRESETS["law"]["title"]
    if v in ("marketing", "ux", "ui", "design", "creative", "communications"):
        return "marketing", DISCIPLINE_PRESETS["marketing"]["title"]
    if v in ("science", "health", "research", "biology", "chemistry", "physics"):
        return "science", DISCIPLINE_PRESETS["science"]["title"]
    if v in ("general", "newgrad", "entry", "intern", "coop", "campus"):
        return "general", DISCIPLINE_PRESETS["general"]["title"]

    # Custom discipline entered by user
    clean_label = val.strip() if val and val.strip() else "General New-Graduate"
    return "custom", clean_label


def build_resume_system_instruction(
    discipline_key: str,
    discipline_title: str,
    target_role: str,
    settings: Settings,
    is_scanned: bool = False,
) -> str:
    """
    Construct a specialized career rubric system instruction incorporating discipline expectations,
    target role goals, and 7 structured evaluation dimensions.
    """
    # Check if custom guild prompt overrides exist for backward compatibility
    if discipline_key == "hw":
        custom_p = getattr(settings, "gemini_resume_prompt_hw", "")
        if custom_p:
            return custom_p
    elif discipline_key == "ele":
        custom_p = getattr(settings, "gemini_resume_prompt_ele", "")
        if custom_p:
            return custom_p
    elif discipline_key == "swe":
        custom_p = getattr(settings, "gemini_resume_prompt", "")
        if custom_p:
            return custom_p

    preset = DISCIPLINE_PRESETS.get(discipline_key)
    discipline_context = preset["focus"] if preset else f"Specialized focus on {discipline_title}."
    skills_context = (
        preset["skills_hint"] if preset else "Core industry tools, frameworks, and domain concepts."
    )
    role_clause = f" specifically targeting the role '{target_role}'" if target_role else ""

    scanned_clause = (
        "\n- **CRITICAL NOTE ON SCANNED PDF**: The document is an image-based scan lacking selectable text. "
        "Highlight that modern ATS parsers cannot extract text from scans, and urge exporting from source documents."
        if is_scanned
        else ""
    )

    return (
        f"You are an expert career advisor and senior industry hiring mentor specializing in {discipline_title}{role_clause}.\n"
        "Your task is to conduct an objective, constructive, and comprehensive review of the candidate's resume.\n\n"
        f"**DISCIPLINE CONTEXT:**\n{discipline_context}\n"
        f"**RELEVANT SKILLS & EXPECTATIONS:**\n{skills_context}\n"
        f"{scanned_clause}\n\n"
        "**EVALUATION RUBRIC & STRUCTURED CRITERIA:**\n"
        "Evaluate the resume across these 7 core dimensions:\n"
        "1. **Formatting & Visual Readability**: Single-column structure, font legibility (10-12pt), whitespace, standard margins, and clear hierarchy.\n"
        "2. **Measurable Accomplishments**: Use of Google XYZ formula ('Accomplished [X], as measured by [Y], by doing [Z]') and quantified metrics (%, $, time, volume).\n"
        "3. **Relevant Skills & Technology Stack**: Categorized hard skills, relevant tools/frameworks, without proficiency bars or fluff.\n"
        "4. **Relevance to Target Role**: Tailoring of project summaries, course work, and bullet points to industry expectations.\n"
        "5. **Clarity, Precision & Tone**: Active voice, strong action verbs, zero first-person pronouns ('I', 'we', 'my'), concise bullets.\n"
        "6. **ATS-Friendly Organization**: Standard headings (Summary/Objective, Education, Experience, Projects, Skills), no complex tables/text boxes.\n"
        "7. **Actionable Edits & Concrete Suggestions**: Provide at least 2 explicit 'Before -> After' rewritten bullet points.\n\n"
        "**OUTPUT FORMAT RULES:**\n"
        "- Structure your review into clear Markdown sections:\n"
        "  - `## Executive Summary & Overall Score` (Score out of 10 with clear justification)\n"
        "  - `## Dimension Ratings & Analysis` (Brief assessment of each of the 7 criteria)\n"
        "  - `## Key Strengths` (2-3 distinct highlights)\n"
        "  - `## Priority Areas for Improvement` (2-3 highest-leverage suggestions)\n"
        "  - `## Actionable Before & After Rewrites` (At least two concrete before/after bullet rewrites)\n"
        "- **OPINION & ADVISORY LABELING**: Label all critiques as evaluative recommendations and best practices, "
        "NEVER as definitive ATS pass/fail verdicts or screening guarantees.\n"
        "- Conclude with: '💡 *Note: Recommendations are advisory based on standard campus career best practices. Employer screening criteria and recruiter preferences vary.*'"
    )


def build_offline_checklist_embed(
    settings: Settings, discipline_title: str, target_role: str = ""
) -> discord.Embed:
    """Build a comprehensive, self-service offline resume checklist embed for students."""
    role_desc = (
        f" for **{target_role}** ({discipline_title})" if target_role else f" for **{discipline_title}**"
    )
    checklist_text = (
        f"### 📋 Self-Review Resume Checklist{role_desc}\n\n"
        "**1. ATS Format & Layout**\n"
        "• Single-column layout (no tables, multi-column boxes, or headers/footers).\n"
        "• Standard clean fonts: Calibri, Arial, Helvetica, Times New Roman (10–12pt body, 14–16pt headings).\n"
        '• Standard margins (0.5" to 1") with consistent line spacing.\n'
        "• Saved as clean vector PDF exported directly from Word, Docs, or LaTeX (never a scanned image).\n\n"
        "**2. Impact & Accomplishments (XYZ Formula)**\n"
        "• Start every bullet with a strong action verb (e.g., *Architected*, *Engineered*, *Optimized*, *Audited*).\n"
        "• Google XYZ Structure: *Accomplished [X], as measured by [Y], by doing [Z]*.\n"
        "• Include quantifiable metrics (e.g., *reduced latency by 35%*, *managed $50K budget*, *served 1,200 users*).\n"
        "• Zero first-person pronouns: remove all instances of *I*, *we*, *my*, or *our*.\n\n"
        "**3. Section Organization**\n"
        "• **Header**: Full Name, City/Province, Email, Phone, LinkedIn, GitHub/Portfolio.\n"
        "• **Education**: Degree, Major, Institution, Graduation Date (GPA optional if >= 3.5).\n"
        "• **Skills**: Grouped logically (e.g., Languages, Frameworks, Tools, Methodologies).\n"
        "• **Experience**: Reverse chronological order with Company, Title, Dates, Location.\n"
        "• **Projects**: Title, Tech Stack, 2–3 impact-focused bullet points.\n\n"
        "**4. Proofreading & Review**\n"
        "• Run spell-check; ensure consistent date formatting (e.g. *May 2024 – Aug 2024* throughout).\n"
        "• Keep under 1 page for undergraduate/new-grad resumes (2 pages only with 5+ years relevant experience).\n\n"
        "💡 *Tip: Run `/ic resume confirm:True` whenever you wish to submit your document for AI review.*"
    )

    return system_embed(
        settings,
        title=f"📄 {settings.bot_display_name} Offline Resume Checklist",
        summary=checklist_text,
        footer_hint=f"Privacy-First Career Tools • {settings.bot_display_name}",
    )


class ResumeReviewPaginationView(discord.ui.View):
    """Interactive paginator for oversized resume reviews (> 3500 characters)."""

    def __init__(
        self,
        *,
        settings: Settings,
        pages: list[str],
        discipline_title: str,
        user_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.settings = settings
        self.pages = pages
        self.discipline_title = discipline_title
        self.user_id = user_id
        self.current_page = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1

    def build_current_embed(self) -> discord.Embed:
        page_content = self.pages[self.current_page]
        return system_embed(
            self.settings,
            title=f"📄 {self.settings.bot_display_name} {self.discipline_title} Resume Review (Page {self.current_page + 1}/{len(self.pages)})",
            summary=page_content,
            footer_hint=f"Page {self.current_page + 1} of {len(self.pages)} • Ephemeral Feedback",
        )

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This review session belongs to another user.", ephemeral=True
            )
            return
        if self.current_page > 0:
            self.current_page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This review session belongs to another user.", ephemeral=True
            )
            return
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)


class ResumeConfirmView(discord.ui.View):
    """
    Opt-in disclosure view presenting third-party AI provider privacy notices,
    no-AI self-review checklist, or cancellation before file transmission.
    """

    def __init__(
        self,
        *,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
        discipline_key: str,
        discipline_title: str,
        target_role: str,
        settings: Settings,
    ) -> None:
        super().__init__(timeout=120)
        self.orig_interaction = interaction
        self.attachment = attachment
        self.discipline_key = discipline_key
        self.discipline_title = discipline_title
        self.target_role = target_role
        self.settings = settings
        self.user_id = interaction.user.id

    def build_disclosure_embed(self) -> discord.Embed:
        role_label = f"**Target Role:** `{self.target_role}`\n" if self.target_role else ""
        size_kb = max(1, self.attachment.size // 1024)
        disclosure_text = (
            f"**File:** `{self.attachment.filename}` ({size_kb} KB)\n"
            f"**Discipline:** `{self.discipline_title}`\n"
            f"{role_label}\n"
            "⚠️ **Third-Party AI Privacy Disclosure**:\n"
            "• **Transmission**: Submitting will forward your document to **Google Gemini** for automated review.\n"
            "• **Confidentiality**: Processing and data retention depend on the server operator's Google Cloud / Gemini API terms.\n"
            "• **Zero Local Retention**: The bot does **not** persist your resume file, personal information, or feedback to any database or disk.\n"
            "• **Ephemeral Privacy**: Feedback is delivered ephemerally and is visible only to you.\n\n"
            "Please confirm whether you want to proceed with AI analysis or view the offline checklist instead."
        )
        return system_embed(
            self.settings,
            title=f"📄 {self.settings.bot_display_name} Resume AI Review — Privacy Notice",
            summary=disclosure_text,
            footer_hint="Privacy-First Career Tools • Confidential & Ephemeral",
        )

    @discord.ui.button(label="Submit for AI Review", style=discord.ButtonStyle.primary, emoji="🚀")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This review session belongs to another user.", ephemeral=True
            )
            return

        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

        await interaction.response.edit_message(
            content="⏳ **Analyzing resume with Gemini...** This may take 15–30 seconds.",
            embed=None,
            view=None,
        )
        await ResumeReviewer.review(
            interaction,
            self.attachment,
            discipline=self.discipline_key,
            role=self.target_role,
            skip_confirm=True,
        )

    @discord.ui.button(label="View No-AI Checklist", style=discord.ButtonStyle.secondary, emoji="📋")
    async def checklist_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This review session belongs to another user.", ephemeral=True
            )
            return

        checklist_emb = build_offline_checklist_embed(self.settings, self.discipline_title, self.target_role)
        await interaction.response.edit_message(content=None, embed=checklist_emb, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This review session belongs to another user.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="❌ **Resume review cancelled.** Your document was not transmitted to any external provider.",
            embed=None,
            view=None,
        )


class ResumeReviewer:
    """Core review coordinator managing validation, two-stage evaluation, and output formatting."""

    @staticmethod
    async def review(
        interaction: discord.Interaction,
        attachment: discord.Attachment,
        discipline: str = "general",
        role: str = "",
        skip_confirm: bool = True,
    ) -> None:
        base_settings = getattr(interaction.client, "settings", None)
        settings = await resolve_guild_settings(interaction.client, interaction)
        container = get_container(interaction.client)

        disc_key, disc_title = normalize_discipline(discipline)

        # 1. Configuration & Killswitch checks
        if not base_settings or not settings.gemini_api_key:
            checklist_emb = build_offline_checklist_embed(settings, disc_title, role)
            await interaction.followup.send(
                content="❌ Gemini API is not configured. Here is our offline self-review checklist instead:",
                embed=checklist_emb,
                ephemeral=True,
            )
            return

        if container.llm_killed or getattr(interaction.client, "_llm_killed", False):
            await interaction.followup.send(
                "🛑 **Resume features are currently paused by an administrator.** Please try again later.",
                ephemeral=True,
            )
            return

        # 2. File read and content signature verification
        if attachment.size > MAX_FILE_SIZE:
            await interaction.followup.send(
                "❌ File is too large. Please upload a file smaller than 5MB.", ephemeral=True
            )
            return

        try:
            file_bytes = await attachment.read()
        except (discord.HTTPException, OSError, ValueError):
            await interaction.followup.send("❌ Failed to read the attached file.", ephemeral=True)
            return

        val_result = validate_resume_bytes(
            file_bytes,
            attachment.filename,
            declared_content_type=attachment.content_type,
        )
        if not val_result.valid:
            await interaction.followup.send(f"❌ {val_result.error}", ephemeral=True)
            return

        mime = val_result.mime_type
        model = getattr(settings, "gemini_resume_model", "gemini-2.5-pro")

        # 3. Stage 1: ATS Document Verification Scan
        # Prompt explicitly includes 'PASS' to maintain compatibility with mock provider contracts.
        role_clause = f" targeting {role}" if role else ""
        ats_prompt = (
            f"You are a strict ATS (Applicant Tracking System). Determine if the provided document is a valid resume "
            f"(specifically for {disc_title}{role_clause}). Reply ONLY with 'PASS' if it is a valid resume, "
            f"or 'FAIL' followed by a short reason if it is not."
        )

        try:
            ats_res = await container.gemini_generate(
                api_key=settings.gemini_api_key,
                model=model,
                prompt=ats_prompt,
                inline_images=[(mime, file_bytes)],
                temperature=0.0,
                timeout_seconds=45,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                task=ModelRole.RESUME,
            )

            raw_res = ats_res.strip()
            if not raw_res.upper().startswith("PASS"):
                if raw_res.upper().startswith("FAIL"):
                    reason = raw_res[4:].strip(" :-\n")
                else:
                    reason = raw_res.strip(" :-\n")
                if not reason:
                    reason = f"Document does not appear to be a valid {disc_title} resume."
                await interaction.followup.send(f"❌ **ATS Scan Failed**: {reason}", ephemeral=True)
                return
        except (GeminiError, TimeoutError, aiohttp.ClientError, ValueError) as e:
            safe = safe_error_summary(e, settings.secret_values())
            await interaction.followup.send(
                f"❌ Failed to reach Gemini for ATS scan ({type(e).__name__}): {safe}", ephemeral=True
            )
            return

        # 4. Stage 2: Comprehensive Multi-Criteria Senior Review
        review_instruction = build_resume_system_instruction(
            discipline_key=disc_key,
            discipline_title=disc_title,
            target_role=role,
            settings=settings,
            is_scanned=val_result.is_scanned,
        )

        try:
            review_res = await container.gemini_generate(
                api_key=settings.gemini_api_key,
                model=model,
                prompt="Based on the attached resume, please provide your review.",
                system_instruction=review_instruction,
                inline_images=[(mime, file_bytes)],
                temperature=0.3,
                timeout_seconds=120,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                task=ModelRole.RESUME,
            )
        except (GeminiError, TimeoutError, aiohttp.ClientError, ValueError) as e:
            safe = safe_error_summary(e, settings.secret_values())
            await interaction.followup.send(
                f"❌ Failed to reach Gemini for review ({type(e).__name__}): {safe}", ephemeral=True
            )
            return

        # Free in-memory attachment bytes immediately
        del file_bytes

        # 5. Output Formatting & Ephemeral Pagination
        # Append scanned warning note to output if applicable
        if val_result.warning and val_result.warning not in review_res:
            review_res = f"{val_result.warning}\n\n" + review_res

        # Format output: if under 3900 characters, single embed; otherwise paginate and provide file
        if len(review_res) <= 3900:
            embed = system_embed(
                settings,
                title=f"📄 {settings.bot_display_name} {discipline} Resume Review",
                summary=review_res,
                footer_hint=f"Powered by {settings.bot_display_name} • Ephemeral Feedback",
            )
            if attachment.url and mime.startswith("image/"):
                embed.set_thumbnail(url=attachment.url)

            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            # Split into <= 3500 char chunks for paginator
            chunks: list[str] = []
            remaining = review_res
            while remaining:
                if len(remaining) <= 3500:
                    chunks.append(remaining)
                    break
                split_at = remaining.rfind("\n\n", 0, 3500)
                if split_at == -1:
                    split_at = remaining.rfind("\n", 0, 3500)
                if split_at == -1:
                    split_at = 3500
                chunks.append(remaining[:split_at].strip())
                remaining = remaining[split_at:].strip()

            paginator = ResumeReviewPaginationView(
                settings=settings,
                pages=chunks,
                discipline_title=disc_title,
                user_id=interaction.user.id,
            )
            file_data = discord.File(
                io.BytesIO(review_res.encode("utf-8")),
                filename=f"{disc_key}_resume_review.md",
            )
            await interaction.followup.send(
                embed=paginator.build_current_embed(),
                view=paginator,
                file=file_data,
                ephemeral=True,
            )


@ic.command(
    name="resume", description="Submit your resume for an AI review across campus career disciplines."
)
@app_commands.describe(
    file="Your resume file (PDF, PNG, JPG up to 5MB)",
    discipline="Career discipline preset or faculty focus",
    role="Optional target role or goal (e.g. Software Developer, Accounting Analyst, Data Scientist)",
    confirm="Skip disclosure prompt and submit directly for review (default: False)",
)
@app_commands.choices(discipline=DISCIPLINE_CHOICES)
@app_commands.checks.cooldown(1, 1800, key=lambda i: i.user.id)
async def resume_cmd(
    interaction: discord.Interaction,
    file: discord.Attachment,
    discipline: str = "general",
    role: str = "",
    confirm: bool = False,
) -> None:
    """Submit a resume for multi-criteria career review with privacy disclosures and fallback support."""
    if not await guard_feature(interaction, "ai"):
        return

    settings = await resolve_guild_settings(interaction.client, interaction)
    disc_val = str(discipline or "general")
    disc_key, disc_title = normalize_discipline(disc_val)

    # If the user chose to bypass the disclosure prompt, defer and execute directly
    if confirm:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await ResumeReviewer.review(
            interaction,
            file,
            discipline=disc_val,
            role=role,
            skip_confirm=True,
        )
        return

    # Present privacy disclosure & confirmation view
    confirm_view = ResumeConfirmView(
        interaction=interaction,
        attachment=file,
        discipline_key=disc_key,
        discipline_title=disc_title,
        target_role=role.strip(),
        settings=settings,
    )
    await interaction.response.send_message(
        embed=confirm_view.build_disclosure_embed(),
        view=confirm_view,
        ephemeral=True,
    )
