from __future__ import annotations

import time
from typing import Any, Optional

import discord
from discord import app_commands

from icbot.app import ic
from icbot.tasks.job_alerts import job_matches_env_filters


def _pick_first(cols: list[str], candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


async def _table_columns(db: Any, table: str) -> list[str]:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return [r[1] for r in rows]


def _format_age(ts: Optional[int]) -> str:
    if not ts or ts <= 0:
        return ""
    delta = int(time.time()) - int(ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _chunk(text: str, limit: int = 3800) -> list[str]:
    parts: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        add = (block + "\n\n")
        if len(buf) + len(add) > limit:
            if buf.strip():
                parts.append(buf.strip())
            buf = add
        else:
            buf += add
    if buf.strip():
        parts.append(buf.strip())
    return parts or [""]


@ic.command(name="jobs", description="Show the most recent SWE/CS jobs the bot has seen.")
@app_commands.describe(limit="How many jobs to show (max 25).")
async def jobs_cmd(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
    # Always public so everyone sees the same postings.
    await interaction.response.defer(thinking=True)

    bot = interaction.client
    settings = getattr(bot, "settings", None)
    db = getattr(bot, "db", None)
    if settings is None or db is None:
        await interaction.followup.send("❌ Bot not initialized (missing settings/db).")
        return

    cols = await _table_columns(db, "job_seen")

    col_title = _pick_first(cols, ["title", "job_title"])
    col_company = _pick_first(cols, ["company", "org", "employer", "source_label"])
    col_location = _pick_first(cols, ["location", "job_location"])
    col_url = _pick_first(cols, ["url", "job_url", "link"])
    col_source = _pick_first(cols, ["source", "platform"])
    col_seen = _pick_first(cols, ["first_seen_unix", "seen_unix", "seen_at_unix", "created_unix", "inserted_unix"])
    col_posted = _pick_first(cols, ["posted_unix", "posted_at_unix"])
    col_blob = _pick_first(cols, ["text_blob", "blob", "description", "snippet"])

    order_col = col_seen or col_posted or "rowid"
    select_cols = [c for c in [col_title, col_company, col_location, col_url, col_source, col_seen, col_posted, col_blob] if c]
    if not select_cols:
        select_cols = cols

    # Pull more than requested, then filter down (because most recent rows may be non-matching).
    scan_n = max(int(limit) * 25, 200)
    scan_n = min(scan_n, 1000)

    q = f"""
        SELECT {", ".join(select_cols)}
        FROM job_seen
        ORDER BY {order_col} DESC
        LIMIT ?
    """
    rows = await db.fetchall(q, (int(scan_n),))
    if not rows:
        await interaction.followup.send("No jobs found in `job_seen` yet.")
        return

    # Same filters as the pollers (from .env via settings).
    loc_allow = list(getattr(settings, "jobs_location_allowlist", None) or []) or None
    kw_allow = list(getattr(settings, "jobs_keywords_allowlist", None) or []) or None
    kw_deny = list(getattr(settings, "jobs_keywords_denylist", None) or []) or None

    from icbot.embeds import job_alert as job_alert_embed

    embeds: list[discord.Embed] = []
    matched = 0

    for r in rows:
        j = dict(zip(select_cols, r))
        title = (j.get(col_title) if col_title else None) or ""
        company = (j.get(col_company) if col_company else None) or ""
        location = (j.get(col_location) if col_location else None) or ""
        url = (j.get(col_url) if col_url else None) or ""
        source = (j.get(col_source) if col_source else None) or ""
        blob = (j.get(col_blob) if col_blob else None) or ""

        if not title:
            continue

        blob2 = " ".join([title, company, location, source]).strip()

        if not job_matches_env_filters(
            text_blob=blob2,
            location=location or blob2,
            loc_allow=loc_allow,
            kw_allow=kw_allow,
            kw_deny=kw_deny,
        ):
            continue

        seen = j.get(col_seen) if col_seen else None
        age = _format_age(int(seen)) if isinstance(seen, (int, float)) else ""

        # Build clean embed
        e = job_alert_embed(
            settings,
            title=title,
            company=company,
            url=url,
            location=location,
            source=source,
            footer_hint=f"Tracking System • {age}" if age else "Tracking System"
        )
        embeds.append(e)

        matched += 1
        if matched >= int(limit):
            break

    if not embeds:
        await interaction.followup.send(
            "No SWE/CS roles matched the current filters in the most recent scan.\n"
            "Check `.env` allow/deny/location lists (you might be filtering too aggressively)."
        )
        return

    # Send in chunks of up to 5 embeds per message (Discord API allows up to 10, but 5 is cleaner)
    chunk_size = 5
    first_chunk = True
    for i in range(0, len(embeds), chunk_size):
        chunk = embeds[i:i + chunk_size]
        if first_chunk:
            await interaction.followup.send(content=f"🧰 **SWE/CS Job Postings** (latest {matched})", embeds=chunk)
            first_chunk = False
        else:
            await interaction.followup.send(embeds=chunk)
