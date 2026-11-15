from typing import Literal

import aiohttp
import discord
from discord import app_commands

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import safe_error_summary
from icbot.registry import guard_feature
from icbot.services.gemini_client import GeminiError

# 5MB limit
MAX_FILE_SIZE = 5 * 1024 * 1024


class ResumeReviewer:
    @staticmethod
    async def review(interaction: discord.Interaction, attachment: discord.Attachment, discipline: str):
        base_settings = getattr(interaction.client, "settings", None)
        settings = await resolve_guild_settings(interaction.client, interaction)
        container = get_container(interaction.client)
        if not base_settings or not settings.gemini_api_key:
            await interaction.followup.send("❌ Gemini API is not configured.", ephemeral=True)
            return

        if container.llm_killed or getattr(interaction.client, "_llm_killed", False):
            await interaction.followup.send(
                "🛑 **Resume features are currently paused by an administrator.** Please try again later.",
                ephemeral=True,
            )
            return

        if attachment.size > MAX_FILE_SIZE:
            await interaction.followup.send(
                "❌ File is too large. Please upload a file smaller than 5MB.", ephemeral=True
            )
            return

        valid_mime_types = ["application/pdf", "image/png", "image/jpeg", "image/jpg"]
        mime = attachment.content_type or ""

        # fallback for missing content_type from discord
        if not mime or mime == "application/octet-stream":
            fn = attachment.filename.lower()
            if fn.endswith(".pdf"):
                mime = "application/pdf"
            elif fn.endswith(".png"):
                mime = "image/png"
            elif fn.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"

        if mime not in valid_mime_types:
            await interaction.followup.send(
                "❌ Unsupported file type. Please upload a PDF, PNG, or JPG.", ephemeral=True
            )
            return

        try:
            file_bytes = await attachment.read()
        except (discord.HTTPException, OSError, ValueError):
            await interaction.followup.send("❌ Failed to read the attached file.", ephemeral=True)
            return

        model = getattr(settings, "gemini_resume_model", "gemini-2.5-pro")

        # 1. ATS Check
        if discipline == "HW":
            ats_prompt = "You are a strict ATS (Applicant Tracking System). Determine if the provided document is a valid resume (specifically for hardware engineering roles). Reply ONLY with 'PASS' if it is a valid resume, or 'FAIL' followed by a short reason if it is not."
        elif discipline == "ELE":
            ats_prompt = "You are a strict ATS (Applicant Tracking System). Determine if the provided document is a valid resume (specifically for electrical engineering roles). Reply ONLY with 'PASS' if it is a valid resume, or 'FAIL' followed by a short reason if it is not."
        else:
            ats_prompt = "You are a strict ATS (Applicant Tracking System). Determine if the provided document is a valid resume (specifically for software engineering or related tech roles). Reply ONLY with 'PASS' if it is a valid resume, or 'FAIL' followed by a short reason if it is not."

        try:
            ats_res = await container.gemini_generate(
                api_key=settings.gemini_api_key,
                model=model,
                prompt=ats_prompt,
                inline_images=[(mime, file_bytes)],
                temperature=0.0,
                timeout_seconds=45,
            )

            ats_res_clean = ats_res.strip().upper()
            if not ats_res_clean.startswith("PASS"):
                reason = ats_res_clean.removeprefix("FAIL").strip(" :-\n")
                if not reason:
                    reason = "Document does not appear to be a tech resume."
                await interaction.followup.send(f"❌ **ATS Scan Failed**: {reason}", ephemeral=True)
                return
        except (GeminiError, TimeoutError, aiohttp.ClientError, ValueError) as e:
            safe = safe_error_summary(e, settings.secret_values())
            await interaction.followup.send(
                f"❌ Failed to reach Gemini for ATS scan ({type(e).__name__}): {safe}", ephemeral=True
            )
            return

        # 2. Senior Review
        if discipline == "HW":
            review_prompt = getattr(settings, "gemini_resume_prompt_hw", "")
            if not review_prompt:
                base = getattr(settings, "gemini_resume_prompt", "")
                if base:
                    review_prompt = (
                        base.replace("Software Engineering", "Hardware Engineering")
                        .replace("SWE/CS", "HW")
                        .replace("SWE", "HW")
                        .replace("Software", "Hardware")
                        .replace("Computer Science", "Computer Engineering")
                    )
                else:
                    review_prompt = "You are a Senior Hardware Engineer reviewing a resume. Provide Overall Rating, Pros, Cons, and Actionable feedback."
        elif discipline == "ELE":
            review_prompt = getattr(settings, "gemini_resume_prompt_ele", "")
            if not review_prompt:
                base = getattr(settings, "gemini_resume_prompt", "")
                if base:
                    review_prompt = (
                        base.replace("Software Engineering", "Electrical Engineering")
                        .replace("SWE/CS", "EE")
                        .replace("SWE", "EE")
                        .replace("Software", "Electrical")
                        .replace("Computer Science", "Electrical Engineering")
                    )
                else:
                    review_prompt = "You are a Senior Electrical Engineer reviewing a resume. Provide Overall Rating, Pros, Cons, and Actionable feedback."
        else:
            review_prompt = getattr(settings, "gemini_resume_prompt", "")
            if not review_prompt:
                review_prompt = "You are a Senior Software Engineer reviewing a resume. Provide Overall Rating, Pros, Cons, and Actionable feedback."

        try:
            review_res = await container.gemini_generate(
                api_key=settings.gemini_api_key,
                model=model,
                prompt="Based on the attached resume, please provide your review.",
                system_instruction=review_prompt,
                inline_images=[(mime, file_bytes)],
                temperature=0.3,
                timeout_seconds=120,
            )
        except (GeminiError, TimeoutError, aiohttp.ClientError, ValueError) as e:
            safe = safe_error_summary(e, settings.secret_values())
            await interaction.followup.send(
                f"❌ Failed to reach Gemini for review ({type(e).__name__}): {safe}", ephemeral=True
            )
            return

        # Format output
        # Truncate to Discord embed summary limits safely
        raw_text = review_res[:3900]
        if len(review_res) > 3900:
            raw_text += "...\n*(Response truncated due to length limits)*"

        embed = system_embed(
            settings,
            title=f"📄 InstigateAI {discipline} Resume Review",
            summary=raw_text,
            footer_hint="Powered by InstigateAI",
        )
        if attachment.url and mime.startswith("image/"):
            embed.set_thumbnail(url=attachment.url)

        await interaction.followup.send(embed=embed)


@ic.command(name="resume", description="Submit your resume for a InstigateAI review.")
@app_commands.describe(
    discipline="The discipline of the resume (SWE, HW, ELE)", file="Your resume (PDF, PNG, JPG)"
)
@app_commands.checks.cooldown(1, 1800, key=lambda i: i.user.id)
async def resume_cmd(
    interaction: discord.Interaction, discipline: Literal["SWE", "HW", "ELE"], file: discord.Attachment
):
    if not await guard_feature(interaction, "ai"):
        return
    await interaction.response.defer(thinking=True)
    await ResumeReviewer.review(interaction, file, discipline)
