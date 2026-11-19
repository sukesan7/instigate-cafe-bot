from __future__ import annotations

import time
from typing import Any

import discord
from discord import app_commands

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import job_alert as job_alert_embed
from icbot.services.jobs.classification import canonicalize_company_name, canonicalize_title
from icbot.services.jobs.models import JobFilterCriteria, JobPosting
from icbot.services.jobs.rules import DISCIPLINE_PRESETS, DeterministicRuleEngine

_rule_engine = DeterministicRuleEngine()


def _pick_first(cols: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


async def _table_columns(db: Any, table: str) -> list[str]:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return [r[1] for r in rows]


def _format_age(ts: int | None, *, now: int | None = None) -> str:
    if not ts or ts <= 0:
        return ""
    cur = int(now) if now is not None else 0
    delta = max(0, cur - int(ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


@ic.command(name="jobs", description="Show the most recent jobs the bot has seen.")
@app_commands.describe(
    limit="How many jobs to show (max 25).",
    preset="Discipline filter preset (e.g. tech_swe, business_finance, accounting, design_ux, all).",
)
async def jobs_cmd(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = 10,
    preset: str | None = None,
):
    # Always public so everyone sees the same postings.
    await interaction.response.defer(thinking=True)

    bot = interaction.client
    base_settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if base_settings is None or db is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings/db).")
        return

    settings = await resolve_guild_settings(bot, interaction)
    container = get_container(bot)
    now_unix = int(container.clock())

    cols = await _table_columns(db, "job_seen")

    col_title = _pick_first(cols, ["title", "job_title"])
    col_company = _pick_first(cols, ["company", "org", "employer", "source_label"])
    col_location = _pick_first(cols, ["location", "job_location"])
    col_url = _pick_first(cols, ["url", "job_url", "link"])
    col_source = _pick_first(cols, ["source", "platform"])
    col_seen = _pick_first(
        cols, ["first_seen_unix", "seen_unix", "seen_at_unix", "created_unix", "inserted_unix"]
    )
    col_posted = _pick_first(cols, ["posted_unix", "posted_at_unix"])
    col_blob = _pick_first(cols, ["text_blob", "blob", "description", "snippet"])

    order_col = col_seen or col_posted or "rowid"
    select_cols = [
        c
        for c in [col_title, col_company, col_location, col_url, col_source, col_seen, col_posted, col_blob]
        if c
    ]
    if not select_cols:
        select_cols = cols

    scan_n = max(int(limit) * 25, 200)
    scan_n = min(scan_n, 1000)

    q = f"""
        SELECT {", ".join(select_cols)}
        FROM job_seen
        ORDER BY {order_col} DESC
        LIMIT ?
    """
    rows = await db.fetchall(q, (int(scan_n),))
    if not rows:
        await interaction.followup.send("No jobs found in `job_seen` yet.")
        return

    # Resolved filter criteria
    loc_allow = tuple(getattr(settings, "jobs_location_allowlist", None) or ())
    kw_allow = tuple(getattr(settings, "jobs_keywords_allowlist", None) or ())
    kw_deny = tuple(getattr(settings, "jobs_keywords_denylist", None) or ())
    early_career = bool(getattr(settings, "jobs_require_early_career", False))
    chosen_preset = (preset or "tech_swe").strip().lower()
    if chosen_preset not in DISCIPLINE_PRESETS:
        chosen_preset = "tech_swe"

    criteria = JobFilterCriteria(
        location_allowlist=loc_allow,
        keywords_allowlist=kw_allow,
        keywords_denylist=kw_deny,
        require_early_career=early_career,
        preset=chosen_preset,
    )

    embeds: list[discord.Embed] = []
    matched = 0

    for r in rows:
        j_dict = dict(zip(select_cols, r, strict=True))
        title = (j_dict.get(col_title) if col_title else None) or ""
        company = (j_dict.get(col_company) if col_company else None) or ""
        location = (j_dict.get(col_location) if col_location else None) or ""
        url = (j_dict.get(col_url) if col_url else None) or ""
        source = (j_dict.get(col_source) if col_source else None) or ""
        blob = (j_dict.get(col_blob) if col_blob else None) or " ".join([title, company, location, source])

        if not title:
            continue

        prov = "greenhouse"
        if "workday" in source.lower():
            prov = "workday"
        elif "lever" in source.lower() or ":" not in source:
            prov = "lever"

        posting = JobPosting(
            provider=prov,  # type: ignore[arg-type]
            employer=company or "Company",
            external_id=url,
            title=title,
            location=location,
            url=url,
            posted_unix=int(j_dict.get(col_posted) or 0) if col_posted and j_dict.get(col_posted) else 0,
            text_blob=blob,
            source_key=source,
        )

        decision = _rule_engine.evaluate(posting, criteria)
        if not decision.matched:
            continue

        seen = j_dict.get(col_seen) if col_seen else None
        age = _format_age(int(seen), now=now_unix) if isinstance(seen, (int, float)) else ""

        e = job_alert_embed(
            settings,
            title=title,
            company=company,
            url=url,
            location=location,
            source=source,
            footer_hint=f"Tracking System • {age}" if age else "Tracking System",
        )
        embeds.append(e)

        matched += 1
        if matched >= int(limit):
            break

    preset_obj = DISCIPLINE_PRESETS.get(chosen_preset, DISCIPLINE_PRESETS["tech_swe"])
    if not embeds:
        await interaction.followup.send(
            f"No roles matched the current '{preset_obj.label}' filters in the most recent scan.\n"
            "Try a different preset (e.g. `/ic jobs preset:all`) or check guild location allowlists."
        )
        return

    chunk_size = 5
    first_chunk = True
    for i in range(0, len(embeds), chunk_size):
        chunk = embeds[i : i + chunk_size]
        if first_chunk:
            await interaction.followup.send(
                content=f"🧰 **{preset_obj.label} Postings** (latest {matched})", embeds=chunk
            )
            first_chunk = False
        else:
            await interaction.followup.send(embeds=chunk)


@jobs_cmd.autocomplete("preset")
async def jobs_preset_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f"{p.label} ({k})", value=k)
        for k, p in DISCIPLINE_PRESETS.items()
        if cur in k.lower() or cur in p.label.lower()
    ][:25]


