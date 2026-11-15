from __future__ import annotations

import zoneinfo
from typing import Any, cast

import discord
from discord import app_commands

from icbot.app import admin
from icbot.config import KNOWN_MODULE_IDS, Settings
from icbot.container import ServiceContainer, get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import ConfigValidationError
from icbot.guild_config import GuildSettingsStore
from icbot.permissions import can_manage_guild, is_admin_or_allowed


def _get_store(container: ServiceContainer) -> GuildSettingsStore:
    if container.guild_settings is not None:
        return container.guild_settings
    if container.db is not None:
        return GuildSettingsStore(container.db)
    raise RuntimeError("Guild settings store is not available.")


def _is_valid_tz(name: str) -> bool:
    """Check if ``name`` represents a valid IANA timezone."""
    if not name or not name.strip():
        return False
    try:
        zoneinfo.ZoneInfo(name.strip())
        return True
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# Modals for Discord-native settings input
# ---------------------------------------------------------------------------


class CampusTimezoneModal(discord.ui.Modal, title="Configure Campus & Timezone"):
    university: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Campus / University Name",
        placeholder="e.g. Toronto Metropolitan University",
        max_length=100,
        required=True,
    )
    timezone: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Timezone (IANA Format)",
        placeholder="e.g. America/Toronto",
        max_length=50,
        required=True,
    )

    def __init__(self, current_campus: str, current_tz: str, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.university.default = current_campus or "Toronto Metropolitan University"
        self.timezone.default = current_tz or "America/Toronto"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("❌ Command context mismatch.", ephemeral=True)
            return
        settings = await resolve_guild_settings(interaction.client, interaction)
        if not can_manage_guild(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to manage this server's configuration.",
                ephemeral=True,
            )
            return

        tz_val = str(self.timezone.value).strip()
        if not _is_valid_tz(tz_val):
            await interaction.response.send_message(
                f"❌ Invalid IANA timezone: `{tz_val}`. Please specify a valid timezone name such as `America/Toronto`, `America/New_York`, or `UTC`.",
                ephemeral=True,
            )
            return

        campus_val = str(self.university.value).strip()
        container = get_container(interaction.client)
        store = _get_store(container)
        await store.update_overrides(
            self.guild_id,
            {"rmp_university": campus_val, "exams_tz": tz_val},
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"✅ **Configuration Updated**: Campus set to **{campus_val}** and timezone set to **{tz_val}**.",
            ephemeral=True,
        )


class ChannelsConfigModal(discord.ui.Modal, title="Configure Server Channels"):
    command_channel: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Bot Commands Channel ID",
        placeholder="Channel ID (0 to clear gate)",
        max_length=25,
        required=False,
    )
    job_alerts_channel: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Job Alerts Channel ID",
        placeholder="Channel ID (0 to disable alerts)",
        max_length=25,
        required=False,
    )
    transit_alerts_channel: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Transit Alerts Channel ID",
        placeholder="Channel ID (0 to disable alerts)",
        max_length=25,
        required=False,
    )

    def __init__(self, current_cmd: int, current_jobs: int, current_transit: int, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.command_channel.default = str(current_cmd) if current_cmd else ""
        self.job_alerts_channel.default = str(current_jobs) if current_jobs else ""
        self.transit_alerts_channel.default = str(current_transit) if current_transit else ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("❌ Command context mismatch.", ephemeral=True)
            return
        settings = await resolve_guild_settings(interaction.client, interaction)
        if not can_manage_guild(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to manage this server's configuration.",
                ephemeral=True,
            )
            return

        def _parse_id(val: str, field_name: str) -> int:
            s = val.strip()
            if not s:
                return 0
            try:
                num = int(s)
                if num < 0:
                    raise ValueError
                return num
            except ValueError as err:
                raise ConfigValidationError(
                    f"{field_name} must be a valid positive channel ID integer."
                ) from err

        try:
            cmd_id = _parse_id(str(self.command_channel.value), "Command Channel ID")
            jobs_id = _parse_id(str(self.job_alerts_channel.value), "Job Alerts Channel ID")
            transit_id = _parse_id(str(self.transit_alerts_channel.value), "Transit Alerts Channel ID")
        except ConfigValidationError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        container = get_container(interaction.client)
        store = _get_store(container)
        updates = {
            "user_command_channel_id": cmd_id,
            "job_alerts_channel_id": jobs_id,
            "ttc_alerts_channel_id": transit_id,
        }
        await store.update_overrides(self.guild_id, updates, actor_id=interaction.user.id)
        cmd_fmt = f"<#{cmd_id}>" if cmd_id else "Anywhere (No gate)"
        jobs_fmt = f"<#{jobs_id}>" if jobs_id else "Disabled"
        transit_fmt = f"<#{transit_id}>" if transit_id else "Disabled"
        await interaction.response.send_message(
            f"✅ **Channels Updated**:\n• Command Channel: {cmd_fmt}\n• Job Alerts: {jobs_fmt}\n• Transit Alerts: {transit_fmt}",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Interactive Guild Setup View
# ---------------------------------------------------------------------------


class ProfileSelect(discord.ui.Select):
    def __init__(self, current_profile: str):
        options = [
            discord.SelectOption(
                label="Student Core",
                value="student_core",
                description="Academics, Careers, Transit, Economy",
                default=current_profile == "student_core",
            ),
            discord.SelectOption(
                label="Community Full",
                value="community_full",
                description="All modules including Casino, Market, AI, Sports",
                default=current_profile == "community_full",
            ),
            discord.SelectOption(
                label="Toronto / TMU",
                value="toronto_tmu",
                description="Student core tailored for TMU campus",
                default=current_profile == "toronto_tmu",
            ),
            discord.SelectOption(
                label="Default Preset",
                value="default",
                description="Core utilities with external APIs opt-in",
                default=current_profile == "default",
            ),
        ]
        super().__init__(
            placeholder="Select feature preset profile...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, GuildSetupView):
            return
        if not await view.interaction_check(interaction):
            return

        chosen = self.values[0]
        container = view.container
        store = _get_store(container)
        await store.update_overrides(view.guild_id, {"feature_profile": chosen}, actor_id=interaction.user.id)

        new_settings = await container.resolve_settings_for_guild(view.guild_id)
        view.settings = new_settings
        embed = view.build_preview_embed(interaction.guild, new_settings)
        await interaction.response.edit_message(embed=embed, view=view)


class GuildSetupView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        admin_user_id: int,
        container: Any,
        settings: Settings,
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.admin_user_id = admin_user_id
        self.container = container
        self.settings = settings
        self.profile_select = ProfileSelect(settings.feature_profile)
        self.add_item(self.profile_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else None)
        if gid != self.guild_id:
            await interaction.response.send_message("❌ Command context mismatch.", ephemeral=True)
            return False
        if not can_manage_guild(interaction, self.settings):
            await interaction.response.send_message(
                "❌ You do not have permission to manage this server's configuration.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Edit Campus & Timezone", style=discord.ButtonStyle.primary, emoji="🏫")
    async def campus_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CampusTimezoneModal(
            current_campus=self.settings.rmp_university,
            current_tz=self.settings.exams_tz,
            guild_id=self.guild_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Edit Channels", style=discord.ButtonStyle.primary, emoji="📢")
    async def channels_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ChannelsConfigModal(
            current_cmd=self.settings.user_command_channel_id,
            current_jobs=self.settings.job_alerts_channel_id,
            current_transit=self.settings.ttc_alerts_channel_id,
            guild_id=self.guild_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Reset Overrides", style=discord.ButtonStyle.danger, emoji="🔄")
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        store = _get_store(self.container)
        await store.reset_overrides(self.guild_id, actor_id=interaction.user.id)
        new_settings = await self.container.resolve_settings_for_guild(self.guild_id)
        self.settings = new_settings
        embed = self.build_preview_embed(interaction.guild, new_settings)
        await interaction.response.edit_message(embed=embed, view=self)

    def build_preview_embed(self, guild: Any, settings: Settings) -> discord.Embed:
        proc_settings = getattr(self.container, "settings", None) or settings
        is_hosted = proc_settings.guild_id == 0
        track_str = (
            "🌐 **Multi-Guild Hosted** (Per-Guild Isolated Settings)"
            if is_hosted
            else f"🏠 **Single-Guild Self-Hosted** (Pinned Guild `{proc_settings.guild_id}`)"
        )
        profile_label = settings.feature_profile.replace("_", " ").title()
        channels_str = (
            f"• Commands: {f'<#{settings.user_command_channel_id}>' if settings.user_command_channel_id else 'Anywhere (No gate)'}\n"
            f"• Jobs Feed: {f'<#{settings.job_alerts_channel_id}>' if settings.job_alerts_channel_id else 'Disabled'}\n"
            f"• Transit Alerts: {f'<#{settings.ttc_alerts_channel_id}>' if settings.ttc_alerts_channel_id else 'Disabled'}"
        )
        roles_str = (
            ", ".join(f"<@&{r}>" for r in settings.allowed_role_ids)
            if settings.allowed_role_ids
            else "Server Administrators only (`Manage Guild`)"
        )

        fields = [
            ("Deployment Track", track_str, False),
            (
                "Campus & Timezone",
                f"**Campus:** {settings.rmp_university}\n**Timezone:** `{settings.exams_tz}`",
                True,
            ),
            ("Feature Profile", f"**Preset:** `{profile_label}`", True),
            ("Configured Channels", channels_str, False),
            ("Admin Role Allowlist", roles_str, False),
        ]
        return system_embed(
            settings,
            title=f"⚙️ Configuration Dashboard — {getattr(guild, 'name', 'Server')}",
            summary="Interactive server administration panel. Use the controls below to configure presets, channels, and timezones.",
            fields=fields,
            footer_hint="Instigate Cafe Bot Setup",
        )


# ---------------------------------------------------------------------------
# Slash Commands: Setup, Config, Features, Roles, and Audit
# ---------------------------------------------------------------------------


@admin.command(
    name="setup", description="Interactive dashboard to configure server settings, channels, and modules."
)
async def setup_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to manage this server's configuration.",
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else 0)
    view = GuildSetupView(gid, interaction.user.id, container, settings)
    embed = view.build_preview_embed(interaction.guild, settings)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@admin.command(name="config", description="View or directly update server configuration overrides.")
@app_commands.describe(
    action="Operation to perform: view configuration, update settings, or reset overrides",
    campus="Campus or university name for RMP and campus lookup",
    timezone="IANA timezone for exams and schedules (e.g. America/Toronto)",
    command_channel="Channel dedicated to bot user commands (optional)",
    job_alerts_channel="Channel for job feed alert broadcasts (optional)",
    transit_alerts_channel="Channel for TTC and GO transit alert broadcasts (optional)",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="View Current Configuration", value="view"),
        app_commands.Choice(name="Update Configuration", value="update"),
        app_commands.Choice(name="Reset to Defaults", value="reset"),
    ]
)
async def config_cmd(
    interaction: discord.Interaction,
    action: app_commands.Choice[str] | None = None,
    campus: str | None = None,
    timezone: str | None = None,
    command_channel: discord.TextChannel | None = None,
    job_alerts_channel: discord.TextChannel | None = None,
    transit_alerts_channel: discord.TextChannel | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else None)
    if gid is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to manage server configuration.",
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    store = _get_store(container)

    act = action.value if action is not None else "view"
    if act == "reset":
        await store.reset_overrides(gid, actor_id=interaction.user.id)
        await interaction.response.send_message(
            "✅ **Configuration Reset**: Cleared all guild overrides back to process defaults.",
            ephemeral=True,
        )
        return

    # Check for direct updates
    updates: dict[str, Any] = {}
    if campus is not None and campus.strip():
        updates["rmp_university"] = campus.strip()
    if timezone is not None and timezone.strip():
        tz_clean = timezone.strip()
        if not _is_valid_tz(tz_clean):
            await interaction.response.send_message(
                f"❌ Invalid IANA timezone: `{tz_clean}`. Example: `America/Toronto`, `America/New_York`, `UTC`.",
                ephemeral=True,
            )
            return
        updates["exams_tz"] = tz_clean
    if command_channel is not None:
        updates["user_command_channel_id"] = command_channel.id
    if job_alerts_channel is not None:
        updates["job_alerts_channel_id"] = job_alerts_channel.id
    if transit_alerts_channel is not None:
        updates["ttc_alerts_channel_id"] = transit_alerts_channel.id

    if updates or act == "update":
        if not updates:
            await interaction.response.send_message(
                "ℹ️ No settings provided to update. Specify options like campus, timezone, or channels.",
                ephemeral=True,
            )
            return
        await store.update_overrides(gid, updates, actor_id=interaction.user.id)
        lines = [f"• `{k}`: `{v}`" for k, v in sorted(updates.items())]
        await interaction.response.send_message(
            "✅ **Configuration Updated**:\n" + "\n".join(lines),
            ephemeral=True,
        )
        return

    # Default action: View current configuration
    guild_cfg = await store.get(gid)
    overrides = dict(guild_cfg.overrides) if guild_cfg else {}

    is_hosted = settings.guild_id == 0
    track_str = (
        "🌐 **Multi-Guild Hosted** (Per-Guild Isolated Settings)"
        if is_hosted
        else f"🏠 **Single-Guild Self-Hosted** (Pinned Guild `{settings.guild_id}`)"
    )

    overrides_desc = (
        "\n".join(f"• `{k}`: `{v}`" for k, v in sorted(overrides.items()))
        if overrides
        else "No custom overrides set (using default process configuration)."
    )

    fields = [
        ("Deployment Track", track_str, False),
        ("Active Feature Profile", f"`{settings.feature_profile}`", True),
        (
            "Campus & Timezone",
            f"**Campus:** {settings.rmp_university}\n**Timezone:** `{settings.exams_tz}`",
            True,
        ),
        (
            "Channels",
            f"• Commands: {f'<#{settings.user_command_channel_id}>' if settings.user_command_channel_id else 'Anywhere'}\n"
            f"• Jobs: {f'<#{settings.job_alerts_channel_id}>' if settings.job_alerts_channel_id else 'Disabled'}\n"
            f"• Transit: {f'<#{settings.ttc_alerts_channel_id}>' if settings.ttc_alerts_channel_id else 'Disabled'}",
            False,
        ),
        ("Guild Custom Overrides", overrides_desc, False),
    ]

    embed = system_embed(
        settings,
        title=f"📋 Configuration Preview — {interaction.guild.name}",
        summary="Read-only preview of active server settings and database overrides.",
        fields=fields,
        footer_hint="Instigate Cafe Bot Config",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin.command(name="features", description="Configure feature profile preset or toggle individual modules.")
@app_commands.describe(
    profile="Preset feature profile (default, student_core, community_full, toronto_tmu)",
    enable="Module ID to add to enabled_features (e.g. casino, market, ai, sports)",
    disable="Module ID to add to disabled_features",
)
@app_commands.choices(
    profile=[
        app_commands.Choice(name="Default Preset (Core on, external APIs opt-in)", value="default"),
        app_commands.Choice(name="Student Core (Academics, Careers, Transit, Economy)", value="student_core"),
        app_commands.Choice(name="Community Full (All features on)", value="community_full"),
        app_commands.Choice(name="Toronto / TMU (TMU student core)", value="toronto_tmu"),
    ]
)
async def features_cmd(
    interaction: discord.Interaction,
    profile: app_commands.Choice[str] | None = None,
    enable: str | None = None,
    disable: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else None)
    if gid is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to manage this server's configuration.",
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    store = _get_store(container)
    current_cfg = await store.get(gid)
    cur_overrides = dict(current_cfg.overrides) if current_cfg else {}

    updates: dict[str, Any] = {}
    if profile is not None:
        updates["feature_profile"] = profile.value

    if enable is not None and enable.strip():
        mod = enable.strip().lower()
        if mod not in KNOWN_MODULE_IDS:
            await interaction.response.send_message(
                f"❌ Unknown module `{mod}`. Known modules are: {', '.join(sorted(KNOWN_MODULE_IDS))}.",
                ephemeral=True,
            )
            return
        cur_enabled = list(cur_overrides.get("enabled_features", settings.enabled_features or []))
        if mod not in cur_enabled:
            cur_enabled.append(mod)
        cur_disabled = list(cur_overrides.get("disabled_features", settings.disabled_features or []))
        if mod in cur_disabled:
            cur_disabled.remove(mod)
        updates["enabled_features"] = cur_enabled
        updates["disabled_features"] = cur_disabled

    if disable is not None and disable.strip():
        mod = disable.strip().lower()
        if mod not in KNOWN_MODULE_IDS:
            await interaction.response.send_message(
                f"❌ Unknown module `{mod}`. Known modules are: {', '.join(sorted(KNOWN_MODULE_IDS))}.",
                ephemeral=True,
            )
            return
        if mod == "core":
            await interaction.response.send_message(
                "❌ The `core` utility module cannot be disabled.", ephemeral=True
            )
            return
        cur_disabled = list(cur_overrides.get("disabled_features", settings.disabled_features or []))
        if mod not in cur_disabled:
            cur_disabled.append(mod)
        cur_enabled = list(cur_overrides.get("enabled_features", settings.enabled_features or []))
        if mod in cur_enabled:
            cur_enabled.remove(mod)
        updates["disabled_features"] = cur_disabled
        updates["enabled_features"] = cur_enabled

    if not updates:
        await interaction.response.send_message("ℹ️ No feature changes requested.", ephemeral=True)
        return

    await store.update_overrides(gid, updates, actor_id=interaction.user.id)
    await interaction.response.send_message(
        "✅ **Feature Configuration Updated**: Applied changes for server.",
        ephemeral=True,
    )


@admin.command(name="roles", description="Manage administrative allowed roles for bot management.")
@app_commands.describe(
    action="Operation to perform on allowed roles",
    role="Discord role to add or remove",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="Add Role to Allowlist", value="add"),
        app_commands.Choice(name="Remove Role from Allowlist", value="remove"),
        app_commands.Choice(name="List Allowed Roles", value="list"),
        app_commands.Choice(name="Clear All Allowed Roles", value="clear"),
    ]
)
async def roles_cmd(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    role: discord.Role | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else None)
    if gid is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to manage this server's configuration.",
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    store = _get_store(container)
    current_cfg = await store.get(gid)
    cur_roles = (
        list(current_cfg.overrides.get("allowed_role_ids", settings.allowed_role_ids or []))
        if current_cfg
        else list(settings.allowed_role_ids or [])
    )

    act = action.value
    if act == "list":
        if not cur_roles:
            await interaction.response.send_message(
                "ℹ️ No specific allowed roles configured. Administrators with `Manage Guild` have access.",
                ephemeral=True,
            )
            return
        roles_str = ", ".join(f"<@&{r}> (`{r}`)" for r in cur_roles)
        await interaction.response.send_message(
            f"🎭 **Configured Admin Roles**:\n{roles_str}", ephemeral=True
        )
        return

    if act == "add":
        if role is None:
            await interaction.response.send_message("❌ Must specify a `role` to add.", ephemeral=True)
            return
        if role.id not in cur_roles:
            cur_roles.append(role.id)
            await store.update_overrides(gid, {"allowed_role_ids": cur_roles}, actor_id=interaction.user.id)
            await interaction.response.send_message(
                f"✅ Added {role.mention} to admin role allowlist.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ {role.mention} is already in the allowlist.", ephemeral=True
            )
        return

    if act == "remove":
        if role is None:
            await interaction.response.send_message("❌ Must specify a `role` to remove.", ephemeral=True)
            return
        if role.id in cur_roles:
            cur_roles.remove(role.id)
            await store.update_overrides(gid, {"allowed_role_ids": cur_roles}, actor_id=interaction.user.id)
            await interaction.response.send_message(
                f"✅ Removed {role.mention} from admin role allowlist.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ {role.mention} is not in the allowlist.", ephemeral=True
            )
        return

    if act == "clear":
        await store.update_overrides(gid, {"allowed_role_ids": []}, actor_id=interaction.user.id)
        await interaction.response.send_message("✅ Cleared all allowed admin roles.", ephemeral=True)
        return


@admin.command(name="audit", description="View recent configuration change audit logs for this server.")
async def audit_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    gid = getattr(interaction, "guild_id", None) or (interaction.guild.id if interaction.guild else None)
    if gid is None:
        await interaction.response.send_message(
            "❌ This command must be used within a server.", ephemeral=True
        )
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not can_manage_guild(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to view configuration audit logs.",
            ephemeral=True,
        )
        return

    container = get_container(interaction.client)
    store = _get_store(container)
    history = await store.get_audit_history(gid, limit=10)

    if not history:
        await interaction.response.send_message(
            "ℹ️ No configuration changes recorded in the audit ledger for this server.",
            ephemeral=True,
        )
        return

    lines: list[str] = []
    for entry in history:
        keys_fmt = ", ".join(f"`{k}`" for k in entry.changed_keys) if entry.changed_keys else "All (Reset)"
        lines.append(
            f"• <t:{entry.created_at}:R> — **{entry.action.upper()}** by <@{entry.actor_id}>: {keys_fmt}"
        )

    embed = system_embed(
        settings,
        title=f"📜 Configuration Audit Log — {interaction.guild.name}",
        summary="Recent administrative configuration mutations recorded in SQLite ledger.",
        fields=[("Recent Modifications", "\n".join(lines), False)],
        footer_hint="Configuration Audit Trail",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Legacy LLM controls preserved exactly
# ---------------------------------------------------------------------------


@admin.command(
    name="llm_stop", description="Stop the LLM from functioning (pauses chatbot and resume tools)."
)
async def llm_stop_cmd(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
        return

    container = get_container(interaction.client)
    container.llm_killed = True
    cast(Any, interaction.client)._llm_killed = True
    supervisor = getattr(interaction.client, "supervisor", None)
    sync_fn = getattr(supervisor, "sync_event_states", None)
    if callable(sync_fn):
        sync_fn()

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

    container = get_container(interaction.client)
    container.llm_killed = False
    cast(Any, interaction.client)._llm_killed = False
    supervisor = getattr(interaction.client, "supervisor", None)
    sync_fn = getattr(supervisor, "sync_event_states", None)
    if callable(sync_fn):
        sync_fn()

    db = container.db or getattr(interaction.client, "db", None)
    if db:
        await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("llm_killed", "0"))

    await interaction.response.send_message(
        "✅ **LLM Features Enabled**: Chatbot and resume review are active.", ephemeral=True
    )
