from __future__ import annotations

from typing import Any

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


def can_manage_guild(
    interaction_or_member: Interaction | discord.Member | Any,
    settings: Settings | None = None,
    *,
    guild: discord.Guild | Any | None = None,
) -> bool:
    """Server-side authorization check for guild configuration and administrative setup.

    Independent of Discord client-side UI visibility. Requires either:
    1. Guild owner (active_guild.owner_id == member.id)
    2. Administrator permission (member.guild_permissions.administrator)
    3. Manage Guild permission (member.guild_permissions.manage_guild)
    4. Member holds an authorized role configured in ``settings.allowed_role_ids``

    Fails closed (returns False) for DM interactions, non-member users, or unauthenticated contexts.
    """
    if isinstance(interaction_or_member, Interaction):
        interaction = interaction_or_member
        active_guild = getattr(interaction, "guild", None)
        if active_guild is None:
            return False
        member = getattr(interaction, "user", None)
    elif isinstance(interaction_or_member, discord.Member):
        member = interaction_or_member
        active_guild = guild or getattr(member, "guild", None)
        if active_guild is None:
            return False
    else:
        # FakeInteraction or custom namespace with .guild and .user
        active_guild = getattr(interaction_or_member, "guild", None)
        if active_guild is None:
            return False
        member = getattr(interaction_or_member, "user", None)

    if not isinstance(member, discord.Member) and not hasattr(member, "guild_permissions"):
        return False

    # 1. Guild owner
    owner_id = getattr(active_guild, "owner_id", None)
    member_id = getattr(member, "id", None)
    if owner_id is not None and member_id is not None and owner_id == member_id:
        return True

    # 2. Permissions: administrator or manage_guild
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (
        getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)
    ):
        return True

    # 3. Configured allowed roles
    if settings is not None and settings.allowed_role_ids:
        roles = getattr(member, "roles", [])
        allowed = set(settings.allowed_role_ids)
        if any(getattr(r, "id", None) in allowed for r in roles):
            return True

    return False
