from __future__ import annotations

from dataclasses import dataclass
import os


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int((s or "").strip() or default)
    except ValueError:
        return default


def _to_float(s: str, default: float = 0.0) -> float:
    try:
        return float((s or "").strip() or default)
    except ValueError:
        return default


def _to_color(s: str, default: int) -> int:
    v = (s or "").strip()
    if not v:
        return default
    try:
        if v.startswith("#"):
            return int(v[1:], 16)
        if v.lower().startswith("0x"):
            return int(v, 16)
        if all(c in "0123456789abcdefABCDEF" for c in v) and len(v) in (6, 8):
            return int(v, 16)
        return int(v)
    except ValueError:
        return default


def _to_bool(s: str, default: bool = False) -> bool:
    v = (s or "").strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def _to_int_list(s: str) -> list[int]:
    out: list[int] = []
    raw = (s or "").strip()
    if not raw:
        return out
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


def _to_str_list(s: str) -> list[str]:
    raw = (s or "").strip()
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


@dataclass(frozen=True)
class Settings:
    # Required core
    token: str
    guild_id: int

    # Channels
    stock_channel_id: int = 0
    user_command_channel_id: int = 0
    ttc_alerts_channel_id: int = 0
    job_alerts_channel_id: int = 0

    # Market watch (real markets) — posts into stock channel when thresholds hit
    market_watch_enabled: bool = False
    market_watch_channel_id: int = 0  # defaults to STOCK_CHANNEL_ID if 0
    market_watch_poll_seconds: int = 60
    market_watch_debug: bool = False  # if true, prints one-line snapshots to terminal
    market_watch_tickers: list[str] | None = None  # comma list in env
    market_watch_vix_level: float = 20.0
    market_watch_index_move_pct: float = 2.5
    market_watch_stock_move_pct: float = 10.0
    market_watch_cooldown_minutes: int = 30

    # Role IDs allowed to do admin-style actions
    allowed_role_ids: list[int] = None  # type: ignore[assignment]
    # Legacy (optional)
    allowed_roles: list[str] = None  # type: ignore[assignment]

    # DB
    db_path: str = "ic_stock.db"

    # Embeds
    embed_logo_url: str = ""
    embed_accent_color: int = 0xA64DFF
    embed_footer_text: str = "Instigate Cafe"

    # Blackjack
    bj_starting_bankroll: int = 100
    bj_dealer_hits_soft_17: bool = True
    bj_max_splits: int = 1
    bj_session_ttl_minutes: int = 20

    # Odds API
    odds_api_key: str = ""

    # TTC Alerts System (supports either Mastodon API OR RSS feed)
    ttc_mastodon_base: str = ""
    ttc_mastodon_acct: str = ""
    ttc_feed_url: str = "https://mstdn.ca/@ttcnotices.rss"

    ttc_poll_seconds: int = 120
    ttc_alert_level: str = "major"  # major | all
    ttc_bootstrap_post_latest: bool = False

    # Job Alerts System (ATS)
    jobs_poll_seconds: int = 600
    jobs_bootstrap_post: bool = False

    greenhouse_boards: list[str] | None = None
    lever_sites: list[str] | None = None

    jobs_location_allowlist: list[str] | None = None
    jobs_keywords_allowlist: list[str] | None = None
    jobs_keywords_denylist: list[str] | None = None

    # Dev
    force_guild_resync: bool = False

    @staticmethod
    def from_env() -> "Settings":
        token = (os.getenv("DISCORD_TOKEN", "") or "").strip()
        guild_id = _to_int(os.getenv("GUILD_ID", "0"), 0)

        stock_channel_id = _to_int(os.getenv("STOCK_CHANNEL_ID", "0"), 0)
        user_command_channel_id = _to_int(os.getenv("USER_COMMAND_CHANNEL_ID", "0"), 0)
        ttc_alerts_channel_id = _to_int(os.getenv("TTC_ALERTS_CHANNEL_ID", "0"), 0)
        job_alerts_channel_id = _to_int(os.getenv("JOB_ALERTS_CHANNEL_ID", "0"), 0)

        # Market watch
        market_watch_enabled = _to_bool(os.getenv("MARKET_WATCH_ENABLED", "false"), False)
        market_watch_channel_id = _to_int(os.getenv("MARKET_WATCH_CHANNEL_ID", "0"), 0)
        # Allow faster polling for test environments (min 10s). Use 60s for typical usage.
        market_watch_poll_seconds = max(10, _to_int(os.getenv("MARKET_WATCH_POLL_SECONDS", "60"), 60))
        market_watch_debug = _to_bool(os.getenv("MARKET_WATCH_DEBUG", "false"), False)
        market_watch_tickers = _to_str_list(os.getenv("MARKET_WATCH_TICKERS", ""))

        # VIX threshold: level (not %). Back-compat: if MARKET_WATCH_VIX_LEVEL is absent,
        # treat MARKET_WATCH_VIX_CHANGE_PCT as the level (older config name).
        raw_vix_level = (os.getenv("MARKET_WATCH_VIX_LEVEL", "") or "").strip()
        if raw_vix_level:
            market_watch_vix_level = max(0.0, _to_float(raw_vix_level, 20.0))
        else:
            market_watch_vix_level = max(0.0, _to_float(os.getenv("MARKET_WATCH_VIX_CHANGE_PCT", "20"), 20.0))

        market_watch_index_move_pct = max(0.0, _to_float(os.getenv("MARKET_WATCH_INDEX_MOVE_PCT", "2.5"), 2.5))
        market_watch_stock_move_pct = max(0.0, _to_float(os.getenv("MARKET_WATCH_STOCK_MOVE_PCT", "10"), 10.0))
        # Allow 1 minute cooldown for testing (min 1). Default is 30.
        market_watch_cooldown_minutes = max(1, _to_int(os.getenv("MARKET_WATCH_COOLDOWN_MINUTES", "30"), 30))

        allowed_role_ids = _to_int_list(os.getenv("ALLOWED_ROLE_IDS", ""))
        allowed_roles = _to_str_list(os.getenv("ALLOWED_ROLES", ""))

        db_path = (os.getenv("DB_PATH", "") or "").strip() or "ic_stock.db"

        # Embeds
        embed_logo_url = (os.getenv("EMBED_LOGO_URL", "") or "").strip()
        embed_accent_color = _to_color(os.getenv("EMBED_ACCENT_COLOR", "#A64DFF"), 0xA64DFF)
        embed_footer_text = (os.getenv("EMBED_FOOTER_TEXT", "Instigate Cafe") or "Instigate Cafe").strip()

        # Blackjack
        bj_start = _to_int(os.getenv("BJ_STARTING_BANKROLL", "100"), 100)
        bj_hs17 = _to_bool(os.getenv("BJ_DEALER_HITS_SOFT_17", "true"), True)
        bj_splits = _to_int(os.getenv("BJ_MAX_SPLITS", "1"), 1)
        bj_ttl = _to_int(os.getenv("BJ_SESSION_TTL_MINUTES", "20"), 20)

        # OddsAPI
        odds_api_key = (os.getenv("ODDS_API_KEY", "") or "").strip()

        # TTC
        ttc_mastodon_base = (os.getenv("TTC_MASTODON_BASE", "") or "").strip()
        ttc_mastodon_acct = (os.getenv("TTC_MASTODON_ACCT", "") or "").strip()
        ttc_feed_url = (os.getenv("TTC_FEED_URL", "") or "https://mstdn.ca/@ttcnotices.rss").strip()

        ttc_poll_seconds = max(30, _to_int(os.getenv("TTC_POLL_SECONDS", "120"), 120))
        ttc_alert_level = (os.getenv("TTC_ALERT_LEVEL", "major") or "major").strip().lower()
        if ttc_alert_level not in ("major", "all"):
            ttc_alert_level = "major"

        ttc_bootstrap_post_latest = _to_bool(os.getenv("TTC_BOOTSTRAP_POST_LATEST", "false"), False)

        # Jobs
        jobs_poll_seconds = max(60, _to_int(os.getenv("JOBS_POLL_SECONDS", "600"), 600))
        jobs_bootstrap_post = _to_bool(os.getenv("JOBS_BOOTSTRAP_POST", "false"), False)

        greenhouse_boards = _to_str_list(os.getenv("GREENHOUSE_BOARDS", ""))
        lever_sites = _to_str_list(os.getenv("LEVER_SITES", ""))

        jobs_location_allowlist = _to_str_list(os.getenv("JOBS_LOCATION_ALLOWLIST", ""))
        jobs_keywords_allowlist = _to_str_list(os.getenv("JOBS_KEYWORDS_ALLOWLIST", ""))
        jobs_keywords_denylist = _to_str_list(os.getenv("JOBS_KEYWORDS_DENYLIST", ""))

        force_guild_resync = _to_bool(os.getenv("FORCE_GUILD_RESYNC", "false"), False)

        return Settings(
            token=token,
            guild_id=guild_id,
            stock_channel_id=stock_channel_id,
            user_command_channel_id=user_command_channel_id,
            ttc_alerts_channel_id=ttc_alerts_channel_id,
            job_alerts_channel_id=job_alerts_channel_id,
            market_watch_enabled=market_watch_enabled,
            market_watch_channel_id=market_watch_channel_id,
            market_watch_poll_seconds=market_watch_poll_seconds,
            market_watch_debug=market_watch_debug,
            market_watch_tickers=market_watch_tickers,
            market_watch_vix_level=market_watch_vix_level,
            market_watch_index_move_pct=market_watch_index_move_pct,
            market_watch_stock_move_pct=market_watch_stock_move_pct,
            market_watch_cooldown_minutes=market_watch_cooldown_minutes,
            allowed_role_ids=allowed_role_ids,
            allowed_roles=allowed_roles,
            db_path=db_path,
            embed_logo_url=embed_logo_url,
            embed_accent_color=embed_accent_color,
            embed_footer_text=embed_footer_text or "Instigate Cafe",
            bj_starting_bankroll=max(1, bj_start),
            bj_dealer_hits_soft_17=bj_hs17,
            bj_max_splits=max(0, bj_splits),
            bj_session_ttl_minutes=max(5, bj_ttl),
            odds_api_key=odds_api_key,
            ttc_mastodon_base=ttc_mastodon_base,
            ttc_mastodon_acct=ttc_mastodon_acct,
            ttc_feed_url=ttc_feed_url,
            ttc_poll_seconds=ttc_poll_seconds,
            ttc_alert_level=ttc_alert_level,
            ttc_bootstrap_post_latest=ttc_bootstrap_post_latest,
            jobs_poll_seconds=jobs_poll_seconds,
            jobs_bootstrap_post=jobs_bootstrap_post,
            greenhouse_boards=greenhouse_boards,
            lever_sites=lever_sites,
            jobs_location_allowlist=jobs_location_allowlist,
            jobs_keywords_allowlist=jobs_keywords_allowlist,
            jobs_keywords_denylist=jobs_keywords_denylist,
            force_guild_resync=force_guild_resync,
        )
