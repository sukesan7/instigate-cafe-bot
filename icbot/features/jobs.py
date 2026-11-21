from __future__ import annotations

import contextlib
import sqlite3
import time
from typing import Any, Literal

import aiosqlite
import discord
from discord import app_commands

from icbot.app import ic
from icbot.config import Settings
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import (
    EMBED_KIND_JOBS,
    empty_state_embed,
    format_footer,
    paginated_embed,
    resolve_embed_color,
    safe_http_url,
    sanitize_text,
    sanitize_untrusted_text,
    system,
)
from icbot.permissions import can_manage_guild
from icbot.registry import guard_feature
from icbot.services.jobs.classification import canonicalize_company_name, canonicalize_title
from icbot.services.jobs.models import JobFilterCriteria, JobPosting
from icbot.services.jobs.rules import DISCIPLINE_PRESETS, DeterministicRuleEngine

_rule_engine = DeterministicRuleEngine()
_JOBS_PER_PAGE = 5


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
    cur = int(now) if now is not None else int(time.time())
    delta = max(0, cur - int(ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _safe_https_job_url(url: str | None) -> str | None:
    """Validate that an external ATS job posting URL strictly uses HTTPS."""
    safe = safe_http_url(url)
    if not safe or not safe.lower().startswith("https://"):
        return None
    return safe


def _format_https_apply_link(company_label: str, url: str | None) -> str:
    """Render a safe HTTPS Markdown apply link or meaningful fallback text without broken Markdown."""
    valid_https = _safe_https_job_url(url)
    if not valid_https:
        return "Link unavailable"
    clean_company = (
        (sanitize_untrusted_text(company_label, 40) or "Employer").replace("[", "\\[").replace("]", "\\]")
    )
    return f"[Apply on {clean_company}]({valid_https})"


# ---------------------------------------------------------------------------
# Pagination View
# ---------------------------------------------------------------------------


class JobPaginationView(discord.ui.View):
    """Interactive pagination view for browsing cached job board postings."""

    def __init__(
        self,
        *,
        settings: Settings,
        postings: list[JobPosting],
        preset_name: str,
        total_matched: int,
        last_updated_str: str,
        is_degraded: bool = False,
        query: str = "",
        location_filter: str = "",
        owner_id: int = 0,
    ) -> None:
        super().__init__(timeout=180)
        self.settings = settings
        self.postings = postings
        self.preset_name = preset_name
        self.total_matched = total_matched
        self.last_updated_str = last_updated_str
        self.is_degraded = is_degraded
        self.query = query
        self.location_filter = location_filter
        self.owner_id = int(owner_id)
        self.current_page = 0
        self.per_page = _JOBS_PER_PAGE
        self.total_pages = max(1, (len(postings) + self.per_page - 1) // self.per_page)
        self.message: discord.WebhookMessage | discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if self.owner_id and int(user_id or 0) != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the user who ran `/ic jobs` can navigate these pages.",
                ephemeral=True,
            )
            return False
        return True

    def build_current_embed(self) -> discord.Embed:
        preset_obj = DISCIPLINE_PRESETS.get(self.preset_name, DISCIPLINE_PRESETS["tech_swe"])
        start_idx = self.current_page * self.per_page
        end_idx = start_idx + self.per_page
        page_items = self.postings[start_idx:end_idx]

        filter_badges = [
            f"🎯 **Preset**: `{preset_obj.label}`",
            f"📊 **Matches**: `{self.total_matched}`",
        ]
        if self.query:
            filter_badges.append(f"🔍 **Query**: `{sanitize_untrusted_text(self.query, 60)}`")
        if self.location_filter:
            filter_badges.append(f"📍 **Location**: `{sanitize_untrusted_text(self.location_filter, 60)}`")
        if self.last_updated_str:
            filter_badges.append(f"🕒 **Cached**: `{sanitize_untrusted_text(self.last_updated_str, 40)}`")

        header_lines = [" • ".join(filter_badges)]
        if self.is_degraded:
            header_lines.append(
                "⚠️ *ATS provider feeds are cooling down or unreachable; displaying cached postings.*"
            )
        else:
            header_lines.append(
                "ℹ️ *Displaying cached postings ingested from configured Greenhouse, Lever, and Workday feeds.*"
            )

        fields: list[tuple[str, str, bool]] = []
        for j in page_items:
            clean_title = sanitize_untrusted_text(j.title, 80) or "Job Role"
            clean_company = sanitize_untrusted_text(j.employer, 40) or "Employer"
            clean_loc = sanitize_untrusted_text(j.location, 50) or "Location Not Listed"
            lvl = (
                sanitize_untrusted_text(j.career_level.replace("_", " ").title(), 40)
                if j.career_level and j.career_level != "unknown"
                else "Early Career"
            )
            src_raw = j.provider or j.source_key or "ATS"
            src_clean = src_raw.split(":", 1)[0].capitalize() if ":" in src_raw else src_raw.capitalize()
            src_label = sanitize_untrusted_text(src_clean, 40) or "ATS"

            if j.posted_unix and int(j.posted_unix) > 0:
                posted_part = f"<t:{int(j.posted_unix)}:R>"
            else:
                posted_part = "Date not listed"
            apply_link = _format_https_apply_link(j.employer, j.url)

            field_title = f"{clean_title} — {clean_company}"
            field_val = (
                f"📍 `{clean_loc}` • 🎓 `{lvl}` • 📡 `{src_label}`\n"
                f"📅 Posted: {posted_part} • 🔗 {apply_link}"
            )
            fields.append((field_title, field_val, False))

        embed = paginated_embed(
            self.settings,
            title=f"💼 Campus Job Board • Page {self.current_page + 1}/{self.total_pages}",
            description="\n\n".join(header_lines),
            fields=fields,
            page=self.current_page,
            total_pages=self.total_pages,
            kind=EMBED_KIND_JOBS,
            status="warning" if self.is_degraded else "normal",
            color=resolve_embed_color(
                self.settings,
                status="warning" if self.is_degraded else "normal",
            ),
        )
        embed.set_footer(
            text=format_footer(
                self.settings,
                "Jobs",
                page=self.current_page + 1,
                total_pages=self.total_pages,
            )
        )
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        self.current_page = max(0, self.current_page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    async def on_timeout(self) -> None:
        self.prev_button.disabled = True
        self.next_button.disabled = True
        if self.message:
            with contextlib.suppress(discord.DiscordException, AttributeError):
                await self.message.edit(view=self)


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------


@ic.command(name="jobs", description="Search through cached job postings with pagination and filters.")
@app_commands.describe(
    limit="How many jobs to scan (max 25).",
    preset="Discipline filter preset (e.g. tech_swe, business_finance, accounting, design_ux, all).",
    query="Optional keyword search on role title or employer.",
    location="Optional location query (e.g. 'Toronto' or 'Remote').",
    use_my_preferences="Filter results using your personal saved job preferences.",
)
async def jobs_cmd(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = 10,
    preset: str | None = None,
    query: str | None = None,
    location: str | None = None,
    use_my_preferences: bool = False,
):
    if not await guard_feature(interaction, "careers"):
        return
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
    is_degraded = bool(container.jobs_bad_sources or container.workday_bad_sources)

    try:
        cols = await _table_columns(db, "job_seen")
    except (sqlite3.Error, aiosqlite.Error, RuntimeError, ValueError, TypeError, AttributeError):
        cols = []

    if not cols:
        empty_embed = empty_state_embed(
            settings,
            title="💼 Campus Job Board • No Cached Listings",
            message=(
                "⚠️ ATS provider feeds are currently rate-limited or unreachable and no cached postings are stored yet."
                if is_degraded
                else "No job postings have been cached in `job_seen` yet."
            ),
            suggestion="Wait for the next background ingestion cycle or verify ATS provider configuration.",
            category="Jobs",
            status="warning" if is_degraded else "normal",
        )
        empty_embed.set_footer(text=format_footer(settings, "Jobs"))
        await interaction.followup.send(embed=empty_embed)
        return

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
        empty_embed = empty_state_embed(
            settings,
            title="💼 Campus Job Board • No Cached Listings",
            message=(
                "⚠️ ATS provider feeds are cooling down or unreachable, and no cached postings are available in `job_seen` yet."
                if is_degraded
                else "No jobs found in `job_seen` yet."
            ),
            suggestion="Check back after the background poller completes an ingestion cycle.",
            category="Jobs",
            status="warning" if is_degraded else "normal",
        )
        empty_embed.set_footer(text=format_footer(settings, "Jobs"))
        await interaction.followup.send(embed=empty_embed)
        return

    # User preferences resolution
    user_prefs: dict[str, Any] | None = None
    if use_my_preferences and hasattr(db, "get_user_job_preferences"):
        user_prefs = await db.get_user_job_preferences(interaction.user.id)

    # Resolved filter criteria
    loc_allow = list(getattr(settings, "jobs_location_allowlist", None) or ())
    kw_allow = list(getattr(settings, "jobs_keywords_allowlist", None) or ())
    kw_deny = list(getattr(settings, "jobs_keywords_denylist", None) or ())
    early_career = bool(getattr(settings, "jobs_require_early_career", False))
    chosen_preset = (preset or "").strip().lower()

    if user_prefs:
        if not chosen_preset:
            chosen_preset = user_prefs.get("preset") or "tech_swe"
        if user_prefs.get("locations"):
            loc_allow.extend(user_prefs["locations"])
        if user_prefs.get("keywords_allow"):
            kw_allow.extend(user_prefs["keywords_allow"])
        if user_prefs.get("keywords_deny"):
            kw_deny.extend(user_prefs["keywords_deny"])

    if not chosen_preset or chosen_preset not in DISCIPLINE_PRESETS:
        chosen_preset = "tech_swe"

    if location:
        loc_allow.append(location.strip())
    if query:
        kw_allow.append(query.strip())

    criteria = JobFilterCriteria(
        location_allowlist=tuple(loc_allow),
        keywords_allowlist=tuple(kw_allow),
        keywords_denylist=tuple(kw_deny),
        require_early_career=early_career,
        preset=chosen_preset,
    )

    matched_postings: list[JobPosting] = []
    latest_seen_ts: int = 0

    for r in rows:
        j_dict = dict(zip(select_cols, r, strict=True))
        t_val = (j_dict.get(col_title) if col_title else None) or ""
        c_val = (j_dict.get(col_company) if col_company else None) or ""
        l_val = (j_dict.get(col_location) if col_location else None) or ""
        u_val = (j_dict.get(col_url) if col_url else None) or ""
        s_val = (j_dict.get(col_source) if col_source else None) or ""
        b_val = (j_dict.get(col_blob) if col_blob else None) or f"{t_val} {c_val} {l_val} {s_val}"

        if not t_val:
            continue

        prov = "greenhouse"
        if "workday" in s_val.lower():
            prov = "workday"
        elif "lever" in s_val.lower() or ":" not in s_val:
            prov = "lever"

        seen_val = j_dict.get(col_seen) if col_seen else None
        if isinstance(seen_val, (int, float)) and int(seen_val) > latest_seen_ts:
            latest_seen_ts = int(seen_val)

        posting = JobPosting(
            provider=prov,  # type: ignore[arg-type]
            employer=c_val or "Company",
            external_id=u_val or t_val,
            title=t_val,
            location=l_val,
            url=u_val,
            posted_unix=int(j_dict.get(col_posted) or 0) if col_posted and j_dict.get(col_posted) else 0,
            text_blob=b_val,
            source_key=s_val,
        )

        decision = _rule_engine.evaluate(posting, criteria)
        if not decision.matched:
            continue

        # Overwrite parsed tags/level with evaluated decision
        object.__setattr__(posting, "career_level", decision.career_level)
        object.__setattr__(posting, "discipline_tags", decision.disciplines)
        matched_postings.append(posting)

    preset_obj = DISCIPLINE_PRESETS.get(chosen_preset, DISCIPLINE_PRESETS["tech_swe"])
    if not matched_postings:
        degraded_note = (
            "\n⚠️ *Note: One or more ATS provider feeds are currently rate-limited or unreachable.*"
            if is_degraded
            else ""
        )
        no_match_embed = empty_state_embed(
            settings,
            title="💼 Campus Job Board • No Matching Roles",
            message=(
                f"No cached roles matched the **{preset_obj.label}** filters in the most recent scan.{degraded_note}"
            ),
            suggestion=(
                "Try a different preset (e.g. `/ic jobs preset:all`), widen your location filter, "
                "or verify your saved `/ic job_preferences`."
            ),
            category="Jobs",
            status="warning" if is_degraded else "normal",
        )
        no_match_embed.set_footer(text=format_footer(settings, "Jobs"))
        await interaction.followup.send(embed=no_match_embed)
        return

    # Only include a cache age badge when a valid timestamp was recorded (never invent 'Recently')
    last_updated_str = _format_age(latest_seen_ts, now=now_unix) if latest_seen_ts > 0 else ""

    view = JobPaginationView(
        settings=settings,
        postings=matched_postings,
        preset_name=chosen_preset,
        total_matched=len(matched_postings),
        last_updated_str=last_updated_str,
        is_degraded=is_degraded,
        query=query or "",
        location_filter=location or "",
        owner_id=interaction.user.id,
    )

    first_embed = view.build_current_embed()
    sent_msg = await interaction.followup.send(embed=first_embed, view=view, wait=True)
    view.message = sent_msg


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


# ---------------------------------------------------------------------------
# Job Channel Subscriptions (/ic job_subscribe)
# ---------------------------------------------------------------------------


@ic.command(
    name="job_subscribe",
    description="[Admin] Manage server job alert channel subscriptions and presets.",
)
@app_commands.describe(
    action="Subscription action: 'subscribe' to add/update, 'unsubscribe' to remove, or 'list' to view all.",
    channel="The Discord channel to broadcast job alerts to.",
    preset="Discipline preset (e.g. tech_swe, business_finance, accounting, design_ux, all).",
    role="Optional role to mention with alerts.",
)
async def job_subscribe_cmd(
    interaction: discord.Interaction,
    action: Literal["subscribe", "unsubscribe", "list"] = "subscribe",
    channel: discord.TextChannel | None = None,
    preset: str = "tech_swe",
    role: discord.Role | None = None,
):
    if not await guard_feature(interaction, "careers"):
        return
    await interaction.response.defer(ephemeral=True)

    bot = interaction.client
    settings = await resolve_guild_settings(bot, interaction)
    db = getattr(bot, "db", None)
    if db is None:
        await interaction.followup.send("❌ Database unavailable.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "❌ Subscriptions can only be configured in a server.", ephemeral=True
        )
        return

    # Authorization check
    if not can_manage_guild(interaction, settings):
        await interaction.followup.send(
            "❌ You need administrator or Manage Server permissions to configure job subscriptions.",
            ephemeral=True,
        )
        return

    guild_id = guild.id
    clean_preset = (preset or "tech_swe").strip().lower()
    if clean_preset not in DISCIPLINE_PRESETS:
        clean_preset = "tech_swe"

    if action == "list" or (action == "subscribe" and channel is None):
        subs = await db.get_guild_job_subscriptions(guild_id)
        if not subs:
            # Fallback to guild settings single channel if present
            default_ch = getattr(settings, "job_alerts_channel_id", 0)
            if default_ch:
                subs = [
                    {
                        "channel_id": default_ch,
                        "preset": "tech_swe",
                        "role_id": None,
                        "enabled": True,
                    }
                ]

        if not subs:
            await interaction.followup.send(
                "ℹ️ No active job alert subscriptions configured for this server.\n"
                "Use `/ic job_subscribe channel:#channel preset:tech_swe` to subscribe a channel.",
                ephemeral=True,
            )
            return

        sub_fields: list[tuple[str, str, bool]] = []
        for s in subs:
            p_key = str(s.get("preset") or "tech_swe")
            p_obj = DISCIPLINE_PRESETS.get(p_key, DISCIPLINE_PRESETS["tech_swe"])
            clean_preset_key = sanitize_untrusted_text(p_key, 40) or "tech_swe"
            role_id_val = s.get("role_id")
            role_fmt = f"<@&{int(role_id_val)}>" if role_id_val else "None"
            status_fmt = "✅ Enabled" if s.get("enabled", True) else "⏸️ Disabled"
            ch_id = int(s["channel_id"])
            ch_obj = bot.get_channel(ch_id) if hasattr(bot, "get_channel") else None
            raw_ch_name = getattr(ch_obj, "name", None)
            clean_ch_name = sanitize_untrusted_text(str(raw_ch_name), 60) if raw_ch_name else str(ch_id)
            sub_fields.append(
                (
                    f"Channel: #{clean_ch_name}",
                    (
                        f"• **Channel**: <#{ch_id}>\n"
                        f"• **Preset**: `{p_obj.label}` (`{clean_preset_key}`)\n"
                        f"• **Alert Role**: {role_fmt}\n"
                        f"• **Status**: {status_fmt}"
                    ),
                    False,
                )
            )
        embed = system(
            settings,
            title="📋 Active Job Alert Subscriptions",
            summary=f"Server ID: `{guild_id}` • Subscribed Channels: `{len(subs)}`",
            fields=sub_fields,
            footer_hint="Jobs • Subscriptions",
            status="normal",
        )
        embed.set_footer(text=format_footer(settings, "Jobs", "Subscriptions"))
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if action == "unsubscribe":
        if channel is None:
            await interaction.followup.send("❌ Please specify the channel to unsubscribe.", ephemeral=True)
            return
        deleted = await db.delete_guild_job_subscription(guild_id, channel.id)
        if deleted:
            await interaction.followup.send(
                f"🗑️ Unsubscribed <#{channel.id}> from job alerts.", ephemeral=True
            )
        else:
            await interaction.followup.send(f"ℹ️ <#{channel.id}> was not subscribed.", ephemeral=True)
        return

    # action == "subscribe" with channel provided
    assert channel is not None
    # Verify bot permissions in target channel
    me = guild.me
    if me:
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                f"⚠️ **Warning**: The bot lacks required permissions ('Send Messages' or 'Embed Links') in <#{channel.id}>.\n"
                "Please update channel permissions so alerts can be posted.",
                ephemeral=True,
            )

    role_id = role.id if role else None
    await db.set_guild_job_subscription(
        guild_id,
        channel.id,
        preset=clean_preset,
        role_id=role_id,
        enabled=True,
    )
    p_obj = DISCIPLINE_PRESETS[clean_preset]
    role_msg = f" with alert role <@&{role_id}>" if role_id else ""
    await interaction.followup.send(
        f"✅ Subscribed <#{channel.id}> to **{p_obj.label}** job alerts{role_msg}!\n"
        f"Alerts will be dispatched as new matching roles are discovered.",
        ephemeral=True,
    )


@job_subscribe_cmd.autocomplete("preset")
async def job_subscribe_preset_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f"{p.label} ({k})", value=k)
        for k, p in DISCIPLINE_PRESETS.items()
        if cur in k.lower() or cur in p.label.lower()
    ][:25]


