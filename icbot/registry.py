"""Explicit slash-command feature registry and capability contracts for Instigate Cafe Bot.

Replaces implicit side-effect imports with a deterministic, testable registry while
preserving the ``/ic`` command group hierarchy and all 41 leaf command qualified names.
Provides modular feature manifests, health diagnostics, and per-guild runtime gating.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Any

import discord
from discord import app_commands

import icbot.features as _features_pkg
from icbot.app import ic
from icbot.config import Settings

log = logging.getLogger(__name__)

# Explicit list of all feature modules that attach slash commands onto icbot.app.ic.
FEATURE_MODULES: tuple[str, ...] = (
    "icbot.features.admin",
    "icbot.features.blackjack",
    "icbot.features.exams",
    "icbot.features.grades",
    "icbot.features.help",
    "icbot.features.info",
    "icbot.features.invest",
    "icbot.features.jobs",
    "icbot.features.lc_leaderboard",
    "icbot.features.notes",
    "icbot.features.prof",
    "icbot.features.profile",
    "icbot.features.report",
    "icbot.features.resume",
    "icbot.features.roulette",
    "icbot.features.slots",
    "icbot.features.sports",
)


@dataclass(frozen=True)
class FeatureManifest:
    """Capability contract and metadata for an extracted bot feature module."""

    module_id: str
    display_name: str
    description: str
    category: str
    source_modules: tuple[str, ...] = ()
    commands_supplied: tuple[str, ...] = ()
    pollers_supplied: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    required_channels: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    storage_tables: tuple[str, ...] = ()
    guild_config_keys: tuple[str, ...] = ()
    default_enabled: bool = True


@dataclass(frozen=True)
class FeatureHealth:
    """Operational readiness and dependency health for a bot feature module."""

    module_id: str
    display_name: str
    enabled: bool
    available: bool
    status: str  # "ready" | "disabled" | "degraded" | "missing_dependency" | "missing_config"
    reason: str = ""
    missing_secrets: tuple[str, ...] = ()
    missing_channels: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()


PRESETS: dict[str, tuple[str, ...]] = {
    "default": (
        "core",
        "academics",
        "careers",
        "transit",
        "economy",
        "casino",
        "market",
    ),
    "student_core": (
        "core",
        "academics",
        "careers",
        "transit",
        "economy",
    ),
    "toronto_tmu": (
        "core",
        "academics",
        "careers",
        "transit",
        "economy",
    ),
    "community_full": (
        "core",
        "academics",
        "careers",
        "transit",
        "economy",
        "casino",
        "market",
        "ai",
        "sports",
    ),
}

FEATURE_MANIFESTS: dict[str, FeatureManifest] = {
    "core": FeatureManifest(
        module_id="core",
        display_name="Core Utility & Admin",
        description="Essential bot administration, diagnostics, help, and LeetCode leaderboard.",
        category="utility",
        source_modules=(
            "icbot.features.admin",
            "icbot.features.info",
            "icbot.features.help",
            "icbot.features.lc_leaderboard",
        ),
        commands_supplied=(
            "ic admin llm_start",
            "ic admin llm_stop",
            "ic botinfo",
            "ic help",
            "ic leaderboard",
            "ic serverinfo",
        ),
        pollers_supplied=(),
        dependencies=(),
        required_secrets=(),
        required_channels=(),
        required_permissions=(),
        storage_tables=("guild_settings",),
        guild_config_keys=(
            "user_command_channel_id",
            "allowed_role_ids",
            "embed_logo_url",
            "embed_accent_color",
            "embed_footer_text",
        ),
        default_enabled=True,
    ),
    "academics": FeatureManifest(
        module_id="academics",
        display_name="Academics & Exams",
        description="Exam schedules, grade distributions, course notes, and professor ratings.",
        category="campus",
        source_modules=(
            "icbot.features.exams",
            "icbot.features.grades",
            "icbot.features.notes",
            "icbot.features.prof",
        ),
        commands_supplied=(
            "ic exam add",
            "ic exam clear",
            "ic exam list",
            "ic exam remove",
            "ic grades",
            "ic notes",
            "ic prof",
        ),
        pollers_supplied=(),
        dependencies=(),
        required_secrets=(),
        required_channels=(),
        required_permissions=(),
        storage_tables=("exams", "grades_course_cache"),
        guild_config_keys=("exams_tz", "notes_sites", "rmp_university"),
        default_enabled=True,
    ),
    "careers": FeatureManifest(
        module_id="careers",
        display_name="Careers & Jobs",
        description="Internship and new grad job feed alerts from Greenhouse, Lever, and Workday.",
        category="careers",
        source_modules=("icbot.features.jobs",),
        commands_supplied=("ic jobs",),
        pollers_supplied=("jobs", "workday"),
        dependencies=(),
        required_secrets=(),
        required_channels=("job_alerts_channel_id",),
        required_permissions=(),
        storage_tables=("job_seen", "workday_cxs_cache"),
        guild_config_keys=(
            "job_alerts_channel_id",
            "jobs_location_allowlist",
            "jobs_keywords_allowlist",
            "jobs_keywords_denylist",
            "jobs_require_early_career",
        ),
        default_enabled=True,
    ),
    "transit": FeatureManifest(
        module_id="transit",
        display_name="Toronto Transit (TTC & GO)",
        description="Real-time service disruption alerts for TTC and Metrolinx GO Transit.",
        category="transit",
        source_modules=(),
        commands_supplied=(),
        pollers_supplied=("ttc", "go"),
        dependencies=(),
        required_secrets=(),
        required_channels=("ttc_alerts_channel_id", "go_alerts_channel_id"),
        required_permissions=(),
        storage_tables=("alert_delivery_dedupes",),
        guild_config_keys=(
            "ttc_alerts_channel_id",
            "go_alerts_channel_id",
            "ttc_alert_level",
            "go_alert_level",
            "go_lines_allowlist",
        ),
        default_enabled=True,
    ),
    "economy": FeatureManifest(
        module_id="economy",
        display_name="Virtual Economy",
        description="Core virtual currency, daily claims, bankrolls, and transaction auditing.",
        category="economy",
        source_modules=("icbot.features.profile",),
        commands_supplied=("ic dailyclaim", "ic profile"),
        pollers_supplied=(),
        dependencies=(),
        required_secrets=(),
        required_channels=(),
        required_permissions=(),
        storage_tables=("users", "economy_ledger"),
        guild_config_keys=("daily_claim_amount",),
        default_enabled=True,
    ),
    "casino": FeatureManifest(
        module_id="casino",
        display_name="Cafe Casino",
        description="Interactive blackjack, roulette, and slot machines.",
        category="gaming",
        source_modules=(
            "icbot.features.blackjack",
            "icbot.features.roulette",
            "icbot.features.slots",
        ),
        commands_supplied=(
            "ic casino blackjack",
            "ic casino payouts",
            "ic casino roulette",
            "ic casino slots",
        ),
        pollers_supplied=(),
        dependencies=("economy",),
        required_secrets=(),
        required_channels=(),
        required_permissions=(),
        storage_tables=("blackjack_users",),
        guild_config_keys=(
            "bj_starting_bankroll",
            "bj_dealer_hits_soft_17",
            "bj_max_splits",
            "bj_session_ttl_minutes",
        ),
        default_enabled=True,
    ),
    "market": FeatureManifest(
        module_id="market",
        display_name="Stock Market & ICSE",
        description="Virtual stock exchange (ICSE), portfolio trading, market reports, and market/crypto watch.",
        category="market",
        source_modules=("icbot.features.invest", "icbot.features.report"),
        commands_supplied=(
            "ic admin move",
            "ic admin set",
            "ic admin sync_catalog",
            "ic admin weekroll",
            "ic icse buy",
            "ic icse cover",
            "ic icse history",
            "ic icse market",
            "ic icse portfolio",
            "ic icse quote",
            "ic icse sell",
            "ic icse short",
            "ic icse stock",
            "ic icse trades",
            "ic report",
        ),
        pollers_supplied=("market_watch", "btc_watch", "market_daily_report"),
        dependencies=("economy",),
        required_secrets=(),
        required_channels=(
            "stock_channel_id",
            "market_watch_channel_id",
            "market_daily_report_channel_id",
            "btc_watch_channel_id",
        ),
        required_permissions=(),
        storage_tables=(
            "fun_stocks",
            "fun_positions",
            "fun_trades",
            "fun_market_weeks",
            "fun_stock_events",
        ),
        guild_config_keys=(
            "stock_channel_id",
            "market_watch_channel_id",
            "market_watch_vix_level",
            "market_watch_index_move_pct",
            "market_watch_stock_move_pct",
            "market_watch_tickers",
            "market_watch_cooldown_minutes",
            "market_daily_report_channel_id",
            "market_daily_report_time",
            "market_daily_report_tz",
            "market_daily_report_top_n",
            "market_daily_report_tickers",
            "btc_watch_channel_id",
            "btc_watch_label",
            "btc_watch_cooldown_minutes",
        ),
        default_enabled=True,
    ),
    "ai": FeatureManifest(
        module_id="ai",
        display_name="Instigate AI Assistant",
        description="Gemini-powered resume critique and conversational mentions.",
        category="ai",
        source_modules=("icbot.features.resume",),
        commands_supplied=("ic resume",),
        pollers_supplied=("llm_mention",),
        dependencies=(),
        required_secrets=("gemini_api_key",),
        required_channels=(),
        required_permissions=(),
        storage_tables=(),
        guild_config_keys=(
            "llm_enabled",
            "llm_cooldown_seconds",
            "llm_max_messages",
            "llm_max_chars",
            "llm_max_image_bytes",
            "llm_max_output_tokens",
            "gemini_model",
            "gemini_resume_model",
            "gemini_resume_prompt",
            "gemini_resume_prompt_hw",
            "gemini_resume_prompt_ele",
        ),
        default_enabled=False,
    ),
    "sports": FeatureManifest(
        module_id="sports",
        display_name="Sports Odds & News",
        description="Upcoming games, betting odds, and sports headlines for NBA, NFL, MMA, UFC, and Boxing.",
        category="sports",
        source_modules=("icbot.features.sports",),
        commands_supplied=(
            "ic sports boxing",
            "ic sports mma",
            "ic sports nba",
            "ic sports nfl",
            "ic sports ufc",
        ),
        pollers_supplied=(),
        dependencies=(),
        required_secrets=(),
        required_channels=(),
        required_permissions=(),
        storage_tables=(),
        guild_config_keys=(
            "sports_rss_nba",
            "sports_rss_nfl",
            "sports_rss_mma",
            "sports_rss_ufc",
            "sports_rss_boxing",
        ),
        default_enabled=False,
    ),
}


class FeatureRegistry:
    """Typed registry managing feature manifests, command mapping, and health status."""

    def __init__(self, manifests: Mapping[str, FeatureManifest] | None = None) -> None:
        self._manifests: dict[str, FeatureManifest] = dict(manifests or FEATURE_MANIFESTS)
        self._command_to_module: dict[str, str] = {}
        for mod_id, manifest in self._manifests.items():
            for cmd in manifest.commands_supplied:
                self._command_to_module[cmd] = mod_id

    @property
    def manifests(self) -> MappingProxyType[str, FeatureManifest]:
        return MappingProxyType(self._manifests)

    def get_manifest(self, module_id: str) -> FeatureManifest | None:
        return self._manifests.get(module_id)

    def get_module_for_command(self, qualified_name: str) -> str | None:
        return self._command_to_module.get(qualified_name)

    def is_feature_enabled(
        self,
        module_id: str,
        settings: Settings,
        *,
        check_dependencies: bool = True,
    ) -> bool:
        """Return True if ``module_id`` is enabled under ``settings`` (and its dependencies met)."""
        manifest = self.get_manifest(module_id)
        if manifest is None:
            return False

        # Blacklist takes highest precedence
        disabled = set(settings.disabled_features or ())
        if module_id in disabled:
            return False

        # Whitelist takes second precedence
        enabled = set(settings.enabled_features or ())
        if module_id in enabled:
            candidate = True
        else:
            profile = (settings.feature_profile or "default").strip().lower()
            preset = PRESETS.get(profile, PRESETS["default"])
            if profile == "default" and module_id in ("ai", "sports"):
                if module_id == "ai":
                    candidate = bool(settings.llm_enabled or (settings.gemini_api_key or "").strip())
                else:  # sports
                    candidate = bool((settings.odds_api_key or "").strip())
            else:
                candidate = module_id in preset

        if not candidate:
            return False

        if check_dependencies:
            for dep in manifest.dependencies:
                if not self.is_feature_enabled(dep, settings, check_dependencies=True):
                    return False

        return True

    def resolve_feature_health(
        self,
        module_id: str,
        settings: Settings,
    ) -> FeatureHealth:
        """Resolve full operational health, missing configuration, and dependency status."""
        manifest = self.get_manifest(module_id)
        if manifest is None:
            return FeatureHealth(
                module_id=module_id,
                display_name=module_id,
                enabled=False,
                available=False,
                status="disabled",
                reason=f"Unknown module {module_id!r}",
            )

        # 1. Check administrative enabled state (ignoring dependencies first)
        enabled = self.is_feature_enabled(module_id, settings, check_dependencies=False)
        if not enabled:
            return FeatureHealth(
                module_id=module_id,
                display_name=manifest.display_name,
                enabled=False,
                available=False,
                status="disabled",
                reason=f"Feature {manifest.display_name!r} is disabled in this server.",
            )

        # 2. Check dependencies
        missing_deps: list[str] = []
        for dep in manifest.dependencies:
            if not self.is_feature_enabled(dep, settings, check_dependencies=True):
                missing_deps.append(dep)

        if missing_deps:
            dep_labels: list[str] = []
            for d in missing_deps:
                m_dep = self.get_manifest(d)
                dep_labels.append(m_dep.display_name if m_dep is not None else d)
            return FeatureHealth(
                module_id=module_id,
                display_name=manifest.display_name,
                enabled=True,
                available=False,
                status="missing_dependency",
                reason=f"Missing required dependency: {', '.join(dep_labels)}",
                missing_dependencies=tuple(missing_deps),
            )

        # 3. Check required secrets
        missing_secrets: list[str] = []
        for sec in manifest.required_secrets:
            val = getattr(settings, sec, "")
            if not val or not str(val).strip():
                missing_secrets.append(sec)

        if missing_secrets:
            return FeatureHealth(
                module_id=module_id,
                display_name=manifest.display_name,
                enabled=True,
                available=False,
                status="missing_config",
                reason=f"Missing required secret/configuration: {', '.join(missing_secrets)}",
                missing_secrets=tuple(missing_secrets),
            )

        # 4. Check required channels (for modules without commands like transit)
        missing_channels: list[str] = []
        for ch in manifest.required_channels:
            ch_id = int(getattr(settings, ch, 0) or 0)
            if ch_id <= 0:
                missing_channels.append(ch)

        if (
            missing_channels
            and manifest.pollers_supplied
            and not manifest.commands_supplied
            and len(missing_channels) == len(manifest.required_channels)
        ):
            return FeatureHealth(
                module_id=module_id,
                display_name=manifest.display_name,
                enabled=True,
                available=False,
                status="missing_config",
                reason=f"Missing required channel ID: {', '.join(missing_channels)}",
                missing_channels=tuple(missing_channels),
            )

        # 5. Check degraded states (optional secrets)
        degraded_reasons: list[str] = []
        if module_id == "sports" and not getattr(settings, "odds_api_key", ""):
            degraded_reasons.append("ODDS_API_KEY not configured (RSS headlines remain active)")
        if module_id == "transit" and not getattr(settings, "go_api_key", ""):
            degraded_reasons.append("GO_API_KEY not configured (TTC alerts remain active)")

        if degraded_reasons:
            return FeatureHealth(
                module_id=module_id,
                display_name=manifest.display_name,
                enabled=True,
                available=True,
                status="degraded",
                reason="; ".join(degraded_reasons),
                missing_channels=tuple(missing_channels),
            )

        return FeatureHealth(
            module_id=module_id,
            display_name=manifest.display_name,
            enabled=True,
            available=True,
            status="ready",
            reason="Ready",
            missing_channels=tuple(missing_channels),
        )

    def resolve_all_health(self, settings: Settings) -> dict[str, FeatureHealth]:
        return {mod_id: self.resolve_feature_health(mod_id, settings) for mod_id in self._manifests}


_GLOBAL_REGISTRY: FeatureRegistry | None = None


def get_feature_registry() -> FeatureRegistry:
    """Return the global process ``FeatureRegistry`` instance."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = FeatureRegistry()
    return _GLOBAL_REGISTRY


