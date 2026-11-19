from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiohttp
import discord

from icbot.container import get_container
from icbot.embeds import go_alert as go_alert_embed
from icbot.errors import format_exception_safe
from icbot.services.alert_dedupe import (
    AlertDedupeManager,
    AlertLifecycleStatus,
    compute_content_hash,
    safe_send_alert,
)
from icbot.services.go_client import GOFetchError, GOMessage, fetch_service_alerts
from icbot.services.http_transport import build_ssl_context

log = logging.getLogger(__name__)
_MIN_DT = datetime.min.replace(tzinfo=UTC)

_MAJOR_PATTERNS = [
    r"\bderail(ed|ment)?\b",
    r"\bcollision\b",
    r"\bfire\b",
    r"\bhaz(?:mat|ardous)\b",
    r"\bpolice\b",
    r"\bmedical\b",
    r"\bemergency\b",
    r"\bfatal\b",
    r"\btrespass\b",
    r"\bswitch\b",
    r"\bsignal\b",
    r"\btrack\b",
    r"\bpower\b",
    r"\boutage\b",
    r"\bno service\b",
    r"\bsuspend(ed|ed)?\b",
    r"\bcancell?ed\b",
    r"\bshuttle\b",
]
_MAJOR_RE = re.compile("|".join(_MAJOR_PATTERNS), re.IGNORECASE)

# GO rail corridor codes are usually short alpha codes (e.g., LW, LE, MI).
_TRAIN_LINE_RE = re.compile(r"^[A-Z]{2,4}$")


