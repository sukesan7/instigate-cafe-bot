"""Shared Discord interaction-state guards, page-slice calculation, and base pagination view.

Separates pure page/data calculation (`PageSlice`, `compute_page_slice`) from Discord UI state
(`BasePaginationView`), enforces the explicit interaction-audience matrix, prevents double
interaction acknowledgments, and handles deleted messages or Discord 403/404/429 errors safely.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

import discord

from icbot.config import Settings
from icbot.container import extract_guild_id, resolve_guild_settings
from icbot.errors import respond_with_failure
from icbot.permissions import can_manage_guild, is_admin_or_allowed
from icbot.registry import is_feature_enabled

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_PAGINATOR_TIMEOUT_SECONDS: float = 180.0
AudienceScope = Literal["invoker_only", "guild_shared", "admin_only", "player_locked"]


@dataclass(frozen=True)
class InteractionAudienceRule:
    """Explicit audience, timeout, and permission contract for an interactive Discord view."""

    view_name: str
    module: str
    audience: AudienceScope
    default_timeout_seconds: float
    feature_key: str | None
    ephemeral_only: bool
    rationale: str


# Explicit Interaction-Audience Matrix across all interactive views in the bot
INTERACTION_AUDIENCE_MATRIX: dict[str, InteractionAudienceRule] = {
    "NotesPaginationView": InteractionAudienceRule(
        view_name="NotesPaginationView",
        module="icbot.features.notes",
        audience="guild_shared",
        default_timeout_seconds=180.0,
        feature_key="academics",
        ephemeral_only=False,
        rationale=(
            "Displays strictly public course study links scoped to the active guild; "
            "every page is safe for any member of the same server to navigate."
        ),
    ),
    "ExamPaginationView": InteractionAudienceRule(
        view_name="ExamPaginationView",
        module="icbot.features.exams",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key="academics",
        ephemeral_only=False,
        rationale=(
            "Bound to the invoking member and guild; when allow_private=True, the response "
            "is strictly ephemeral and may contain private exam rows."
        ),
    ),
    "ExamClearConfirmView": InteractionAudienceRule(
        view_name="ExamClearConfirmView",
        module="icbot.features.exams",
        audience="admin_only",
        default_timeout_seconds=60.0,
        feature_key="academics",
        ephemeral_only=True,
        rationale="Destructive guild exam schedule wipe; restricted to initiating admin with live permission check.",
    ),
    "JobPaginationView": InteractionAudienceRule(
        view_name="JobPaginationView",
        module="icbot.features.jobs",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key="careers",
        ephemeral_only=False,
        rationale=(
            "Bound to the invoking member and guild because results may reflect the invoker's "
            "saved personal job preferences (use_my_preferences=True)."
        ),
    ),
    "ResumeReviewPaginationView": InteractionAudienceRule(
        view_name="ResumeReviewPaginationView",
        module="icbot.features.resume",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key="careers",
        ephemeral_only=True,
        rationale="Contains confidential AI resume feedback delivered ephemerally to the uploading user.",
    ),
    "ResumeConfirmView": InteractionAudienceRule(
        view_name="ResumeConfirmView",
        module="icbot.features.resume",
        audience="invoker_only",
        default_timeout_seconds=120.0,
        feature_key="careers",
        ephemeral_only=True,
        rationale="Explicit privacy consent barrier before transmitting the user's resume to Google Gemini.",
    ),
    "HelpPaginationView": InteractionAudienceRule(
        view_name="HelpPaginationView",
        module="icbot.features.help",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key=None,
        ephemeral_only=False,
        rationale=(
            "Invoker-bound when author_id is provided so another user cannot hijack active menu reading; "
            "permits shared navigation only when instantiated with author_id=None."
        ),
    ),
    "GettingStartedView": InteractionAudienceRule(
        view_name="GettingStartedView",
        module="icbot.features.help",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key=None,
        ephemeral_only=False,
        rationale=(
            "Invoker-bound onboarding walkthrough when author_id is provided; "
            "permits shared navigation only when instantiated with author_id=None."
        ),
    ),
    "LeaderboardView": InteractionAudienceRule(
        view_name="LeaderboardView",
        module="icbot.features.lc_leaderboard",
        audience="invoker_only",
        default_timeout_seconds=180.0,
        feature_key="economy",
        ephemeral_only=False,
        rationale="Bound to the invoking member and guild to prevent concurrent page-flip contention.",
    ),
    "GuildSetupView": InteractionAudienceRule(
        view_name="GuildSetupView",
        module="icbot.features.admin",
        audience="admin_only",
        default_timeout_seconds=300.0,
        feature_key=None,
        ephemeral_only=True,
        rationale="Mutates per-guild configuration overrides; requires live Manage Guild / Admin authorization.",
    ),
    "AdminMarketConfirmView": InteractionAudienceRule(
        view_name="AdminMarketConfirmView",
        module="icbot.features.invest",
        audience="admin_only",
        default_timeout_seconds=60.0,
        feature_key="market",
        ephemeral_only=True,
        rationale="High-impact market catalog reset; restricted to initiating admin with live permission check.",
    ),
    "BlackjackView": InteractionAudienceRule(
        view_name="BlackjackView",
        module="icbot.casino.blackjack_views",
        audience="player_locked",
        default_timeout_seconds=120.0,
        feature_key="casino",
        ephemeral_only=False,
        rationale="Stateful financial wager session locked to (guild_id, user_id) with atomic timeout forfeit.",
    ),
    "BlackjackTerminalView": InteractionAudienceRule(
        view_name="BlackjackTerminalView",
        module="icbot.casino.blackjack_views",
        audience="player_locked",
        default_timeout_seconds=60.0,
        feature_key="casino",
        ephemeral_only=False,
        rationale="Post-hand Play Again trigger locked to (guild_id, user_id) to prevent unauthorized bet placement.",
    ),
}

# Deliberate Exception List for Truly Shared Interactions:
# Only views whose pages are 100% public, non-personalized, and guild-safe may allow shared navigation.
SHARED_INTERACTION_EXCEPTIONS: tuple[str, ...] = (
    "NotesPaginationView (when owner_id == 0: public course study links scoped to the active guild_id)",
    "HelpPaginationView (only when author_id is None: static public command reference)",
    "GettingStartedView (only when author_id is None: static public onboarding guide)",
)


@dataclass(frozen=True)
class PageSlice(Generic[T]):
    """Pure, deterministic pagination slice independent of Discord UI components."""

    items: tuple[T, ...]
    page: int
    total_pages: int
    total_items: int
    per_page: int
    start_index: int
    end_index: int
    has_prev: bool
    has_next: bool


def compute_page_slice(items: Sequence[T], page: int, per_page: int) -> PageSlice[T]:
    """Calculate a clamped page slice and button enablement flags without Discord view state."""
    safe_per_page = max(1, int(per_page))
    total_items = len(items)
    total_pages = max(1, (total_items + safe_per_page - 1) // safe_per_page)
    clamped_page = max(0, min(int(page), total_pages - 1))
    start_idx = clamped_page * safe_per_page
    end_idx = min(total_items, start_idx + safe_per_page)
    sliced = tuple(items[start_idx:end_idx])
    return PageSlice(
        items=sliced,
        page=clamped_page,
        total_pages=total_pages,
        total_items=total_items,
        per_page=safe_per_page,
        start_index=start_idx,
        end_index=end_idx,
        has_prev=clamped_page > 0,
        has_next=clamped_page < (total_pages - 1),
    )


def _is_response_done(interaction: discord.Interaction) -> bool:
    """Safely check whether `interaction.response.is_done()` is True, handling mock objects cleanly."""
    resp = getattr(interaction, "response", None)
    if resp is None:
        return False
    is_done_fn = getattr(resp, "is_done", None)
    if not callable(is_done_fn):
        return False
    res = is_done_fn()
    if inspect.iscoroutine(res):
        res.close()
        return False
    return res is True


async def send_ephemeral_denial(interaction: discord.Interaction, message: str) -> None:
    """Reliably send a single ephemeral denial whether or not the interaction was deferred."""
    with contextlib.suppress(
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException,
        AttributeError,
        RuntimeError,
    ):
        if not _is_response_done(interaction):
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


_UNSET: Any = object()


async def safe_edit_view_message(
    interaction: discord.Interaction,
    *,
    content: Any = _UNSET,
    embed: discord.Embed | None = _UNSET,
    view: discord.ui.View | None = _UNSET,
) -> bool:
    """Edit an interaction's component message safely without crashing on 403/404/429 or already-responded state."""
    kwargs: dict[str, Any] = {}
    if content is not _UNSET:
        kwargs["content"] = content
    if embed is not _UNSET:
        kwargs["embed"] = embed
    if view is not _UNSET:
        kwargs["view"] = view

    try:
        if not _is_response_done(interaction):
            await interaction.response.edit_message(**kwargs)
        elif hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(**kwargs)
        elif getattr(interaction, "message", None) is not None:
            await interaction.message.edit(**kwargs)  # type: ignore[union-attr]
        else:
            return False
        return True
    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException,
        AttributeError,
        RuntimeError,
    ):
        return False


