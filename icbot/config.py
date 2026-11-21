"""Typed, validated configuration and capability readiness for Instigate Cafe Bot."""

from __future__ import annotations

import dataclasses
import math
import os
import re
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icbot.errors import ConfigValidationError, redact_secrets
from icbot.theme import DEFAULT_THEME_NAME, KNOWN_THEME_NAMES, get_theme_preset, is_accessible_accent

# Discord "snowflake" IDs fit in signed int64 for most libs
MAX_SNOWFLAKE = 9223372036854775807

_HEX_COLOR_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")
_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
_TRUE_VALUES = ("1", "true", "yes", "y", "on")
_FALSE_VALUES = ("0", "false", "no", "n", "off")
_VALID_ALERT_LEVELS = frozenset({"major", "all"})
KNOWN_FEATURE_PROFILES: frozenset[str] = frozenset(
    {"default", "student_core", "community_full", "toronto_tmu"}
)
KNOWN_MODULE_IDS: frozenset[str] = frozenset(
    {"core", "academics", "careers", "transit", "economy", "casino", "market", "ai", "sports"}
)


def _to_int(value: Any, default: int = 0, *, clamp_snowflake: bool = False) -> int:
    """Best-effort int parsing with optional snowflake clamping."""
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        n = int(s)
        if clamp_snowflake and (n < 0 or n > MAX_SNOWFLAKE):
            return default
        return n
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    try:
        if value is None:
            return default
        s = str(value).strip().lower()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
            return False
        return default
    except (TypeError, ValueError):
        return default


def _to_color(value: Any, default: int = 0xA64DFF) -> int:
    """Parse embed color from '#RRGGBB', '0xRRGGBB', 'RRGGBB', or int."""
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return default

        # Already numeric
        if s.isdigit():
            return int(s)

        m = _HEX_COLOR_RE.match(s)
        if m:
            return int(m.group(1), 16)

        return default
    except (TypeError, ValueError):
        return default


