from __future__ import annotations

import time
from typing import Any, Optional

import discord
from discord import app_commands

from icbot.app import ic


def _pick_first(cols: list[str], candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


async def _table_columns(db: Any, table: str) -> list[str]:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    # PRAGMA columns: cid, name, type, notnull, dflt_value, pk
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


@ic.command(name="jobs", description="Show the most recent jobs the bot has seen.")
@app_commands.describe(limit="How many jobs to show (max 25).")
async def jobs_cmd(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
    # Always public (not ephemeral) so everyone sees the same postings.
    await interaction.response.defer(thinking=True)

    bot = interaction.client
    db = getattr(bot, "db", None)
    if db is None:
        await interaction.followup.send("❌ Bot DB not initialized.")
        return

    cols = await _table_columns(db, "job_seen")

    col_title = _pick_first(cols, ["title", "job_title"])
    col_company = _pick_first(cols, ["company", "org", "employer", "source_label"])
    col_location = _pick_first(cols, ["location", "job_location"])
    col_url = _pick_first(cols, ["url", "job_url", "link"])
    col_source = _pick_first(cols, ["source", "platform"])
    col_seen = _pick_first(cols, ["seen_unix", "first_seen_unix", "seen_at_unix", "created_unix", "inserted_unix"])
    col_posted = _pick_first(cols, ["posted_unix", "posted_at_unix"])

    order_col = col_seen or col_posted or "rowid"

    select_cols = [c for c in [col_title, col_company, col_location, col_url, col_source, col_seen, col_posted] if c]
    if not select_cols:
        # Last-resort: select all columns
        select_cols = cols

    q = f"""
        SELECT {", ".join(select_cols)}
        FROM job_seen
        ORDER BY {order_col} DESC
        LIMIT ?
    """
    rows = await db.fetchall(q, (int(limit),))
    if not rows:
        await interaction.followup.send("No jobs found in `job_seen` yet.")
        return

    # Build lines
    lines: list[str] = []
    for r in rows:
        j = dict(zip(select_cols, r))
        title = (j.get(col_title) if col_title else None) or "Untitled role"
        company = (j.get(col_company) if col_company else None) or ""
        location = (j.get(col_location) if col_location else None) or ""
        url = (j.get(col_url) if col_url else None) or ""
        source = (j.get(col_source) if col_source else None) or ""

        seen = j.get(col_seen) if col_seen else None
        age = _format_age(int(seen)) if isinstance(seen, (int, float)) else ""

        meta = " • ".join([x for x in [company, location, source, age] if x])
        head = f"**{title}**"
        if url:
            lines.append(f"{head}\n{meta}\n<{url}>")
        else:
            lines.append(f"{head}\n{meta}")

    body = "\n\n".join(lines).strip()
    pages = _chunk(body, limit=3800)

    embed = discord.Embed(title=f"🧰 Recent Job Postings (latest {len(rows)})", description=pages[0])
    embed.set_footer(text="Instigate Jobs Watcher")

    await interaction.followup.send(embed=embed)

    for p in pages[1:]:
        e2 = discord.Embed(title="🧰 Recent Job Postings (cont.)", description=p)
        e2.set_footer(text="Instigate Jobs Watcher")
        await interaction.followup.send(embed=e2)
