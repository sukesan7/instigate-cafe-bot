from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Discord "snowflake" IDs fit in signed int64 for most libs
MAX_SNOWFLAKE = 9223372036854775807


def _to_int(value: Any, default: int = 0, *, clamp_snowflake: bool = False) -> int:
    """Best-effort int parsing with optional snowflake clamping."""
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        n = int(s)
        if clamp_snowflake and n > MAX_SNOWFLAKE:
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
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        return default
    except (TypeError, ValueError):
        return default


_HEX_COLOR_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")


def _to_color(value: Any, default: int = 0xA64DFF) -> int:
    """Parse embed color from '#RRGGBB', '0xRRGGBB', 'RRGGBB', or int."""
    try:
        if value is None:
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
    s = str(raw).strip()
    if not s:
        return []

    out: list[str] = []
    for tok in s.split(","):
        t = tok.strip()
        if not t:
            continue
        # Common accidental token from line continuation or copy/paste
        if t in ("\\", "\\\\"):
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

    # Embeds
    embed_logo_url: str = ""
    embed_accent_color: int = 0xA64DFF
    embed_footer_text: str = "Instigate Cafe"

    # Odds API
    odds_api_key: str = ""

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
    # If set, only alerts mentioning these line codes/names will post.
    # Example: ["LW", "LE", "ST"] or ["lakeshore west", "stouffville"]
    go_lines_allowlist: list[str] | None = None
    # TLS options (Windows sometimes lacks a usable CA store for Python/aiohttp)
    go_ssl_verify: bool = True
    # Optional path to a CA bundle (PEM). If empty, uses certifi if available.
    go_ca_bundle_path: str = ""
    go_debug: bool = False

    # Exams
    exams_tz: str = "America/Toronto"

    # LLM mention assistant (Gemini)
    llm_enabled: bool = False
    llm_cooldown_seconds: int = 3
    llm_max_messages: int = 25
    llm_max_chars: int = 12000
    llm_debug: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_resume_model: str = "gemini-2.5-pro"
    gemini_resume_prompt: str = ""
    gemini_resume_prompt_hw: str = ""
    gemini_resume_prompt_ele: str = ""

    # Jobs alerts (Greenhouse/Lever)
    jobs_poll_seconds: int = 600
    greenhouse_boards: list[str] | None = None
    lever_sites: list[str] | None = None
    jobs_location_allowlist: list[str] | None = None
    jobs_keywords_allowlist: list[str] | None = None
    jobs_keywords_denylist: list[str] | None = None
    jobs_bootstrap_post: bool = False
    jobs_require_early_career: bool = False

    # Workday jobs (new)
    workday_sites: list[str] | None = None  # e.g. "RBC|https://rbc.wd3.myworkdayjobs.com/en-US/RBC"
    workday_poll_seconds: int = 0  # if 0, defaults to jobs_poll_seconds
    workday_timeout_seconds: int = 12

    # Dev & Runtime Safeguard
    force_guild_resync: bool = False
    allow_gateway: bool = False

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

    # Daily market summary (posts at a scheduled local time)
    market_daily_report_enabled: bool = False
    market_daily_report_channel_id: int = 0
    market_daily_report_time: str = "16:30"  # HH:MM in market_daily_report_tz
    market_daily_report_tz: str = "America/Toronto"
    market_daily_report_top_n: int = 8
    market_daily_report_tickers: list[str] | None = None

    # Bitcoin address watch (e.g., "Satoshi-era" / known whale addresses)
    btc_watch_enabled: bool = False
    btc_watch_poll_seconds: int = 300
    btc_watch_channel_id: int = 0
    btc_watch_label: str = "BTC Watch"
    btc_watch_addresses: list[str] | None = None
    btc_esplora_base_url: str = ""  # defaults to blockstream.info/api
    btc_watch_cooldown_minutes: int = 30

    # DB
    db_path: str = "ic_stock.db"

    # Course Notes
    notes_sites: list[str] | None = None

    # Lore
    lore_path: str = ""

    # RateMyProfessors
    rmp_university: str = "Toronto Metropolitan University"

    # ---- Compatibility aliases (older callers) ----
    @property
    def greenhouse_board_tokens(self) -> list[str]:
        return list(self.greenhouse_boards or [])

    @property
    def lever_site_tokens(self) -> list[str]:
        return list(self.lever_sites or [])

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()

        guild_raw = os.getenv("GUILD_ID", "").strip()
        guild_id = _to_int(guild_raw, 0, clamp_snowflake=True)
        guild_id_invalid = bool(guild_raw) and (guild_id == 0) and (_to_int(guild_raw, 0) > MAX_SNOWFLAKE)

        stock_channel_id = _to_int(os.getenv("STOCK_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        user_command_channel_id = _to_int(os.getenv("USER_COMMAND_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        ttc_alerts_channel_id = _to_int(os.getenv("TTC_ALERTS_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        job_alerts_channel_id = _to_int(os.getenv("JOB_ALERTS_CHANNEL_ID", "0"), 0, clamp_snowflake=True)

        allowed_roles = _to_str_list(os.getenv("ALLOWED_ROLE_IDS", ""))
        allowed_role_ids: list[int] = []
        for r in allowed_roles:
            n = _to_int(r, 0, clamp_snowflake=True)
            if n:
                allowed_role_ids.append(n)

        embed_logo_url = os.getenv("EMBED_LOGO_URL", "").strip()
        embed_accent_color = _to_color(os.getenv("EMBED_ACCENT_COLOR", ""), 0xA64DFF)
        embed_footer_text = os.getenv("EMBED_FOOTER_TEXT", "Instigate Cafe").strip() or "Instigate Cafe"

        odds_api_key = os.getenv("ODDS_API_KEY", "").strip()

        ttc_mastodon_base = os.getenv("TTC_MASTODON_BASE", "").strip()
        ttc_mastodon_acct = os.getenv("TTC_MASTODON_ACCT", "").strip()
        ttc_poll_seconds = _to_int(os.getenv("TTC_POLL_SECONDS", "120"), 120)
        ttc_alert_level = os.getenv("TTC_ALERT_LEVEL", "major").strip().lower() or "major"
        ttc_bootstrap_post_latest = _to_bool(os.getenv("TTC_BOOTSTRAP_POST_LATEST", "false"), False)
        ttc_include_replies = _to_bool(os.getenv("TTC_INCLUDE_REPLIES", "true"), True)
        ttc_include_reblogs = _to_bool(os.getenv("TTC_INCLUDE_REBLOGS", "false"), False)
        ttc_debug = _to_bool(os.getenv("TTC_DEBUG", "false"), False)

        go_api_base = (
            os.getenv("GO_API_BASE", "https://api.openmetrolinx.com/OpenDataAPI/api/V1").strip()
            or "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
        )
        go_api_key = os.getenv("GO_API_KEY", "").strip()
        go_alerts_channel_id = _to_int(os.getenv("GO_ALERTS_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        go_poll_seconds = _to_int(os.getenv("GO_POLL_SECONDS", "180"), 180)
        go_alert_level = os.getenv("GO_ALERT_LEVEL", "major").strip().lower() or "major"
        go_bootstrap_post_latest = _to_bool(os.getenv("GO_BOOTSTRAP_POST_LATEST", "true"), True)
        go_include_buses = _to_bool(os.getenv("GO_INCLUDE_BUSES", "false"), False)
        go_lines_allowlist = _to_str_list(os.getenv("GO_LINES_ALLOWLIST", ""))
        go_ssl_verify = _to_bool(os.getenv("GO_SSL_VERIFY", "true"), True)
        go_ca_bundle_path = os.getenv("GO_CA_BUNDLE_PATH", "").strip()
        go_debug = _to_bool(os.getenv("GO_DEBUG", "false"), False)

        # LLM mention assistant (Gemini)
        llm_enabled = _to_bool(os.getenv("LLM_ENABLED", "false"), False)
        llm_cooldown_seconds = _to_int(os.getenv("LLM_COOLDOWN_SECONDS", "3"), 3)
        llm_max_messages = _to_int(os.getenv("LLM_MAX_MESSAGES", "25"), 25)
        llm_max_chars = _to_int(os.getenv("LLM_MAX_CHARS", "12000"), 12000)
        llm_debug = _to_bool(os.getenv("LLM_DEBUG", "false"), False)
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        gemini_resume_model = os.getenv("GEMINI_RESUME_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
        gemini_resume_prompt = os.getenv("GEMINI_RESUME_PROMPT", "").strip()
        gemini_resume_prompt_hw = os.getenv("GEMINI_RESUME_PROMPT_HW", "").strip()
        gemini_resume_prompt_ele = os.getenv("GEMINI_RESUME_PROMPT_ELE", "").strip()

        jobs_poll_seconds = _to_int(os.getenv("JOBS_POLL_SECONDS", "600"), 600)
        greenhouse_boards = _to_str_list(os.getenv("GREENHOUSE_BOARDS", ""))
        lever_sites = _to_str_list(os.getenv("LEVER_SITES", ""))
        jobs_location_allowlist = _to_str_list(os.getenv("JOBS_LOCATION_ALLOWLIST", ""))
        jobs_keywords_allowlist = _to_str_list(os.getenv("JOBS_KEYWORDS_ALLOWLIST", ""))
        jobs_keywords_denylist = _to_str_list(os.getenv("JOBS_KEYWORDS_DENYLIST", ""))
        jobs_bootstrap_post = _to_bool(os.getenv("JOBS_BOOTSTRAP_POST", "false"), False)
        jobs_require_early_career = _to_bool(os.getenv("JOBS_REQUIRE_EARLY_CAREER", "false"), False)

        # Workday (new)
        workday_sites = _to_str_list(os.getenv("WORKDAY_SITES", ""))
        workday_poll_seconds = _to_int(os.getenv("WORKDAY_POLL_SECONDS", "0"), 0)
        workday_timeout_seconds = _to_int(os.getenv("WORKDAY_TIMEOUT_SECONDS", "12"), 12)

        force_guild_resync = _to_bool(os.getenv("FORCE_GUILD_RESYNC", "false"), False)
        allow_gateway = _to_bool(os.getenv("ICBOT_ALLOW_GATEWAY", "false"), False)

        daily_claim_amount = _to_int(os.getenv("DAILY_CLAIM_AMOUNT", "100"), 100)

        bj_starting_bankroll = _to_int(os.getenv("BJ_STARTING_BANKROLL", "1000"), 1000)
        bj_dealer_hits_soft_17 = _to_bool(os.getenv("BJ_DEALER_HITS_SOFT_17", "true"), True)
        bj_max_splits = _to_int(os.getenv("BJ_MAX_SPLITS", "1"), 1)
        bj_session_ttl_minutes = _to_int(os.getenv("BJ_SESSION_TTL_MINUTES", "20"), 20)

        market_watch_enabled = _to_bool(os.getenv("MARKET_WATCH_ENABLED", "false"), False)
        market_watch_poll_seconds = _to_int(os.getenv("MARKET_WATCH_POLL_SECONDS", "60"), 60)
        market_watch_channel_id = _to_int(os.getenv("MARKET_WATCH_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        market_watch_vix_level = _to_float(os.getenv("MARKET_WATCH_VIX_LEVEL", "20"), 20.0)
        market_watch_index_move_pct = _to_float(os.getenv("MARKET_WATCH_INDEX_MOVE_PCT", "2.5"), 2.5)
        market_watch_stock_move_pct = _to_float(os.getenv("MARKET_WATCH_STOCK_MOVE_PCT", "10"), 10.0)
        market_watch_tickers = _to_str_list(os.getenv("MARKET_WATCH_TICKERS", ""))
        market_watch_cooldown_minutes = _to_int(os.getenv("MARKET_WATCH_COOLDOWN_MINUTES", "30"), 30)

        # Daily market summary
        market_daily_report_enabled = _to_bool(os.getenv("MARKET_DAILY_REPORT_ENABLED", "false"), False)
        market_daily_report_channel_id = _to_int(
            os.getenv("MARKET_DAILY_REPORT_CHANNEL_ID", "0"), 0, clamp_snowflake=True
        )
        market_daily_report_time = os.getenv("MARKET_DAILY_REPORT_TIME", "16:30").strip() or "16:30"
        market_daily_report_tz = (
            os.getenv("MARKET_DAILY_REPORT_TZ", "America/Toronto").strip() or "America/Toronto"
        )
        market_daily_report_top_n = _to_int(os.getenv("MARKET_DAILY_REPORT_TOP_N", "8"), 8)
        market_daily_report_tickers = _to_str_list(os.getenv("MARKET_DAILY_REPORT_TICKERS", ""))

        # Bitcoin address watch
        btc_watch_enabled = _to_bool(os.getenv("BTC_WATCH_ENABLED", "false"), False)
        btc_watch_poll_seconds = _to_int(os.getenv("BTC_WATCH_POLL_SECONDS", "300"), 300)
        btc_watch_channel_id = _to_int(os.getenv("BTC_WATCH_CHANNEL_ID", "0"), 0, clamp_snowflake=True)
        btc_watch_label = os.getenv("BTC_WATCH_LABEL", "BTC Watch").strip() or "BTC Watch"
        btc_watch_addresses = _to_str_list(os.getenv("BTC_WATCH_ADDRESSES", ""))
        btc_esplora_base_url = os.getenv("BTC_ESPLORA_BASE_URL", "").strip()
        btc_watch_cooldown_minutes = _to_int(os.getenv("BTC_WATCH_COOLDOWN_MINUTES", "30"), 30)

        db_path = os.getenv("DB_PATH", "ic_stock.db").strip() or "ic_stock.db"
        exams_tz = os.getenv("EXAMS_TZ", "America/Toronto").strip() or "America/Toronto"
        notes_sites = _to_str_list(os.getenv("NOTES_SITES", ""))
        lore_path = os.getenv("LORE_PATH", "").strip()
        rmp_university = (
            os.getenv("RMP_UNIVERSITY", "Toronto Metropolitan University").strip()
            or "Toronto Metropolitan University"
        )

        # Default workday poll seconds to jobs poll seconds if unset
        if workday_poll_seconds <= 0:
            workday_poll_seconds = jobs_poll_seconds

        return cls(
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
            odds_api_key=odds_api_key,
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
            llm_enabled=llm_enabled,
            llm_cooldown_seconds=llm_cooldown_seconds,
            llm_max_messages=llm_max_messages,
            llm_max_chars=llm_max_chars,
            llm_debug=llm_debug,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            gemini_resume_model=gemini_resume_model,
            gemini_resume_prompt=gemini_resume_prompt,
            gemini_resume_prompt_hw=gemini_resume_prompt_hw,
            gemini_resume_prompt_ele=gemini_resume_prompt_ele,
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
            market_daily_report_enabled=market_daily_report_enabled,
            market_daily_report_channel_id=market_daily_report_channel_id,
            market_daily_report_time=market_daily_report_time,
            market_daily_report_tz=market_daily_report_tz,
            market_daily_report_top_n=market_daily_report_top_n,
            market_daily_report_tickers=market_daily_report_tickers,
            btc_watch_enabled=btc_watch_enabled,
            btc_watch_poll_seconds=btc_watch_poll_seconds,
            btc_watch_channel_id=btc_watch_channel_id,
            btc_watch_label=btc_watch_label,
            btc_watch_addresses=btc_watch_addresses,
            btc_esplora_base_url=btc_esplora_base_url,
            btc_watch_cooldown_minutes=btc_watch_cooldown_minutes,
            db_path=db_path,
            exams_tz=exams_tz,
            notes_sites=notes_sites,
            lore_path=lore_path,
            rmp_university=rmp_university,
        )
