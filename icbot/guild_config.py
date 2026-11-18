"""Per-guild configuration separation, storage, and effective-settings resolution.

Process secrets, DB/lore paths, TLS settings, and gateway safeguards belong strictly to
process ``Settings``. Guild overrides cover only safe per-guild channels, roles, branding,
timezones, prompts, and non-secret feature flags/thresholds stored in ``guild_settings``.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from icbot.config import MAX_SNOWFLAKE, Settings, _is_valid_snowflake, _to_color, _to_str_list
from icbot.db import Database
from icbot.errors import ConfigValidationError
from icbot.theme import KNOWN_THEME_NAMES, get_theme_preset

SECRET_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "gemini_api_key",
        "go_api_key",
        "odds_api_key",
    }
)

PROCESS_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "allow_gateway",
        "force_guild_resync",
        "guild_id",
        "guild_id_raw",
        "guild_id_invalid",
        "db_path",
        "grades_db_path",
        "lore_path",
        "ttc_mastodon_base",
        "ttc_mastodon_acct",
        "ttc_poll_seconds",
        "go_api_base",
        "go_poll_seconds",
        "go_ssl_verify",
        "go_ca_bundle_path",
        "jobs_poll_seconds",
        "workday_poll_seconds",
        "workday_timeout_seconds",
        "market_watch_poll_seconds",
        "btc_watch_poll_seconds",
        "btc_esplora_base_url",
        "task_startup_jitter_seconds",
        "task_max_backoff_seconds",
    }
)

GUILD_OVERRIDABLE_KEYS: frozenset[str] = frozenset(
    {
        # Feature profile & module overrides
        "feature_profile",
        "enabled_features",
        "disabled_features",
        # Channels & roles
        "stock_channel_id",
        "user_command_channel_id",
        "ttc_alerts_channel_id",
        "job_alerts_channel_id",
        "go_alerts_channel_id",
        "market_watch_channel_id",
        "market_daily_report_channel_id",
        "btc_watch_channel_id",
        "allowed_role_ids",
        # Embed branding & Themes
        "theme_preset",
        "bot_display_name",
        "stock_exchange_name",
        "currency_name",
        "currency_symbol",
        "primary_stock_symbol",
        "primary_stock_name",
        "source_attribution",
        "embed_logo_url",
        "embed_accent_color",
        "embed_footer_text",
        # Academics
        "exams_tz",
        "notes_sites",
        "rmp_university",
        "campus_id",
        "campus_url",
        "academic_course_aliases",
        "academic_allowed_hosts",
        "academic_enabled_providers",
        # Sports feeds (non-secret RSS overrides only)
        "sports_rss_nba",
        "sports_rss_nfl",
        "sports_rss_mma",
        "sports_rss_ufc",
        "sports_rss_boxing",
        # LLM behavior (uses process GEMINI_API_KEY)
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
        # Economy & Casino rules
        "daily_claim_amount",
        "bj_starting_bankroll",
        "bj_dealer_hits_soft_17",
        "bj_max_splits",
        "bj_session_ttl_minutes",
        # Careers filters
        "jobs_location_allowlist",
        "jobs_keywords_allowlist",
        "jobs_keywords_denylist",
        "jobs_require_early_career",
        # Transit/Market/BTC thresholds & labels
        "ttc_alert_level",
        "go_alert_level",
        "go_lines_allowlist",
        "market_watch_vix_level",
        "market_watch_index_move_pct",
        "market_watch_stock_move_pct",
        "market_watch_tickers",
        "market_watch_cooldown_minutes",
        "market_daily_report_time",
        "market_daily_report_tz",
        "market_daily_report_top_n",
        "market_daily_report_tickers",
        "btc_watch_label",
        "btc_watch_cooldown_minutes",
    }
)

_INT_KEYS = frozenset(
    {
        "stock_channel_id",
        "user_command_channel_id",
        "ttc_alerts_channel_id",
        "job_alerts_channel_id",
        "go_alerts_channel_id",
        "market_watch_channel_id",
        "market_daily_report_channel_id",
        "btc_watch_channel_id",
        "llm_cooldown_seconds",
        "llm_max_messages",
        "llm_max_chars",
        "llm_max_image_bytes",
        "llm_max_output_tokens",
        "daily_claim_amount",
        "bj_starting_bankroll",
        "bj_max_splits",
        "bj_session_ttl_minutes",
        "market_watch_cooldown_minutes",
        "market_daily_report_top_n",
        "btc_watch_cooldown_minutes",
    }
)

_BOOL_KEYS = frozenset(
    {
        "llm_enabled",
        "bj_dealer_hits_soft_17",
        "jobs_require_early_career",
    }
)

_FLOAT_KEYS = frozenset(
    {
        "market_watch_vix_level",
        "market_watch_index_move_pct",
        "market_watch_stock_move_pct",
    }
)

_STR_KEYS = frozenset(
    {
        "feature_profile",
        "theme_preset",
        "bot_display_name",
        "stock_exchange_name",
        "currency_name",
        "currency_symbol",
        "primary_stock_symbol",
        "primary_stock_name",
        "source_attribution",
        "embed_logo_url",
        "embed_footer_text",
        "exams_tz",
        "rmp_university",
        "campus_id",
        "campus_url",
        "sports_rss_nba",
        "sports_rss_nfl",
        "sports_rss_mma",
        "sports_rss_ufc",
        "sports_rss_boxing",
        "gemini_model",
        "gemini_resume_model",
        "gemini_resume_prompt",
        "gemini_resume_prompt_hw",
        "gemini_resume_prompt_ele",
        "ttc_alert_level",
        "go_alert_level",
        "market_daily_report_time",
        "market_daily_report_tz",
        "btc_watch_label",
    }
)

_STR_LIST_KEYS = frozenset(
    {
        "enabled_features",
        "disabled_features",
        "notes_sites",
        "academic_course_aliases",
        "academic_allowed_hosts",
        "academic_enabled_providers",
        "jobs_location_allowlist",
        "jobs_keywords_allowlist",
        "jobs_keywords_denylist",
        "go_lines_allowlist",
        "market_watch_tickers",
        "market_daily_report_tickers",
    }
)


def _coerce_guild_value(key: str, value: Any) -> Any:
    if key == "allowed_role_ids":
        if not isinstance(value, (list, tuple)):
            raise ConfigValidationError("allowed_role_ids must be a list of integer role IDs.")
        roles: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ConfigValidationError("allowed_role_ids must contain integer role IDs.")
            if not _is_valid_snowflake(item, allow_zero=False):
                raise ConfigValidationError(f"allowed_role_ids contains out-of-range ID: {item}")
            roles.append(int(item))
        return roles

    if key == "theme_preset":
        if value is None:
            raise ConfigValidationError("theme_preset must be a string.")
        preset_name = str(value).strip().lower()
        if preset_name not in KNOWN_THEME_NAMES:
            raise ConfigValidationError(
                f"Unknown theme_preset {preset_name!r}; known presets are {', '.join(sorted(KNOWN_THEME_NAMES))}."
            )
        return preset_name

    if key == "embed_accent_color":
        if isinstance(value, bool):
            raise ConfigValidationError("embed_accent_color must be a color integer or '#RRGGBB' string.")
        parsed = _to_color(value, -1)
        if parsed < 0 or parsed > 0xFFFFFF:
            raise ConfigValidationError("embed_accent_color must be in 0x000000..0xFFFFFF.")
        return parsed

    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        raise ConfigValidationError(f"{key} must be a boolean.")

    if key in _INT_KEYS:
        if isinstance(value, bool):
            raise ConfigValidationError(f"{key} must be an integer.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{key} must be an integer.") from exc

    if key in _FLOAT_KEYS:
        if isinstance(value, bool):
            raise ConfigValidationError(f"{key} must be a float.")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{key} must be a float.") from exc

    if key in _STR_LIST_KEYS:
        if isinstance(value, (list, tuple, str)):
            return _to_str_list(value)
        raise ConfigValidationError(f"{key} must be a list of strings.")

    if key in _STR_KEYS:
        if value is None:
            raise ConfigValidationError(f"{key} must be a string.")
        return str(value).strip()

    raise ConfigValidationError(f"Unsupported guild override key: {key!r}")


def validate_guild_overrides(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize per-guild configuration overrides, rejecting secrets/process keys."""
    if not isinstance(raw, Mapping):
        raise ConfigValidationError("Guild overrides must be a mapping.")

    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if key in SECRET_SETTING_KEYS:
            raise ConfigValidationError(
                f"Secret key {key!r} belongs in process configuration and cannot be set per guild."
            )
        if key in PROCESS_ONLY_KEYS:
            raise ConfigValidationError(f"Process-level key {key!r} cannot be overridden per guild.")
        if key not in GUILD_OVERRIDABLE_KEYS:
            raise ConfigValidationError(f"Unknown or non-overridable guild setting key: {key!r}")
        cleaned[key] = _coerce_guild_value(key, v)

    # Validate merged values against Settings schema/range rules
    if cleaned:
        dataclasses.replace(Settings(), **cleaned).validate(strict=True)
    return cleaned