def _canon_line_token(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[_-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


_LINE_ALIASES: dict[str, str] = {
    # Lakeshore
    "lw": "LW",
    "lakeshore west": "LW",
    "lakeshore w": "LW",
    "le": "LE",
    "lakeshore east": "LE",
    "lakeshore e": "LE",
    # Stouffville
    "st": "ST",
    "stouffville": "ST",
    # Others
    "rh": "RH",
    "richmond hill": "RH",
    "br": "BR",
    "barrie": "BR",
    "ki": "KI",
    "kitchener": "KI",
    "mi": "MI",
    "milton": "MI",
    "ni": "NI",
    "niagara": "NI",
    "up": "UP",
    "up express": "UP",
    "upx": "UP",
}


def _normalize_allowlist(raw: list[str]) -> set[str]:
    """Return a set of canonical line codes (and/or literal tokens) from env input."""
    out: set[str] = set()
    for tok in raw or []:
        t = _canon_line_token(str(tok))
        if not t:
            continue
        code = _LINE_ALIASES.get(t)
        if code:
            out.add(code)
            continue
        # If user already provided a code like LW/LE/ST
        up = t.upper()
        if _TRAIN_LINE_RE.match(up):
            out.add(up)
            continue
        # Otherwise keep literal (used as a text substring match)
        out.add(t)
    return out


def _matches_allowlist(m: GOMessage, allow: set[str]) -> bool:
    if not allow:
        return True
    line_codes = {str(x).strip().upper() for x in (m.lines or []) if str(x).strip()}
    blob = _text_blob(m).lower()
    for a in allow:
        if not a:
            continue
        # Canonical codes
        if a.isupper() and a in line_codes:
            return True
        # Text token (names, etc.)
        if a.islower() and a in blob:
            return True
        # Also allow matching codes in text if Lines[] is missing
        if a.isupper() and re.search(rf"\b{re.escape(a)}\b", blob, flags=re.IGNORECASE):
            return True
    return False


def _make_go_connector(settings) -> aiohttp.TCPConnector:
    """Build an aiohttp connector with a CA bundle that works on Windows venvs.

    If GO_SSL_VERIFY=false, verification is disabled and a security warning is logged.
    If GO_CA_BUNDLE_PATH is set, that PEM file is used.
    Otherwise we try certifi (added to requirements.txt).
    """
    verify = bool(getattr(settings, "go_ssl_verify", True))
    cafile = str(getattr(settings, "go_ca_bundle_path", "") or "").strip()
    ssl_ctx = build_ssl_context(verify=verify, ca_bundle_path=cafile if cafile else None)
    if ssl_ctx is False:
        return aiohttp.TCPConnector(ssl=False)
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _text_blob(m: GOMessage) -> str:
    parts = [
        m.subject_en or "",
        m.body_en or "",
        m.category or "",
        m.subcategory or "",
        " ".join(m.lines or []),
        " ".join(m.stops or []),
    ]
    return " ".join(p for p in parts if p).strip()


def _is_major(m: GOMessage) -> bool:
    blob = _text_blob(m).lower()
    if "disruption" in (m.category or "").lower():
        return True
    if "suspend" in (m.subcategory or "").lower():
        return True
    return bool(_MAJOR_RE.search(blob))


def _is_train_related(m: GOMessage) -> bool:
    for code in m.lines or []:
        c = str(code).strip().upper()
        if _TRAIN_LINE_RE.match(c):
            return True
    return False


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # Examples in docs: "2026-01-09 15:56:04"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError as exc:
            log.debug("GO timestamp %r failed format %s: %s", s, fmt, exc)
    return None


async def _get_meta(db, key: str) -> str:
    row = await db.fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return str(row[0]) if row else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


async def _resolve_channel(bot: discord.Client, channel_id: int):
    try:
        ch = bot.get_channel(channel_id)
        if ch:
            return ch
        return await bot.fetch_channel(channel_id)
    except (discord.DiscordException, LookupError, ValueError):
        return None


@asynccontextmanager
async def _scoped_go_session(
    shared_session: aiohttp.ClientSession | None,
    settings,
    *,
    go_session: aiohttp.ClientSession | None = None,
):
    verify = bool(getattr(settings, "go_ssl_verify", True))
    cafile = str(getattr(settings, "go_ca_bundle_path", "") or "").strip()
    if not verify or cafile:
        if go_session is not None and not go_session.closed:
            yield go_session
            return
    elif shared_session is not None and not shared_session.closed:
        yield shared_session
        return
    async with aiohttp.ClientSession(
        connector=_make_go_connector(settings),
        timeout=aiohttp.ClientTimeout(total=18),
    ) as session:
        yield session


async def poll_and_post_go_alerts(bot: discord.Client, *, raise_on_error: bool = False) -> None:
    """Poll GO Transit Service Alerts and post to a channel."""
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        print("[GO] missing bot.settings or bot.db")
        return
    container = get_container(bot)
    secrets = settings.secret_values() if hasattr(settings, "secret_values") else ()

    api_base = str(getattr(settings, "go_api_base", "") or "").strip()
    access_key = str(getattr(settings, "go_api_key", "") or "").strip()

    # Channel defaults to TTC if not explicitly set.
    channel_id = int(
        getattr(settings, "go_alerts_channel_id", 0) or getattr(settings, "ttc_alerts_channel_id", 0) or 0
    )

    if not channel_id:
        print("[GO] WARNING: no channel id set (GO_ALERTS_CHANNEL_ID or TTC_ALERTS_CHANNEL_ID). ")
        return

    if not api_base or not access_key:
        print("[GO] WARNING: GO API not configured (GO_API_BASE/GO_API_KEY missing). ")
        return

    poll_level = str(getattr(settings, "go_alert_level", "major") or "major").strip().lower()
    poll_level = poll_level if poll_level in ("major", "all") else "major"

    bootstrap_post_latest = bool(getattr(settings, "go_bootstrap_post_latest", True))
    include_buses = bool(getattr(settings, "go_include_buses", False))
    allow_raw = list(getattr(settings, "go_lines_allowlist", None) or [])
    allow = _normalize_allowlist(allow_raw)
    debug = bool(getattr(settings, "go_debug", False))

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        print("[GO] cannot resolve channel")
        return

    # Fetch with small retry on standalone calls; supervised calls delegate retry/backoff to TaskSupervisor
    msgs: list[GOMessage] = []
    last_err: BaseException | None = None
    max_attempts = 1 if raise_on_error else 3
    async with _scoped_go_session(
        container.http_session, settings, go_session=container.go_http_session
    ) as session:
        for attempt in range(max_attempts):
            try:
                msgs = await fetch_service_alerts(session, api_base, access_key, timeout_seconds=18)
                last_err = None
                break
            except (TimeoutError, aiohttp.ClientError, GOFetchError) as e:
                last_err = e
                if raise_on_error or attempt + 1 >= max_attempts:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))

    if last_err is not None:
        print(f"[GO] fetch failed: {format_exception_safe(last_err, secrets)}")
        if raise_on_error:
            raise last_err
        return

    if not msgs:
        if debug:
            print("[GO] poll: fetched=0")
        return

    # Filter to train-related messages unless include_buses=true
    if not include_buses:
        msgs = [m for m in msgs if _is_train_related(m)]

    # Filter to allowlisted lines (if configured)
    if allow:
        msgs = [m for m in msgs if _matches_allowlist(m, allow)]

    if not msgs:
        if debug:
            print("[GO] poll: fetched items but 0 remained after filters (include_buses/allowlist).")
        return

    # Sort (oldest -> newest) by PostedDateTime where possible.
    msgs_sorted = sorted(
        msgs,
        key=lambda m: _parse_dt(m.posted_dt) or _MIN_DT,
    )

    newest = max(msgs_sorted, key=lambda m: _parse_dt(m.posted_dt) or _MIN_DT)

    last_code = (await _get_meta(db, "go_last_servicealert_code")).strip()

    if debug:
        major_ct = sum(1 for m in msgs_sorted if _is_major(m))
        print(
            f"[GO] poll: fetched={len(msgs_sorted)} major={major_ct} level={poll_level} "
            f"include_buses={include_buses} allowlist={'on' if bool(allow) else 'off'} "
            f"last_code={'set' if bool(last_code) else 'none'}"
        )
        if len(msgs_sorted) > 0 and poll_level == "major" and major_ct == 0:
            print(
                "[GO] NOTE: items fetched but none matched MAJOR patterns; set GO_ALERT_LEVEL=all to see them."
            )

    guild = getattr(channel, "guild", None)
    guild_id = int(getattr(guild, "id", 0) or 0)
    dedupe_mgr = AlertDedupeManager(clock=container.clock)

    # Bootstrap behavior: optionally post ONE latest matching item, then checkpoint
    if not last_code or not await dedupe_mgr.is_bootstrapped(db, guild_id, "go_alert", channel_id):
        if bootstrap_post_latest:
            for m in reversed(msgs_sorted):  # newest-first
                major = _is_major(m)
                if poll_level == "major" and not major:
                    continue
                level = "MAJOR" if major else "INFO"
                embed = go_alert_embed(
                    settings,
                    headline=m.subject_en or "GO Transit Alert",
                    details=m.body_en or None,
                    level=level,
                    meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
                    posted_dt=m.posted_dt,
                    lines=m.lines,
                )
                ok, send_status = await safe_send_alert(
                    bot,
                    channel_id,
                    embed=embed,
                    fallback_text=f"GO {level}: {m.subject_en or ''} - {m.body_en or ''}".strip(),
                )
                if not ok and raise_on_error:
                    raise RuntimeError(f"GO post failed: {send_status}")
                break

        # Checkpoint all existing items in dedupe manager to prevent historic dumping
        for m in msgs_sorted:
            h = compute_content_hash(f"{m.status}:{m.subject_en}:{m.body_en}:{','.join(m.lines)}")
            await dedupe_mgr.checkpoint_bootstrap_item(db, guild_id, "go_alert", channel_id, m.code, h)
        await dedupe_mgr.mark_bootstrapped(db, guild_id, "go_alert", channel_id)

        await _set_meta(db, "go_last_servicealert_code", newest.code)
        if debug:
            print(
                f"[GO] bootstrap: checkpoint={newest.code} posted_latest={bootstrap_post_latest} level={poll_level}"
            )
        return

    # If we can't find the last_code in the current feed (e.g., retention), treat it like a bootstrap.
    codes = [m.code for m in msgs_sorted]
    if last_code not in codes:
        if debug:
            print("[GO] WARNING: last checkpoint not found in feed; treating as bootstrap.")
        if bootstrap_post_latest:
            for m in reversed(msgs_sorted):
                major = _is_major(m)
                if poll_level == "major" and not major:
                    continue
                level = "MAJOR" if major else "INFO"
                embed = go_alert_embed(
                    settings,
                    headline=m.subject_en or "GO Transit Alert",
                    details=m.body_en or None,
                    level=level,
                    meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
                    posted_dt=m.posted_dt,
                    lines=m.lines,
                )
                ok, send_status = await safe_send_alert(
                    bot,
                    channel_id,
                    embed=embed,
                    fallback_text=f"GO {level}: {m.subject_en or ''} - {m.body_en or ''}".strip(),
                )
                if not ok and raise_on_error:
                    raise RuntimeError(f"GO post failed: {send_status}")
                break
        await _set_meta(db, "go_last_servicealert_code", newest.code)
        return

    # Normal operation: post messages after the checkpoint (oldest -> newest)
    started = False
    for m in msgs_sorted:
        if m.code == last_code:
            started = True
            continue
        if not started:
            continue

        major = _is_major(m)
        if poll_level == "major" and not major:
            await _set_meta(db, "go_last_servicealert_code", m.code)
            continue

        h = compute_content_hash(f"{m.status}:{m.subject_en}:{m.body_en}:{','.join(m.lines)}")
        lifecycle = await dedupe_mgr.check_alert(db, guild_id, "go_alert", channel_id, m.code, h)
        if lifecycle == AlertLifecycleStatus.IDENTICAL_DUPLICATE:
            continue

        headline = (
            f"[UPDATE] {m.subject_en}"
            if lifecycle == AlertLifecycleStatus.MATERIAL_UPDATE and m.subject_en
            else (m.subject_en or "GO Transit Alert")
        )
        level = "MAJOR" if major else "INFO"
        embed = go_alert_embed(
            settings,
            headline=headline,
            details=m.body_en or None,
            level=level,
            meta=f"{m.category or ''} {m.subcategory or ''}".strip() or None,
            posted_dt=m.posted_dt,
            lines=m.lines,
        )

        ok, send_status = await safe_send_alert(
            bot,
            channel_id,
            embed=embed,
            fallback_text=f"GO {level}: {headline} - {m.body_en or ''}".strip(),
        )
        if not ok:
            print(f"[GO] post failed: {send_status}")
            if raise_on_error:
                raise RuntimeError(f"GO post failed: {send_status}")
            return

        await dedupe_mgr.record_delivery(db, guild_id, "go_alert", channel_id, m.code, h)
        await _set_meta(db, "go_last_servicealert_code", m.code)
