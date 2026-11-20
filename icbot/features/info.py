from __future__ import annotations

import io
import sqlite3

import discord

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import redact_secrets
from icbot.logging import BOT_VERSION, generate_sanitized_issue_report
from icbot.permissions import can_manage_guild
from icbot.registry import FEATURE_MANIFESTS, get_feature_registry


@ic.command(name="serverinfo", description="Display server information.")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    settings = await resolve_guild_settings(interaction.client, interaction)

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
        ("🎭 Roles", f"**{role_count}** Roles", True),
    ]

    embed = system_embed(
        settings,
        title=f"ℹ️ {guild.name} Server Info",
        summary=summary,
        fields=fields,
        footer_hint="Server Information",
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    await interaction.response.send_message(embed=embed)


@ic.command(name="botinfo", description="Display bot capabilities and information.")
async def botinfo(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)

    llm_model = (
        str(getattr(settings, "gemini_model", "gemini-2.5-flash") or "gemini-2.5-flash").strip()
        or "gemini-2.5-flash"
    )

    caps = settings.capabilities()
    cap_labels = (
        ("llm_assistant", "LLM Assistant"),
        ("resume_review", "Resume Review"),
        ("odds_api", "Sports Odds"),
        ("ttc_alerts", "TTC Alerts"),
        ("go_alerts", "GO Transit Alerts"),
        ("job_alerts", "Job Alerts"),
        ("market_watch", "Market Watch"),
        ("market_daily_report", "Daily Market Report"),
        ("btc_watch", "BTC Watch"),
    )
    cap_lines: list[str] = []
    for key, label in cap_labels:
        st = caps.get(key)
        if st is None:
            continue
        if st.available:
            badge = "✅ Ready"
        elif st.configured:
            badge = f"⚠️ Misconfigured ({st.reason})" if st.reason else "⚠️ Misconfigured"
        else:
            badge = "⚪ Disabled"
        cap_lines.append(f"• **{label}:** {badge}")

    summary = (
        f"**{settings.bot_display_name}** is a multi-purpose campus utility, economy simulation, and alert engine.\n\n"
        f"**Community:** {settings.embed_footer_text}\n"
        f"**Source:** {settings.source_attribution}\n"
        "**Tech Stack:** Python 3, discord.py, aiosqlite, Google Gemini API, GraphQL\n\n"
        "**Capabilities & Features:**\n"
        f"• **{settings.stock_exchange_name}:** Virtual market tracking {settings.currency_symbol}{settings.primary_stock_symbol} and user portfolios.\n"
        "• **Job Board:** Scrapes and filters technical New Grad/Intern roles from ATS endpoints (Lever, Greenhouse, Workday).\n"
        "• **Intelligent LLM:** Mentions and resume reviews powered dynamically by Google Gemini.\n"
        f"• **Casino & Games:** Economy system fully integrated with Casino table games ({settings.currency_name}).\n"
        f"• **University Toolkit:** Notes and RateMyProfessors data for {settings.rmp_university}.\n"
        "• **System Watchdogs:** Routine alerts for transit service notices, crypto watches, and daily market summaries."
    )

    fields = [
        ("LLM Engine", f"Powered by **{llm_model}**", False),
        ("Integration Readiness", "\n".join(cap_lines) or "Standard offline features active", False),
    ]
    task_health_fn = getattr(interaction.client, "task_health", None)
    if callable(task_health_fn):
        health_map = task_health_fn()
        if isinstance(health_map, dict) and health_map:
            secrets = settings.secret_values()
            t_lines = [
                redact_secrets(
                    th.to_summary_line()
                    if hasattr(th, "to_summary_line")
                    else f"• `{getattr(th, 'name', 'task')}`: **{getattr(th, 'state', 'unknown')}**",
                    secrets,
                )
                for th in health_map.values()
            ]
            rendered_tasks = "\n".join(t_lines)
            if len(rendered_tasks) > 1020:
                rendered_tasks = rendered_tasks[:1019].rstrip() + "…"
            fields.append(("Background Tasks", rendered_tasks, False))
    fields.append(("Source", settings.source_attribution or "Internal Instigate Cafe Repository", False))

    embed = system_embed(
        settings,
        title=f"🤖 {settings.bot_display_name} Information",
        summary=summary,
        fields=fields,
        footer_hint=settings.embed_footer_text,
    )

    # Try attaching local logo if possible (using standard static URL fallback for simplicity if host differs)
    # The prompt noted "the logo from assets/logo.png".
    try:
        logo_file = discord.File("assets/ic_logo.png", filename="logo.png")
    except FileNotFoundError:
        embed.set_thumbnail(url=None)
        await interaction.response.send_message(embed=embed)
        return

    embed.set_thumbnail(url="attachment://logo.png")

    try:
        await interaction.response.send_message(embed=embed, file=logo_file)
    except FileNotFoundError:
        embed.set_thumbnail(url=None)
        await interaction.response.send_message(embed=embed)


@ic.command(
    name="status", description="Display operational status and actionable diagnostics for server features."
)
async def status_cmd(interaction: discord.Interaction, issue_report: bool = False):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return

    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You need Manage Server permissions or an authorized admin role to view server status.",
            ephemeral=True,
        )
        return

    reg = get_feature_registry()
    container = get_container(interaction.client)
    secrets = settings.secret_values()

    # 1. Deployment Mode & Version
    proc_settings = getattr(container, "settings", None) or settings
    is_hosted = proc_settings.guild_id == 0
    mode_str = (
        "🌐 **Multi-Guild Hosted** (Per-Guild Isolated Settings)"
        if is_hosted
        else f"🏠 **Single-Guild Self-Hosted** (Pinned Guild `{proc_settings.guild_id}`)"
    )

    # 2. Storage / Database Health
    db_status_str = "Standard in-memory / uninitialized"
    schema_v = 0
    journal_m = "unknown"
    db = container.db or getattr(interaction.client, "db", None)
    health_fn = getattr(db, "get_storage_health", None) or getattr(db, "storage_health", None)
    if callable(health_fn):
        try:
            health = await health_fn()
            schema_v = getattr(health, "schema_version", 0)
            journal_m = getattr(health, "journal_mode", "unknown")
            wal_badge = "🟢 WAL" if health.journal_mode == "wal" else f"⚪ {health.journal_mode.upper()}"
            int_badge = "🟢 OK" if health.integrity_ok else "🔴 FAILED"
            db_status_str = f"{wal_badge} • Integrity: {int_badge} • Schema v{health.schema_version}"
        except (sqlite3.Error, RuntimeError, OSError, AttributeError):
            db_status_str = "⚠️ Error inspecting storage"

    # 3. Feature Modules Health & Actionable Fixes
    module_rows: list[str] = []
    actionable_fixes: list[str] = []
    feature_statuses: dict[str, str] = {}

    for mod_id, manifest in FEATURE_MANIFESTS.items():
        fh = reg.resolve_feature_health(mod_id, settings)
        feature_statuses[mod_id] = fh.status
        if fh.status == "ready":
            badge = "🟢 **Working**"
        elif fh.status == "degraded":
            badge = "🟡 **Degraded**"
            actionable_fixes.append(f"• **{fh.display_name}**: {fh.reason}")
        elif fh.status == "missing_dependency":
            badge = "🔴 **Dependency Missing**"
            dep_names = ", ".join(f"`{d}`" for d in fh.missing_dependencies)
            first_dep = fh.missing_dependencies[0] if fh.missing_dependencies else "prerequisite"
            actionable_fixes.append(
                f"• **{fh.display_name}**: Requires feature {dep_names}. Enable it with `/ic admin config features enable:{first_dep}`."
            )
        elif fh.status == "missing_config":
            badge = "🔴 **Missing Config**"
            if fh.missing_secrets:
                sec_names = ", ".join(f"`{s.upper()}`" for s in fh.missing_secrets)
                actionable_fixes.append(
                    f"• **{fh.display_name}**: Missing secret key {sec_names} in host environment."
                )
            if fh.missing_channels:
                ch_names = ", ".join(f"`{c}`" for c in fh.missing_channels)
                actionable_fixes.append(
                    f"• **{fh.display_name}**: Missing alert channel {ch_names}. Configure via `/ic admin setup`."
                )
        elif fh.status == "disabled":
            badge = "⚪ **Disabled**"
            actionable_fixes.append(
                f"• **{fh.display_name}**: Module is disabled. Enable with `/ic admin config features enable:{mod_id}`."
            )
        else:
            badge = f"⚪ **{fh.status.title()}**"

        module_rows.append(f"• **{manifest.display_name}**: {badge}")

    fields: list[tuple[str, str, bool]] = [
        ("Deployment Track", f"{mode_str}\n• **Bot Version:** `{BOT_VERSION}`", True),
        ("Database Health", db_status_str, True),
        (
            "Campus & Timezone",
            f"**Campus:** {settings.rmp_university}\n**Timezone:** `{settings.exams_tz}`",
            False,
        ),
        ("Feature Modules Status", "\n".join(module_rows), False),
    ]

    if actionable_fixes:
        fields.append(("Actionable Diagnostics & Fixes", "\n".join(actionable_fixes), False))
    else:
        fields.append(
            (
                "Actionable Diagnostics & Fixes",
                "✅ All active features and configurations are fully operational.",
                False,
            )
        )

    # 4. Background Tasks & Provider Status
    task_health_fn = getattr(interaction.client, "task_health", None)
    if callable(task_health_fn):
        health_map = task_health_fn()
        if isinstance(health_map, dict) and health_map:
            t_lines = [
                f"• `{name}`: **{th.state}**"
                + (f" (failures={th.consecutive_failures})" if th.consecutive_failures else "")
                for name, th in sorted(health_map.items())
            ]
            fields.append(("Provider Tasks & Supervisor", "\n".join(t_lines), False))

    # 5. Channel Permission Audit
    channel_audits: list[tuple[str, int]] = [
        ("Job Alerts", int(settings.job_alerts_channel_id or 0)),
        ("TTC Alerts", int(settings.ttc_alerts_channel_id or 0)),
        ("GO Alerts", int(settings.go_alerts_channel_id or 0)),
        ("Stock Ticker", int(settings.stock_channel_id or 0)),
    ]
    perm_warnings: list[str] = []
    verified_channels: list[str] = []

    bot_member = getattr(interaction.guild, "me", None)
    for label, ch_id in channel_audits:
        if ch_id <= 0:
            continue
        get_ch = getattr(interaction.guild, "get_channel", None)
        if not callable(get_ch):
            continue
        ch = get_ch(ch_id)
        if ch is None:
            perm_warnings.append(f"• **{label}**: Channel ID `{ch_id}` not found in this server.")
            continue
        if bot_member is not None and hasattr(ch, "permissions_for"):
            perms = ch.permissions_for(bot_member)
            missing: list[str] = []
            if not getattr(perms, "view_channel", True):
                missing.append("View Channel")
            if not getattr(perms, "send_messages", True):
                missing.append("Send Messages")
            if not getattr(perms, "embed_links", True):
                missing.append("Embed Links")
            ch_repr = getattr(ch, "mention", f"#{getattr(ch, 'name', ch_id)}")
            if missing:
                perm_warnings.append(f"• **{label}** ({ch_repr}): Missing {', '.join(missing)}")
            else:
                verified_channels.append(f"{label} ({ch_repr})")
        else:
            ch_repr = getattr(ch, "mention", f"#{getattr(ch, 'name', ch_id)}")
            verified_channels.append(f"{label} ({ch_repr})")

    if perm_warnings:
        fields.append(("⚠️ Channel Permission Warnings", "\n".join(perm_warnings), False))
    elif verified_channels:
        fields.append(
            ("Channel Permissions", f"✅ Bot permissions verified in: {', '.join(verified_channels)}", False)
        )

    # Redact any accidental secrets from field values
    safe_fields = [(name, redact_secrets(val, secrets), inline) for name, val, inline in fields]

    embed = system_embed(
        settings,
        title=f"📊 System Status — {interaction.guild.name}",
        summary=f"Live operational health diagnostic report for {settings.bot_display_name} (`{BOT_VERSION}`).",
        fields=safe_fields,
        footer_hint="Instigate Cafe Bot Diagnostics",
    )

    if issue_report:
        report_md = generate_sanitized_issue_report(
            version=BOT_VERSION,
            db_schema_version=schema_v,
            db_journal_mode=journal_m,
            feature_statuses=feature_statuses,
            secrets=secrets,
        )
        report_file = discord.File(io.BytesIO(report_md.encode("utf-8")), filename="status_issue_report.md")
        await interaction.response.send_message(embed=embed, file=report_file, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