def is_feature_enabled(
    module_id: str,
    settings: Settings,
    *,
    check_dependencies: bool = True,
) -> bool:
    """Convenience helper querying the global ``FeatureRegistry``."""
    return get_feature_registry().is_feature_enabled(
        module_id, settings, check_dependencies=check_dependencies
    )


def resolve_feature_health(module_id: str, settings: Settings) -> FeatureHealth:
    """Convenience helper resolving health from the global ``FeatureRegistry``."""
    return get_feature_registry().resolve_feature_health(module_id, settings)


async def guard_feature(
    interaction: discord.Interaction[Any],
    module_id: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Check if ``module_id`` is enabled/available for the interaction, responding ephemerally if not.

    Prevents execution, data collection, and DB mutations when a feature or its
    dependency is disabled in the guild.
    """
    client = getattr(interaction, "client", None)
    if settings is not None:
        eff_settings = settings
    else:
        try:
            from icbot.container import resolve_guild_settings

            eff_settings = await resolve_guild_settings(client, interaction)
        except (discord.DiscordException, RuntimeError, TypeError, AttributeError, ValueError):
            eff_settings = getattr(client, "settings", None) or Settings()

    reg = get_feature_registry()
    health = reg.resolve_feature_health(module_id, eff_settings)
    if not health.available:
        if health.status == "missing_dependency":
            dep_labels: list[str] = []
            for dep in health.missing_dependencies:
                man = reg.get_manifest(dep)
                dep_labels.append(f"**{man.display_name}**" if man else f"**{dep}**")
            msg = (
                f"⚠️ The **{health.display_name}** feature is unavailable because required feature "
                f"{', '.join(dep_labels)} is disabled in this server."
            )
        elif health.status == "missing_config":
            msg = (
                f"⚠️ The **{health.display_name}** feature is unavailable due to missing server configuration."
            )
        else:
            msg = f"⚠️ The **{health.display_name}** feature is currently disabled in this server."

        resp = getattr(interaction, "response", None)
        if resp is not None:
            is_done_fn = getattr(resp, "is_done", lambda: False)
            done = is_done_fn() if callable(is_done_fn) else bool(is_done_fn)
            try:
                if not done:
                    send_fn = getattr(resp, "send_message", None)
                    if callable(send_fn):
                        res = send_fn(msg, ephemeral=True)
                        if inspect.isawaitable(res):
                            await res
                else:
                    followup = getattr(interaction, "followup", None)
                    send_fn = getattr(followup, "send", None) if followup else None
                    if callable(send_fn):
                        res = send_fn(msg, ephemeral=True)
                        if inspect.isawaitable(res):
                            await res
            except (discord.DiscordException, RuntimeError, TypeError, AttributeError, ValueError) as exc:
                log.debug("Failed sending guard_feature response: %s", exc)
        return False
    return True


async def check_command_gate(
    interaction: discord.Interaction[Any],
    settings: Settings,
) -> bool:
    """Enforce per-guild feature gating in ``CommandTree.interaction_check``."""
    cmd = getattr(interaction, "command", None)
    qname = getattr(cmd, "qualified_name", "") if cmd else ""
    if not qname or not qname.startswith("ic"):
        return True

    reg = get_feature_registry()
    module_id = reg.get_module_for_command(qname)
    if module_id is None:
        return True

    return await guard_feature(interaction, module_id, settings=settings)


# ---------------------------------------------------------------------------
# Slash command registration & module discovery
# ---------------------------------------------------------------------------


def discover_feature_modules() -> tuple[str, ...]:
    """Return sorted import paths for every non-private module under ``icbot.features``."""
    names = sorted(
        f"{_features_pkg.__name__}.{mod.name}"
        for mod in pkgutil.iter_modules(_features_pkg.__path__)
        if not mod.name.startswith("_")
    )
    return tuple(names)


def load_feature_modules(modules: Sequence[str] | None = None) -> tuple[ModuleType, ...]:
    """Import configured feature modules so their commands bind onto the ``/ic`` tree."""
    targets = tuple(modules) if modules is not None else FEATURE_MODULES
    return tuple(importlib.import_module(name) for name in targets)


def get_root_group() -> app_commands.Group:
    """Return the root ``/ic`` application command group."""
    return ic


def iter_leaf_commands(root: app_commands.Group | None = None) -> list[app_commands.Command[Any, Any, Any]]:
    """Return all leaf ``app_commands.Command`` objects registered under ``/ic``."""
    target = root or get_root_group()
    return [cmd for cmd in target.walk_commands() if isinstance(cmd, app_commands.Command)]


def registered_qualified_names(root: app_commands.Group | None = None) -> tuple[str, ...]:
    """Return sorted qualified names for every leaf command under ``/ic``."""
    return tuple(sorted(cmd.qualified_name for cmd in iter_leaf_commands(root)))


def register_ic_commands(
    tree: app_commands.CommandTree[Any],
    *,
    guild: discord.abc.Snowflake | None = None,
    modules: Sequence[str] | None = None,
    override: bool = True,
) -> app_commands.Group:
    """Load feature modules explicitly and bind ``/ic`` onto ``tree`` cleanly.

    Uses ``override=True`` so repeated registration remains idempotent without
    ever invoking ``tree.clear_commands()``.
    """
    load_feature_modules(modules)
    root = get_root_group()
    if guild is not None:
        tree.add_command(root, guild=guild, override=override)
    else:
        tree.add_command(root, override=override)
    return root