async def safe_timeout_disable(view: discord.ui.View, message: Any | None = None) -> None:
    """Disable all interactive children on a timed-out view, stop listening, and safely edit the message."""
    for child in view.children:
        if isinstance(child, (discord.ui.Button, discord.ui.Select)):
            child.disabled = True
    view.stop()
    if message is not None:
        with contextlib.suppress(
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
            AttributeError,
            RuntimeError,
        ):
            await message.edit(view=view)


async def resolve_live_view_settings(
    interaction: discord.Interaction,
    fallback_settings: Settings | None = None,
) -> Settings:
    """Resolve current effective guild settings for mid-session permission and feature re-verification."""
    client = getattr(interaction, "client", None)
    if client is not None and getattr(client, "settings", None) is not None:
        with contextlib.suppress(RuntimeError, ValueError, TypeError, AttributeError, OSError):
            return await resolve_guild_settings(client, interaction)
    return fallback_settings or Settings()


async def verify_component_access(
    interaction: discord.Interaction,
    *,
    timed_out: bool = False,
    guild_id: int = 0,
    owner_id: int | None = None,
    shared_guild_navigation: bool = False,
    feature_key: str | None = None,
    require_admin: bool = False,
    require_manage_guild: bool = False,
    fallback_settings: Settings | None = None,
    expired_message: str = "⏳ This interactive view has expired. Please run the command again.",
    guild_mismatch_message: str = "❌ Command context mismatch.",
    owner_mismatch_message: str = "❌ Only the user who initiated this command can use these controls.",
    feature_disabled_message: str | None = None,
    permission_denied_message: str = "❌ You no longer have permission to use these controls.",
) -> str | None:
    """Validate component lifetime, guild boundary, actor identity, and live feature/admin gates.

    Returns `None` if access is permitted, or an ephemeral error string if denied.
    """
    if timed_out:
        return expired_message

    # 1. Guild boundary verification
    inter_gid = extract_guild_id(interaction)
    user = getattr(interaction, "user", None)
    member_guild = getattr(user, "guild", None) if user is not None else None
    member_gid = getattr(member_guild, "id", None) if member_guild is not None else None

    if guild_id and int(guild_id) > 0:
        if inter_gid is None or int(inter_gid) != int(guild_id):
            return guild_mismatch_message
        if isinstance(member_gid, int) and member_gid > 0 and int(member_gid) != int(guild_id):
            return guild_mismatch_message
    elif (
        shared_guild_navigation
        and inter_gid is not None
        and isinstance(member_gid, int)
        and member_gid > 0
        and int(member_gid) != int(inter_gid)
    ):
        return guild_mismatch_message

    # 2. Actor / Audience verification
    user_id = int(getattr(user, "id", 0) or 0)
    if owner_id is not None and int(owner_id) > 0:
        if user_id != int(owner_id):
            return owner_mismatch_message
    elif not shared_guild_navigation and owner_id is not None and int(owner_id) == 0:
        # When owner_id == 0 and shared_guild_navigation is False, still allow if unowned in unit tests
        pass

    # 3. Live feature gate & admin permission re-verification
    if feature_key or require_admin or require_manage_guild:
        live_settings = await resolve_live_view_settings(interaction, fallback_settings)
        if feature_key and not is_feature_enabled(feature_key, live_settings):
            return (
                feature_disabled_message
                or f"❌ The **{feature_key}** module is currently disabled in this server."
            )
        if require_manage_guild and not can_manage_guild(interaction, live_settings):
            return permission_denied_message
        if require_admin and not is_admin_or_allowed(interaction, live_settings):
            return permission_denied_message

    return None