# ---------------------------------------------------------------------------
# User Job Preferences (/ic job_preferences)
# ---------------------------------------------------------------------------


@ic.command(
    name="job_preferences",
    description="View or update your private job notification and search preferences.",
)
@app_commands.describe(
    preset="Your preferred discipline preset (e.g. tech_swe, business_finance, accounting, design_ux, all).",
    locations="Comma-separated location allowlist (e.g. 'Toronto, Remote').",
    keywords="Comma-separated keywords of interest (e.g. 'python, intern').",
    remote_only="Whether to exclusively search for remote positions.",
    clear="Reset and remove all your saved preferences.",
)
async def job_preferences_cmd(
    interaction: discord.Interaction,
    preset: str | None = None,
    locations: str | None = None,
    keywords: str | None = None,
    remote_only: bool | None = None,
    clear: bool = False,
):
    if not await guard_feature(interaction, "careers"):
        return
    await interaction.response.defer(ephemeral=True)

    bot = interaction.client
    db = getattr(bot, "db", None)
    if db is None:
        await interaction.followup.send("❌ Database unavailable.", ephemeral=True)
        return

    settings = await resolve_guild_settings(bot, interaction)
    user_id = interaction.user.id

    if clear:
        await db.delete_user_job_preferences(user_id)
        await interaction.followup.send("🗑️ Your personal job preferences have been cleared.", ephemeral=True)
        return

    existing = await db.get_user_job_preferences(user_id) or {}

    # Check if this is an update or read
    is_update = any(arg is not None for arg in (preset, locations, keywords, remote_only))

    if is_update:
        new_preset = (preset or existing.get("preset") or "tech_swe").strip().lower()
        if new_preset not in DISCIPLINE_PRESETS:
            new_preset = "tech_swe"

        new_locs = (
            [loc.strip() for loc in locations.split(",") if loc.strip()]
            if locations is not None
            else existing.get("locations", [])
        )
        new_kws = (
            [kw.strip() for kw in keywords.split(",") if kw.strip()]
            if keywords is not None
            else existing.get("keywords_allow", [])
        )
        new_remote = bool(remote_only) if remote_only is not None else existing.get("remote_only", False)

        await db.set_user_job_preferences(
            user_id,
            preset=new_preset,
            locations=new_locs,
            keywords_allow=new_kws,
            remote_only=new_remote,
        )
        existing = await db.get_user_job_preferences(user_id) or {}
        prefix = "✅ **Job Preferences Updated**\n\n"
    else:
        prefix = "👤 **Your Current Job Preferences**\n\n"

    p_key = str(existing.get("preset", "tech_swe"))
    p_obj = DISCIPLINE_PRESETS.get(p_key, DISCIPLINE_PRESETS["tech_swe"])
    clean_p_key = sanitize_untrusted_text(p_key, 40) or "tech_swe"
    raw_locs = [
        sanitize_untrusted_text(str(loc), 60).replace("`", "")
        for loc in existing.get("locations", [])
        if str(loc).strip()
    ]
    raw_kws = [
        sanitize_untrusted_text(str(kw), 60).replace("`", "")
        for kw in existing.get("keywords_allow", [])
        if str(kw).strip()
    ]
    loc_display = ", ".join(loc for loc in raw_locs if loc) or "Any Location"
    kw_display = ", ".join(kw for kw in raw_kws if kw) or "None"
    remote_display = "Yes (Remote Only)" if existing.get("remote_only") else "No (All Types)"

    embed = system(
        settings,
        title="🔒 Private Job Search Preferences",
        summary=(
            f"{prefix}"
            f"• **Preset**: `{p_obj.label}` (`{clean_p_key}`)\n"
            f"• **Locations**: `{loc_display}`\n"
            f"• **Keywords**: `{kw_display}`\n"
            f"• **Remote Only**: `{remote_display}`\n\n"
            "*These preferences are keyed to your Discord user ID (portable across servers on this bot instance) "
            "and apply when using `/ic jobs use_my_preferences:True`.*"
        ),
        footer_hint="Jobs • Local SQLite Storage (Plaintext on Host Disk)",
        status="success" if is_update else "normal",
    )
    embed.set_footer(text=format_footer(settings, "Jobs", "Local SQLite Storage (Plaintext on Host Disk)"))
    await interaction.followup.send(embed=embed, ephemeral=True)


