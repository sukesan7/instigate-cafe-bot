from __future__ import annotations
import discord
from discord import Interaction
from icbot.config import Settings


def _has_allowed_role(member: discord.Member, settings: Settings) -> bool:
    if not settings.allowed_roles:
        return False
    member_role_names = {r.name for r in getattr(member, "roles", [])}
    return any(r in member_role_names for r in settings.allowed_roles)


def is_admin_or_allowed(interaction: Interaction, settings: Settings) -> bool:
    member = interaction.user
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator:
            return True
        if _has_allowed_role(member, settings):
            return True
    return False