@dataclass(frozen=True)
class GuildSettings:
    """Validated per-guild configuration overrides persisted in SQLite."""

    guild_id: int
    overrides: Mapping[str, Any] = dataclasses.field(default_factory=lambda: MappingProxyType({}))
    updated_at: int = 0

    def __post_init__(self) -> None:
        if not _is_valid_snowflake(self.guild_id, allow_zero=False):
            raise ConfigValidationError(
                f"guild_id must be a valid Discord snowflake (1..{MAX_SNOWFLAKE}), got {self.guild_id!r}."
            )
        validated = validate_guild_overrides(self.overrides)
        object.__setattr__(self, "overrides", MappingProxyType(validated))
        object.__setattr__(self, "updated_at", int(self.updated_at or 0))

    @classmethod
    def from_overrides(
        cls,
        guild_id: int,
        overrides: Mapping[str, Any],
        *,
        updated_at: int = 0,
    ) -> GuildSettings:
        return cls(guild_id=guild_id, overrides=overrides, updated_at=updated_at)


def resolve_effective_settings(base: Settings, guild_cfg: GuildSettings | None) -> Settings:
    """Merge non-secret ``GuildSettings`` overrides onto process ``base`` settings."""
    if guild_cfg is None or not guild_cfg.overrides:
        return base
    validated = validate_guild_overrides(guild_cfg.overrides)
    if not validated:
        return base

    if "theme_preset" in validated:
        preset = get_theme_preset(validated["theme_preset"])
        preset_defaults: dict[str, Any] = {
            "embed_accent_color": preset.accent_color,
            "embed_footer_text": preset.footer_text,
            "bot_display_name": preset.display_name,
            "stock_exchange_name": preset.stock_exchange_name,
            "currency_name": preset.currency_name,
            "currency_symbol": preset.currency_symbol,
            "primary_stock_symbol": preset.primary_stock_symbol,
            "primary_stock_name": preset.primary_stock_name,
            "source_attribution": preset.source_attribution,
        }
        merged_overrides = {**preset_defaults, **validated}
    else:
        merged_overrides = validated

    return dataclasses.replace(base, **merged_overrides)


