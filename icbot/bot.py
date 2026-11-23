"""CampusBot lifecycle, command tree orchestration, task supervision, and gateway safeguard.

Centralizes Discord client construction, one-time ``setup_hook()`` initialization,
controlled slash-command registration/sync, reconnect-safe ``on_ready()``,
unified background/event task supervision, shared HTTP session management, and
clean shutdown / startup rollback.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from icbot.config import MAX_SNOWFLAKE, Settings
from icbot.container import ServiceContainer, resolve_guild_settings
from icbot.db import Database
from icbot.errors import (
    format_exception_safe,
    respond_with_failure,
)
from icbot.guild_config import GuildSettingsStore
from icbot.logging import get_correlation_id
from icbot.permissions import is_admin_or_allowed
from icbot.registry import check_command_gate, register_ic_commands
from icbot.scheduler import (
    TaskHealth,
    TaskSupervisor,
    build_default_task_specs,
    invoke_poller_callable,
    parse_hhmm,
    seconds_until_next_local_time,
)
from icbot.services import btc_esplora, crypto_fng, yahoo_quotes
from icbot.tasks.btc_watch import poll_and_post_btc_watch
from icbot.tasks.go_alerts import poll_and_post_go_alerts
from icbot.tasks.job_alerts import poll_and_post_jobs
from icbot.tasks.jobs_workday_alerts import poll_and_post_workday_jobs
from icbot.tasks.llm_assistant import maybe_handle_llm_mention
from icbot.tasks.market_daily_report import post_daily_market_report
from icbot.tasks.market_watch import poll_and_post_market_watch
from icbot.tasks.ttc_alerts import poll_and_post_ttc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentPolicy:
    """Resolved process-startup Gateway intent policy for ``CampusBot``.

    Gateway intents are negotiated once on connection (``IDENTIFY``) from startup
    process ``Settings`` (never mutable per-guild overrides). Changing privileged
    intents requires updating the Discord Developer Portal and restarting the bot.
    """

    guilds: bool = True
    messages: bool = True
    message_content: bool = False
    members: bool = False
    presences: bool = False
    explicit_override: bool = False
    reason: str = "least-privilege default (zero privileged intents)"

    @property
    def privileged_intents(self) -> tuple[str, ...]:
        """Return names of any privileged Gateway intents requested by this policy."""
        enabled: list[str] = []
        if self.message_content:
            enabled.append("message_content")
        if self.members:
            enabled.append("members")
        if self.presences:
            enabled.append("presences")
        return tuple(enabled)


def resolve_intent_policy(
    settings: Settings | None = None,
    *,
    explicit_intents: discord.Intents | None = None,
) -> IntentPolicy:
    """Derive a least-privilege startup Gateway intent policy from process ``Settings``.

    - Slash commands (``/ic``), ``/ic resume``, and ``/ic help`` require zero privileged intents.
    - Unprivileged ``guilds`` and ``messages`` (``guild_messages`` + ``dm_messages``) stay enabled
      so ``on_message`` can receive direct ``@mention`` events (whose content is delivered by
      Discord's mention-message exception without the privileged ``MESSAGE_CONTENT`` bit).
    - Privileged ``MESSAGE_CONTENT`` is requested ONLY when process startup explicitly enables
      both ``LLM_ENABLED=true`` and ``LLM_CHANNEL_HISTORY_ENABLED=true`` to read surrounding
      channel messages. Non-empty API keys or enabled feature profiles never silently enable it.
    """
    if explicit_intents is not None:
        return IntentPolicy(
            guilds=bool(explicit_intents.guilds),
            messages=bool(explicit_intents.messages),
            message_content=bool(explicit_intents.message_content),
            members=bool(explicit_intents.members),
            presences=bool(explicit_intents.presences),
            explicit_override=True,
            reason="explicit caller-supplied discord.Intents",
        )

    eff = settings if settings is not None else Settings()
    need_message_content = bool(eff.llm_enabled and eff.llm_channel_history_enabled)
    if need_message_content:
        reason = "LLM_ENABLED=true and LLM_CHANNEL_HISTORY_ENABLED=true at process startup"
    elif eff.llm_enabled:
        reason = "least-privilege default (direct @mentions use unprivileged messages intent)"
    else:
        reason = "least-privilege default (zero privileged intents)"

    return IntentPolicy(
        guilds=True,
        messages=True,
        message_content=need_message_content,
        members=False,
        presences=False,
        explicit_override=False,
        reason=reason,
    )


def build_intents(settings: Settings | None = None) -> discord.Intents:
    """Return the least-privilege Gateway intents for Instigate Cafe Bot startup ``Settings``."""
    policy = resolve_intent_policy(settings)
    intents = discord.Intents.default()
    intents.guilds = policy.guilds
    intents.messages = policy.messages
    intents.message_content = policy.message_content
    intents.members = policy.members
    intents.presences = policy.presences
    return intents


def format_privileged_intents_error(policy: IntentPolicy) -> str:
    """Build an actionable operator diagnostic when Discord rejects privileged intents."""
    requested = ", ".join(policy.privileged_intents) or "message_content"
    return (
        f"Discord rejected privileged Gateway intent(s) ({requested}) requested by startup policy "
        f"({policy.reason}). To fix this, either:\n"
        "  1. Enable 'Message Content Intent' in the Discord Developer Portal "
        "(Application -> Bot -> Privileged Gateway Intents) and restart the bot, OR\n"
        "  2. Set LLM_CHANNEL_HISTORY_ENABLED=false in process configuration and restart to run "
        "with zero privileged intents (/ic slash commands, /ic resume, and direct @mention replies "
        "remain available)."
    )


def validate_gateway_launch(settings: Settings, *, allow_gateway: bool = False) -> None:
    """Enforce explicit operator opt-in before opening a live Discord gateway session.

    Gateway connections default to disabled in local development and CI to prevent
    accidental live bot launches when importing or inspecting the repository.
    """
    if allow_gateway or settings.allow_gateway:
        return
    raise SystemExit(
        "Gateway launch disabled by runtime safeguard. "
        "Set ICBOT_ALLOW_GATEWAY=true or pass --allow-gateway to connect to Discord."
    )


def _fmt_csv(items: list[str], *, limit: int = 12) -> str:
    cleaned = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not cleaned:
        return "(default)"
    if len(cleaned) <= limit:
        return ",".join(cleaned)
    head = ",".join(cleaned[:limit])
    return f"{head},...(+{len(cleaned) - limit})"


def _parse_hhmm(raw: str, default_h: int = 16, default_m: int = 30) -> tuple[int, int]:
    return parse_hhmm(raw, default_h=default_h, default_m=default_m)


def _seconds_until_next(hh: int, mm: int, tz_name: str, *, now_unix: float | None = None) -> float:
    """Return DST-accurate seconds until next local ``(hh:mm)`` occurrence in ``tz_name``."""
    return seconds_until_next_local_time(hh, mm, tz_name, now_unix=now_unix)


async def ic_channel_gate(
    interaction: discord.Interaction[Any],
    settings: Settings | None = None,
) -> bool:
    """Gate ``/ic`` slash commands by role and ``USER_COMMAND_CHANNEL_ID``.

    - Direct messages are always allowed.
    - Non-``/ic`` commands pass through untouched.
    - Admins and ``ALLOWED_ROLE_IDS`` holders may run ``/ic`` in any channel.
    - Standard members are restricted to ``USER_COMMAND_CHANNEL_ID`` when set.
    """
    if not interaction.guild:
        return True

    cmd = getattr(interaction, "command", None)
    qname = getattr(cmd, "qualified_name", "") or ""
    if not qname.startswith("ic"):
        return True

    eff_settings = settings or await resolve_guild_settings(interaction.client, interaction)

    if is_admin_or_allowed(interaction, eff_settings):
        return True

    allowed_ch = int(getattr(eff_settings, "user_command_channel_id", 0) or 0)
    if allowed_ch and interaction.channel_id != allowed_ch:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Use bot commands in <#{allowed_ch}> to keep the server clean.",
                ephemeral=True,
            )
        return False

    return True


async def on_app_command_error(
    interaction: discord.Interaction[Any],
    error: app_commands.AppCommandError,
) -> None:
    """Handle application command cooldowns, check failures, and classified failure taxonomy."""
    inner = getattr(error, "original", error)
    if isinstance(inner, asyncio.CancelledError):
        raise inner

    if isinstance(error, app_commands.CommandOnCooldown):
        mins, secs = divmod(error.retry_after, 60)
        msg = (
            f"⏳ This command is on cooldown. Please try again in **{math.ceil(mins)}m {math.ceil(secs)}s**."
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return

    if isinstance(error, app_commands.CheckFailure):
        return

    cid = get_correlation_id()
    cmd_name = interaction.command.qualified_name if interaction.command else "unknown"
    await respond_with_failure(
        interaction,
        error,
        logger=log,
        command_name=cmd_name,
        correlation_id=cid,
        prefer_embed=False,
    )


class _CafeCommandTree(app_commands.CommandTree["CampusBot"]):
    async def interaction_check(self, interaction: discord.Interaction[CampusBot], /) -> bool:
        eff_settings = await resolve_guild_settings(self.client, interaction)
        if not await check_command_gate(interaction, eff_settings):
            return False
        return await ic_channel_gate(interaction, eff_settings)

    async def on_error(
        self,
        interaction: discord.Interaction[CampusBot],
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        await on_app_command_error(interaction, error)


class CampusBot(commands.Bot):
    """Stateful Discord bot with explicit lifecycle, DI ownership, sync, and shutdown."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        db: Database | None = None,
        services: ServiceContainer | None = None,
        clock: Callable[[], float] | None = None,
        http_session: aiohttp.ClientSession | None = None,
        register_commands: bool = True,
        sync_commands: bool = True,
        feature_modules: Sequence[str] | None = None,
        raise_on_sync_error: bool = False,
        intents: discord.Intents | None = None,
        command_prefix: Any = "!",
        **options: Any,
    ) -> None:
        default_allowed_mentions = options.pop("allowed_mentions", discord.AllowedMentions.none())
        if services is not None:
            eff_settings = settings if settings is not None else services.settings
        else:
            eff_settings = settings if settings is not None else Settings()

        intent_policy = resolve_intent_policy(eff_settings, explicit_intents=intents)
        resolved_intents = intents if intents is not None else build_intents(eff_settings)

        super().__init__(
            command_prefix=command_prefix,
            help_command=None,
            intents=resolved_intents,
            tree_cls=_CafeCommandTree,
            allowed_mentions=default_allowed_mentions,
            **options,
        )
        if services is not None:
            self.services: ServiceContainer = services
            eff_db = (
                db
                if db is not None
                else (services.db if services.db is not None else Database(eff_settings.db_path))
            )
            self.services.settings = eff_settings
            if self.services.db is None:
                self.services.db = eff_db
            if self.services.guild_settings is None and self.services.db is not None:
                self.services.guild_settings = GuildSettingsStore(self.services.db, clock=self.services.clock)
        else:
            eff_db = db if db is not None else Database(eff_settings.db_path)
            self.services = ServiceContainer.from_settings(
                eff_settings,
                db=eff_db,
                clock=clock,
                http_session=http_session,
            )

        self.settings: Settings = eff_settings
        self.db: Database = eff_db
        self.intent_policy: IntentPolicy = intent_policy
        self.http_session: aiohttp.ClientSession | None = self.services.http_session

        self._register_commands_enabled = register_commands
        self._sync_commands_enabled = sync_commands
        self._feature_modules = tuple(feature_modules) if feature_modules is not None else None
        self._raise_on_sync_error = raise_on_sync_error

        self.ready_count: int = 0
        self.setup_complete: bool = False
        self.commands_synced: bool = False
        self.last_sync_error: Exception | None = None
        self._llm_killed: bool = self.services.llm_killed
        self._lore_cache: dict[str, dict[str, Any]] = self.services.lore_cache
        self._llm_last_reply: dict[int, float] = self.services.llm_last_reply
        self._jobs_bad_sources: set[str] = self.services.jobs_bad_sources
        self._workday_bad_sources: set[str] = self.services.workday_bad_sources
        self.supervisor: TaskSupervisor = self._build_supervisor()
        self.supervisor.reconcile()

    def _secrets(self) -> tuple[str, ...]:
        return self.settings.secret_values()

    def _build_supervisor(self) -> TaskSupervisor:
        sup = TaskSupervisor(
            settings_getter=lambda: self.settings,
            is_closed=self.is_closed,
            wait_until_ready=self.wait_until_ready,
            secrets_getter=self._secrets,
            clock=self.services.clock,
            sleep_fn=self.services.sleep_fn,
            jitter_rng=self.services.jitter_rng,
            llm_killed_getter=lambda: bool(self.services.llm_killed or self._llm_killed),
        )

        defaults: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
            "poll_and_post_ttc": poll_and_post_ttc,
            "poll_and_post_go_alerts": poll_and_post_go_alerts,
            "poll_and_post_jobs": poll_and_post_jobs,
            "poll_and_post_workday_jobs": poll_and_post_workday_jobs,
            "poll_and_post_market_watch": poll_and_post_market_watch,
            "poll_and_post_btc_watch": poll_and_post_btc_watch,
            "post_daily_market_report": post_daily_market_report,
        }

        def _make_runner(symbol_name: str) -> Callable[[], Coroutine[Any, Any, Any]]:
            default_fn = defaults[symbol_name]

            async def _run() -> Any:
                fn = globals().get(symbol_name, default_fn)
                return await invoke_poller_callable(fn, self, raise_on_error=True)

            return _run

        sup.register_many(
            build_default_task_specs(
                run_ttc=_make_runner("poll_and_post_ttc"),
                run_go=_make_runner("poll_and_post_go_alerts"),
                run_jobs=_make_runner("poll_and_post_jobs"),
                run_workday=_make_runner("poll_and_post_workday_jobs"),
                run_market_watch=_make_runner("poll_and_post_market_watch"),
                run_btc_watch=_make_runner("poll_and_post_btc_watch"),
                run_daily_report=_make_runner("post_daily_market_report"),
            )
        )
        return sup

    @property
    def background_tasks(self) -> dict[str, asyncio.Task[None]]:
        """Shallow snapshot of currently tracked background scheduler tasks."""
        return self.supervisor.active_tasks

    def task_health(self) -> dict[str, TaskHealth]:
        """Return redacted snapshots of all supervised background and event tasks."""
        return self.supervisor.health_snapshot()

    def reconcile_tasks(self, settings: Settings | None = None) -> dict[str, asyncio.Task[None]]:
        """Re-evaluate capability gates and start/stop background tasks live."""
        if settings is not None:
            self.settings = settings
            self.services.settings = settings
            return self.supervisor.reconcile(settings)
        return self.supervisor.reconcile(self.settings)

    @property
    def target_sync_guild(self) -> discord.Object | None:
        """Guild target for fast dev sync, or ``None`` for controlled global sync."""
        if self.settings.guild_id and not getattr(self.settings, "guild_id_invalid", False):
            return discord.Object(id=self.settings.guild_id)
        return None

    async def _load_persisted_state(self) -> None:
        try:
            row = await self.db.fetchone("SELECT value FROM meta WHERE key=?", ("llm_killed",))
            killed = bool(row and str(row[0]) == "1")
            self._llm_killed = killed
            self.services.llm_killed = killed
            self.supervisor.sync_event_states()
            if killed:
                log.info("[BOOT] Loaded persisted llm_killed=True state from DB.")
        except (aiosqlite.Error, sqlite3.Error, TypeError, ValueError) as exc:
            log.warning(
                "[BOOT] Error loading LLM killswitch state: %s",
                format_exception_safe(exc, self._secrets()),
            )

    async def _init_http_sessions(self) -> None:
        self.http_session = await self.services.ensure_http_session()
        await self.services.ensure_go_http_session(self.settings)

    async def _close_http_sessions(self) -> None:
        self.http_session = None
        try:
            await self.services.close()
        except (aiohttp.ClientError, OSError, RuntimeError) as exc:
            log.warning(
                "[SHUTDOWN] Error closing bot http_session: %s",
                format_exception_safe(exc, self._secrets()),
            )

        for name, closer in (
            ("yahoo_quotes", yahoo_quotes.close_session),
            ("btc_esplora", btc_esplora.close_session),
            ("crypto_fng", crypto_fng.close_session),
        ):
            try:
                await closer()
            except (aiohttp.ClientError, OSError, RuntimeError) as exc:
                log.warning(
                    "[SHUTDOWN] Error closing %s session: %s",
                    name,
                    format_exception_safe(exc, self._secrets()),
                )

    def _log_startup_config(self) -> None:
        settings = self.settings
        pol = self.intent_policy
        log.info(
            "[INTENTS] guilds=%s messages=%s message_content=%s members=%s presences=%s (%s)",
            pol.guilds,
            pol.messages,
            pol.message_content,
            pol.members,
            pol.presences,
            pol.reason,
        )
        if bool(getattr(settings, "llm_channel_history_enabled", False)) and not bool(
            getattr(settings, "llm_enabled", False)
        ):
            log.warning(
                "[INTENTS] LLM_CHANNEL_HISTORY_ENABLED=true has no effect while LLM_ENABLED=false; "
                "MESSAGE_CONTENT privileged intent was not requested."
            )
        try:
            caps = settings.capabilities()
            cap_summary = ", ".join(
                f"{name}={'ready' if c.available else ('misconfigured' if c.configured else 'off')}"
                for name, c in sorted(caps.items())
            )
            log.info("[CAPABILITIES] %s", cap_summary)
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("[CAPABILITIES] summary error: %s", format_exception_safe(exc, self._secrets()))

        try:
            mw_enabled = bool(getattr(settings, "market_watch_enabled", False))
            mw_poll = int(getattr(settings, "market_watch_poll_seconds", 0) or 0)
            mw_channel = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
                getattr(settings, "stock_channel_id", 0) or 0
            )
            mw_vix_level = float(getattr(settings, "market_watch_vix_level", 0.0) or 0.0)
            mw_index_thr = float(getattr(settings, "market_watch_index_move_pct", 0.0) or 0.0)
            mw_stock_thr = float(getattr(settings, "market_watch_stock_move_pct", 0.0) or 0.0)
            mw_cooldown = int(getattr(settings, "market_watch_cooldown_minutes", 0) or 0)
            mw_tickers = getattr(settings, "market_watch_tickers", None) or []
            log.info(
                "[MARKET] config: enabled=%s channel_id=%s poll=%ss vix_level>=%g "
                "idx_move>=%g%% stk_move>=%g%% cooldown=%sm tickers=%s db=%s",
                mw_enabled,
                mw_channel,
                mw_poll,
                mw_vix_level,
                mw_index_thr,
                mw_stock_thr,
                mw_cooldown,
                _fmt_csv(list(mw_tickers)),
                settings.db_path,
            )
            if mw_enabled and not mw_channel:
                log.warning(
                    "[MARKET] enabled without channel id (MARKET_WATCH_CHANNEL_ID or STOCK_CHANNEL_ID)."
                )
        except (AttributeError, KeyError, LookupError, TypeError, ValueError) as exc:
            log.warning("[MARKET] config log error: %s", format_exception_safe(exc, self._secrets()))

        try:
            j_poll = int(getattr(settings, "jobs_poll_seconds", 0) or 0)
            if isinstance(settings, Settings):
                gh = list(settings.greenhouse_boards or [])
                lv = list(settings.lever_sites or [])
                jobs_ch = int(settings.job_alerts_channel_id or 0)
            else:
                gh = list(
                    getattr(settings, "greenhouse_boards", None)
                    or getattr(settings, "greenhouse_board_tokens", None)
                    or []
                )
                lv = list(
                    getattr(settings, "lever_sites", None)
                    or getattr(settings, "lever_site_tokens", None)
                    or []
                )
                jobs_ch = int(
                    getattr(settings, "job_alerts_channel_id", 0)
                    or getattr(settings, "jobs_channel_id", 0)
                    or 0
                )
            wd = list(getattr(settings, "workday_sites", None) or [])
            log.info(
                "[JOBS] config: channel_id=%s poll=%ss greenhouse=%s lever=%s workday=%s",
                jobs_ch,
                j_poll,
                _fmt_csv(gh),
                _fmt_csv(lv),
                _fmt_csv(wd),
            )
        except (AttributeError, KeyError, LookupError, TypeError, ValueError) as exc:
            log.warning("[JOBS] config log error: %s", format_exception_safe(exc, self._secrets()))

        try:
            ttc_channel = int(getattr(settings, "ttc_alerts_channel_id", 0) or 0)
            ttc_poll = int(getattr(settings, "ttc_poll_seconds", 0) or 0)
            ttc_level = str(getattr(settings, "ttc_alert_level", "major") or "major").strip().lower()
            ttc_base = str(getattr(settings, "ttc_mastodon_base", "") or "").strip()
            ttc_acct = str(getattr(settings, "ttc_mastodon_acct", "") or "").strip()
            ttc_replies = bool(getattr(settings, "ttc_include_replies", True))
            log.info(
                "[TTC] config: channel_id=%s poll=%ss level=%s include_replies=%s base=%s acct=%s",
                ttc_channel,
                ttc_poll,
                ttc_level,
                ttc_replies,
                ttc_base or "(none)",
                ttc_acct or "(none)",
            )
            if not ttc_channel:
                log.warning("[TTC] no TTC channel id set (TTC_ALERTS_CHANNEL_ID).")
            if not ttc_base or not ttc_acct:
                log.warning("[TTC] missing TTC_MASTODON_BASE or TTC_MASTODON_ACCT.")
        except (AttributeError, KeyError, LookupError, TypeError, ValueError) as exc:
            log.warning("[TTC] config log error: %s", format_exception_safe(exc, self._secrets()))

        try:
            llm_enabled = bool(getattr(settings, "llm_enabled", False))
            llm_model = str(
                getattr(settings, "gemini_model", "gemini-2.5-flash") or "gemini-2.5-flash"
            ).strip()
            llm_msgs = int(getattr(settings, "llm_max_messages", 25) or 25)
            llm_chars = int(getattr(settings, "llm_max_chars", 12000) or 12000)
            llm_cd = float(getattr(settings, "llm_cooldown_seconds", 3) or 3)
            has_key = bool(str(getattr(settings, "gemini_api_key", "") or "").strip())
            log.info(
                "[LLM] config: enabled=%s provider=gemini model=%s max_messages=%s max_chars=%s cooldown=%gs",
                llm_enabled,
                llm_model,
                llm_msgs,
                llm_chars,
                llm_cd,
            )
            if llm_enabled and not has_key:
                log.warning("[LLM] enabled without GEMINI_API_KEY set.")
        except (AttributeError, KeyError, LookupError, TypeError, ValueError) as exc:
            log.warning("[LLM] config log error: %s", format_exception_safe(exc, self._secrets()))

    def _register_commands(self) -> None:
        if getattr(self.settings, "guild_id_invalid", False):
            raw = getattr(self.settings, "guild_id_raw", "")
            log.warning(
                "[BOOT] Invalid GUILD_ID %r (> %s); falling back to controlled global command scope.",
                raw,
                MAX_SNOWFLAKE,
            )

        if self.settings.force_guild_resync:
            log.info(
                "[BOOT] FORCE_GUILD_RESYNC ignored for destructive clear_commands(); "
                "using idempotent override registration."
            )

        register_ic_commands(
            self.tree,
            guild=self.target_sync_guild,
            modules=self._feature_modules,
            override=True,
        )

    async def sync_commands(self) -> list[app_commands.AppCommand]:
        """Execute a controlled initial slash-command sync without clearing existing commands.

        - When ``GUILD_ID`` is configured (> 0 and valid), syncs only to that development guild
          so updates appear immediately without touching global registrations.
        - Otherwise performs a single controlled global sync during ``setup_hook()`` and never
          repeats global syncs on gateway reconnects.
        """
        guild = self.target_sync_guild
        try:
            if guild is not None:
                synced = await self.tree.sync(guild=guild)
                log.info("Synced commands to development guild %s (%s root entries).", guild.id, len(synced))
            else:
                synced = await self.tree.sync()
                log.info(
                    "Synced commands globally (%s root entries; propagation may take up to 1h).",
                    len(synced),
                )
        except (discord.DiscordException, aiohttp.ClientError, TimeoutError) as exc:
            self.last_sync_error = exc
            self.commands_synced = False
            if self._raise_on_sync_error:
                raise
            log.error(
                "Initial command sync failed (%s); Discord will not receive updated /ic commands until fixed.",
                format_exception_safe(exc, self._secrets()),
            )
            return []

        self.last_sync_error = None
        self.commands_synced = True
        return synced

    def _start_background_tasks(self) -> None:
        self.supervisor.start(self.settings)

    async def _cancel_background_tasks(self) -> None:
        await self.supervisor.stop_all()

    async def _cleanup_runtime(self) -> None:
        log.info("[SHUTDOWN] Initiating runtime resource cleanup...")
        self.setup_complete = False
        await self._cancel_background_tasks()
        log.debug("[SHUTDOWN] Background supervisor tasks cancelled.")
        await self._close_http_sessions()
        log.debug("[SHUTDOWN] HTTP sessions closed.")
        try:
            await self.db.close()
            log.debug("[SHUTDOWN] Database connections closed.")
        except (aiosqlite.Error, sqlite3.Error, RuntimeError, OSError) as exc:
            log.warning(
                "[SHUTDOWN] Error closing database: %s",
                format_exception_safe(exc, self._secrets()),
            )
        log.info("[SHUTDOWN] Runtime cleanup complete.")

    async def setup_hook(self) -> None:
        """Perform idempotent one-time startup before gateway readiness."""
        if self.setup_complete:
            log.debug("setup_hook() invoked after completion; keeping existing tasks and sessions.")
            return

        if self._ready is discord.utils.MISSING:
            await self._async_setup_hook()
        await super().setup_hook()

        committed = False
        try:
            await self.db.init()
            await self._load_persisted_state()
            await self._init_http_sessions()
            self._log_startup_config()

            if self._register_commands_enabled:
                self._register_commands()
            if self._sync_commands_enabled:
                await self.sync_commands()

            self._start_background_tasks()
            self.setup_complete = True
            committed = True
        finally:
            if not committed:
                await self._cleanup_runtime()

    async def on_ready(self) -> None:
        """Reconnect-safe readiness handler: tracks readiness only (no re-init/re-sync/respawn)."""
        self.ready_count += 1
        state_label = "initial ready" if self.ready_count == 1 else f"reconnect ready #{self.ready_count}"
        if self.user is not None:
            log.info("Bot %s (%s, id=%s).", state_label, self.user, self.user.id)
        else:
            log.info("Bot %s.", state_label)

    async def on_message(self, message: discord.Message, /) -> None:
        """Route explicit ``@bot`` mentions to the LLM assistant; dispatch prefix commands only if defined."""
        if self.user is not None and self.user in (message.mentions or []):
            self.supervisor.sync_event_states()
            self.supervisor.record_event_start("llm_assistant")
            try:
                fn = globals().get("maybe_handle_llm_mention", maybe_handle_llm_mention)
                handled = await invoke_poller_callable(fn, self, message, raise_on_error=True)
            except asyncio.CancelledError:
                self.supervisor.sync_event_states()
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                AttributeError,
                LookupError,
            ) as exc:
                self.supervisor.record_event_error("llm_assistant", exc)
                if bool(getattr(self.settings, "llm_debug", False)):
                    log.debug(
                        "[LLM] on_message handler error: %s",
                        format_exception_safe(exc, self._secrets()),
                    )
            else:
                if handled is False:
                    self.supervisor.sync_event_states()
                else:
                    self.supervisor.record_event_success("llm_assistant")
        elif (
            bool(getattr(self.settings, "llm_debug", False))
            and self.user is not None
            and getattr(message, "reference", None) is not None
            and message.author
            and not message.author.bot
        ):
            mentioned_ids = [u.id for u in (message.mentions or [])]
            if self.user.id in mentioned_ids:
                log.debug("[LLM] reply contains explicit bot mention via reply toggle.")

        if self.commands:
            await self.process_commands(message)

    async def close(self) -> None:
        """Cancel background tasks, close owned HTTP/DB resources, and close Discord client."""
        try:
            await self._cleanup_runtime()
        finally:
            await super().close()


def create_bot(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    services: ServiceContainer | None = None,
    clock: Callable[[], float] | None = None,
    http_session: aiohttp.ClientSession | None = None,
    register_commands: bool = True,
    sync_commands: bool = True,
    feature_modules: Sequence[str] | None = None,
    raise_on_sync_error: bool = False,
    intents: discord.Intents | None = None,
) -> CampusBot:
    """Construct a ``CampusBot`` instance with explicit settings, DI container, and lifecycle ownership."""
    if services is not None:
        eff_settings = settings if settings is not None else services.settings
    else:
        eff_settings = settings if settings is not None else Settings.from_env()
    return CampusBot(
        settings=eff_settings,
        db=db,
        services=services,
        clock=clock,
        http_session=http_session,
        register_commands=register_commands,
        sync_commands=sync_commands,
        feature_modules=feature_modules,
        raise_on_sync_error=raise_on_sync_error,
        intents=intents,
    )