@ic.command(
    name="job_preview",
    description="Test job filter criteria against a sample posting with an explainable trace.",
)
@app_commands.describe(
    title="Role or job title to test (e.g. 'Financial Analyst Intern').",
    employer="Company name (e.g. 'Stripe' or 'RBC').",
    location="Location text (e.g. 'Toronto, ON' or 'Remote').",
    preset="Discipline preset (e.g. tech_swe, business_finance, accounting, design_ux, all).",
    require_early_career="Whether early-career / student status is strictly required.",
)
async def job_preview_cmd(
    interaction: discord.Interaction,
    title: str,
    employer: str = "Example Corp",
    location: str = "Toronto, ON",
    preset: str = "tech_swe",
    require_early_career: bool = True,
):
    await interaction.response.defer(ephemeral=True)

    bot = interaction.client
    settings = await resolve_guild_settings(bot, interaction)

    clean_title = canonicalize_title(title)
    clean_employer = canonicalize_company_name(employer)
    clean_preset = (preset or "tech_swe").strip().lower()
    if clean_preset not in DISCIPLINE_PRESETS:
        clean_preset = "tech_swe"

    loc_allow = tuple(getattr(settings, "jobs_location_allowlist", None) or ())
    kw_allow = tuple(getattr(settings, "jobs_keywords_allowlist", None) or ())
    kw_deny = tuple(getattr(settings, "jobs_keywords_denylist", None) or ())

    criteria = JobFilterCriteria(
        location_allowlist=loc_allow,
        keywords_allowlist=kw_allow,
        keywords_denylist=kw_deny,
        require_early_career=require_early_career,
        preset=clean_preset,
    )

    blob = f"{clean_title} {clean_employer} {location}".strip()
    posting = JobPosting(
        provider="greenhouse",
        employer=clean_employer,
        external_id="preview-01",
        title=clean_title,
        location=location,
        url="https://example.invalid/preview",
        posted_unix=int(time.time()),
        text_blob=blob,
    )

    decision = _rule_engine.evaluate(posting, criteria)
    preset_obj = DISCIPLINE_PRESETS[clean_preset]

    verdict_emoji = "✅" if decision.matched else "❌"
    verdict_text = "MATCHED (Eligible for announcement)" if decision.matched else "REJECTED (Filtered out)"
    color = 0x2ECC71 if decision.matched else 0xE74C3C

    embed = discord.Embed(
        title=f"{verdict_emoji} Job Filter Preview: {verdict_text}",
        color=color,
        description=f"**Decision Summary:**\n`{decision.explanation}`",
    )
    embed.add_field(
        name="Posting Under Test",
        value=f"**Title:** {clean_title}\n**Employer:** {clean_employer}\n**Location:** {location}",
        inline=False,
    )
    embed.add_field(
        name="Extracted Metadata",
        value=f"**Career Level:** `{decision.career_level}`\n**Disciplines:** `{', '.join(decision.disciplines) or 'None'}`",
        inline=True,
    )
    embed.add_field(
        name="Filter Preset",
        value=f"**{preset_obj.label}** (`{preset_obj.key}`)\n*{preset_obj.description}*",
        inline=True,
    )

    trace_lines = []
    for item in decision.trace:
        status_sym = "✅" if item.passed else "❌"
        trace_lines.append(f"{status_sym} **`{item.rule}`**: {item.reason}")

    embed.add_field(
        name=f"Explainable Rule Trace ({len(decision.trace)} checks)",
        value="\n".join(trace_lines)[:1024],
        inline=False,
    )
    embed.set_footer(text="Instigate Cafe Bot • Deterministic Rule Engine Preview")

    await interaction.followup.send(embed=embed, ephemeral=True)


@job_preview_cmd.autocomplete("preset")
async def job_preview_preset_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f"{p.label} ({k})", value=k)
        for k, p in DISCIPLINE_PRESETS.items()
        if cur in k.lower() or cur in p.label.lower()
    ][:25]
