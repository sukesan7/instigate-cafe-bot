from __future__ import annotations

import discord
from discord import Interaction

from icbot.config import Settings


def _has_allowed_role_id(member: discord.Member, settings: Settings) -> bool:
    if not settings.allowed_role_ids:
        return False
    allowed = set(settings.allowed_role_ids)
    return any(r.id in allowed for r in getattr(member, "roles", []))


def is_admin_or_allowed(interaction: Interaction, settings: Settings) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    # True admin permission always wins
    if member.guild_permissions.administrator:
        return True

    # Preferred: role IDs
    return bool(_has_allowed_role_id(member, settings))