def _to_str_list(raw: Any) -> list[str]:
    """Parse comma-separated env lists, stripping junk tokens (e.g., '\\')."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = [str(x).strip() for x in raw]
    else:
        s = str(raw).strip()
        if not s:
            return []
        tokens = [tok.strip() for tok in s.split(",")]

    out: list[str] = []
    for t in tokens:
        if not t or t in ("\\", "\\\\"):
            continue
        out.append(t)

    # de-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq


def _is_valid_http_url(raw: str) -> bool:
    s = (raw or "").strip()
    if not s or any(ch.isspace() for ch in s):
        return False
    parsed = urlparse(s)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _is_valid_hhmm(raw: str) -> bool:
    m = _HHMM_RE.fullmatch((raw or "").strip())
    if not m:
        return False
    hh, mm = int(m.group(1)), int(m.group(2))
    return 0 <= hh <= 23 and 0 <= mm <= 59


def _is_valid_tz(tz_name: str) -> bool:
    name = (tz_name or "").strip()
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False


def _is_valid_snowflake(val: int, *, allow_zero: bool = True) -> bool:
    if isinstance(val, bool) or not isinstance(val, int):
        return False
    lower = 0 if allow_zero else 1
    return lower <= val <= MAX_SNOWFLAKE


def _is_memory_sqlite_path(raw: str) -> bool:
    s = (raw or "").strip()
    return s == ":memory:" or s.startswith("file::memory:") or "mode=memory" in s


def normalize_sqlite_path(raw: str, *, allow_empty: bool = False) -> str:
    """Normalize a configured SQLite database file path while preserving special memory URIs."""
    s = (raw or "").strip()
    if not s:
        if allow_empty:
            return ""
        raise ConfigValidationError("SQLite database path must not be empty.")
    if "\x00" in s:
        raise ConfigValidationError("SQLite database path must not contain null bytes.")
    if _is_memory_sqlite_path(s):
        return s
    if s in (".", "./", "..", "../") or s.endswith(("/", "\\")):
        raise ConfigValidationError(f"SQLite database path must point to a file, not a directory: {raw!r}")
    expanded = os.path.expanduser(s)
    normed = os.path.normpath(expanded)
    if normed in ("", "."):
        raise ConfigValidationError(f"SQLite database path must point to a file, not a directory: {raw!r}")
    return normed


# ---------------------------------------------------------------------------
# Sub-configuration dataclasses (immutable domain groups)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscordConfig:
    token: str = ""
    guild_id: int = 0
    guild_id_raw: str = ""
    guild_id_invalid: bool = False
    stock_channel_id: int = 0
    user_command_channel_id: int = 0
    allowed_role_ids: tuple[int, ...] = ()
    embed_logo_url: str = ""
    embed_accent_color: int = 0xA64DFF
    embed_footer_text: str = "Instigate Cafe"
    bot_display_name: str = "InstigateAI"
    stock_exchange_name: str = "Instigate Cafe Stock Exchange"
    currency_name: str = "IC Cash"
    currency_symbol: str = "$"
    primary_stock_symbol: str = "IC"
    primary_stock_name: str = "Instigate Cafe"
    source_attribution: str = "Internal Instigate Cafe Repository"
    theme_preset: str = "instigate_cafe"
    force_guild_resync: bool = False
    allow_gateway: bool = False


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "ic_stock.db"
    grades_db_path: str = "tmu_grades.db"
    lore_path: str = ""


@dataclass(frozen=True)
class AcademicsConfig:
    exams_tz: str = "America/Toronto"
    notes_sites: tuple[str, ...] = ()
    rmp_university: str = "Toronto Metropolitan University"
    campus_id: str = ""
    campus_url: str = ""
    academic_course_aliases: tuple[str, ...] = ()
    academic_allowed_hosts: tuple[str, ...] = ()
    academic_enabled_providers: tuple[str, ...] = ()
    exam_reminders_enabled: bool = False
    exam_reminders_channel_id: int = 0
    exam_reminder_lead_hours: tuple[int, ...] = (24, 2)


@dataclass(frozen=True)
class CareersConfig:
    job_alerts_channel_id: int = 0
    jobs_poll_seconds: int = 600
    greenhouse_boards: tuple[str, ...] = ()
    lever_sites: tuple[str, ...] = ()
    jobs_location_allowlist: tuple[str, ...] = ()
    jobs_keywords_allowlist: tuple[str, ...] = ()
    jobs_keywords_denylist: tuple[str, ...] = ()
    jobs_bootstrap_post: bool = False
    jobs_require_early_career: bool = False
    workday_sites: tuple[str, ...] = ()
    workday_poll_seconds: int = 0
    workday_timeout_seconds: int = 12


@dataclass(frozen=True)
class GeminiConfig:
    llm_enabled: bool = False
    llm_cooldown_seconds: int = 3
    llm_max_messages: int = 25
    llm_max_chars: int = 12000
    llm_max_image_bytes: int = 2_000_000
    llm_max_output_tokens: int = 1536
    llm_debug: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_resume_model: str = "gemini-2.5-pro"
    gemini_resume_prompt: str = ""
    gemini_resume_prompt_hw: str = ""
    gemini_resume_prompt_ele: str = ""
    llm_channel_history_enabled: bool = False
    llm_lore_enabled: bool = False
    llm_images_enabled: bool = True
    llm_allow_dm: bool = False
    llm_allowed_channel_ids: list[int] | None = None
    llm_allowed_role_ids: list[int] | None = None


@dataclass(frozen=True)
class EconomyConfig:
    daily_claim_amount: int = 100
    bj_starting_bankroll: int = 1000
    bj_dealer_hits_soft_17: bool = True
    bj_max_splits: int = 1
    bj_session_ttl_minutes: int = 20


@dataclass(frozen=True)
class TransitConfig:
    ttc_alerts_channel_id: int = 0
    ttc_mastodon_base: str = ""
    ttc_mastodon_acct: str = ""
    ttc_poll_seconds: int = 120
    ttc_alert_level: str = "major"
    ttc_bootstrap_post_latest: bool = False
    ttc_include_replies: bool = True
    ttc_include_reblogs: bool = False
    ttc_debug: bool = False
    go_api_base: str = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    go_api_key: str = ""
    go_alerts_channel_id: int = 0
    go_poll_seconds: int = 180
    go_alert_level: str = "major"
    go_bootstrap_post_latest: bool = True
    go_include_buses: bool = False
    go_lines_allowlist: tuple[str, ...] = ()
    go_ssl_verify: bool = True
    go_ca_bundle_path: str = ""
    go_debug: bool = False


@dataclass(frozen=True)
class FinancialAlertsConfig:
    market_watch_enabled: bool = False
    market_watch_poll_seconds: int = 60
    market_watch_channel_id: int = 0
    market_watch_vix_level: float = 20.0
    market_watch_index_move_pct: float = 2.5
    market_watch_stock_move_pct: float = 10.0
    market_watch_tickers: tuple[str, ...] = ()
    market_watch_cooldown_minutes: int = 30
    market_watch_debug: bool = False
    market_daily_report_enabled: bool = False
    market_daily_report_channel_id: int = 0
    market_daily_report_time: str = "16:30"
    market_daily_report_tz: str = "America/Toronto"
    market_daily_report_top_n: int = 8
    market_daily_report_tickers: tuple[str, ...] = ()
    market_daily_report_debug: bool = False
    btc_watch_enabled: bool = False
    btc_watch_poll_seconds: int = 300
    btc_watch_channel_id: int = 0
    btc_watch_label: str = "BTC Watch"
    btc_watch_addresses: tuple[str, ...] = ()
    btc_esplora_base_url: str = ""
    btc_watch_cooldown_minutes: int = 30
    btc_watch_debug: bool = False


@dataclass(frozen=True)
class PollIntervalBounds:
    """Inclusive safe operating bounds for periodic background pollers."""

    min_seconds: int
    max_seconds: int
    allow_zero: bool = False
    default_seconds: int = 120

    def is_valid(self, value: int) -> bool:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if self.allow_zero and value == 0:
            return True
        return self.min_seconds <= value <= self.max_seconds

    def clamp(self, value: int, *, inherited: int | None = None) -> int:
        v = int(value)
        if v <= 0 and inherited is not None and inherited > 0:
            v = int(inherited)
        if self.allow_zero and v == 0:
            return 0
        if v <= 0:
            v = self.default_seconds
        return max(self.min_seconds, min(v, self.max_seconds))


POLL_INTERVAL_BOUNDS: dict[str, PollIntervalBounds] = {
    "ttc_poll_seconds": PollIntervalBounds(
        min_seconds=30, max_seconds=86400, allow_zero=False, default_seconds=120
    ),
    "go_poll_seconds": PollIntervalBounds(
        min_seconds=60, max_seconds=86400, allow_zero=False, default_seconds=180
    ),
    "jobs_poll_seconds": PollIntervalBounds(
        min_seconds=60, max_seconds=86400, allow_zero=True, default_seconds=600
    ),
    "workday_poll_seconds": PollIntervalBounds(
        min_seconds=60, max_seconds=86400, allow_zero=True, default_seconds=600
    ),
    "market_watch_poll_seconds": PollIntervalBounds(
        min_seconds=10, max_seconds=86400, allow_zero=False, default_seconds=60
    ),
    "btc_watch_poll_seconds": PollIntervalBounds(
        min_seconds=30, max_seconds=86400, allow_zero=False, default_seconds=300
    ),
}


@dataclass(frozen=True)
class SchedulerConfig:
    task_startup_jitter_seconds: float = 15.0
    task_max_backoff_seconds: float = 1800.0


@dataclass(frozen=True)
class SportsConfig:
    odds_api_key: str = ""
    sports_rss_nba: str = ""
    sports_rss_nfl: str = ""
    sports_rss_mma: str = ""
    sports_rss_ufc: str = ""
    sports_rss_boxing: str = ""

    def rss_urls_for(self, sport: str) -> list[str]:
        attr = {
            "nba": self.sports_rss_nba,
            "nfl": self.sports_rss_nfl,
            "mma": self.sports_rss_mma,
            "ufc": self.sports_rss_ufc,
            "boxing": self.sports_rss_boxing,
        }.get((sport or "").strip().lower(), "")
        return _to_str_list(attr)


# ---------------------------------------------------------------------------
# Validation & Capability reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class CapabilityStatus:
    name: str
    configured: bool
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class ConfigValidationReport:
    issues: tuple[ValidationIssue, ...]
    capabilities: dict[str, CapabilityStatus]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Composite Settings (flat API preserved + typed component properties)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    # Discord
    token: str = ""
    guild_id: int = 0
    guild_id_raw: str = ""
    guild_id_invalid: bool = False

    # Channels
    stock_channel_id: int = 0
    user_command_channel_id: int = 0
    ttc_alerts_channel_id: int = 0
    job_alerts_channel_id: int = 0

    # Roles
    allowed_role_ids: list[int] | None = None

    # Embeds & Branding
    embed_logo_url: str = ""
    embed_accent_color: int = 0xA64DFF
    embed_footer_text: str = "Instigate Cafe"
    bot_display_name: str = "InstigateAI"
    stock_exchange_name: str = "Instigate Cafe Stock Exchange"
    currency_name: str = "IC Cash"
    currency_symbol: str = "$"
    primary_stock_symbol: str = "IC"
    primary_stock_name: str = "Instigate Cafe"
    source_attribution: str = "Internal Instigate Cafe Repository"
    theme_preset: str = "instigate_cafe"

    # Sports / Odds API
    odds_api_key: str = ""
    sports_rss_nba: str = ""
    sports_rss_nfl: str = ""
    sports_rss_mma: str = ""
    sports_rss_ufc: str = ""
    sports_rss_boxing: str = ""

    # TTC alerts
    ttc_mastodon_base: str = ""
    ttc_mastodon_acct: str = ""
    ttc_poll_seconds: int = 120
    ttc_alert_level: str = "major"
    ttc_bootstrap_post_latest: bool = False
    ttc_include_replies: bool = True
    ttc_include_reblogs: bool = False
    ttc_debug: bool = False

    # GO Transit alerts (official GO Open Data API)
    go_api_base: str = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    go_api_key: str = ""
    go_alerts_channel_id: int = 0
    go_poll_seconds: int = 180
    go_alert_level: str = "major"
    go_bootstrap_post_latest: bool = True
    go_include_buses: bool = False
    go_lines_allowlist: list[str] | None = None
    go_ssl_verify: bool = True
    go_ca_bundle_path: str = ""
    go_debug: bool = False

    # Exams
    exams_tz: str = "America/Toronto"

    # LLM mention assistant (Gemini)
    llm_enabled: bool = False
    llm_cooldown_seconds: int = 3
    llm_max_messages: int = 25
    llm_max_chars: int = 12000
    llm_max_image_bytes: int = 2_000_000
    llm_max_output_tokens: int = 1536
    llm_debug: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_resume_model: str = "gemini-2.5-pro"
    gemini_resume_prompt: str = ""
    gemini_resume_prompt_hw: str = ""
    gemini_resume_prompt_ele: str = ""
    llm_channel_history_enabled: bool = False
    llm_lore_enabled: bool = False
    llm_images_enabled: bool = True
    llm_allow_dm: bool = False
    llm_allowed_channel_ids: list[int] | None = None
    llm_allowed_role_ids: list[int] | None = None

    # Jobs alerts (Greenhouse/Lever)
    jobs_poll_seconds: int = 600
    greenhouse_boards: list[str] | None = None
    lever_sites: list[str] | None = None
    jobs_location_allowlist: list[str] | None = None
    jobs_keywords_allowlist: list[str] | None = None
    jobs_keywords_denylist: list[str] | None = None
    jobs_bootstrap_post: bool = False
    jobs_require_early_career: bool = False

    # Workday jobs
    workday_sites: list[str] | None = None
    workday_poll_seconds: int = 0
    workday_timeout_seconds: int = 12

    # Dev & Runtime Safeguard
    force_guild_resync: bool = False
    allow_gateway: bool = False
    task_startup_jitter_seconds: float = 15.0
    task_max_backoff_seconds: float = 1800.0

    # Feature registry & presets
    feature_profile: str = "default"
    enabled_features: list[str] | None = None
    disabled_features: list[str] | None = None

    # Economy
    daily_claim_amount: int = 100

    # Blackjack settings
    bj_starting_bankroll: int = 1000
    bj_dealer_hits_soft_17: bool = True
    bj_max_splits: int = 1
    bj_session_ttl_minutes: int = 20

    # Market watch
    market_watch_enabled: bool = False
    market_watch_poll_seconds: int = 60
    market_watch_channel_id: int = 0
    market_watch_vix_level: float = 20.0
    market_watch_index_move_pct: float = 2.5
    market_watch_stock_move_pct: float = 10.0
    market_watch_tickers: list[str] | None = None
    market_watch_cooldown_minutes: int = 30
    market_watch_debug: bool = False

    # Daily market summary
    market_daily_report_enabled: bool = False
    market_daily_report_channel_id: int = 0
    market_daily_report_time: str = "16:30"
    market_daily_report_tz: str = "America/Toronto"
    market_daily_report_top_n: int = 8
    market_daily_report_tickers: list[str] | None = None
    market_daily_report_debug: bool = False

    # Bitcoin address watch
    btc_watch_enabled: bool = False
    btc_watch_poll_seconds: int = 300
    btc_watch_channel_id: int = 0
    btc_watch_label: str = "BTC Watch"
    btc_watch_addresses: list[str] | None = None
    btc_esplora_base_url: str = ""
    btc_watch_cooldown_minutes: int = 30
    btc_watch_debug: bool = False

    # DB / Storage
    db_path: str = "ic_stock.db"
    grades_db_path: str = "tmu_grades.db"

    # Course Notes
    notes_sites: list[str] | None = None

    # Lore
    lore_path: str = ""

    # RateMyProfessors & Campus Academics
    rmp_university: str = "Toronto Metropolitan University"
    campus_id: str = ""
    campus_url: str = ""
    academic_course_aliases: list[str] | None = None
    academic_allowed_hosts: list[str] | None = None
    academic_enabled_providers: list[str] | None = None
    exam_reminders_enabled: bool = False
    exam_reminders_channel_id: int = 0
    exam_reminder_lead_hours: list[int] | None = None

    # Logging, Observability & Telemetry
    log_file: str = ""
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3
    telemetry_enabled: bool = False

    # ---- Typed component views ----

    @property
    def discord(self) -> DiscordConfig:
        return DiscordConfig(
            token=self.token,
            guild_id=self.guild_id,
            guild_id_raw=self.guild_id_raw,
            guild_id_invalid=self.guild_id_invalid,
            stock_channel_id=self.stock_channel_id,
            user_command_channel_id=self.user_command_channel_id,
            allowed_role_ids=tuple(self.allowed_role_ids or ()),
            embed_logo_url=self.embed_logo_url,
            embed_accent_color=self.embed_accent_color,
            embed_footer_text=self.embed_footer_text,
            bot_display_name=self.bot_display_name,
            stock_exchange_name=self.stock_exchange_name,
            currency_name=self.currency_name,
            currency_symbol=self.currency_symbol,
            primary_stock_symbol=self.primary_stock_symbol,
            primary_stock_name=self.primary_stock_name,
            source_attribution=self.source_attribution,
            theme_preset=self.theme_preset,
            force_guild_resync=self.force_guild_resync,
            allow_gateway=self.allow_gateway,
        )

    @property
    def storage(self) -> StorageConfig:
        return StorageConfig(
            db_path=self.db_path,
            grades_db_path=self.grades_db_path,
            lore_path=self.lore_path,
        )

    @property
    def academics(self) -> AcademicsConfig:
        return AcademicsConfig(
            exams_tz=self.exams_tz,
            notes_sites=tuple(self.notes_sites or ()),
            rmp_university=self.rmp_university,
            campus_id=self.campus_id,
            campus_url=self.campus_url,
            academic_course_aliases=tuple(self.academic_course_aliases or ()),
            academic_allowed_hosts=tuple(self.academic_allowed_hosts or ()),
            academic_enabled_providers=tuple(self.academic_enabled_providers or ()),
            exam_reminders_enabled=self.exam_reminders_enabled,
            exam_reminders_channel_id=self.exam_reminders_channel_id,
            exam_reminder_lead_hours=tuple(self.exam_reminder_lead_hours or (24, 2)),
        )

    @property
    def careers(self) -> CareersConfig:
        return CareersConfig(
            job_alerts_channel_id=self.job_alerts_channel_id,
            jobs_poll_seconds=self.jobs_poll_seconds,
            greenhouse_boards=tuple(self.greenhouse_boards or ()),
            lever_sites=tuple(self.lever_sites or ()),
            jobs_location_allowlist=tuple(self.jobs_location_allowlist or ()),
            jobs_keywords_allowlist=tuple(self.jobs_keywords_allowlist or ()),
            jobs_keywords_denylist=tuple(self.jobs_keywords_denylist or ()),
            jobs_bootstrap_post=self.jobs_bootstrap_post,
            jobs_require_early_career=self.jobs_require_early_career,
            workday_sites=tuple(self.workday_sites or ()),
            workday_poll_seconds=self.workday_poll_seconds,
            workday_timeout_seconds=self.workday_timeout_seconds,
        )

    @property
    def gemini(self) -> GeminiConfig:
        return GeminiConfig(
            llm_enabled=self.llm_enabled,
            llm_cooldown_seconds=self.llm_cooldown_seconds,
            llm_max_messages=self.llm_max_messages,
            llm_max_chars=self.llm_max_chars,
            llm_max_image_bytes=self.llm_max_image_bytes,
            llm_max_output_tokens=self.llm_max_output_tokens,
            llm_debug=self.llm_debug,
            gemini_api_key=self.gemini_api_key,
            gemini_model=self.gemini_model,
            gemini_resume_model=self.gemini_resume_model,
            gemini_resume_prompt=self.gemini_resume_prompt,
            gemini_resume_prompt_hw=self.gemini_resume_prompt_hw,
            gemini_resume_prompt_ele=self.gemini_resume_prompt_ele,
            llm_channel_history_enabled=self.llm_channel_history_enabled,
            llm_lore_enabled=self.llm_lore_enabled,
            llm_images_enabled=self.llm_images_enabled,
            llm_allow_dm=self.llm_allow_dm,
            llm_allowed_channel_ids=self.llm_allowed_channel_ids,
            llm_allowed_role_ids=self.llm_allowed_role_ids,
        )

    @property
    def economy(self) -> EconomyConfig:
        return EconomyConfig(
            daily_claim_amount=self.daily_claim_amount,
            bj_starting_bankroll=self.bj_starting_bankroll,
            bj_dealer_hits_soft_17=self.bj_dealer_hits_soft_17,
            bj_max_splits=self.bj_max_splits,
            bj_session_ttl_minutes=self.bj_session_ttl_minutes,
        )

    @property
    def transit(self) -> TransitConfig:
        return TransitConfig(
            ttc_alerts_channel_id=self.ttc_alerts_channel_id,
            ttc_mastodon_base=self.ttc_mastodon_base,
            ttc_mastodon_acct=self.ttc_mastodon_acct,
            ttc_poll_seconds=self.ttc_poll_seconds,
            ttc_alert_level=self.ttc_alert_level,
            ttc_bootstrap_post_latest=self.ttc_bootstrap_post_latest,
            ttc_include_replies=self.ttc_include_replies,
            ttc_include_reblogs=self.ttc_include_reblogs,
            ttc_debug=self.ttc_debug,
            go_api_base=self.go_api_base,
            go_api_key=self.go_api_key,
            go_alerts_channel_id=self.go_alerts_channel_id,
            go_poll_seconds=self.go_poll_seconds,
            go_alert_level=self.go_alert_level,
            go_bootstrap_post_latest=self.go_bootstrap_post_latest,
            go_include_buses=self.go_include_buses,
            go_lines_allowlist=tuple(self.go_lines_allowlist or ()),
            go_ssl_verify=self.go_ssl_verify,
            go_ca_bundle_path=self.go_ca_bundle_path,
            go_debug=self.go_debug,
        )

    @property
    def financial(self) -> FinancialAlertsConfig:
        return FinancialAlertsConfig(
            market_watch_enabled=self.market_watch_enabled,
            market_watch_poll_seconds=self.market_watch_poll_seconds,
            market_watch_channel_id=self.market_watch_channel_id,
            market_watch_vix_level=self.market_watch_vix_level,
            market_watch_index_move_pct=self.market_watch_index_move_pct,
            market_watch_stock_move_pct=self.market_watch_stock_move_pct,
            market_watch_tickers=tuple(self.market_watch_tickers or ()),
            market_watch_cooldown_minutes=self.market_watch_cooldown_minutes,
            market_watch_debug=self.market_watch_debug,
            market_daily_report_enabled=self.market_daily_report_enabled,
            market_daily_report_channel_id=self.market_daily_report_channel_id,
            market_daily_report_time=self.market_daily_report_time,
            market_daily_report_tz=self.market_daily_report_tz,
            market_daily_report_top_n=self.market_daily_report_top_n,
            market_daily_report_tickers=tuple(self.market_daily_report_tickers or ()),
            market_daily_report_debug=self.market_daily_report_debug,
            btc_watch_enabled=self.btc_watch_enabled,
            btc_watch_poll_seconds=self.btc_watch_poll_seconds,
            btc_watch_channel_id=self.btc_watch_channel_id,
            btc_watch_label=self.btc_watch_label,
            btc_watch_addresses=tuple(self.btc_watch_addresses or ()),
            btc_esplora_base_url=self.btc_esplora_base_url,
            btc_watch_cooldown_minutes=self.btc_watch_cooldown_minutes,
            btc_watch_debug=self.btc_watch_debug,
        )

    @property
    def sports(self) -> SportsConfig:
        return SportsConfig(
            odds_api_key=self.odds_api_key,
            sports_rss_nba=self.sports_rss_nba,
            sports_rss_nfl=self.sports_rss_nfl,
            sports_rss_mma=self.sports_rss_mma,
            sports_rss_ufc=self.sports_rss_ufc,
            sports_rss_boxing=self.sports_rss_boxing,
        )

    @property
    def scheduler(self) -> SchedulerConfig:
        return SchedulerConfig(
            task_startup_jitter_seconds=self.task_startup_jitter_seconds,
            task_max_backoff_seconds=self.task_max_backoff_seconds,
        )

    @classmethod
    def from_components(
        cls,
        *,
        discord: DiscordConfig | None = None,
        storage: StorageConfig | None = None,
        academics: AcademicsConfig | None = None,
        careers: CareersConfig | None = None,
        gemini: GeminiConfig | None = None,
        economy: EconomyConfig | None = None,
        transit: TransitConfig | None = None,
        financial: FinancialAlertsConfig | None = None,
        sports: SportsConfig | None = None,
        scheduler: SchedulerConfig | None = None,
        feature_profile: str = "default",
        enabled_features: Sequence[str] | None = None,
        disabled_features: Sequence[str] | None = None,
    ) -> Settings:
        """Assemble a composite ``Settings`` instance from typed sub-configs."""
        d = discord or DiscordConfig()
        s = storage or StorageConfig()
        a = academics or AcademicsConfig()
        c = careers or CareersConfig()
        g = gemini or GeminiConfig()
        e = economy or EconomyConfig()
        t = transit or TransitConfig()
        f = financial or FinancialAlertsConfig()
        sp = sports or SportsConfig()
        sc = scheduler or SchedulerConfig()
        return cls(
            token=d.token,
            guild_id=d.guild_id,
            guild_id_raw=d.guild_id_raw,
            guild_id_invalid=d.guild_id_invalid,
            stock_channel_id=d.stock_channel_id,
            user_command_channel_id=d.user_command_channel_id,
            allowed_role_ids=list(d.allowed_role_ids),
            embed_logo_url=d.embed_logo_url,
            embed_accent_color=d.embed_accent_color,
            embed_footer_text=d.embed_footer_text,
            bot_display_name=d.bot_display_name,
            stock_exchange_name=d.stock_exchange_name,
            currency_name=d.currency_name,
            currency_symbol=d.currency_symbol,
            primary_stock_symbol=d.primary_stock_symbol,
            primary_stock_name=d.primary_stock_name,
            source_attribution=d.source_attribution,
            theme_preset=d.theme_preset,
            force_guild_resync=d.force_guild_resync,
            allow_gateway=d.allow_gateway,
            task_startup_jitter_seconds=sc.task_startup_jitter_seconds,
            task_max_backoff_seconds=sc.task_max_backoff_seconds,
            odds_api_key=sp.odds_api_key,
            sports_rss_nba=sp.sports_rss_nba,
            sports_rss_nfl=sp.sports_rss_nfl,
            sports_rss_mma=sp.sports_rss_mma,
            sports_rss_ufc=sp.sports_rss_ufc,
            sports_rss_boxing=sp.sports_rss_boxing,
            ttc_alerts_channel_id=t.ttc_alerts_channel_id,
            ttc_mastodon_base=t.ttc_mastodon_base,
            ttc_mastodon_acct=t.ttc_mastodon_acct,
            ttc_poll_seconds=t.ttc_poll_seconds,
            ttc_alert_level=t.ttc_alert_level,
            ttc_bootstrap_post_latest=t.ttc_bootstrap_post_latest,
            ttc_include_replies=t.ttc_include_replies,
            ttc_include_reblogs=t.ttc_include_reblogs,
            ttc_debug=t.ttc_debug,
            go_api_base=t.go_api_base,
            go_api_key=t.go_api_key,
            go_alerts_channel_id=t.go_alerts_channel_id,
            go_poll_seconds=t.go_poll_seconds,
            go_alert_level=t.go_alert_level,
            go_bootstrap_post_latest=t.go_bootstrap_post_latest,
            go_include_buses=t.go_include_buses,
            go_lines_allowlist=list(t.go_lines_allowlist),
            go_ssl_verify=t.go_ssl_verify,
            go_ca_bundle_path=t.go_ca_bundle_path,
            go_debug=t.go_debug,
            exams_tz=a.exams_tz,
            notes_sites=list(a.notes_sites),
            rmp_university=a.rmp_university,
            campus_id=a.campus_id,
            campus_url=a.campus_url,
            academic_course_aliases=list(a.academic_course_aliases),
            academic_allowed_hosts=list(a.academic_allowed_hosts),
            academic_enabled_providers=list(a.academic_enabled_providers),
            exam_reminders_enabled=a.exam_reminders_enabled,
            exam_reminders_channel_id=a.exam_reminders_channel_id,
            exam_reminder_lead_hours=list(a.exam_reminder_lead_hours),
            llm_enabled=g.llm_enabled,
            llm_cooldown_seconds=g.llm_cooldown_seconds,
            llm_max_messages=g.llm_max_messages,
            llm_max_chars=g.llm_max_chars,
            llm_max_image_bytes=g.llm_max_image_bytes,
            llm_max_output_tokens=g.llm_max_output_tokens,
            llm_debug=g.llm_debug,
            gemini_api_key=g.gemini_api_key,
            gemini_model=g.gemini_model,
            gemini_resume_model=g.gemini_resume_model,
            gemini_resume_prompt=g.gemini_resume_prompt,
            gemini_resume_prompt_hw=g.gemini_resume_prompt_hw,
            gemini_resume_prompt_ele=g.gemini_resume_prompt_ele,
            llm_channel_history_enabled=g.llm_channel_history_enabled,
            llm_lore_enabled=g.llm_lore_enabled,
            llm_images_enabled=g.llm_images_enabled,
            llm_allow_dm=g.llm_allow_dm,
            llm_allowed_channel_ids=list(g.llm_allowed_channel_ids)
            if g.llm_allowed_channel_ids is not None
            else None,
            llm_allowed_role_ids=list(g.llm_allowed_role_ids) if g.llm_allowed_role_ids is not None else None,
            job_alerts_channel_id=c.job_alerts_channel_id,
            jobs_poll_seconds=c.jobs_poll_seconds,
            greenhouse_boards=list(c.greenhouse_boards),
            lever_sites=list(c.lever_sites),
            jobs_location_allowlist=list(c.jobs_location_allowlist),
            jobs_keywords_allowlist=list(c.jobs_keywords_allowlist),
            jobs_keywords_denylist=list(c.jobs_keywords_denylist),
            jobs_bootstrap_post=c.jobs_bootstrap_post,
            jobs_require_early_career=c.jobs_require_early_career,
            workday_sites=list(c.workday_sites),
            workday_poll_seconds=c.workday_poll_seconds,
            workday_timeout_seconds=c.workday_timeout_seconds,
            daily_claim_amount=e.daily_claim_amount,
            bj_starting_bankroll=e.bj_starting_bankroll,
            bj_dealer_hits_soft_17=e.bj_dealer_hits_soft_17,
            bj_max_splits=e.bj_max_splits,
            bj_session_ttl_minutes=e.bj_session_ttl_minutes,
            market_watch_enabled=f.market_watch_enabled,
            market_watch_poll_seconds=f.market_watch_poll_seconds,
            market_watch_channel_id=f.market_watch_channel_id,
            market_watch_vix_level=f.market_watch_vix_level,
            market_watch_index_move_pct=f.market_watch_index_move_pct,
            market_watch_stock_move_pct=f.market_watch_stock_move_pct,
            market_watch_tickers=list(f.market_watch_tickers),
            market_watch_cooldown_minutes=f.market_watch_cooldown_minutes,
            market_watch_debug=f.market_watch_debug,
            market_daily_report_enabled=f.market_daily_report_enabled,
            market_daily_report_channel_id=f.market_daily_report_channel_id,
            market_daily_report_time=f.market_daily_report_time,
            market_daily_report_tz=f.market_daily_report_tz,
            market_daily_report_top_n=f.market_daily_report_top_n,
            market_daily_report_tickers=list(f.market_daily_report_tickers),
            market_daily_report_debug=f.market_daily_report_debug,
            btc_watch_enabled=f.btc_watch_enabled,
            btc_watch_poll_seconds=f.btc_watch_poll_seconds,
            btc_watch_channel_id=f.btc_watch_channel_id,
            btc_watch_label=f.btc_watch_label,
            btc_watch_addresses=list(f.btc_watch_addresses),
            btc_esplora_base_url=f.btc_esplora_base_url,
            btc_watch_cooldown_minutes=f.btc_watch_cooldown_minutes,
            btc_watch_debug=f.btc_watch_debug,
            db_path=s.db_path,
            grades_db_path=s.grades_db_path,
            lore_path=s.lore_path,
            feature_profile=feature_profile,
            enabled_features=list(enabled_features) if enabled_features is not None else None,
            disabled_features=list(disabled_features) if disabled_features is not None else None,
        )

    @property
    def effective_workday_poll_seconds(self) -> int:
        return self.workday_poll_seconds if self.workday_poll_seconds > 0 else self.jobs_poll_seconds

    def secret_values(self) -> tuple[str, ...]:
        """Return configured secret strings for centralized log/error redaction."""
        vals = (self.token, self.gemini_api_key, self.go_api_key, self.odds_api_key)
        return tuple(v.strip() for v in vals if isinstance(v, str) and v.strip())

    def sports_rss_urls(self, sport: str) -> list[str]:
        """Return configured RSS feed URLs for ``sport`` (or empty list to use built-in defaults)."""
        return self.sports.rss_urls_for(sport)

    # ---- Compatibility aliases (deprecated callers) ----

    @property
    def greenhouse_board_tokens(self) -> list[str]:
        warnings.warn(
            "Settings.greenhouse_board_tokens is deprecated; use Settings.greenhouse_boards.",
            DeprecationWarning,
            stacklevel=2,
        )
        return list(self.greenhouse_boards or [])

    @property
    def lever_site_tokens(self) -> list[str]:
        warnings.warn(
            "Settings.lever_site_tokens is deprecated; use Settings.lever_sites.",
            DeprecationWarning,
            stacklevel=2,
        )
        return list(self.lever_sites or [])

    @property
    def jobs_channel_id(self) -> int:
        warnings.warn(
            "Settings.jobs_channel_id is deprecated; use Settings.job_alerts_channel_id.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.job_alerts_channel_id

    def capabilities(self) -> dict[str, CapabilityStatus]:
        """Return per-feature readiness without exposing secret values."""
        has_token = bool((self.token or "").strip())
        has_gemini = bool((self.gemini_api_key or "").strip())
        has_go_key = bool((self.go_api_key or "").strip())
        has_odds = bool((self.odds_api_key or "").strip())

        go_chan = int(self.go_alerts_channel_id or 0) or int(self.ttc_alerts_channel_id or 0)
        mw_chan = int(self.market_watch_channel_id or 0) or int(self.stock_channel_id or 0)
        dr_chan = (
            int(self.market_daily_report_channel_id or 0)
            or int(self.market_watch_channel_id or 0)
            or int(self.stock_channel_id or 0)
        )
        btc_chan = (
            int(self.btc_watch_channel_id or 0)
            or int(self.market_watch_channel_id or 0)
            or int(self.stock_channel_id or 0)
        )
        job_sources = bool(self.greenhouse_boards or self.lever_sites or self.workday_sites)

        return {
            "discord_gateway": CapabilityStatus(
                name="discord_gateway",
                configured=has_token,
                available=bool(has_token and self.allow_gateway),
                reason=(
                    ""
                    if (has_token and self.allow_gateway)
                    else ("gateway opt-in disabled" if has_token else "DISCORD_TOKEN not configured")
                ),
            ),
            "llm_assistant": CapabilityStatus(
                name="llm_assistant",
                configured=self.llm_enabled,
                available=bool(self.llm_enabled and has_gemini),
                reason=(
                    ""
                    if (not self.llm_enabled or has_gemini)
                    else "LLM_ENABLED is true without GEMINI_API_KEY"
                ),
            ),
            "resume_review": CapabilityStatus(
                name="resume_review",
                configured=has_gemini,
                available=has_gemini,
                reason="" if has_gemini else "GEMINI_API_KEY not configured",
            ),
            "odds_api": CapabilityStatus(
                name="odds_api",
                configured=has_odds,
                available=has_odds,
                reason="" if has_odds else "ODDS_API_KEY not configured (RSS headlines remain active)",
            ),
            "ttc_alerts": CapabilityStatus(
                name="ttc_alerts",
                configured=bool(self.ttc_alerts_channel_id),
                available=bool(
                    self.ttc_alerts_channel_id and self.ttc_mastodon_base and self.ttc_mastodon_acct
                ),
                reason=(
                    ""
                    if not self.ttc_alerts_channel_id or (self.ttc_mastodon_base and self.ttc_mastodon_acct)
                    else "Missing TTC_MASTODON_BASE or TTC_MASTODON_ACCT"
                ),
            ),
            "go_alerts": CapabilityStatus(
                name="go_alerts",
                configured=bool(go_chan),
                available=bool(go_chan and has_go_key),
                reason="" if not go_chan or has_go_key else "GO_API_KEY not configured",
            ),
            "job_alerts": CapabilityStatus(
                name="job_alerts",
                configured=bool(self.job_alerts_channel_id),
                available=bool(self.job_alerts_channel_id and job_sources),
                reason="" if not self.job_alerts_channel_id or job_sources else "No job boards configured",
            ),
            "market_watch": CapabilityStatus(
                name="market_watch",
                configured=self.market_watch_enabled,
                available=bool(self.market_watch_enabled and mw_chan),
                reason="" if not self.market_watch_enabled or mw_chan else "Missing alert channel ID",
            ),
            "market_daily_report": CapabilityStatus(
                name="market_daily_report",
                configured=self.market_daily_report_enabled,
                available=bool(self.market_daily_report_enabled and dr_chan),
                reason="" if not self.market_daily_report_enabled or dr_chan else "Missing report channel ID",
            ),
            "btc_watch": CapabilityStatus(
                name="btc_watch",
                configured=self.btc_watch_enabled,
                available=bool(self.btc_watch_enabled and btc_chan and self.btc_watch_addresses),
                reason=(
                    ""
                    if not self.btc_watch_enabled or (btc_chan and self.btc_watch_addresses)
                    else "Missing BTC watch channel ID or addresses"
                ),
            ),
        }

    def validate(
        self,
        *,
        strict: bool = False,
        extra_issues: Sequence[ValidationIssue] = (),
    ) -> ConfigValidationReport:
        """Validate schema, ranges, formats, URLs, and integration readiness."""
        issues: list[ValidationIssue] = list(extra_issues)

        def _err(field: str, msg: str) -> None:
            issues.append(ValidationIssue(field=field, message=msg, severity="error"))

        def _warn(field: str, msg: str) -> None:
            issues.append(ValidationIssue(field=field, message=msg, severity="warning"))

        # Snowflakes
        if self.guild_id_invalid:
            _err("guild_id", f"GUILD_ID exceeds MAX_SNOWFLAKE ({MAX_SNOWFLAKE}).")

        snowflake_fields = (
            ("guild_id", self.guild_id),
            ("stock_channel_id", self.stock_channel_id),
            ("user_command_channel_id", self.user_command_channel_id),
            ("ttc_alerts_channel_id", self.ttc_alerts_channel_id),
            ("job_alerts_channel_id", self.job_alerts_channel_id),
            ("go_alerts_channel_id", self.go_alerts_channel_id),
            ("market_watch_channel_id", self.market_watch_channel_id),
            ("market_daily_report_channel_id", self.market_daily_report_channel_id),
            ("btc_watch_channel_id", self.btc_watch_channel_id),
            ("exam_reminders_channel_id", self.exam_reminders_channel_id),
        )
        for fname, val in snowflake_fields:
            if not _is_valid_snowflake(val, allow_zero=True):
                _err(fname, f"{fname} must be an integer between 0 and {MAX_SNOWFLAKE}.")

        for lead_h in self.exam_reminder_lead_hours or ():
            if isinstance(lead_h, bool) or not isinstance(lead_h, int) or lead_h < 1 or lead_h > 336:
                _err(
                    "exam_reminder_lead_hours",
                    "exam_reminder_lead_hours must contain integers between 1 and 336.",
                )

        for rid in self.allowed_role_ids or ():
            if not _is_valid_snowflake(rid, allow_zero=False):
                _err("allowed_role_ids", f"Role ID must be in range 1..{MAX_SNOWFLAKE}.")

        # Color
        if (
            isinstance(self.embed_accent_color, bool)
            or not isinstance(self.embed_accent_color, int)
            or not (0 <= self.embed_accent_color <= 0xFFFFFF)
        ):
            _err(
                "embed_accent_color", "embed_accent_color must be a 24-bit RGB integer (0x000000..0xFFFFFF)."
            )
        elif not is_accessible_accent(self.embed_accent_color):
            _warn(
                "embed_accent_color",
                f"Accent color 0x{self.embed_accent_color:06X} has extreme relative luminance; may have poor readability on Discord dark/light themes.",
            )

        # URLs
        if self.embed_logo_url and not _is_valid_http_url(self.embed_logo_url):
            _err("embed_logo_url", "embed_logo_url must be a valid http:// or https:// URL.")
        if self.ttc_mastodon_base and not _is_valid_http_url(self.ttc_mastodon_base):
            _err("ttc_mastodon_base", "ttc_mastodon_base must be a valid http:// or https:// URL.")
        if self.go_api_base and not _is_valid_http_url(self.go_api_base):
            _err("go_api_base", "go_api_base must be a valid http:// or https:// URL.")
        if not self.go_api_base and self.go_alerts_channel_id and self.go_api_key:
            _err("go_api_base", "go_api_base cannot be empty when GO alerts are enabled.")
        if self.btc_esplora_base_url and not _is_valid_http_url(self.btc_esplora_base_url):
            _err("btc_esplora_base_url", "btc_esplora_base_url must be a valid http:// or https:// URL.")
        if self.campus_url and not _is_valid_http_url(self.campus_url):
            _err("campus_url", "campus_url must be a valid http:// or https:// URL.")

        for sport_field in (
            "sports_rss_nba",
            "sports_rss_nfl",
            "sports_rss_mma",
            "sports_rss_ufc",
            "sports_rss_boxing",
        ):
            raw_urls = _to_str_list(getattr(self, sport_field, ""))
            for u in raw_urls:
                if not _is_valid_http_url(u):
                    _err(sport_field, f"{sport_field} contains invalid URL: {u!r}")

        for site in self.notes_sites or ():
            parts = str(site).split("|", 1)
            if len(parts) != 2 or not parts[0].strip():
                _err("notes_sites", "notes_sites entries must use 'Title|https://...' format.")
            else:
                site_url = parts[1].strip().strip('"').strip("'")
                if not _is_valid_http_url(site_url):
                    _err("notes_sites", f"notes_sites entry for {parts[0].strip()!r} has invalid URL.")

        for wd in self.workday_sites or ():
            parts = [p.strip() for p in str(wd).split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                _err("workday_sites", "workday_sites entries must use 'Label|URL' format.")
            else:
                cand = parts[1]
                if "://" in cand and not _is_valid_http_url(cand):
                    _err("workday_sites", f"workday_sites entry {parts[0]!r} has invalid careers URL.")
                if len(parts) >= 3 and parts[2] and "://" in parts[2] and not _is_valid_http_url(parts[2]):
                    _err("workday_sites", f"workday_sites entry {parts[0]!r} has invalid API override URL.")

        # Enums
        if (self.ttc_alert_level or "").strip().lower() not in _VALID_ALERT_LEVELS:
            _err("ttc_alert_level", "ttc_alert_level must be 'major' or 'all'.")
        if (self.go_alert_level or "").strip().lower() not in _VALID_ALERT_LEVELS:
            _err("go_alert_level", "go_alert_level must be 'major' or 'all'.")

        # Timezones & HH:MM
        if not _is_valid_tz(self.exams_tz):
            _err("exams_tz", f"Unknown IANA timezone for exams_tz: {self.exams_tz!r}")
        if not _is_valid_tz(self.market_daily_report_tz):
            _err(
                "market_daily_report_tz",
                f"Unknown IANA timezone for market_daily_report_tz: {self.market_daily_report_tz!r}",
            )
        if not _is_valid_hhmm(self.market_daily_report_time):
            _err(
                "market_daily_report_time",
                f"market_daily_report_time must use 24-hour HH:MM format, got {self.market_daily_report_time!r}",
            )

        # Storage paths
        raw_db_path = (self.db_path or "").strip()
        if not raw_db_path:
            _err("db_path", "db_path must not be empty.")
        else:
            try:
                norm_db = normalize_sqlite_path(raw_db_path, allow_empty=False)
                if not _is_memory_sqlite_path(norm_db) and os.path.isdir(norm_db):
                    _err("db_path", f"db_path points to an existing directory: {raw_db_path!r}")
            except ConfigValidationError as exc:
                _err("db_path", str(exc))

        raw_grades_path = (self.grades_db_path or "").strip()
        if raw_grades_path:
            try:
                norm_grades = normalize_sqlite_path(raw_grades_path, allow_empty=True)
                if norm_grades and not _is_memory_sqlite_path(norm_grades) and os.path.isdir(norm_grades):
                    _err(
                        "grades_db_path",
                        f"grades_db_path points to an existing directory: {raw_grades_path!r}",
                    )
            except ConfigValidationError as exc:
                _err("grades_db_path", str(exc))

        # Bounds
        for fname, bounds in POLL_INTERVAL_BOUNDS.items():
            val = getattr(self, fname)
            if not bounds.is_valid(val):
                zero_note = "0 (disabled) or " if bounds.allow_zero else ""
                _err(
                    fname,
                    (
                        f"{fname} must be {zero_note}between {bounds.min_seconds} and "
                        f"{bounds.max_seconds} seconds (got {val})."
                    ),
                )

        if (
            not math.isfinite(self.task_startup_jitter_seconds)
            or self.task_startup_jitter_seconds < 0.0
            or self.task_startup_jitter_seconds > 300.0
        ):
            _err(
                "task_startup_jitter_seconds",
                "task_startup_jitter_seconds must be between 0 and 300 seconds.",
            )
        if (
            not math.isfinite(self.task_max_backoff_seconds)
            or self.task_max_backoff_seconds < 30.0
            or self.task_max_backoff_seconds > 86400.0
        ):
            _err(
                "task_max_backoff_seconds",
                "task_max_backoff_seconds must be between 30 and 86400 seconds.",
            )

        if self.workday_timeout_seconds < 1:
            _err("workday_timeout_seconds", "workday_timeout_seconds must be >= 1.")
        if self.llm_cooldown_seconds < 0:
            _err("llm_cooldown_seconds", "llm_cooldown_seconds must be >= 0.")
        if self.llm_max_messages < 1:
            _err("llm_max_messages", "llm_max_messages must be >= 1.")
        if self.llm_max_chars < 100:
            _err("llm_max_chars", "llm_max_chars must be >= 100.")
        if self.llm_max_image_bytes < 0:
            _err("llm_max_image_bytes", "llm_max_image_bytes must be >= 0.")
        if self.llm_max_output_tokens < 1:
            _err("llm_max_output_tokens", "llm_max_output_tokens must be >= 1.")
        if self.daily_claim_amount < 0:
            _err("daily_claim_amount", "daily_claim_amount must be >= 0.")
        if self.bj_starting_bankroll < 1:
            _err("bj_starting_bankroll", "bj_starting_bankroll must be >= 1.")
        if self.bj_max_splits < 0:
            _err("bj_max_splits", "bj_max_splits must be >= 0.")
        if self.bj_session_ttl_minutes < 1:
            _err("bj_session_ttl_minutes", "bj_session_ttl_minutes must be >= 1.")
        if not math.isfinite(self.market_watch_vix_level) or self.market_watch_vix_level <= 0:
            _err("market_watch_vix_level", "market_watch_vix_level must be > 0.")
        if not math.isfinite(self.market_watch_index_move_pct) or self.market_watch_index_move_pct <= 0:
            _err("market_watch_index_move_pct", "market_watch_index_move_pct must be > 0.")
        if not math.isfinite(self.market_watch_stock_move_pct) or self.market_watch_stock_move_pct <= 0:
            _err("market_watch_stock_move_pct", "market_watch_stock_move_pct must be > 0.")
        if self.market_watch_cooldown_minutes < 0:
            _err("market_watch_cooldown_minutes", "market_watch_cooldown_minutes must be >= 0.")
        if self.market_daily_report_top_n < 1:
            _err("market_daily_report_top_n", "market_daily_report_top_n must be >= 1.")
        if self.btc_watch_cooldown_minutes < 0:
            _err("btc_watch_cooldown_minutes", "btc_watch_cooldown_minutes must be >= 0.")

        # Logging & observability validations
        if self.log_max_bytes <= 0:
            _err("log_max_bytes", "log_max_bytes must be > 0.")
        if self.log_backup_count < 0:
            _err("log_backup_count", "log_backup_count must be >= 0.")

        # Feature profile & module validations
        if self.theme_preset and self.theme_preset.strip().lower() not in KNOWN_THEME_NAMES:
            _warn(
                "theme_preset",
                f"Unknown theme_preset {self.theme_preset!r}; known presets are {', '.join(sorted(KNOWN_THEME_NAMES))}.",
            )
        if self.feature_profile and self.feature_profile.strip().lower() not in KNOWN_FEATURE_PROFILES:
            _warn(
                "feature_profile",
                f"Unknown feature_profile {self.feature_profile!r}; known presets are {', '.join(sorted(KNOWN_FEATURE_PROFILES))}.",
            )
        for mod in self.enabled_features or ():
            if mod not in KNOWN_MODULE_IDS:
                _warn(
                    "enabled_features",
                    f"Unknown module_id {mod!r} in enabled_features; known modules are {', '.join(sorted(KNOWN_MODULE_IDS))}.",
                )
        for mod in self.disabled_features or ():
            if mod not in KNOWN_MODULE_IDS:
                _warn(
                    "disabled_features",
                    f"Unknown module_id {mod!r} in disabled_features; known modules are {', '.join(sorted(KNOWN_MODULE_IDS))}.",
                )

        # Enabled integration prerequisite warnings (non-fatal degradation)
        caps = self.capabilities()
        for cap_name in (
            "llm_assistant",
            "ttc_alerts",
            "go_alerts",
            "market_watch",
            "market_daily_report",
            "btc_watch",
        ):
            cap = caps[cap_name]
            if cap.configured and not cap.available and cap.reason:
                _warn(cap_name, cap.reason)

        report = ConfigValidationReport(issues=tuple(issues), capabilities=caps)
        if strict and not report.is_valid:
            secrets = self.secret_values()
            details = "; ".join(f"{e.field}: {redact_secrets(e.message, secrets)}" for e in report.errors)
            raise ConfigValidationError(f"Invalid configuration ({details})")
        return report

    @classmethod
    def from_env(
        cls,
        *,
        strict: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Load settings from environment variables.

        When ``strict=False`` (default), preserves legacy tolerant parsing/clamping
        so existing dev setups and unit tests degrade safely. When ``strict=True``,
        raises ``ConfigValidationError`` on malformed env values or constraint failures.
        """
        env = os.environ if environ is None else environ
        raw_issues: list[ValidationIssue] = []

        def _env(key: str, default: str = "") -> str:
            v = env.get(key)
            return default if v is None else str(v)

        def _parse_int_field(env_key: str, field: str, default: int, *, snowflake: bool = False) -> int:
            raw = _env(env_key, "").split("#")[0].strip()
            if not raw:
                return default
            try:
                n = int(raw)
            except (TypeError, ValueError):
                raw_issues.append(ValidationIssue(field, f"{env_key} must be an integer.", "error"))
                return default
            if snowflake and (n < 0 or n > MAX_SNOWFLAKE):
                raw_issues.append(
                    ValidationIssue(field, f"{env_key} must be between 0 and {MAX_SNOWFLAKE}.", "error")
                )
                return 0
            return n

        def _parse_float_field(env_key: str, field: str, default: float) -> float:
            raw = _env(env_key, "").split("#")[0].strip()
            if not raw:
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                raw_issues.append(ValidationIssue(field, f"{env_key} must be numeric.", "error"))
                return default

        def _parse_bool_field(env_key: str, field: str, default: bool) -> bool:
            raw = _env(env_key, "").split("#")[0].strip().lower()
            if not raw:
                return default
            if raw in _TRUE_VALUES:
                return True
            if raw in _FALSE_VALUES:
                return False
            raw_issues.append(ValidationIssue(field, f"{env_key} must be a boolean.", "error"))
            return default

        # Legacy environment variable warnings (never expose values!)
        if _env("ALPHAVANTAGE_API_KEY", "").strip():
            warnings.warn(
                "ALPHAVANTAGE_API_KEY is deprecated and unused; market quotes use Yahoo Finance.",
                DeprecationWarning,
                stacklevel=2,
            )

        token = _env("DISCORD_TOKEN", "").strip()

        guild_raw = _env("GUILD_ID", "").strip()
        guild_id = 0
        guild_id_invalid = False
        if guild_raw:
            try:
                parsed_gid = int(guild_raw)
            except (TypeError, ValueError):
                raw_issues.append(ValidationIssue("guild_id", "GUILD_ID must be an integer.", "error"))
            else:
                if parsed_gid < 0:
                    raw_issues.append(
                        ValidationIssue(
                            "guild_id", f"GUILD_ID must be between 0 and {MAX_SNOWFLAKE}.", "error"
                        )
                    )
                elif parsed_gid > MAX_SNOWFLAKE:
                    guild_id_invalid = True
                    raw_issues.append(
                        ValidationIssue(
                            "guild_id", f"GUILD_ID must be between 0 and {MAX_SNOWFLAKE}.", "error"
                        )
                    )
                else:
                    guild_id = parsed_gid

        stock_channel_id = _parse_int_field("STOCK_CHANNEL_ID", "stock_channel_id", 0, snowflake=True)
        user_command_channel_id = _parse_int_field(
            "USER_COMMAND_CHANNEL_ID", "user_command_channel_id", 0, snowflake=True
        )

        ttc_env_raw = _env("TTC_ALERTS_CHANNEL_ID", "").strip()
        legacy_ttc_raw = _env("TTC_CHANNEL_ID", "").strip()
        if ttc_env_raw:
            ttc_alerts_channel_id = _parse_int_field(
                "TTC_ALERTS_CHANNEL_ID", "ttc_alerts_channel_id", 0, snowflake=True
            )
        elif legacy_ttc_raw:
            warnings.warn(
                "TTC_CHANNEL_ID is deprecated; use TTC_ALERTS_CHANNEL_ID.",
                DeprecationWarning,
                stacklevel=2,
            )
            ttc_alerts_channel_id = _parse_int_field(
                "TTC_CHANNEL_ID", "ttc_alerts_channel_id", 0, snowflake=True
            )
        else:
            ttc_alerts_channel_id = 0

        job_env_raw = _env("JOB_ALERTS_CHANNEL_ID", "").strip()
        legacy_jobs_raw = _env("JOBS_CHANNEL_ID", "").strip()
        if job_env_raw:
            job_alerts_channel_id = _parse_int_field(
                "JOB_ALERTS_CHANNEL_ID", "job_alerts_channel_id", 0, snowflake=True
            )
        elif legacy_jobs_raw:
            warnings.warn(
                "JOBS_CHANNEL_ID is deprecated; use JOB_ALERTS_CHANNEL_ID.",
                DeprecationWarning,
                stacklevel=2,
            )
            job_alerts_channel_id = _parse_int_field(
                "JOBS_CHANNEL_ID", "job_alerts_channel_id", 0, snowflake=True
            )
        else:
            job_alerts_channel_id = 0

        allowed_roles = _to_str_list(_env("ALLOWED_ROLE_IDS", ""))
        allowed_role_ids: list[int] = []
        for r in allowed_roles:
            try:
                n = int(r)
            except ValueError:
                raw_issues.append(
                    ValidationIssue("allowed_role_ids", "ALLOWED_ROLE_IDS must contain integer IDs.", "error")
                )
                continue
            if n <= 0 or n > MAX_SNOWFLAKE:
                raw_issues.append(
                    ValidationIssue(
                        "allowed_role_ids",
                        f"ALLOWED_ROLE_IDS contains out-of-range snowflake (1..{MAX_SNOWFLAKE}).",
                        "error",
                    )
                )
                continue
            allowed_role_ids.append(n)

        theme_preset = _env("THEME_PRESET", DEFAULT_THEME_NAME).strip().lower() or DEFAULT_THEME_NAME
        theme_defaults = get_theme_preset(theme_preset)

        embed_logo_url = _env("EMBED_LOGO_URL", theme_defaults.logo_url).strip()
        raw_color = _env("EMBED_ACCENT_COLOR", "").strip()
        if raw_color and _to_color(raw_color, -1) == -1:
            raw_issues.append(
                ValidationIssue(
                    "embed_accent_color",
                    "EMBED_ACCENT_COLOR must be '#RRGGBB', '0xRRGGBB', 'RRGGBB', or decimal int.",
                    "error",
                )
            )
        embed_accent_color = _to_color(raw_color, theme_defaults.accent_color)
        embed_footer_text = (
            _env("EMBED_FOOTER_TEXT", theme_defaults.footer_text).strip() or theme_defaults.footer_text
        )

        bot_display_name = (
            _env("BOT_DISPLAY_NAME", theme_defaults.display_name).strip() or theme_defaults.display_name
        )
        stock_exchange_name = (
            _env("STOCK_EXCHANGE_NAME", theme_defaults.stock_exchange_name).strip()
            or theme_defaults.stock_exchange_name
        )
        currency_name = (
            _env("CURRENCY_NAME", theme_defaults.currency_name).strip() or theme_defaults.currency_name
        )
        currency_symbol = (
            _env("CURRENCY_SYMBOL", theme_defaults.currency_symbol).strip() or theme_defaults.currency_symbol
        )
        primary_stock_symbol = (
            _env("PRIMARY_STOCK_SYMBOL", theme_defaults.primary_stock_symbol).strip()
            or theme_defaults.primary_stock_symbol
        )
        primary_stock_name = (
            _env("PRIMARY_STOCK_NAME", theme_defaults.primary_stock_name).strip()
            or theme_defaults.primary_stock_name
        )
        source_attribution = (
            _env("SOURCE_ATTRIBUTION", theme_defaults.source_attribution).strip()
            or theme_defaults.source_attribution
        )

        odds_api_key = _env("ODDS_API_KEY", "").strip()
        sports_rss_nba = _env("SPORTS_RSS_NBA", "").strip()
        sports_rss_nfl = _env("SPORTS_RSS_NFL", "").strip()
        sports_rss_mma = _env("SPORTS_RSS_MMA", "").strip()
        sports_rss_ufc = _env("SPORTS_RSS_UFC", "").strip()
        sports_rss_boxing = _env("SPORTS_RSS_BOXING", "").strip()

        ttc_mastodon_base = _env("TTC_MASTODON_BASE", "").strip()
        ttc_mastodon_acct = _env("TTC_MASTODON_ACCT", "").strip()
        ttc_poll_seconds = _parse_int_field("TTC_POLL_SECONDS", "ttc_poll_seconds", 120)
        ttc_alert_level = _env("TTC_ALERT_LEVEL", "major").split("#")[0].strip().lower() or "major"
        ttc_bootstrap_post_latest = _parse_bool_field(
            "TTC_BOOTSTRAP_POST_LATEST", "ttc_bootstrap_post_latest", False
        )
        ttc_include_replies = _parse_bool_field("TTC_INCLUDE_REPLIES", "ttc_include_replies", True)
        ttc_include_reblogs = _parse_bool_field("TTC_INCLUDE_REBLOGS", "ttc_include_reblogs", False)
        ttc_debug = _parse_bool_field("TTC_DEBUG", "ttc_debug", False)

        go_api_base = (
            _env("GO_API_BASE", "https://api.openmetrolinx.com/OpenDataAPI/api/V1").strip()
            or "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
        )
        go_api_key = _env("GO_API_KEY", "").strip()
        go_alerts_channel_id = _parse_int_field(
            "GO_ALERTS_CHANNEL_ID", "go_alerts_channel_id", 0, snowflake=True
        )
        go_poll_seconds = _parse_int_field("GO_POLL_SECONDS", "go_poll_seconds", 180)
        go_alert_level = _env("GO_ALERT_LEVEL", "major").split("#")[0].strip().lower() or "major"
        go_bootstrap_post_latest = _parse_bool_field(
            "GO_BOOTSTRAP_POST_LATEST", "go_bootstrap_post_latest", True
        )
        go_include_buses = _parse_bool_field("GO_INCLUDE_BUSES", "go_include_buses", False)
        go_lines_allowlist = _to_str_list(_env("GO_LINES_ALLOWLIST", ""))
        go_ssl_verify = _parse_bool_field("GO_SSL_VERIFY", "go_ssl_verify", True)
        go_ca_bundle_path = _env("GO_CA_BUNDLE_PATH", "").strip()
        go_debug = _parse_bool_field("GO_DEBUG", "go_debug", False)

        # LLM mention assistant (Gemini)
        llm_enabled = _parse_bool_field("LLM_ENABLED", "llm_enabled", False)
        llm_cooldown_seconds = _parse_int_field("LLM_COOLDOWN_SECONDS", "llm_cooldown_seconds", 3)
        llm_max_messages = _parse_int_field("LLM_MAX_MESSAGES", "llm_max_messages", 25)
        llm_max_chars = _parse_int_field("LLM_MAX_CHARS", "llm_max_chars", 12000)
        llm_max_image_bytes = _parse_int_field("LLM_MAX_IMAGE_BYTES", "llm_max_image_bytes", 2_000_000)
        llm_max_output_tokens = _parse_int_field("LLM_MAX_OUTPUT_TOKENS", "llm_max_output_tokens", 1536)
        llm_debug = _parse_bool_field("LLM_DEBUG", "llm_debug", False)
        gemini_api_key = _env("GEMINI_API_KEY", "").strip()

        gemini_model_env = _env("GEMINI_MODEL", "").strip()
        legacy_llm_model = _env("LLM_MODEL", "").strip()
        if gemini_model_env:
            gemini_model = gemini_model_env
        elif legacy_llm_model:
            warnings.warn(
                "LLM_MODEL is deprecated; use GEMINI_MODEL.",
                DeprecationWarning,
                stacklevel=2,
            )
            gemini_model = legacy_llm_model
        else:
            gemini_model = "gemini-2.5-flash"

        gemini_resume_model = _env("GEMINI_RESUME_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
        gemini_resume_prompt = _env("GEMINI_RESUME_PROMPT", "").strip()
        gemini_resume_prompt_hw = _env("GEMINI_RESUME_PROMPT_HW", "").strip()
        gemini_resume_prompt_ele = _env("GEMINI_RESUME_PROMPT_ELE", "").strip()
        llm_channel_history_enabled = _parse_bool_field(
            "LLM_CHANNEL_HISTORY_ENABLED", "llm_channel_history_enabled", False
        )
        llm_lore_enabled = _parse_bool_field("LLM_LORE_ENABLED", "llm_lore_enabled", False)
        llm_images_enabled = _parse_bool_field("LLM_IMAGES_ENABLED", "llm_images_enabled", True)
        llm_allow_dm = _parse_bool_field("LLM_ALLOW_DM", "llm_allow_dm", False)

        llm_allowed_channels_raw = _to_str_list(_env("LLM_ALLOWED_CHANNEL_IDS", ""))
        llm_allowed_channel_ids: list[int] | None = None
        if llm_allowed_channels_raw:
            llm_allowed_channel_ids = []
            for ch in llm_allowed_channels_raw:
                try:
                    c_id = int(ch)
                    if 0 < c_id <= MAX_SNOWFLAKE:
                        llm_allowed_channel_ids.append(c_id)
                except ValueError:
                    continue

        llm_allowed_roles_raw = _to_str_list(_env("LLM_ALLOWED_ROLE_IDS", ""))
        llm_allowed_role_ids: list[int] | None = None
        if llm_allowed_roles_raw:
            llm_allowed_role_ids = []
            for r in llm_allowed_roles_raw:
                try:
                    r_id = int(r)
                    if 0 < r_id <= MAX_SNOWFLAKE:
                        llm_allowed_role_ids.append(r_id)
                except ValueError:
                    continue

        jobs_poll_seconds = _parse_int_field("JOBS_POLL_SECONDS", "jobs_poll_seconds", 600)
        greenhouse_boards = _to_str_list(_env("GREENHOUSE_BOARDS", ""))
        lever_sites = _to_str_list(_env("LEVER_SITES", ""))
        jobs_location_allowlist = _to_str_list(_env("JOBS_LOCATION_ALLOWLIST", ""))
        jobs_keywords_allowlist = _to_str_list(_env("JOBS_KEYWORDS_ALLOWLIST", ""))
        jobs_keywords_denylist = _to_str_list(_env("JOBS_KEYWORDS_DENYLIST", ""))
        jobs_bootstrap_post = _parse_bool_field("JOBS_BOOTSTRAP_POST", "jobs_bootstrap_post", False)
        jobs_require_early_career = _parse_bool_field(
            "JOBS_REQUIRE_EARLY_CAREER", "jobs_require_early_career", False
        )

        # Workday
        workday_sites = _to_str_list(_env("WORKDAY_SITES", ""))
        workday_poll_seconds = _parse_int_field("WORKDAY_POLL_SECONDS", "workday_poll_seconds", 0)
        workday_timeout_seconds = _parse_int_field("WORKDAY_TIMEOUT_SECONDS", "workday_timeout_seconds", 12)

        force_guild_resync = _parse_bool_field("FORCE_GUILD_RESYNC", "force_guild_resync", False)
        allow_gateway = _parse_bool_field("ICBOT_ALLOW_GATEWAY", "allow_gateway", False)
        task_startup_jitter_seconds = _parse_float_field(
            "TASK_STARTUP_JITTER_SECONDS", "task_startup_jitter_seconds", 15.0
        )
        task_max_backoff_seconds = _parse_float_field(
            "TASK_MAX_BACKOFF_SECONDS", "task_max_backoff_seconds", 1800.0
        )

        daily_claim_amount = _parse_int_field("DAILY_CLAIM_AMOUNT", "daily_claim_amount", 100)

        bj_starting_bankroll = _parse_int_field("BJ_STARTING_BANKROLL", "bj_starting_bankroll", 1000)
        bj_dealer_hits_soft_17 = _parse_bool_field("BJ_DEALER_HITS_SOFT_17", "bj_dealer_hits_soft_17", True)
        bj_max_splits = _parse_int_field("BJ_MAX_SPLITS", "bj_max_splits", 1)
        bj_session_ttl_minutes = _parse_int_field("BJ_SESSION_TTL_MINUTES", "bj_session_ttl_minutes", 20)

        market_watch_enabled = _parse_bool_field("MARKET_WATCH_ENABLED", "market_watch_enabled", False)
        market_watch_poll_seconds = _parse_int_field(
            "MARKET_WATCH_POLL_SECONDS", "market_watch_poll_seconds", 60
        )
        market_watch_channel_id = _parse_int_field(
            "MARKET_WATCH_CHANNEL_ID", "market_watch_channel_id", 0, snowflake=True
        )
        market_watch_vix_level = _parse_float_field("MARKET_WATCH_VIX_LEVEL", "market_watch_vix_level", 20.0)
        market_watch_index_move_pct = _parse_float_field(
            "MARKET_WATCH_INDEX_MOVE_PCT", "market_watch_index_move_pct", 2.5
        )
        market_watch_stock_move_pct = _parse_float_field(
            "MARKET_WATCH_STOCK_MOVE_PCT", "market_watch_stock_move_pct", 10.0
        )
        market_watch_tickers = _to_str_list(_env("MARKET_WATCH_TICKERS", ""))
        market_watch_cooldown_minutes = _parse_int_field(
            "MARKET_WATCH_COOLDOWN_MINUTES", "market_watch_cooldown_minutes", 30
        )
        market_watch_debug = _parse_bool_field("MARKET_WATCH_DEBUG", "market_watch_debug", False)

        # Daily market summary
        market_daily_report_enabled = _parse_bool_field(
            "MARKET_DAILY_REPORT_ENABLED", "market_daily_report_enabled", False
        )
        market_daily_report_channel_id = _parse_int_field(
            "MARKET_DAILY_REPORT_CHANNEL_ID", "market_daily_report_channel_id", 0, snowflake=True
        )
        market_daily_report_time = _env("MARKET_DAILY_REPORT_TIME", "16:30").strip() or "16:30"
        market_daily_report_tz = (
            _env("MARKET_DAILY_REPORT_TZ", "America/Toronto").strip() or "America/Toronto"
        )
        market_daily_report_top_n = _parse_int_field(
            "MARKET_DAILY_REPORT_TOP_N", "market_daily_report_top_n", 8
        )
        market_daily_report_tickers = _to_str_list(_env("MARKET_DAILY_REPORT_TICKERS", ""))
        market_daily_report_debug = _parse_bool_field(
            "MARKET_DAILY_REPORT_DEBUG", "market_daily_report_debug", False
        )

        # Bitcoin address watch
        btc_watch_enabled = _parse_bool_field("BTC_WATCH_ENABLED", "btc_watch_enabled", False)
        btc_watch_poll_seconds = _parse_int_field("BTC_WATCH_POLL_SECONDS", "btc_watch_poll_seconds", 300)
        btc_watch_channel_id = _parse_int_field(
            "BTC_WATCH_CHANNEL_ID", "btc_watch_channel_id", 0, snowflake=True
        )
        btc_watch_label = _env("BTC_WATCH_LABEL", "BTC Watch").strip() or "BTC Watch"
        btc_watch_addresses = _to_str_list(_env("BTC_WATCH_ADDRESSES", ""))
        btc_esplora_base_url = _env("BTC_ESPLORA_BASE_URL", "").strip()
        btc_watch_cooldown_minutes = _parse_int_field(
            "BTC_WATCH_COOLDOWN_MINUTES", "btc_watch_cooldown_minutes", 30
        )
        btc_watch_debug = _parse_bool_field("BTC_WATCH_DEBUG", "btc_watch_debug", False)

        db_env_raw = _env("DB_PATH", "").strip()
        legacy_sqlite_raw = _env("SQLITE_PATH", "").strip()
        if db_env_raw:
            raw_db_choice = db_env_raw
        elif legacy_sqlite_raw:
            warnings.warn(
                "SQLITE_PATH is deprecated and scheduled for removal in v1.1.0; use DB_PATH instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            raw_db_choice = legacy_sqlite_raw
        else:
            raw_db_choice = "ic_stock.db"

        try:
            db_path = normalize_sqlite_path(raw_db_choice, allow_empty=False)
        except ConfigValidationError as exc:
            raw_issues.append(ValidationIssue("db_path", str(exc), "error"))
            db_path = raw_db_choice

        if "GRADES_DB_PATH" in env:
            grades_raw = str(env.get("GRADES_DB_PATH") or "").strip()
        else:
            grades_raw = "tmu_grades.db"

        if grades_raw:
            try:
                grades_db_path = normalize_sqlite_path(grades_raw, allow_empty=True)
            except ConfigValidationError as exc:
                raw_issues.append(ValidationIssue("grades_db_path", str(exc), "error"))
                grades_db_path = grades_raw
        else:
            grades_db_path = ""
        exams_tz = _env("EXAMS_TZ", "America/Toronto").strip() or "America/Toronto"
        notes_sites = _to_str_list(_env("NOTES_SITES", ""))
        lore_path = _env("LORE_PATH", "").strip()
        rmp_university = (
            _env("RMP_UNIVERSITY", "Toronto Metropolitan University").strip()
            or "Toronto Metropolitan University"
        )
        campus_id = _env("CAMPUS_ID", "").strip()
        campus_url = _env("CAMPUS_URL", "").strip()
        academic_course_aliases = _to_str_list(_env("ACADEMIC_COURSE_ALIASES", ""))
        academic_allowed_hosts = _to_str_list(_env("ACADEMIC_ALLOWED_HOSTS", ""))
        academic_enabled_providers = _to_str_list(_env("ACADEMIC_ENABLED_PROVIDERS", ""))
        exam_reminders_enabled = _parse_bool_field("EXAM_REMINDERS_ENABLED", "exam_reminders_enabled", False)
        exam_reminders_channel_id = _parse_int_field(
            "EXAM_REMINDERS_CHANNEL_ID", "exam_reminders_channel_id", 0, snowflake=True
        )
        raw_lead_hours = _to_str_list(_env("EXAM_REMINDER_LEAD_HOURS", ""))
        exam_reminder_lead_hours: list[int] | None = None
        if raw_lead_hours:
            parsed_leads: list[int] = []
            for item in raw_lead_hours:
                try:
                    h = int(item)
                except ValueError:
                    raw_issues.append(
                        ValidationIssue(
                            "exam_reminder_lead_hours",
                            "EXAM_REMINDER_LEAD_HOURS must contain integer hours.",
                            "error",
                        )
                    )
                    continue
                parsed_leads.append(h)
            exam_reminder_lead_hours = parsed_leads
        feature_profile = _env("FEATURE_PROFILE", "default").strip() or "default"
        enabled_features = _to_str_list(_env("ENABLED_FEATURES", ""))
        disabled_features = _to_str_list(_env("DISABLED_FEATURES", ""))
        log_file = _env("LOG_FILE", "").strip()
        log_max_bytes = _parse_int_field("LOG_MAX_BYTES", "log_max_bytes", 5_000_000)
        log_backup_count = _parse_int_field("LOG_BACKUP_COUNT", "log_backup_count", 3)
        telemetry_enabled = _parse_bool_field("ICBOT_TELEMETRY", "telemetry_enabled", False)

        # Preserve fallback rule only when workday_poll_seconds was non-negative unset/zero
        if workday_poll_seconds == 0:
            workday_poll_seconds = max(0, jobs_poll_seconds)

        settings = cls(
            token=token,
            guild_id=guild_id,
            guild_id_raw=guild_raw,
            guild_id_invalid=guild_id_invalid,
            stock_channel_id=stock_channel_id,
            user_command_channel_id=user_command_channel_id,
            ttc_alerts_channel_id=ttc_alerts_channel_id,
            job_alerts_channel_id=job_alerts_channel_id,
            allowed_role_ids=allowed_role_ids,
            embed_logo_url=embed_logo_url,
            embed_accent_color=embed_accent_color,
            embed_footer_text=embed_footer_text,
            bot_display_name=bot_display_name,
            stock_exchange_name=stock_exchange_name,
            currency_name=currency_name,
            currency_symbol=currency_symbol,
            primary_stock_symbol=primary_stock_symbol,
            primary_stock_name=primary_stock_name,
            source_attribution=source_attribution,
            theme_preset=theme_preset,
            odds_api_key=odds_api_key,
            sports_rss_nba=sports_rss_nba,
            sports_rss_nfl=sports_rss_nfl,
            sports_rss_mma=sports_rss_mma,
            sports_rss_ufc=sports_rss_ufc,
            sports_rss_boxing=sports_rss_boxing,
            ttc_mastodon_base=ttc_mastodon_base,
            ttc_mastodon_acct=ttc_mastodon_acct,
            ttc_poll_seconds=ttc_poll_seconds,
            ttc_alert_level=ttc_alert_level,
            ttc_bootstrap_post_latest=ttc_bootstrap_post_latest,
            ttc_include_replies=ttc_include_replies,
            ttc_include_reblogs=ttc_include_reblogs,
            ttc_debug=ttc_debug,
            go_api_base=go_api_base,
            go_api_key=go_api_key,
            go_alerts_channel_id=go_alerts_channel_id,
            go_poll_seconds=go_poll_seconds,
            go_alert_level=go_alert_level,
            go_bootstrap_post_latest=go_bootstrap_post_latest,
            go_include_buses=go_include_buses,
            go_lines_allowlist=go_lines_allowlist,
            go_ssl_verify=go_ssl_verify,
            go_ca_bundle_path=go_ca_bundle_path,
            go_debug=go_debug,
            exams_tz=exams_tz,
            llm_enabled=llm_enabled,
            llm_cooldown_seconds=llm_cooldown_seconds,
            llm_max_messages=llm_max_messages,
            llm_max_chars=llm_max_chars,
            llm_max_image_bytes=llm_max_image_bytes,
            llm_max_output_tokens=llm_max_output_tokens,
            llm_debug=llm_debug,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            gemini_resume_model=gemini_resume_model,
            gemini_resume_prompt=gemini_resume_prompt,
            gemini_resume_prompt_hw=gemini_resume_prompt_hw,
            gemini_resume_prompt_ele=gemini_resume_prompt_ele,
            llm_channel_history_enabled=llm_channel_history_enabled,
            llm_lore_enabled=llm_lore_enabled,
            llm_images_enabled=llm_images_enabled,
            llm_allow_dm=llm_allow_dm,
            llm_allowed_channel_ids=llm_allowed_channel_ids,
            llm_allowed_role_ids=llm_allowed_role_ids,
            jobs_poll_seconds=jobs_poll_seconds,
            greenhouse_boards=greenhouse_boards,
            lever_sites=lever_sites,
            jobs_location_allowlist=jobs_location_allowlist,
            jobs_keywords_allowlist=jobs_keywords_allowlist,
            jobs_keywords_denylist=jobs_keywords_denylist,
            jobs_bootstrap_post=jobs_bootstrap_post,
            jobs_require_early_career=jobs_require_early_career,
            workday_sites=workday_sites,
            workday_poll_seconds=workday_poll_seconds,
            workday_timeout_seconds=workday_timeout_seconds,
            force_guild_resync=force_guild_resync,
            allow_gateway=allow_gateway,
            task_startup_jitter_seconds=task_startup_jitter_seconds,
            task_max_backoff_seconds=task_max_backoff_seconds,
            daily_claim_amount=daily_claim_amount,
            bj_starting_bankroll=bj_starting_bankroll,
            bj_dealer_hits_soft_17=bj_dealer_hits_soft_17,
            bj_max_splits=bj_max_splits,
            bj_session_ttl_minutes=bj_session_ttl_minutes,
            market_watch_enabled=market_watch_enabled,
            market_watch_poll_seconds=market_watch_poll_seconds,
            market_watch_channel_id=market_watch_channel_id,
            market_watch_vix_level=market_watch_vix_level,
            market_watch_index_move_pct=market_watch_index_move_pct,
            market_watch_stock_move_pct=market_watch_stock_move_pct,
            market_watch_tickers=market_watch_tickers,
            market_watch_cooldown_minutes=market_watch_cooldown_minutes,
            market_watch_debug=market_watch_debug,
            market_daily_report_enabled=market_daily_report_enabled,
            market_daily_report_channel_id=market_daily_report_channel_id,
            market_daily_report_time=market_daily_report_time,
            market_daily_report_tz=market_daily_report_tz,
            market_daily_report_top_n=market_daily_report_top_n,
            market_daily_report_tickers=market_daily_report_tickers,
            market_daily_report_debug=market_daily_report_debug,
            btc_watch_enabled=btc_watch_enabled,
            btc_watch_poll_seconds=btc_watch_poll_seconds,
            btc_watch_channel_id=btc_watch_channel_id,
            btc_watch_label=btc_watch_label,
            btc_watch_addresses=btc_watch_addresses,
            btc_esplora_base_url=btc_esplora_base_url,
            btc_watch_cooldown_minutes=btc_watch_cooldown_minutes,
            btc_watch_debug=btc_watch_debug,
            db_path=db_path,
            grades_db_path=grades_db_path,
            notes_sites=notes_sites,
            lore_path=lore_path,
            rmp_university=rmp_university,
            campus_id=campus_id,
            campus_url=campus_url,
            academic_course_aliases=academic_course_aliases,
            academic_allowed_hosts=academic_allowed_hosts,
            academic_enabled_providers=academic_enabled_providers,
            exam_reminders_enabled=exam_reminders_enabled,
            exam_reminders_channel_id=exam_reminders_channel_id,
            exam_reminder_lead_hours=exam_reminder_lead_hours,
            feature_profile=feature_profile,
            enabled_features=enabled_features,
            disabled_features=disabled_features,
            log_file=log_file,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
            telemetry_enabled=telemetry_enabled,
        )
        if strict:
            settings.validate(strict=True, extra_issues=raw_issues)
        return settings

    @classmethod
    def load_validated(cls, *, environ: Mapping[str, str] | None = None) -> Settings:
        """Load and strictly validate environment settings, raising ``ConfigValidationError`` on errors."""
        return cls.from_env(strict=True, environ=environ)

    def replace(self, **changes: Any) -> Settings:
        """Return a new ``Settings`` instance with specified fields updated."""
        return dataclasses.replace(self, **changes)


def load_settings(*, strict: bool = True, environ: Mapping[str, str] | None = None) -> Settings:
    """Load process configuration from ``environ`` (or ``os.environ``), defaulting to strict validation."""
    return Settings.from_env(strict=strict, environ=environ)
