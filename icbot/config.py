from __future__ import annotations
from dataclasses import dataclass
import os


def _to_int(s: str, default: int = 0) -> int:
    try:
        return int((s or "").strip() or default)
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


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int
    stock_channel_id: int

    # Allowlist by ROLE ID
    allowed_role_ids: list[int]

    # Legacy: allowlist by role NAME (optional fallback)
    allowed_roles: list[str]

    # Non-admin users can only run /ic commands in this channel (0 disables)
    user_command_channel_id: int = 0

    # === Auto-alerts: TTC ===
    # Channel where TTC alerts are auto-posted (0 disables TTC alerts)
    ttc_alerts_channel_id: int = 0
    # RSS feed URL for TTC notices
    ttc_feed_url: str = "https://mstdn.ca/@ttcnotices.rss"
    # Poll interval (seconds)
    ttc_poll_seconds: int = 120
    # "major" to only post serious alerts; "all" to post everything
    ttc_alert_level: str = "major"

    # If True, clear+resync guild commands on startup (useful for test servers)
    force_guild_resync: bool = False

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

    @staticmethod
    def from_env() -> "Settings":
        token = (os.getenv("DISCORD_TOKEN", "") or "").strip()
        guild_id = _to_int(os.getenv("GUILD_ID", "0"), 0)
        stock_channel_id = _to_int(os.getenv("STOCK_CHANNEL_ID", "0"), 0)

        # Role IDs (preferred)
        allowed_role_ids = _to_int_list(os.getenv("ALLOWED_ROLE_IDS", ""))

        # Legacy: role names (optional)
        allowed_raw = (os.getenv("ALLOWED_ROLES", "") or "").strip()
        allowed_roles = [r.strip() for r in allowed_raw.split(",") if r.strip()]

        # Channel gate
        user_command_channel_id = _to_int(os.getenv("USER_COMMAND_CHANNEL_ID", "0"), 0)

        # TTC alerts
        ttc_alerts_channel_id = _to_int(os.getenv("TTC_ALERTS_CHANNEL_ID", "0"), 0)
        ttc_feed_url = (os.getenv("TTC_FEED_URL", "") or "https://mstdn.ca/@ttcnotices.rss").strip()
        ttc_poll_seconds = max(30, _to_int(os.getenv("TTC_POLL_SECONDS", "120"), 120))

        ttc_alert_level = (os.getenv("TTC_ALERT_LEVEL", "major") or "major").strip().lower()
        if ttc_alert_level not in ("major", "all"):
            ttc_alert_level = "major"

        force_guild_resync = _to_bool(os.getenv("FORCE_GUILD_RESYNC", "false"), False)

        embed_logo_url = (os.getenv("EMBED_LOGO_URL", "") or "").strip()
        embed_accent_color = _to_color(os.getenv("EMBED_ACCENT_COLOR", "#A64DFF"), 0xA64DFF)
        embed_footer_text = (os.getenv("EMBED_FOOTER_TEXT", "Instigate Cafe") or "Instigate Cafe").strip() or "Instigate Cafe"

        bj_start = _to_int(os.getenv("BJ_STARTING_BANKROLL", "100"), 100)
        bj_hs17 = _to_bool(os.getenv("BJ_DEALER_HITS_SOFT_17", "true"), True)
        bj_splits = _to_int(os.getenv("BJ_MAX_SPLITS", "1"), 1)
        bj_ttl = _to_int(os.getenv("BJ_SESSION_TTL_MINUTES", "20"), 20)

        return Settings(
            token=token,
            guild_id=guild_id,
            stock_channel_id=stock_channel_id,
            allowed_role_ids=allowed_role_ids,
            allowed_roles=allowed_roles,
            user_command_channel_id=user_command_channel_id,
            ttc_alerts_channel_id=ttc_alerts_channel_id,
            ttc_feed_url=ttc_feed_url,
            ttc_poll_seconds=ttc_poll_seconds,
            ttc_alert_level=ttc_alert_level,
            force_guild_resync=force_guild_resync,
            embed_logo_url=embed_logo_url,
            embed_accent_color=embed_accent_color,
            embed_footer_text=embed_footer_text,
            bj_starting_bankroll=max(1, bj_start),
            bj_dealer_hits_soft_17=bj_hs17,
            bj_max_splits=max(0, bj_splits),
            bj_session_ttl_minutes=max(5, bj_ttl),
        )
