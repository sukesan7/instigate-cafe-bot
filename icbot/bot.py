"""CampusBot lifecycle, command tree orchestration, and gateway safeguard.

Centralizes Discord client construction, one-time ``setup_hook()`` initialization,
controlled slash-command registration/sync, reconnect-safe ``on_ready()``,
cancellable background task tracking, and clean shutdown / startup rollback.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from icbot.config import MAX_SNOWFLAKE, Settings
from icbot.container import ServiceContainer, resolve_guild_settings
from icbot.db import Database
from icbot.errors import BotError, format_exception_safe, safe_error_summary
from icbot.guild_config import GuildSettingsStore
from icbot.permissions import is_admin_or_allowed
from icbot.registry import register_ic_commands
from icbot.services import btc_esplora, crypto_fng, yahoo_quotes
from icbot.services.fun_market import FunMarketError
from icbot.services.go_client import GOFetchError
from icbot.services.ttc_client import TTCFetchError
from icbot.tasks.btc_watch import poll_and_post_btc_watch
from icbot.tasks.go_alerts import poll_and_post_go_alerts
from icbot.tasks.job_alerts import poll_and_post_jobs
from icbot.tasks.jobs_workday_alerts import poll_and_post_workday_jobs
from icbot.tasks.llm_assistant import maybe_handle_llm_mention
from icbot.tasks.market_daily_report import post_daily_market_report
from icbot.tasks.market_watch import poll_and_post_market_watch
from icbot.tasks.ttc_alerts import poll_and_post_ttc

log = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    """Return the gateway intents required by Instigate Cafe Bot."""
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True  # needed for message-count tracking and @mention context
    intents.message_content = True  # required for @bot mentions and chat context
    return intents


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
    cleaned = (raw or "").strip()
    if not cleaned:
        return default_h, default_m
    try:
        if ":" in cleaned:
            h, m = cleaned.split(":", 1)
            return int(h), int(m)
        if len(cleaned) == 4 and cleaned.isdigit():
            return int(cleaned[:2]), int(cleaned[2:])
    except (TypeError, ValueError) as exc:
        log.debug("Invalid HHMM value %r: %s", raw, exc)
    return default_h, default_m


def _seconds_until_next(hh: int, mm: int, tz_name: str) -> float:
    """Return seconds until next local ``(hh:mm)`` occurrence in ``tz_name``."""
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        now = datetime.now(UTC)

    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


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
    """Handle application command cooldowns, check failures, domain errors, and unexpected errors."""
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

    client_settings = getattr(getattr(interaction, "client", None), "settings", None)
    secrets = client_settings.secret_values() if isinstance(client_settings, Settings) else ()
    inner = getattr(error, "original", error)

    if isinstance(inner, BotError):
        log.warning("App command domain/provider error: %s", format_exception_safe(inner, secrets))
        user_msg = f"❌ {safe_error_summary(inner, secrets)}"
    else:
        log.error("App command error: %s", format_exception_safe(error, secrets))
        user_msg = "❌ An unexpected error occurred."

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(user_msg, ephemeral=True)
        else:
            await interaction.followup.send(user_msg, ephemeral=True)
    except (discord.DiscordException, aiohttp.ClientError, RuntimeError) as exc:
        log.debug("Failed sending app command error response: %s", format_exception_safe(exc, secrets))


class _CafeCommandTree(app_commands.CommandTree["CampusBot"]):
    async def interaction_check(self, interaction: discord.Interaction[CampusBot], /) -> bool:
        eff_settings = await resolve_guild_settings(self.client, interaction)
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
        super().__init__(
            command_prefix=command_prefix,
            help_command=None,
            intents=intents if intents is not None else build_intents(),
            tree_cls=_CafeCommandTree,
            **options,
        )
        if services is not None:
            self.services: ServiceContainer = services
            eff_settings = settings if settings is not None else services.settings
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
            eff_settings = settings if settings is not None else Settings()
            eff_db = db if db is not None else Database(eff_settings.db_path)
            self.services = ServiceContainer.from_settings(
                eff_settings,
                db=eff_db,
                clock=clock,
                http_session=http_session,
            )

        self.settings: Settings = eff_settings
        self.db: Database = eff_db
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
        self._background_tasks: dict[str, asyncio.Task[None]] = {}

    def _secrets(self) -> tuple[str, ...]:
        return self.settings.secret_values()

    @property
    def background_tasks(self) -> dict[str, asyncio.Task[None]]:
        """Shallow snapshot of currently tracked background scheduler tasks."""
        return dict(self._background_tasks)

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
            if killed:
                log.info("[BOOT] Loaded persisted llm_killed=True state from DB.")
        except (aiosqlite.Error, sqlite3.Error, TypeError, ValueError) as exc:
            log.warning(
                "[BOOT] Error loading LLM killswitch state: %s",
                format_exception_safe(exc, self._secrets()),
            )

    async def _init_http_sessions(self) -> None:
        if self.http_session is None or self.http_session.closed:
            self.http_session = await self.services.ensure_http_session()
        await yahoo_quotes.init_session()
        await btc_esplora.init_session()
        await crypto_fng.init_session()

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
            log.warning(
                "Initial command sync failed (%s); retaining in-memory /ic command tree.",
                format_exception_safe(exc, self._secrets()),
            )
            return []

        self.last_sync_error = None
        self.commands_synced = True
        return synced

    def _spawn_task(self, name: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        existing = self._background_tasks.get(name)
        if existing is not None and not existing.done():
            coro.close()
            return existing

        task = asyncio.create_task(coro, name=f"icbot:{name}")
        self._background_tasks[name] = task

        def _on_done(finished: asyncio.Task[None]) -> None:
            if self._background_tasks.get(name) is finished:
                self._background_tasks.pop(name, None)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                log.error(
                    "Background task %s terminated with exception: %s",
                    name,
                    format_exception_safe(exc, self._secrets()),
                )

        task.add_done_callback(_on_done)
        return task

    def _start_background_tasks(self) -> None:
        settings = self.settings

        if getattr(settings, "ttc_alerts_channel_id", 0):
            self._spawn_task("ttc", self._ttc_loop())

        go_channel = int(
            getattr(settings, "go_alerts_channel_id", 0) or getattr(settings, "ttc_alerts_channel_id", 0) or 0
        )
        go_key = str(getattr(settings, "go_api_key", "") or "").strip()
        if go_channel and go_key:
            self._spawn_task("go", self._go_loop())
        elif go_channel and not go_key:
            log.info("[GO] enabled via channel without GO_API_KEY; skipping GO poller.")

        if isinstance(settings, Settings):
            jobs_channel_id = int(settings.job_alerts_channel_id or 0)
        else:
            jobs_channel_id = int(
                getattr(settings, "job_alerts_channel_id", 0) or getattr(settings, "jobs_channel_id", 0) or 0
            )
        if jobs_channel_id:
            self._spawn_task("jobs", self._jobs_loop())
        else:
            log.info("[JOBS] no jobs channel id set; skipping ATS jobs poller.")

        workday_sites = getattr(settings, "workday_sites", None) or []
        if jobs_channel_id and workday_sites:
            self._spawn_task("workday", self._workday_loop())
        elif workday_sites:
            log.warning("[WORKDAY] workday_sites set without jobs channel id; skipping Workday poller.")

        mw_enabled = bool(getattr(settings, "market_watch_enabled", False))
        mw_channel = int(getattr(settings, "market_watch_channel_id", 0) or 0) or int(
            getattr(settings, "stock_channel_id", 0) or 0
        )
        if mw_enabled and mw_channel:
            self._spawn_task("market_watch", self._market_loop())
        elif mw_enabled and not mw_channel:
            log.warning("[MARKET] enabled without channel_id configured; skipping market watch.")

        if bool(getattr(settings, "market_daily_report_enabled", False)):
            self._spawn_task("daily_market_report", self._daily_market_report_loop())

        if bool(getattr(settings, "btc_watch_enabled", False)):
            self._spawn_task("btc_watch", self._btc_watch_loop())

    async def _cancel_background_tasks(self) -> None:
        tasks = list(self._background_tasks.values())
        self._background_tasks.clear()
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup_runtime(self) -> None:
        self.setup_complete = False
        await self._cancel_background_tasks()
        await self._close_http_sessions()
        try:
            await self.db.close()
        except (aiosqlite.Error, sqlite3.Error, RuntimeError, OSError) as exc:
            log.warning(
                "[SHUTDOWN] Error closing database: %s",
                format_exception_safe(exc, self._secrets()),
            )

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
        try:
            if self.user is not None and self.user in (message.mentions or []):
                await maybe_handle_llm_mention(self, message)
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
            if bool(getattr(self.settings, "llm_debug", False)):
                log.debug(
                    "[LLM] on_message handler error: %s",
                    format_exception_safe(exc, self._secrets()),
                )

        if self.commands:
            await self.process_commands(message)

    async def close(self) -> None:
        """Cancel background tasks, close owned HTTP/DB resources, and close Discord client."""
        try:
            await self._cleanup_runtime()
        finally:
            await super().close()

    async def _ttc_loop(self) -> None:
        """Background poller that posts TTC alerts into TTC_ALERTS_CHANNEL_ID."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await poll_and_post_ttc(self)
            except asyncio.CancelledError:
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                TTCFetchError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[TTC] loop error: %s", format_exception_safe(exc, self._secrets()))
            await asyncio.sleep(max(30, int(getattr(self.settings, "ttc_poll_seconds", 120) or 120)))

    async def _go_loop(self) -> None:
        """Background poller for GO Transit Service Alerts."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await poll_and_post_go_alerts(self)
            except asyncio.CancelledError:
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                GOFetchError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[GO] loop error: %s", format_exception_safe(exc, self._secrets()))
            await asyncio.sleep(max(60, int(getattr(self.settings, "go_poll_seconds", 180) or 180)))

    async def _jobs_loop(self) -> None:
        """Background poller that posts job alerts into the configured jobs channel."""
        await self.wait_until_ready()
        poll_s_init = int(getattr(settings := self.settings, "jobs_poll_seconds", 600) or 600)
        log.info("[JOBS] loop started (poll=%ss)", poll_s_init)

        while not self.is_closed():
            cycle_start = self.services.clock()
            try:
                log.debug("[JOBS] cycle begin")
                await poll_and_post_jobs(self)
                log.debug("[JOBS] cycle end (elapsed=%.1fs)", self.services.clock() - cycle_start)
            except asyncio.CancelledError:
                log.debug("[JOBS] loop cancelled")
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[JOBS] loop error: %s", format_exception_safe(exc, self._secrets()))

            poll_s = max(60, int(getattr(settings, "jobs_poll_seconds", 600) or 600))
            await asyncio.sleep(poll_s)

    async def _workday_loop(self) -> None:
        """Background poller for Workday sites (posts into the configured jobs channel)."""
        await self.wait_until_ready()
        poll_s0 = int(
            getattr(self.settings, "workday_poll_seconds", 0)
            or getattr(self.settings, "jobs_poll_seconds", 600)
            or 600
        )
        log.info("[WORKDAY] loop started (poll=%ss)", poll_s0)

        while not self.is_closed():
            cycle_start = self.services.clock()
            try:
                log.debug("[WORKDAY] cycle begin")
                await poll_and_post_workday_jobs(self)
                log.debug("[WORKDAY] cycle end (elapsed=%.1fs)", self.services.clock() - cycle_start)
            except asyncio.CancelledError:
                log.debug("[WORKDAY] loop cancelled")
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[WORKDAY] loop error: %s", format_exception_safe(exc, self._secrets()))

            poll_s = max(
                60,
                int(
                    getattr(self.settings, "workday_poll_seconds", 0)
                    or getattr(self.settings, "jobs_poll_seconds", 600)
                    or 600
                ),
            )
            await asyncio.sleep(poll_s)

    async def _market_loop(self) -> None:
        """Background poller that posts market volatility alerts."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await poll_and_post_market_watch(self)
            except asyncio.CancelledError:
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[MARKET] loop error: %s", format_exception_safe(exc, self._secrets()))
            await asyncio.sleep(max(10, int(getattr(self.settings, "market_watch_poll_seconds", 300) or 300)))

    async def _daily_market_report_loop(self) -> None:
        """Posts a daily market recap at a scheduled local time (e.g. 16:30 America/Toronto)."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if not bool(getattr(self.settings, "market_daily_report_enabled", False)):
                    await asyncio.sleep(300)
                    continue

                hh, mm = _parse_hhmm(
                    str(getattr(self.settings, "market_daily_report_time", "16:30") or "16:30")
                )
                tz_name = str(
                    getattr(self.settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto"
                )
                await asyncio.sleep(_seconds_until_next(hh, mm, tz_name))
                await post_daily_market_report(self)
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                FunMarketError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[DAILY] loop error: %s", format_exception_safe(exc, self._secrets()))
                await asyncio.sleep(120)

    async def _btc_watch_loop(self) -> None:
        """Polls watched BTC addresses and posts alerts on movement."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await poll_and_post_btc_watch(self)
            except asyncio.CancelledError:
                raise
            except (
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                aiosqlite.Error,
                sqlite3.Error,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                LookupError,
            ) as exc:
                log.warning("[BTC] loop error: %s", format_exception_safe(exc, self._secrets()))
            await asyncio.sleep(max(30, int(getattr(self.settings, "btc_watch_poll_seconds", 300) or 300)))


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