@dataclass(frozen=True)
class GuildSettingsAuditEntry:
    """Immutable audit record tracking administrative per-guild configuration changes."""

    audit_id: int
    guild_id: int
    actor_id: int
    action: str
    changed_keys: tuple[str, ...]
    previous_json: str
    new_json: str
    created_at: int


class GuildSettingsStore:
    """SQLite-backed CRUD store for non-secret per-guild configuration overrides."""

    def __init__(self, db: Database, *, clock: Callable[[], float] | None = None) -> None:
        self.db = db
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock() if self._clock is not None else time.time())

    async def record_audit(
        self,
        guild_id: int,
        actor_id: int,
        action: str,
        changed_keys: Sequence[str],
        previous_json: str,
        new_json: str,
    ) -> None:
        """Persist an audit log entry for a configuration modification."""
        now = self._now()
        keys_str = ",".join(sorted(changed_keys))
        await self.db.execute(
            """
            INSERT INTO guild_settings_audit (
                guild_id, actor_id, action, changed_keys, previous_json, new_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(guild_id), int(actor_id), str(action), keys_str, str(previous_json), str(new_json), now),
        )

    async def get_audit_history(self, guild_id: int, limit: int = 10) -> list[GuildSettingsAuditEntry]:
        """Retrieve recent configuration audit entries for ``guild_id`` in reverse chronological order."""
        gid = int(guild_id)
        rows = await self.db.fetchall(
            """
            SELECT audit_id, guild_id, actor_id, action, changed_keys, previous_json, new_json, created_at
            FROM guild_settings_audit
            WHERE guild_id = ?
            ORDER BY created_at DESC, audit_id DESC
            LIMIT ?
            """,
            (gid, max(1, min(limit, 50))),
        )
        entries: list[GuildSettingsAuditEntry] = []
        for r in rows or []:
            raw_keys = str(r[4])
            parsed_keys = tuple(k.strip() for k in raw_keys.split(",") if k.strip())
            entries.append(
                GuildSettingsAuditEntry(
                    audit_id=int(r[0]),
                    guild_id=int(r[1]),
                    actor_id=int(r[2]),
                    action=str(r[3]),
                    changed_keys=parsed_keys,
                    previous_json=str(r[5]),
                    new_json=str(r[6]),
                    created_at=int(r[7]),
                )
            )
        return entries

    async def get(self, guild_id: int) -> GuildSettings | None:
        gid = int(guild_id)
        if not _is_valid_snowflake(gid, allow_zero=False):
            return None
        row = await self.db.fetchone(
            "SELECT settings_json, updated_at FROM guild_settings WHERE guild_id=?",
            (gid,),
        )
        if row is None:
            return None
        raw_json, updated_at = row
        try:
            parsed = json.loads(str(raw_json))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ConfigValidationError(f"Corrupt guild_settings JSON stored for guild {gid}") from exc
        if not isinstance(parsed, Mapping):
            raise ConfigValidationError(f"guild_settings payload for guild {gid} must be a JSON object")
        return GuildSettings(guild_id=gid, overrides=parsed, updated_at=int(updated_at or 0))

    async def set_overrides(
        self,
        guild_id: int,
        overrides: Mapping[str, Any],
        *,
        actor_id: int = 0,
    ) -> GuildSettings:
        gid = int(guild_id)
        existing = await self.get(gid)
        prev_payload = json.dumps(dict(existing.overrides), sort_keys=True) if existing is not None else "{}"

        now = self._now()
        gs = GuildSettings(guild_id=gid, overrides=overrides, updated_at=now)
        payload = json.dumps(dict(gs.overrides), sort_keys=True)
        await self.db.execute(
            """
            INSERT OR REPLACE INTO guild_settings (guild_id, settings_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (gid, payload, now),
        )
        if actor_id > 0:
            changed = list(set(overrides.keys()) | (set(existing.overrides.keys()) if existing else set()))
            await self.record_audit(
                guild_id=gid,
                actor_id=actor_id,
                action="set",
                changed_keys=changed,
                previous_json=prev_payload,
                new_json=payload,
            )
        return gs

    async def update_overrides(
        self,
        guild_id: int,
        updates: Mapping[str, Any],
        *,
        actor_id: int = 0,
    ) -> GuildSettings:
        gid = int(guild_id)
        existing = await self.get(gid)
        prev_payload = json.dumps(dict(existing.overrides), sort_keys=True) if existing is not None else "{}"
        merged: dict[str, Any] = dict(existing.overrides) if existing is not None else {}
        for k, v in updates.items():
            key = str(k).strip()
            if v is None:
                merged.pop(key, None)
            else:
                merged[key] = v
        now = self._now()
        gs = GuildSettings(guild_id=gid, overrides=merged, updated_at=now)
        new_payload = json.dumps(dict(gs.overrides), sort_keys=True)
        await self.db.execute(
            """
            INSERT OR REPLACE INTO guild_settings (guild_id, settings_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (gid, new_payload, now),
        )
        if actor_id > 0:
            await self.record_audit(
                guild_id=gid,
                actor_id=actor_id,
                action="update",
                changed_keys=list(updates.keys()),
                previous_json=prev_payload,
                new_json=new_payload,
            )
        return gs

    async def reset_overrides(self, guild_id: int, *, actor_id: int = 0) -> bool:
        """Reset all per-guild configuration overrides back to default process settings."""
        gid = int(guild_id)
        existing = await self.get(gid)
        if existing is None:
            return False
        prev_payload = json.dumps(dict(existing.overrides), sort_keys=True)
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id=?", (gid,))
        if actor_id > 0:
            await self.record_audit(
                guild_id=gid,
                actor_id=actor_id,
                action="reset",
                changed_keys=tuple(existing.overrides.keys()),
                previous_json=prev_payload,
                new_json="{}",
            )
        return True

    async def clear(self, guild_id: int) -> None:
        gid = int(guild_id)
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id=?", (gid,))

    async def export_guild_config(self, guild_id: int) -> dict[str, Any]:
        """Export non-secret per-guild configuration overrides in a portable format."""
        gid = int(guild_id)
        if not _is_valid_snowflake(gid, allow_zero=False):
            raise ConfigValidationError(
                f"guild_id must be a valid Discord snowflake (1..{MAX_SNOWFLAKE}), got {guild_id!r}."
            )
        cfg = await self.get(gid)
        return {
            "format_version": 1,
            "exported_at": self._now(),
            "guild_id": gid,
            "overrides": dict(cfg.overrides) if cfg is not None else {},
        }

    async def import_guild_config(
        self,
        guild_id: int,
        payload: Mapping[str, Any],
        *,
        overwrite: bool = False,
    ) -> GuildSettings:
        """Non-destructively import and validate per-guild configuration overrides.

        Safety:
            - Validates that no secret keys or process-only keys are imported.
            - When overwrite=False (default), merges non-destructively onto existing overrides.
            - When overwrite=True, completely replaces overrides.
        """
        gid = int(guild_id)
        if not _is_valid_snowflake(gid, allow_zero=False):
            raise ConfigValidationError(
                f"guild_id must be a valid Discord snowflake (1..{MAX_SNOWFLAKE}), got {guild_id!r}."
            )
        if not isinstance(payload, Mapping):
            raise ConfigValidationError("Guild configuration import payload must be a mapping / JSON object.")

        raw_overrides = payload.get("overrides", payload)
        if not isinstance(raw_overrides, Mapping):
            raise ConfigValidationError("Payload overrides must be a mapping / JSON object.")

        validated = validate_guild_overrides(raw_overrides)
        if overwrite:
            return await self.set_overrides(gid, validated)
        return await self.update_overrides(gid, validated)

    async def resolve_for_guild(self, base: Settings, guild_id: int | None) -> Settings:
        if not guild_id or not _is_valid_snowflake(int(guild_id), allow_zero=False):
            return base
        guild_cfg = await self.get(int(guild_id))
        return resolve_effective_settings(base, guild_cfg)