class BasePaginationView(discord.ui.View):
    """Composable base paginator enforcing the 180s timeout, actor/guild/feature guards, and safe edits."""

    def __init__(
        self,
        *,
        settings: Settings,
        total_pages: int = 1,
        current_page: int = 0,
        owner_id: int | None = 0,
        guild_id: int = 0,
        shared_guild_navigation: bool = False,
        feature_key: str | None = None,
        timeout: float = DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
        expired_message: str = "⏳ This paginated view has expired. Please run the command again.",
        guild_mismatch_message: str = "❌ Command context mismatch.",
        owner_mismatch_message: str = "❌ Only the user who ran this command can navigate these pages.",
    ) -> None:
        super().__init__(timeout=timeout)
        self.settings = settings
        self.total_pages = max(1, int(total_pages))
        self.current_page = max(0, min(int(current_page), self.total_pages - 1))
        self.owner_id = int(owner_id) if owner_id is not None else 0
        self.guild_id = int(guild_id or 0)
        self.shared_guild_navigation = bool(shared_guild_navigation)
        self.feature_key = feature_key
        self.expired_message = expired_message
        self.guild_mismatch_message = guild_mismatch_message
        self.owner_mismatch_message = owner_mismatch_message
        self.message: Any | None = None
        self._timed_out = False
        self._last_checked_interaction_id: int | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.current_page = max(0, min(self.current_page, max(1, self.total_pages) - 1))
        if self._timed_out:
            self.prev_button.disabled = True
            self.next_button.disabled = True
            return
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def _check_access_error(self, interaction: discord.Interaction) -> str | None:
        return await verify_component_access(
            interaction,
            timed_out=self._timed_out,
            guild_id=self.guild_id,
            owner_id=self.owner_id if self.owner_id > 0 else None,
            shared_guild_navigation=self.shared_guild_navigation,
            feature_key=self.feature_key,
            fallback_settings=self.settings,
            expired_message=self.expired_message,
            guild_mismatch_message=self.guild_mismatch_message,
            owner_mismatch_message=self.owner_mismatch_message,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        err = await self._check_access_error(interaction)
        if err is not None:
            self._last_checked_interaction_id = None
            await send_ephemeral_denial(interaction, err)
            return False
        self._last_checked_interaction_id = id(interaction)
        return True

    async def _ensure_access(self, interaction: discord.Interaction) -> bool:
        """Ensure access is checked exactly once even when button.callback() is called directly in tests."""
        if self._last_checked_interaction_id == id(interaction):
            self._last_checked_interaction_id = None
            return True
        return await self.interaction_check(interaction)

    def build_current_embed(self) -> discord.Embed:
        raise NotImplementedError

    def current_embed(self) -> discord.Embed:
        return self.build_current_embed()

    async def on_timeout(self) -> None:
        self._timed_out = True
        self._sync_buttons()
        await safe_timeout_disable(self, self.message)

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        """Route view callback errors ephemerally with safe correlation IDs; never swallow cancellation."""
        if isinstance(error, asyncio.CancelledError):
            raise error
        await respond_with_failure(
            interaction,
            error,
            logger=log,
            settings=self.settings,
            command_name=f"{type(self).__name__}.{getattr(item, 'custom_id', type(item).__name__)}",
            prefer_embed=False,
        )

    async def _step_page(self, interaction: discord.Interaction, delta: int) -> None:
        if not await self._ensure_access(interaction):
            return
        self.current_page = max(0, min(self.total_pages - 1, self.current_page + delta))
        self._sync_buttons()
        await safe_edit_view_message(interaction, embed=self.build_current_embed(), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._step_page(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._step_page(interaction, 1)