@job_preferences_cmd.autocomplete("preset")
async def job_preferences_preset_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f"{p.label} ({k})", value=k)
        for k, p in DISCIPLINE_PRESETS.items()
        if cur in k.lower() or cur in p.label.lower()
    ][:25]


# ---------------------------------------------------------------------------
# Filter Preview (/ic job_preview)
# ---------------------------------------------------------------------------


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
    if not await guard_feature(interaction, "careers"):
        return
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

    safe_title_display = sanitize_untrusted_text(clean_title, 120) or "Untitled Role"
    safe_employer_display = sanitize_untrusted_text(clean_employer, 80) or "Employer"
    safe_location_display = sanitize_untrusted_text(location, 80) or "Not Listed"
    safe_explanation = sanitize_untrusted_text(decision.explanation, 300).replace("`", "")

    trace_lines = []
    for item in decision.trace:
        status_sym = "✅" if item.passed else "❌"
        safe_rule = sanitize_untrusted_text(item.rule, 50).replace("`", "")
        safe_reason = sanitize_untrusted_text(item.reason, 180)
        trace_lines.append(f"{status_sym} **`{safe_rule}`**: {safe_reason}")

    preview_fields: list[tuple[str, str, bool]] = [
        (
            "Posting Under Test",
            (
                f"**Title:** {safe_title_display}\n"
                f"**Employer:** {safe_employer_display}\n"
                f"**Location:** {safe_location_display}"
            ),
            False,
        ),
        (
            "Extracted Metadata",
            (
                f"**Career Level:** `{sanitize_untrusted_text(decision.career_level, 40)}`\n"
                f"**Disciplines:** `{sanitize_untrusted_text(', '.join(decision.disciplines) or 'None', 120)}`"
            ),
            True,
        ),
        (
            "Filter Preset",
            f"**{preset_obj.label}** (`{preset_obj.key}`)\n*{sanitize_text(preset_obj.description, 200)}*",
            True,
        ),
        (
            f"Explainable Rule Trace ({len(decision.trace)} checks)",
            "\n".join(trace_lines) or "No rule trace available.",
            False,
        ),
    ]

    embed = system(
        settings,
        title=f"{verdict_emoji} Job Filter Preview: {verdict_text}",
        summary=f"**Decision Summary:**\n`{safe_explanation}`",
        fields=preview_fields,
        footer_hint="Jobs • Deterministic Rule Engine Preview",
        status="success" if decision.matched else "error",
    )
    embed.set_footer(text=format_footer(settings, "Jobs", "Deterministic Rule Engine Preview"))

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
